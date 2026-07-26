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

CORPUS_UUID_NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# Redis key prefix for source tracking
_SOURCE_KEY_PREFIX = "corpus:source:"
_SOURCE_INDEX_KEY = "corpus:source_index"  # Sorted set: source_name scored by ingested_at


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slug(name: str) -> str:
    """Create a URL-safe slug from a source name."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# Qdrant: chunks (via VectorClient)
# ---------------------------------------------------------------------------


async def store_chunks(
    chunks: list[Chunk],
    vector_client,
    ingest_id: str | None = None,
) -> int:
    """Store chunks into the shared firekeep_memory collection via VectorClient.

    Args:
        chunks: List of Chunk objects with content and metadata.
        vector_client: VectorClient instance (handles embedding + upsert).
        ingest_id: Per-run staging tag (SP0 A4). Stored in the nested chunk
            metadata (payload.metadata.ingest_id) so the staged re-ingest
            flow can delete the previous generation while keeping this one.
    """
    if not chunks:
        return 0

    count = 0
    for chunk in chunks:
        source_slug = _slug(chunk.metadata.source_name)
        metadata = {
            "source": "corpus",
            "domain": chunk.metadata.source_type,
            "tags": ["corpus", chunk.metadata.source_type, source_slug],
            "source_name": chunk.metadata.source_name,
            "chunk_index": chunk.metadata.chunk_index,
            "total_chunks": chunk.metadata.total_chunks,
        }
        if ingest_id:
            metadata["ingest_id"] = ingest_id
        await vector_client.upsert(text=chunk.content, metadata=metadata)
        count += 1

    logger.info("Stored %d corpus chunks via VectorClient", count)
    return count


# ---------------------------------------------------------------------------
# Redis: source tracking
# ---------------------------------------------------------------------------


async def track_source(
    source_name: str,
    source_type: str,
    chunk_count: int,
    redis_client=None,
) -> None:
    """Record source metadata in Redis for listing and management.

    Uses SET (not accumulation) because the staged re-ingest flow (SP0 A4)
    stages the new generation, swaps out the old one, then calls this last —
    re-ingestion is idempotent from the caller's point of view even though
    the underlying Qdrant swap is stage-then-delete, not delete-then-store.
    """
    if redis_client is None:
        return

    now = datetime.now(timezone.utc)
    payload = json.dumps({
        "name": source_name,
        "source_type": source_type,
        "chunks": chunk_count,
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
