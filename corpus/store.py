"""Storage layer for corpus chunks and source tracking.

Writes to:
- Qdrant: Document chunks via VectorClient (shared firekeep_memory collection)
- Redis: Source metadata for list/delete tracking

The Neo4j entity graph write path was removed (2026-05-27) — audit found
0 entities ever extracted in production and graph recall queries never
include corpus entity types. Chunk path (Qdrant) is unaffected.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from corpus.models import Chunk

logger = logging.getLogger(__name__)

# Copied verbatim from cortex/app/db/vector.py's FIREKEEP_UUID_NAMESPACE:
# corpus is a shared lib and MUST NOT import cortex, but corpus points live
# in the same collection, so the two constants must stay byte-equal —
# pinned by corpus/tests/test_point_identity.py.
FIREKEEP_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Redis key prefix for source tracking
_SOURCE_KEY_PREFIX = "corpus:source:"
_SOURCE_INDEX_KEY = "corpus:source_index"  # Sorted set: source_name scored by ingested_at

# Dex ids whose `<dex>:`-prefixed source names are reserved (Docdex §4.3).
# corpus/api.py derives its scope gate from this set, so the record's `dex`
# field and the gate can never disagree.
KNOWN_DEX_IDS = frozenset({"docdex"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Create a URL-safe slug from a source name."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def source_dex(source_name: str) -> str:
    """The dex id claimed by a source name's `<dex>:` prefix, or "".

    Exact match against KNOWN_DEX_IDS: a bare "docdex" (no colon) is an
    ordinary name, not a claim on the reserved namespace.
    """
    prefix, sep, _ = source_name.partition(":")
    return prefix if sep and prefix in KNOWN_DEX_IDS else ""


def dex_source_prefix(source_id: str) -> str:
    """The reserved name prefix for one Docdex client source (spec §3).

    Docdex names each synced file ``docdex:<source_id>:<sha256(relpath)>``;
    the bulk delete route removes every TRACKED source under this prefix.
    The trailing colon is load-bearing: without it source_id "src1" would
    also match "src10"'s files.
    """
    return f"docdex:{source_id}:"


# ---------------------------------------------------------------------------
# Qdrant: chunks (via VectorClient)
# ---------------------------------------------------------------------------


def corpus_point_id(workspace_id: str, source_name: str,
                    ingest_id: str, chunk_index: int) -> str:
    """Source-scoped identity: identical text across sources/members must
    NEVER share a point (uuid5(text) did; deleting one member's source
    then deleted the other's chunk — spec §4.2).

    The ``corpus|`` domain prefix separates this id space from memory points
    (which are ``uuid5(text)`` in the same namespace) by CONSTRUCTION rather
    than by accident: a memory whose text happened to equal
    ``"ws|name|ingest|idx"`` can no longer collide with a corpus point."""
    raw = f"corpus|{workspace_id}|{source_name}|{ingest_id}|{chunk_index}"
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, raw))


async def store_chunks(
    chunks: list[Chunk],
    vector_client,
    ingest_id: str | None = None,
    workspace_id: str | None = None,
    member_id: str | None = None,
    visibility: str = "workspace",
    client_metadata: dict[str, str] | None = None,
) -> int:
    """Store chunks into the shared firekeep_memory collection via VectorClient.

    Args:
        chunks: List of Chunk objects with content and metadata.
        vector_client: VectorClient instance (handles embedding + upsert).
        ingest_id: Per-run staging tag (SP0 A4). Stored in the nested chunk
            metadata (payload.metadata.ingest_id) so the staged re-ingest
            flow can delete the previous generation while keeping this one.
        workspace_id / member_id: the ingesting principal's tenancy. WITHOUT
            THESE THE CHUNK IS NOT RECALLABLE. ``VectorClient.search`` applies
            ``workspace_id`` as a hard ``must`` filter, so a chunk written with
            ``workspace_id=None`` matches no real caller's recall — measured on
            the live store, a freshly ingested chunk was the TOP hit (0.8193)
            with the filter off and ABSENT with the caller's real workspace,
            while ``corpus_ingest``'s own docstring promises "every chunk is
            searchable via memory_recall". The startup ``migrate_single_workspace``
            backfill healed it on the next restart, which made the damage window
            "until someone restarts cortex-api" rather than permanent — a
            migration is not a substitute for stamping at write time.
        visibility: chunk scope (Docdex §4.1) — stamped EXPLICITLY on every
            new write ("workspace" default); absence keeps its legacy meaning
            (pre-Phase-V point) that the visibility filter honors.
        client_metadata: the API-bounded client dict; rides into the nested
            payload metadata, but server stamps below always win a collision
            (reserved keys are rejected at the API boundary — this guards the
            non-HTTP callers too).
    """
    if not chunks:
        return 0

    count = 0
    for chunk in chunks:
        source_slug = _slug(chunk.metadata.source_name)
        metadata = {
            # Client metadata first: the server stamps below must win every
            # key collision.
            **(client_metadata or {}),
            "source": "corpus",
            "domain": chunk.metadata.source_type,
            "tags": ["corpus", chunk.metadata.source_type, source_slug],
            "source_name": chunk.metadata.source_name,
            "chunk_index": chunk.metadata.chunk_index,
            "total_chunks": chunk.metadata.total_chunks,
            # `upsert` promotes this to the top-level payload key the
            # visibility filter matches on.
            "visibility": visibility,
            # Generation gate (spec §4.5 option (a)): written gated;
            # commit_generation flips the whole run live in ONE set_payload at
            # swap completion, and recall's GENERATION_GUARD excludes anything
            # still False — a mid-ingest failure leaves nothing recallable.
            "committed": False,
        }
        if ingest_id:
            metadata["ingest_id"] = ingest_id
        # Emitted only when known: `upsert` promotes these to top-level payload
        # keys, and writing an explicit None would stamp the very value the
        # filter cannot match, overwriting a migration backfill on re-ingest.
        if workspace_id:
            metadata["workspace_id"] = workspace_id
        if member_id:
            metadata["member_id"] = member_id
        await vector_client.upsert(
            text=chunk.content,
            metadata=metadata,
            point_id=corpus_point_id(
                workspace_id or "",
                chunk.metadata.source_name,
                ingest_id or "",
                chunk.metadata.chunk_index,
            ),
        )
        count += 1

    logger.info("Stored %d corpus chunks via VectorClient", count)
    return count


async def commit_generation(
    vector_client,
    source_name: str,
    ingest_id: str,
) -> None:
    """Flip a staged generation live: ONE set_payload over (source_name, ingest_id).

    Spec §4.5 option (a): chunks are written ``committed: False`` and recall
    excludes them (GENERATION_GUARD), so the staged swap becomes atomic to
    recall — this call is the commit point. A failure anywhere before it
    leaves the new generation unrecallable and the old one fully live; the
    ingest_id condition keeps the flip from resurrecting an earlier run's
    orphaned generation of the same source.

    VectorClient has no set-payload-by-filter wrapper and corpus cannot
    import cortex, so this reaches the raw Qdrant client the way
    cortex/app/workspace_migration.py does. A stand-in without the raw
    handle (bare recording fakes) gets no commit — its chunks stay gated,
    which fails CLOSED at recall, never open.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    raw_client = getattr(vector_client, "_client", None)
    if raw_client is None:
        return
    await raw_client.set_payload(
        collection_name=vector_client._collection,
        payload={"committed": True},
        points=Filter(
            must=[
                FieldCondition(key="source", match=MatchValue(value="corpus")),
                FieldCondition(
                    key="metadata.source_name", match=MatchValue(value=source_name)
                ),
                FieldCondition(
                    key="metadata.ingest_id", match=MatchValue(value=ingest_id)
                ),
            ]
        ),
    )
    logger.info(
        "Committed corpus generation for source '%s' (ingest %s)",
        source_name,
        ingest_id,
    )


# ---------------------------------------------------------------------------
# Redis: source tracking
# ---------------------------------------------------------------------------


async def track_source(
    source_name: str,
    source_type: str,
    chunk_count: int,
    redis_client=None,
    visibility: str = "workspace",
    workspace_id: str | None = None,
    member_id: str | None = None,
) -> None:
    """Record source metadata in Redis for listing and management.

    Uses SET (not accumulation) because the staged re-ingest flow (SP0 A4)
    stages the new generation, swaps out the old one, then calls this last —
    re-ingestion is idempotent from the caller's point of view even though
    the underlying Qdrant swap is stage-then-delete, not delete-then-store.

    ``workspace_id`` / ``member_id`` are the ingesting principal's tenancy,
    stamped server-side (never client-asserted — Docdex §4.3). Empty means a
    pre-ownership record; corpus/api.py's authz treats those as the
    single-workspace legacy world. ``dex`` records which reserved namespace
    (if any) the name claims.
    """
    if redis_client is None:
        return

    now = datetime.now(timezone.utc)
    payload = json.dumps({
        "name": source_name,
        "source_type": source_type,
        "chunks": chunk_count,
        "visibility": visibility,
        "workspace_id": workspace_id or "",
        "member_id": member_id or "",
        "dex": source_dex(source_name),
        "last_ingested": now.isoformat(),
    })
    key = f"{_SOURCE_KEY_PREFIX}{source_name}"
    await redis_client.set(key, payload)
    await redis_client.zadd(_SOURCE_INDEX_KEY, {source_name: now.timestamp()})
    logger.debug("Tracked corpus source '%s' (%d chunks)", source_name, chunk_count)


async def list_sources(redis_client=None) -> list[dict]:
    """List all ingested corpus sources, most recent first."""
    if redis_client is None:
        return []

    try:
        names = await redis_client.zrevrange(_SOURCE_INDEX_KEY, 0, -1)
        if not names:
            return []
        results = []
        for name in names:
            # This is a shared module: the caller's redis client may or may
            # not have decode_responses=True. Cortex's app.state.redis_client
            # does NOT, so zrevrange yields bytes — normalize to str before
            # building the key, or the f-string embeds the b'...' repr and
            # the GET misses (the /corpus/sources "always empty" bug).
            if isinstance(name, bytes):
                name = name.decode("utf-8")
            raw = await redis_client.get(f"{_SOURCE_KEY_PREFIX}{name}")
            if raw:
                try:
                    results.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    pass
        return results
    except Exception:
        logger.exception("Failed to list corpus sources from Redis")
        return []


# ---------------------------------------------------------------------------
# Delete operations
# ---------------------------------------------------------------------------


async def delete_source_chunks(
    source_name: str,
    vector_client,
    exclude_ingest_id: str | None = None,
) -> None:
    """Delete all Qdrant points belonging to a corpus source.

    When exclude_ingest_id is given, points written by that ingest run are
    kept — used by the staged re-ingest flow (SP0 A4) to delete only the
    previous generation of chunks after the new one is fully committed.
    """
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    must_not = None
    if exclude_ingest_id:
        must_not = [
            FieldCondition(
                key="metadata.ingest_id", match=MatchValue(value=exclude_ingest_id)
            ),
        ]
    await vector_client.delete_by_filter(
        Filter(
            must=[
                FieldCondition(key="source", match=MatchValue(value="corpus")),
                FieldCondition(
                    key="metadata.source_name", match=MatchValue(value=source_name)
                ),
            ],
            must_not=must_not,
        )
    )
    logger.info(
        "Deleted corpus chunks for source '%s'%s",
        source_name,
        f" (excluding ingest {exclude_ingest_id})" if exclude_ingest_id else "",
    )


async def delete_source_tracking(
    source_name: str,
    redis_client=None,
) -> None:
    """Delete the Redis tracking record for a source."""
    if redis_client is None:
        return
    await redis_client.delete(f"{_SOURCE_KEY_PREFIX}{source_name}")
    await redis_client.zrem(_SOURCE_INDEX_KEY, source_name)
    logger.info("Deleted corpus source tracking for '%s'", source_name)


async def delete_source(
    source_name: str,
    vector_client,
    redis_client=None,
) -> dict:
    """Full cleanup: delete chunks and tracking record for a source.

    Returns a summary dict suitable for the REST response.
    """
    await delete_source_chunks(source_name, vector_client)
    await delete_source_tracking(source_name, redis_client)
    return {
        "source_name": source_name,
        "chunks_deleted": "all",
        "entities_deleted": "all",  # Kept for API response compatibility
    }


async def delete_dex_source(
    source_id: str,
    source_names: list[str],
    delete_one,
) -> dict:
    """Bulk-remove a dex client source: one exact-name delete per tracked record.

    ``source_names`` is the caller's ALREADY-AUTHORIZED deletion set, derived
    from the tracked source records — bounded by what was actually ingested,
    never a Qdrant prefix query ("one bounded bulk operation", Docdex §3).
    ``delete_one`` is the wired single-source delete (chunks + tracking),
    called ``delete_one(source_name=...)``.

    Chunk counts are reported only when the per-source delete returns real
    numbers; today ``delete_source`` reports "all", so the honest answer is
    "unknown" — never a fabricated count.
    """
    counts: list = []
    for name in source_names:
        result = await delete_one(source_name=name)
        counts.append(
            result.get("chunks_deleted") if isinstance(result, dict) else None
        )
    chunks: int | str = (
        sum(counts) if counts and all(isinstance(c, int) for c in counts)
        else "unknown"
    )
    logger.info(
        "Bulk-deleted %d corpus sources for dex source '%s'",
        len(source_names), source_id,
    )
    return {"deleted_sources": len(source_names), "deleted_chunks": chunks}
