"""Qdrant vector database client for FirekeepCortex.

Handles embedding generation, vector upserts, and semantic search
for the RAG engine's vector retrieval path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from typing import TYPE_CHECKING, Any

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    PayloadSchemaType,
    PointStruct,
    Range,
    VectorParams,
)

from app.db.visibility import GENERATION_GUARD, visibility_should
from app.exceptions import VectorStoreError
from app.models import normalize_namespace

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

# Deterministic namespace for content-based UUIDs (uuid5).
FIREKEEP_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def mem2_seed(workspace_id: str | None, namespace: str, text: str) -> str:
    """The canonical "mem2" scope encoding, shared across identity schemes.

    JSON encoding of ``["mem2", workspace_id, normalize_namespace(namespace),
    text]`` (compact separators, ``ensure_ascii=False``). Scoping the seed on
    workspace_id and namespace — not text alone — means identical text in two
    workspaces (or two namespaces of the same workspace) can never collapse
    onto the same identity, and JSON-encoding each field (rather than joining
    with a delimiter) means no value a caller controls, including the text
    itself, can forge a collision by embedding the delimiter.

    This is the ONE place the encoding is built. ``memory_point_id`` below
    hashes it via uuid5 for Qdrant point ids; ``Neo4jClient`` (identity-v2 D4)
    hashes the same seed via SHA-256 for graph chain-node ids. Two different
    hash algorithms over the identical seed means "same (workspace, namespace,
    text)" is recognized as the same scope in both stores without either
    store's identity scheme depending on the other's.

    Immutability invariant: this encoding is the registered identity contract
    for "mem2" points. Changing it changes every existing point's id, which
    orphans stored vectors from anything that recomputes rather than reads
    the id back. Do not alter the seed shape without a migration.

    namespace is normalized (see ``app.models.normalize_namespace``) before
    seeding, so this function is idempotent regardless of the caller's
    casing/hyphenation of the namespace. workspace_id is embedded as-is
    (including ``None``, which json-encodes as ``null``) — callers that must
    refuse an unscoped identity enforce that themselves (see
    ``memory_point_id``'s ValueError below).
    """
    return json.dumps(
        ["mem2", workspace_id, normalize_namespace(namespace), text],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def memory_point_id(workspace_id: str, namespace: str, text: str) -> str:
    """Mint a scoped, deterministic point id for a memory (identity-v2 D1).

    ``uuid.uuid5(FIREKEEP_UUID_NAMESPACE, mem2_seed(workspace_id, namespace,
    text))`` — see ``mem2_seed`` for the encoding rationale.

    Raises:
        ValueError: workspace_id is falsy (None or empty) — identity must be
            scoped to a verified workspace; there is no unscoped mint.
    """
    if not workspace_id:
        raise ValueError("memory_point_id requires a non-empty workspace_id")

    seed = mem2_seed(workspace_id, namespace, text)
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed))


def _v1_point_id(text: str) -> str:
    """The OLD (pre-identity-v2) point id formula: bare ``uuid5(text)``.

    Identity-v2 D5: the transitional compat-window bridge in ``upsert()``
    below uses this to find a point that predates ``memory_point_id`` scoping,
    and the (currently inert) migration tool will import this same helper for
    its classification predicate. Never use this to MINT a new point —
    ``memory_point_id`` is the only identity-v2 mint; this is read-only
    lookup of the old identity. Transitional; retire with the migration.
    """
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, text))


# Maximum number of embedding vectors to cache in memory.
_EMBED_CACHE_MAX_SIZE = 512

# Maximum number of texts to send in a single batch embedding request.
_BATCH_CHUNK_SIZE = 32

# Floor for _embed's shrink-to-fit loop: if an input is STILL rejected as too
# long at this many chars, give up and raise (well under any embedding model's
# token context, so this is a safety backstop, not an expected outcome).
_MIN_EMBED_CHARS = 256

# Team-continuity keys promoted from the upsert ``metadata`` arg to the
# top-level Qdrant payload, so endpoints like /memory/contributors can read
# ``payload.agent_id`` / ``payload.project`` directly without descending into
# the nested ``metadata`` sub-dict.
_PROMOTED_PAYLOAD_KEYS = {
    "agent_id", "session_id", "project", "workspace_id", "member_id",
}

# Keys excluded from the nested ``metadata`` sub-dict in the Qdrant payload —
# either because they already live at the top level (source/tags/domain/timestamp)
# or because they were just promoted (_PROMOTED_PAYLOAD_KEYS). Single source of
# truth: extend _PROMOTED_PAYLOAD_KEYS and this set updates automatically.
_EXCLUDED_FROM_NESTED_METADATA = (
    {"source", "tags", "domain", "timestamp", "visibility", "committed"}
    | _PROMOTED_PAYLOAD_KEYS
)

# Views ``list_memories`` accepts. "available" is everything recall can still
# reach, "archived" is the dashboard's recovery view, "all" is the pre-lifecycle
# behaviour and stays the default so existing callers are unaffected.
_MEMORY_VIEWS = ("all", "available", "archived")

# Recovery provenance written when a memory is archived. These answer "who
# archived this, why, from what state, and until when can it be recovered" —
# the archived view renders them and restore_memory unwinds them. They must
# also survive a re-learn of identical text (see _merge_lifecycle), otherwise
# an archived memory silently loses its purge deadline and its pre-archive
# status while keeping status="archived".
_ARCHIVE_PROVENANCE_KEYS = (
    "archived_at",
    "archived_from_status",
    "archive_source",
    "archive_reason",
    "purge_eligible_at",
)


def _projected_metadata(payload: dict | None, point_id: str) -> dict[str, Any]:
    """Flatten a Qdrant payload into the ``metadata`` dict recall consumers read.

    This projection is the read half of ``upsert``'s payload shape, and the two
    drifted: ``upsert`` promotes ``_PROMOTED_PAYLOAD_KEYS`` to the top level AND
    strips them from the nested ``metadata`` sub-dict, while this list named only
    source/tags/domain/timestamp. Promoting a key therefore moved it OUT of the
    one place the reader looked, and every memory written since reported no
    author at recall. Deriving the keys from the same constant is what stops the
    next promotion from silently doing it again.

    Promoted keys are emitted only when actually present, so a record written
    before the promotion keeps whatever its nested metadata held instead of being
    overwritten with None.
    """
    # `is None`, not `not payload`: a missing payload (Qdrant can return one
    # when with_payload=False) is the "nothing to project" case, but an empty
    # *dict* is a real payload whose fields should still get their defaults
    # (e.g. memory_type="episodic") rather than being collapsed to {}.
    if payload is None:
        return {}
    return {
        # "id" was added by Task 8 (access-count HINCRBY reads it) — this
        # replacement MUST keep it.
        "id": point_id,
        "source": payload.get("source", ""),
        "tags": payload.get("tags", []),
        "domain": payload.get("domain", ""),
        "timestamp": payload.get("timestamp", ""),
        # `or {}`, not a `{}` default: a payload carrying an explicit
        # metadata=None makes `**` raise TypeError, and this projection runs
        # on every recall result. GC tolerates that shape (`payload.get(
        # "metadata") or {}`), so tolerating it here is also what keeps the
        # two reads below agreeing on every input rather than on most of them.
        **(payload.get("metadata") or {}),
        # Team continuity: who wrote this, in what session, for what project.
        **{k: payload[k] for k in _PROMOTED_PAYLOAD_KEYS if k in payload},
        # Lifecycle fields last so the top-level payload is authoritative —
        # recall scoring reads these (SP0 C2).
        # Dreaming Task 5 (audit finding #3): recall read memory_type through
        # this projection while GC (app/workers/gc.py) reads it from the
        # top-level payload first, nested metadata as fallback — the two
        # could disagree. memory_type belongs here, not among the earlier
        # explicit keys, for the same reason status/confirmed_count do: the
        # top-level payload must win over any nested legacy copy.
        #
        # The three-step read below is NOT decoration — it is gc.py:341-345's
        # order, character for character, and BOTH halves are load-bearing.
        # A plain `payload.get("metadata", {})` spread (the pre-Task-5 state)
        # let a stale nested copy beat the top level. A plain
        # `payload.get("memory_type", "episodic")` sitting after that spread
        # is the exact mirror-image defect: its literal default fires whenever
        # the top-level key is ABSENT, overriding the nested value the spread
        # had just supplied — so a legacy point carrying only
        # metadata.memory_type="reference" recalled as "episodic" (a 90-day
        # half-life) while GC still read "reference" (no age decay). Legacy
        # "procedural" degraded 180d -> 90d the same way. Only the explicit
        # top-level -> nested -> literal chain agrees with GC on every shape.
        "memory_type": (
            payload.get("memory_type")
            or (payload.get("metadata") or {}).get("memory_type")
            or "episodic"
        ),
        "status": payload.get("status", "active"),
        "confirmed_count": payload.get("confirmed_count", 0),
        "contradicted_count": payload.get("contradicted_count", 0),
        "superseded_by": payload.get("superseded_by"),
        # OWM (app/owm.py): outcome-weighted efficacy, read by the RAG
        # lifecycle scorer. Absent -> neutral.
        "owm_efficacy": payload.get("owm_efficacy"),
        "owm_n": payload.get("owm_n"),
        # Feedback counters (set_feedback): direct thumbs from the dashboard or
        # the memory_feedback MCP tool, read by the RAG feedback multiplier.
        # Absent -> neutral, and the scorer must see them or they are inert.
        "feedback_useful_count": payload.get("feedback_useful_count", 0),
        "feedback_not_useful_count": payload.get("feedback_not_useful_count", 0),
        # Contested (workers/memory_agent.py deep_contradiction_pass): two
        # unconfirmed memories genuinely disagree and neither was silently
        # picked. Surfaced in recall annotations and the autopilot inbox.
        "contested": payload.get("contested"),
        "contested_with": payload.get("contested_with"),
    }


def _merge_lifecycle(existing: dict | None, fresh: dict) -> dict:
    """Merge lifecycle fields from an existing point payload into a fresh one.

    SP0 A3 (defect #6): re-learning identical text must not reset a memory's
    lifecycle. Rules: keep original created_at / agent_id / project (unless
    the original attribution is the "unknown" sentinel); confirmed_count and
    contradicted_count take the max; status is preserved — re-learning
    identical text does NOT resurrect a superseded/deprecated memory;
    archive provenance (_ARCHIVE_PROVENANCE_KEYS) rides along with that
    preserved status, so an archived memory keeps its recovery window and its
    pre-archive status instead of becoming an un-restorable, un-purgeable
    orphan; timestamp refreshes as last-seen. Pure function, no I/O.
    """
    if not existing:
        return fresh
    merged = dict(fresh)
    created = existing.get("created_at") or existing.get("timestamp")
    if created:
        merged["created_at"] = created
    for key in ("agent_id", "project"):
        original = existing.get(key)
        if original not in (None, "unknown"):
            merged[key] = original
    merged["confirmed_count"] = max(
        int(existing.get("confirmed_count") or 0),
        int(fresh.get("confirmed_count") or 0),
    )
    merged["contradicted_count"] = max(
        int(existing.get("contradicted_count") or 0),
        int(fresh.get("contradicted_count") or 0),
    )
    merged["status"] = existing.get("status") or fresh.get("status", "active")
    for key in _ARCHIVE_PROVENANCE_KEYS:
        if existing.get(key) is not None:
            merged[key] = existing[key]
    if existing.get("superseded_by"):
        merged["superseded_by"] = existing["superseded_by"]
    if existing.get("last_confirmed_at"):
        merged["last_confirmed_at"] = existing["last_confirmed_at"]
    return merged


def namespace_condition(namespace: str | None) -> Filter | FieldCondition | None:
    """The one namespace scope clause, shared by search and near-duplicate collapse.

    NAMESPACE IS A CATEGORY, NOT A TENANT. That is the design decision this
    function encodes, and it is worth stating because the code used to imply
    otherwise. ``workspace_id`` is the tenancy boundary: it is applied as a hard
    ``must`` in the same filter, it is DERIVED FROM THE VERIFIED PRINCIPAL, and
    a caller cannot choose it. ``namespace`` is the opposite — a free string on
    the request body that any caller may set to any value — so it can never
    isolate anything, and scoping recall to it buys no security while costing
    coverage. Measured on the live store: all 4347 memories carry ONE
    ``workspace_id``, while ``namespace`` holds 18 distinct values that read as
    topics — ``infrastructure``, ``engineering``, ``product``, ``research``,
    ``team``, ``architecture``, ``strategy``, ``release_operations`` — plus a
    few historical service names.

    THE SEMANTICS:

    * ``None`` — no clause. Every namespace the caller's ``workspace_id``
      already permits. This is what an unspecified namespace means, and it is
      the recall default (``ContextQuery.namespace`` defaults to ``None``).
    * any string, INCLUDING ``"default"`` — exactly that namespace. Asking for
      one category returns that category.

    The literal string ``"default"`` no longer means "everything". It used to,
    by accident: both call sites read ``if namespace != "default":`` before
    appending the condition, so ``default`` applied no filter at all while the
    response echoed ``namespace: "default"`` back as though it had. That was a
    real defect and it is gone — but the first fix for it made the clause
    unconditional while the client kit still sent the literal ``"default"`` on
    every recall, which hid 146 memories (129 of them active, several written
    the same week) behind a filter nobody asked for. The product's own shipped
    guidance tells agents to store operational facts under
    ``namespace="infrastructure"``; a default recall that cannot see them is a
    product that forgets on purpose. Separating "unspecified" from "default" is
    what lets both statements be true at once.

    ``default`` keeps ONE special property, orthogonal to the above: points
    written before the field existed carry no ``namespace`` key, and Qdrant will
    not match a missing field against ``MatchValue("default")``. An explicit
    ``namespace="default"`` therefore matches ``namespace == "default" OR the
    field is absent``, so scoping to the default category cannot silently
    disappear legacy memories.
    """
    if namespace is None:
        return None
    if namespace != "default":
        return FieldCondition(key="namespace", match=MatchValue(value=namespace))
    return Filter(
        should=[
            FieldCondition(key="namespace", match=MatchValue(value="default")),
            IsEmptyCondition(is_empty=PayloadField(key="namespace")),
        ]
    )


def _similarity_filter(namespace: str, domain: str | None, workspace_id: str) -> Filter:
    """Build the query filter used by ``find_similar`` (contradiction detection).

    ``workspace_id`` is a hard ``must`` (identity-v2 D4) — the same tenancy
    boundary ``search()`` applies, and for the identical reason: without it, a
    near-duplicate memory learned in workspace B could supersede a memory that
    belongs to workspace A. Required, not optional — see ``find_similar``'s
    own fail-closed check.

    Two ``must_not`` guards, both audit findings (Dreaming Task 5):
      1. ``confirmed_count > 0`` — this filter previously checked status/
         namespace/domain but not confirmed_count, so an ordinary /memory/learn
         could silently supersede a memory a human explicitly confirmed. GC's
         own scan already treats confirmed_count > 0 as untouchable; this
         brings contradiction detection in line with that guard.
      2. ``source == "dream"`` — ``find_similar``'s only caller is
         ``contradiction.py``'s ``detect_and_supersede``, invoked from every
         ordinary ``/memory/learn``; without this guard, learning ANY new
         memory similar enough to a dream could supersede that dream. This
         is distinct from the memory_agent's 6-hourly passes: those build
         their own filters (``_active_non_corpus_filter``) and never call
         ``find_similar`` at all — dreams are protected there separately.
      3. ``source == "dream_profile"`` — the person profiles written by
         ``app/dreams/profile.py``. This was MISSING while the docs claimed
         it was here (final-review I1). A profile is broad prose stamped
         ``domain="general"``, which makes it a plausible >=0.85 cosine match
         for an ordinary general-domain ``/memory/learn`` — so without this
         guard a routine learn could supersede the profile, and the "one
         continuously-updated profile per human" contract would be defeated
         by the most ordinary write path in the system.

    NAMESPACE ON THE WRITE PATH IS SCOPED, AND UNLIKE RECALL THAT IS THE POINT.
    ``/memory/learn`` always names a namespace (the model defaults it to
    ``"default"``), so this filter is always scoped to exactly one category.
    Previously ``default`` was the unfiltered wildcard here too, which made the
    behaviour asymmetric in the destructive direction: a write into ``default``
    could supersede an ``infrastructure`` memory, while a write into
    ``infrastructure`` could not see a ``default`` near-duplicate. Scoping it
    both ways only ever NARROWS what can be superseded — no memory becomes less
    recallable, and a memory filed under a different category is no longer
    collapsed by a write that never mentioned it. The cost is that the same fact
    stored under two categories will not be collapsed into one; that is the
    honest consequence of treating namespace as a category, and a duplicate is
    cheaper than a wrongful supersession.
    """
    conditions = [
        FieldCondition(key="status", match=MatchValue(value="active")),
        FieldCondition(key="workspace_id", match=MatchValue(value=workspace_id)),
    ]
    ns_clause = namespace_condition(namespace)
    if ns_clause is not None:
        conditions.append(ns_clause)
    if domain:
        conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
    must_not = [
        FieldCondition(key="confirmed_count", range=Range(gt=0)),
        FieldCondition(key="source", match=MatchValue(value="dream")),
        FieldCondition(key="source", match=MatchValue(value="dream_profile")),
    ]
    return Filter(must=conditions, must_not=must_not)


class VectorClient:
    """Async Qdrant client with integrated embedding generation."""

    def __init__(self, settings: Settings) -> None:
        self._host = settings.QDRANT_HOST
        self._port = settings.QDRANT_PORT
        self._collection = settings.QDRANT_COLLECTION
        self._embedding_dim = settings.EMBEDDING_DIM
        self._llm_base_url = settings.LLM_BASE_URL
        self._llm_api_key = settings.LLM_API_KEY
        self._embedding_model = settings.EMBEDDING_MODEL
        self._client = AsyncQdrantClient(host=self._host, port=self._port)
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self._embed_retry_attempts = max(1, settings.EMBED_RETRY_ATTEMPTS)
        self._embed_max_chars = max(1, settings.EMBED_MAX_CHARS)
        # Identity-v2 D5: transitional lifecycle bridge, see upsert().
        self._v1_bridge_enabled = settings.MEMORY_ID_V1_BRIDGE
        # LRU embedding cache: hash(text) -> embedding vector
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        # TTL cache for get_stats()
        self._stats_cache: dict | None = None
        self._stats_cache_time: float = 0.0
        self._STATS_CACHE_TTL = 60.0

    async def initialize(self) -> None:
        """Create the Qdrant collection if it doesn't already exist.

        identity-v2 D6: "exists" is decided by `get_collection(name)`
        succeeding, not by membership in `get_collections()`'s list. The
        migration's cut-over model points ``QDRANT_COLLECTION`` at a new
        canonical name and, later, an operator may create an alias
        (old name -> new collection) for out-of-band tools. Qdrant v1.13.2
        resolves an alias transparently through `get_collection()` but does
        NOT list it in `get_collections()` -- empirically verified in
        review -- so the old membership check would see "not present" for
        an alias-resolved name and attempt `create_collection` at that
        name. Qdrant refuses a name colliding with an existing alias, so
        startup would abort every time a deploy used an alias. Only a
        genuine not-found (404) triggers creation; any other failure
        (including from create_collection itself) is wrapped as
        VectorStoreError so a transient outage surfaces as a startup
        failure rather than a swallowed retry-forever loop.
        """
        should_create = False
        try:
            await self._client.get_collection(self._collection)
        except UnexpectedResponse as exc:
            if exc.status_code == 404:
                should_create = True
            else:
                raise VectorStoreError(
                    f"Failed to initialize Qdrant collection: {exc}"
                ) from exc
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to initialize Qdrant collection: {exc}"
            ) from exc

        if should_create:
            try:
                await self._client.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(
                        size=self._embedding_dim,
                        distance=Distance.COSINE,
                    ),
                )
                logger.info(
                    "Created Qdrant collection '%s' (dim=%d, cosine)",
                    self._collection,
                    self._embedding_dim,
                )
            except Exception as exc:
                raise VectorStoreError(
                    f"Failed to initialize Qdrant collection: {exc}"
                ) from exc
        else:
            logger.info(
                "Qdrant collection '%s' already exists (directly or via alias)",
                self._collection,
            )

        # Create payload indexes for faster filtered queries
        for field_name in ("tags", "namespace"):
            try:
                await self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                )
            except Exception:
                pass  # Index may already exist

    async def close(self) -> None:
        """Close the underlying Qdrant and HTTP clients."""
        await self._http_client.aclose()
        await self._client.close()
        logger.info("Qdrant client closed")

    async def ping(self) -> None:
        """Verify connectivity to Qdrant. Raises on failure."""
        await self._client.get_collections()

    async def embeddings_ready(self) -> tuple[bool, str]:
        """Can this deployment actually embed right now? Returns (ready, detail).

        Exists because "the stack is up" and "your memories are searchable" are
        different facts, and the install used to conflate them. The ~3.3GB model
        pull runs in the background now (install.sh), and until it finishes every
        write returns HTTP 200 with status="partial" and is queued for backfill —
        successful-looking, and not recallable. Something has to be able to SAY
        that, or "partial" is discovered by a user wondering why recall is empty.

        Deliberately the real embed call rather than a model-registry lookup: the
        question is whether embedding WORKS, and a model that is listed but not
        loadable answers that question wrong. /health caches for 10s, and the
        one-character input hits the embed cache after the first call, so the
        cost is one tiny request per 10s at worst.

        Never raises: a health probe that can fail is a health endpoint that can
        500, and a caller learns strictly less from an exception than from
        (False, why).
        """
        try:
            vector = await self._embed_post("ok")
        except Exception as exc:  # noqa: BLE001 — a probe reports, it does not raise
            detail = str(exc)
            # The distinctive first-install shape: ollama answers, but the model
            # is not there yet. Worth separating from "the endpoint is down",
            # because one resolves itself and the other needs a human.
            lowered = detail.lower()
            if "not found" in lowered or "try pulling" in lowered or "404" in lowered:
                return False, f"model {self._embedding_model!r} is not pulled yet"
            return False, detail[:200]
        if not vector:
            return False, f"model {self._embedding_model!r} returned an empty vector"
        return True, f"{self._embedding_model} ({len(vector)}-dim)"

    async def memory_count(self) -> int | None:
        """Return the number of points in the collection, or None on failure."""
        try:
            info = await self._client.get_collection(self._collection)
            return info.points_count
        except Exception:
            return None

    async def get_stats(self) -> dict:
        """Return vector store statistics.

        Scrolls a sample of points to find oldest/newest timestamps
        and namespace (domain) distribution. Results are cached for 60 seconds.
        """
        now = time.monotonic()
        if self._stats_cache is not None and (now - self._stats_cache_time) < self._STATS_CACHE_TTL:
            return self._stats_cache

        try:
            info = await self._client.get_collection(self._collection)
            total = info.points_count or 0
        except Exception:
            return {
                "total": 0,
                "oldest_memory": None,
                "newest_memory": None,
                "namespace_counts": {},
            }

        oldest: str | None = None
        newest: str | None = None
        namespace_counts: dict[str, int] = {}

        # Scroll through all points to aggregate stats
        offset = None
        while True:
            try:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=None,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                break

            if not records:
                break

            for point in records:
                payload = point.payload or {}
                ts = payload.get("timestamp")
                if ts:
                    ts_str = str(ts)
                    if oldest is None or ts_str < oldest:
                        oldest = ts_str
                    if newest is None or ts_str > newest:
                        newest = ts_str
                ns = payload.get("namespace", "default")
                namespace_counts[ns] = namespace_counts.get(ns, 0) + 1

            if next_offset is None:
                break
            offset = next_offset

        result = {
            "total": total,
            "oldest_memory": oldest,
            "newest_memory": newest,
            "namespace_counts": namespace_counts,
        }
        self._stats_cache = result
        self._stats_cache_time = time.monotonic()
        return result

    async def scroll_all(self, namespace: str | None = None, batch_size: int = 100):
        """Async generator that yields all points, optionally filtered by namespace.

        Yields dicts with id, text, metadata, namespace.
        """
        scroll_filter = None
        if namespace:
            scroll_filter = Filter(
                must=[
                    FieldCondition(
                        key="namespace",
                        match=MatchAny(any=[namespace]),
                    )
                ]
            )

        offset = None
        while True:
            try:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=scroll_filter,
                    limit=batch_size,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception:
                break

            if not records:
                break

            for point in records:
                payload = point.payload or {}
                yield {
                    "id": str(point.id),
                    "text": payload.get("text", ""),
                    "metadata": payload.get("metadata", {}),
                    "namespace": payload.get("namespace", "default"),
                    "domain": payload.get("domain", "general"),
                    "tags": payload.get("tags", []),
                    "source": payload.get("source", ""),
                    "created_at": payload.get("timestamp", ""),
                }

            if next_offset is None:
                break
            offset = next_offset

    def _list_filter(
        self, view: str, namespace: str | None, member_id: str | None = None
    ) -> Filter:
        """Build the lifecycle/namespace filter shared by both list_memories legs.

        Always includes the visibility egress conditions (Docdex §4.4): the
        ``should`` group admits legacy/workspace chunks plus the caller's own
        private ones (none when ``member_id`` is absent — fail closed), and
        ``GENERATION_GUARD`` excludes never-committed corpus generations.
        Memory points carry neither field, so they pass both.
        """
        must: list[FieldCondition] = []
        must_not: list[FieldCondition] = []
        if view == "archived":
            must.append(
                FieldCondition(key="status", match=MatchValue(value="archived"))
            )
        elif view == "available":
            must_not.append(
                FieldCondition(key="status", match=MatchValue(value="archived"))
            )
        if namespace:
            must.append(
                FieldCondition(key="namespace", match=MatchAny(any=[namespace]))
            )
        must_not.append(GENERATION_GUARD)
        return Filter(
            must=must or None,
            should=visibility_should(member_id),
            must_not=must_not,
        )

    @staticmethod
    def _list_row(point: Any, score: float | None, view: str) -> dict:
        """Project one point into a listing row.

        The archived view additionally carries the recovery metadata a human
        needs to decide whether to restore: where the archive came from, why,
        and how long is left before it becomes purge-eligible. Ordinary views
        keep the original seven-key shape.
        """
        payload = point.payload or {}
        row = {
            "id": str(point.id),
            "score": score,
            "text": payload.get("text", ""),
            "domain": payload.get("domain", ""),
            "tags": payload.get("tags", []),
            "timestamp": payload.get("timestamp", ""),
            "source": payload.get("source", ""),
        }
        if view != "archived":
            return row
        row.update(
            {
                "memory_type": payload.get("memory_type", "episodic"),
                "status": payload.get("status", "active"),
                "archived_at": payload.get("archived_at"),
                "archive_source": payload.get("archive_source"),
                "archive_reason": payload.get("archive_reason"),
                "purge_eligible_at": payload.get("purge_eligible_at"),
                "confirmed_count": payload.get("confirmed_count", 0),
                "access_count": payload.get("access_count", 0),
                "agent_id": payload.get("agent_id"),
                "project": payload.get("project"),
                "metadata": payload.get("metadata", {}),
            }
        )
        return row

    async def list_memories(
        self,
        limit: int = 20,
        offset: int = 0,
        query: str | None = None,
        namespace: str | None = None,
        view: str = "all",
        member_id: str | None = None,
    ) -> list[dict]:
        """List memories with optional search.

        If query is provided, do semantic search. Otherwise scroll through points.
        If namespace is provided, filter by domain.

        ``member_id`` is the caller's VERIFIED member identity: member-private
        corpus chunks are listed only for their owner (Docdex §4.4). None —
        every pre-existing caller — lists no private chunks (fail closed).

        ``view`` selects the lifecycle slice: "all" (default, unfiltered),
        "available" (everything recall can still reach) or "archived" (the
        dashboard's recovery view, which also projects archive provenance).
        An unknown view raises ValueError *before* the store is touched — a
        typo'd view must not silently degrade to listing everything, which is
        exactly how archived memories would leak back into a caller expecting
        only live ones.
        """
        if view not in _MEMORY_VIEWS:
            raise ValueError(
                f"view must be one of {', '.join(_MEMORY_VIEWS)}; got {view!r}"
            )

        effective_limit = min(limit, 100)
        query_filter = self._list_filter(view, namespace, member_id)

        try:
            if query:
                # Semantic search
                vector = await self._embed(query)
                results = await self._client.query_points(
                    collection_name=self._collection,
                    query=vector,
                    query_filter=query_filter,
                    limit=effective_limit + offset,
                    with_payload=True,
                )
                points = results.points[offset:]
                return [self._list_row(p, p.score, view) for p in points]
            else:
                # Scroll through points
                records, _next_offset = await self._client.scroll(
                    collection_name=self._collection,
                    scroll_filter=query_filter,
                    limit=effective_limit + offset,
                    with_payload=True,
                    with_vectors=False,
                )
                points = records[offset:]
                return [self._list_row(p, None, view) for p in points]
        except Exception as exc:
            logger.error("Failed to list memories: %s", exc)
            return []

    async def upsert(
        self,
        text: str,
        metadata: dict[str, Any],
        namespace: str = "default",
        point_id: str | None = None,
    ) -> str:
        """Embed text and upsert into Qdrant.

        Args:
            text: The text content to embed and store.
            metadata: Must include 'source', 'tags', 'domain'. Team-continuity
                      keys ('agent_id', 'session_id', 'project') are promoted
                      to top-level payload fields when present, so endpoints
                      like /memory/contributors can filter/group on them.
                      Other keys are stored under 'metadata'.
            namespace: Tenant namespace for multi-tenant isolation. Normalized
                      once here (see app.models.normalize_namespace) before
                      use in either the stored payload or a minted point id.
            point_id: caller-scoped identity (corpus uses source-scoped IDs so
                      identical text never collapses across sources/members —
                      Docdex §4.2); None mints a scoped id from
                      metadata["workspace_id"] via memory_point_id
                      (identity-v2 D1) — a write with no point_id and no
                      verified workspace_id is refused rather than minted
                      unscoped (identity-v2 D3, fail-closed).

        Returns:
            The generated point ID as a string.

        Raises:
            VectorStoreError: point_id is None and metadata carries no
                workspace_id — there is no unscoped mint.
        """
        try:
            namespace = normalize_namespace(namespace)
            minted = point_id is None
            workspace_id = metadata.get("workspace_id")
            if point_id is None:
                try:
                    point_id = memory_point_id(workspace_id, namespace, text)
                except ValueError as exc:
                    raise VectorStoreError(
                        "memory write refused: no verified workspace_id — "
                        "cannot mint a scoped point id"
                    ) from exc

            vector = await self._embed(text)

            ts = metadata.get(
                "timestamp",
                datetime.now(timezone.utc).isoformat(),
            )
            payload = {
                "text": text,
                "source": metadata.get("source", "unknown"),
                "tags": metadata.get("tags", []),
                "domain": metadata.get("domain", "general"),
                "namespace": namespace,
                "timestamp": ts,
                "created_at": ts,
                # SP0 B2: top-level copy so GC (and Qdrant keyword filters like
                # skills/api.py's memory_type filter) can read it directly.
                # The nested metadata.memory_type copy below is retained for
                # back-compat readers (rag.py decay).
                "memory_type": metadata.get("memory_type", "episodic"),
                # Team continuity: promote each key in _PROMOTED_PAYLOAD_KEYS
                # to top-level so contributors endpoint and project filters
                # can read them directly. Defaults: agent_id/session_id ->
                # "unknown", everything else -> None.
                **{
                    k: metadata.get(
                        k, "unknown" if k in {"agent_id", "session_id"} else None
                    )
                    for k in _PROMOTED_PAYLOAD_KEYS
                },
                # Docdex §4.1: corpus stamps `visibility` and the shared
                # visibility filter matches it at the TOP level. Promoted only
                # when present — memory writes carry none, and absence is the
                # legacy meaning the filter honors, so no default is stamped.
                **(
                    {"visibility": metadata["visibility"]}
                    if "visibility" in metadata
                    else {}
                ),
                # Generation gate (Docdex §4.5): `committed` matches at the TOP
                # level too — GENERATION_GUARD excludes never-committed corpus
                # chunks from recall, and it looks top-level. Without this
                # promotion the flag lands only in nested metadata and the guard
                # is a no-op, leaving a mid-ingest generation fully recallable.
                # Present-only, like visibility — memory writes carry none.
                **(
                    {"committed": metadata["committed"]}
                    if "committed" in metadata
                    else {}
                ),
                "metadata": {
                    k: v
                    for k, v in metadata.items()
                    if k not in _EXCLUDED_FROM_NESTED_METADATA
                },
                "status": "active",
                "confirmed_count": 0,
                "contradicted_count": 0,
                "last_confirmed_at": None,
                "superseded_by": None,
            }

            # SP0 A3 (defect #6): uuid5(text) collisions merge lifecycle
            # fields instead of wholesale-replacing the point.
            existing_payload: dict | None = None
            try:
                existing_points = await self._client.retrieve(
                    self._collection, [point_id], with_payload=True
                )
                if existing_points and isinstance(
                    getattr(existing_points[0], "payload", None), dict
                ):
                    existing_payload = existing_points[0].payload
            except Exception as exc:
                # Pre-fetch failure must not block the write — treat as new.
                logger.warning(
                    "Lifecycle pre-fetch failed for %s (treating as new): %s",
                    point_id,
                    exc,
                )
            # Identity-v2 D5 compat bridge: a v2-id miss may still be an old
            # v1 point (bare uuid5(text)) — relearning it would otherwise
            # mint/pass a fresh v2 id and resurrect an archived/superseded
            # memory ACTIVE. This must cover BOTH id sources: `minted` (no
            # point_id given — transfer/backfill) AND the /memory/learn shape,
            # where main.py precomputes the identical memory_point_id and
            # passes it in as an explicit point_id (identity-v2 D2) — the
            # primary relearn path, and the one D5 exists to protect. There is
            # no reliable way to tell "explicit id that happens to equal the
            # scheme" from "the route computed it itself", so classification
            # recomputes the scheme id and compares: cheap (one more uuid5),
            # and a caller-supplied id from a DIFFERENT scheme (corpus's
            # source-scoped ids, dreams, skills) fails the equality and is
            # correctly excluded — a route-vs-upsert formula divergence fails
            # SAFE (the bridge silently doesn't fire; the write still uses
            # whatever id the caller passed).
            is_memory_scheme_id = minted or (
                bool(workspace_id)
                and point_id == memory_point_id(workspace_id, namespace, text)
            )
            if existing_payload is None and is_memory_scheme_id and self._v1_bridge_enabled:
                try:
                    v1_points = await self._client.retrieve(
                        self._collection, [_v1_point_id(text)], with_payload=True
                    )
                    if v1_points and isinstance(
                        getattr(v1_points[0], "payload", None), dict
                    ):
                        existing_payload = v1_points[0].payload
                except Exception as exc:
                    logger.warning(
                        "v1 lifecycle bridge pre-fetch failed for %s "
                        "(treating as new): %s",
                        point_id,
                        exc,
                    )
            payload = _merge_lifecycle(existing_payload, payload)

            await self._client.upsert(
                collection_name=self._collection,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload=payload,
                    )
                ],
            )
            return point_id
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to upsert vector: {exc}"
            ) from exc

    async def upsert_point(self, point_id: str, text: str, payload: dict) -> str:
        """Write a point at a CALLER-CHOSEN id with a caller-owned payload.

        `upsert` derives its id via memory_point_id(workspace_id, namespace, text)
        when the caller passes no point_id, and merges lifecycle from whatever
        point already sits at that id — which is right for learned memories and
        wrong for anything that must be updated in place (skills already work
        around it with a raw PointStruct; dreams are the second case). Nothing
        here infers, promotes or supersedes: the payload is written verbatim.
        """
        try:
            vector = await self._embed(text)
            await self._client.upsert(
                collection_name=self._collection,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
            return point_id
        except VectorStoreError:
            # Same guard `upsert` above already carries: `_embed` raises
            # VectorStoreError of its own, and re-wrapping it here nested one
            # "Failed to ..." message inside another, burying the real cause
            # (e.g. the context-length text the embed path reports).
            raise
        except Exception as exc:
            raise VectorStoreError(f"Failed to upsert point {point_id}: {exc}") from exc

    async def delete_by_filter(self, payload_filter: Filter) -> None:
        """Delete points matching a payload filter.

        Args:
            payload_filter: Qdrant Filter with field conditions.
        """
        try:
            await self._client.delete(
                collection_name=self._collection,
                points_selector=payload_filter,
            )
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to delete by filter: {exc}"
            ) from exc

    async def search(
        self,
        query: str,
        top_k: int = 5,
        filter_tags: list[str] | None = None,
        namespace: str | None = "default",
        include_archived: bool = False,
        project: str | None = None,
        workspace_id: str | None = None,
        score_threshold: float | None = None,
        member_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Embed query and search Qdrant for similar vectors.

        Args:
            query: The search query text.
            top_k: Maximum number of results to return.
            filter_tags: Optional list of tags to filter on (match any).
            namespace: Category scope. ``None`` (the recall default) searches
                every namespace inside ``workspace_id``; any string, including
                ``"default"``, scopes to exactly that namespace. Tenancy is
                ``workspace_id``'s job, not this argument's — see
                ``namespace_condition``.
            member_id: The caller's VERIFIED member identity. Member-private
                corpus chunks match only for their owner (Docdex §4.4); None
                — every non-corpus caller — matches no private chunks
                (fail closed).

        Returns:
            List of dicts with id, score, text, and metadata.
        """
        try:
            vector = await self._embed(query)

            filter_conditions = []
            must_not_conditions = []
            if filter_tags:
                filter_conditions.append(
                    FieldCondition(
                        key="tags",
                        match=MatchAny(any=filter_tags),
                    )
                )
            # Hard project scope (SP0 C3): when the caller declares a project,
            # only that project's memories may match. `project` is a promoted
            # top-level payload field (see _PROMOTED_PAYLOAD_KEYS).
            if project:
                filter_conditions.append(
                    FieldCondition(
                        key="project",
                        match=MatchValue(value=project),
                    )
                )
            if workspace_id:
                filter_conditions.append(
                    FieldCondition(
                        key="workspace_id",
                        match=MatchValue(value=workspace_id),
                    )
                )
            ns_clause = namespace_condition(namespace)
            if ns_clause is not None:
                filter_conditions.append(ns_clause)
            if not include_archived:
                must_not_conditions.append(
                    FieldCondition(
                        key="status",
                        match=MatchValue(value="archived"),
                    )
                )
            # Exclude explicitly invalidated memories (versioned memory support).
            # Points without the is_valid field are treated as valid (pre-versioning data).
            # Qdrant skips points where the filtered field doesn't exist, so only
            # points with is_valid=False are excluded — this is the correct behavior.
            must_not_conditions.append(
                FieldCondition(
                    key="is_valid",
                    match=MatchValue(value=False),
                )
            )
            # Exclude unapproved draft skills (SP2 recall-safety back-door fix).
            # skill_recall / skills_section already exclude drafts via explicit
            # scroll() filters, but this search() path — which backs the
            # primary memory_recall/recall_streaming RAG interface — had no
            # such guard, so a semantically-relevant DRAFT skill could surface
            # before human approval. Points without the skill_status field
            # (i.e. every regular memory) do not match and are unaffected.
            must_not_conditions.append(
                FieldCondition(
                    key="skill_status",
                    match=MatchValue(value="draft"),
                )
            )
            # Docdex §4.4 egress: at least one visibility branch must match
            # (legacy-absent, workspace, or the caller's own private chunks),
            # and a never-committed corpus generation matches for no one.
            # Memory points carry neither field, so they are unaffected.
            must_not_conditions.append(GENERATION_GUARD)
            query_filter = Filter(
                must=filter_conditions or None,
                should=visibility_should(member_id),
                must_not=must_not_conditions,
            )

            results = await self._client.query_points(
                collection_name=self._collection,
                query=vector,
                query_filter=query_filter,
                limit=top_k,
                with_payload=True,
                score_threshold=score_threshold,
            )

            return [
                {
                    "id": str(point.id),
                    "score": point.score,
                    "text": point.payload.get("text", "") if point.payload else "",
                    "metadata": _projected_metadata(point.payload, str(point.id)),
                }
                for point in results.points
            ]
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to search vectors: {exc}"
            ) from exc

    async def batch_embed(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts, using cache and batching.

        Checks the embedding cache first, then sends uncached texts to the
        embedding API in chunks of up to 32. Falls back to individual _embed
        calls if the batch request fails.

        Args:
            texts: List of text strings to embed.

        Returns:
            List of embedding vectors in the same order as the input texts.
        """
        if not texts:
            return []

        # Map each input position to its cache key and check for hits
        cache_keys = [
            hashlib.sha256(t.encode()).hexdigest() for t in texts
        ]
        results: list[list[float] | None] = [None] * len(texts)
        uncached_indices: list[int] = []

        for i, key in enumerate(cache_keys):
            if key in self._embed_cache:
                self._embed_cache.move_to_end(key)
                results[i] = self._embed_cache[key]
                logger.debug("batch_embed cache hit for index %d", i)
            else:
                uncached_indices.append(i)

        if not uncached_indices:
            return results  # type: ignore[return-value]

        # Batch embed uncached texts in chunks
        uncached_texts = [texts[i] for i in uncached_indices]
        try:
            uncached_embeddings = await self._batch_embed_api(uncached_texts)
        except Exception:
            logger.warning(
                "Batch embed API failed, falling back to individual embeds"
            )
            uncached_embeddings = []
            for text in uncached_texts:
                uncached_embeddings.append(await self._embed(text))

        # Store results and update cache
        for idx, embedding in zip(uncached_indices, uncached_embeddings):
            results[idx] = embedding
            self._cache_put(cache_keys[idx], embedding)

        return results  # type: ignore[return-value]

    async def _batch_embed_api(self, texts: list[str]) -> list[list[float]]:
        """Send texts to the embedding API in chunks, return ordered results."""
        url = f"{self._llm_base_url}/embeddings"
        headers = {}
        if self._llm_api_key:
            headers["Authorization"] = f"Bearer {self._llm_api_key}"

        all_embeddings: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_CHUNK_SIZE):
            chunk = texts[start : start + _BATCH_CHUNK_SIZE]
            # Same context-window cap as _embed(): a single over-long text in the
            # batch would 400 the whole chunk.
            chunk = [t[: self._embed_max_chars] for t in chunk]
            try:
                response = await self._http_client.post(
                    url,
                    json={
                        "model": self._embedding_model,
                        "input": chunk,
                    },
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                # Sort by index to ensure correct ordering
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                all_embeddings.extend(item["embedding"] for item in sorted_data)
            except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                raise VectorStoreError(
                    f"Batch embedding request failed: {exc}"
                ) from exc
            except (KeyError, IndexError, TypeError) as exc:
                raise VectorStoreError(
                    f"Failed to parse batch embedding response: {exc}"
                ) from exc

        return all_embeddings

    async def _embed(self, text: str) -> list[float]:
        """Generate an embedding vector via the LLM embeddings endpoint.

        Calls POST {LLM_BASE_URL}/embeddings with the configured model.
        Results are cached by content hash (SHA-256) with LRU eviction.
        """
        # Cap to the model's context window BEFORE anything else — a too-long
        # input 400s ("input length exceeds the context length"), a non-retryable
        # error that would strand the memory vector-less. Truncate before the
        # cache key so the key matches what is actually embedded.
        if len(text) > self._embed_max_chars:
            text = text[: self._embed_max_chars]
        # Shrink-to-fit: even under the char cap, DENSE text (code, URLs,
        # technical terms) can exceed the model's TOKEN context — the endpoint
        # then 400s ("input length exceeds the context length"), a non-retryable
        # error that would strand the memory vector-less. Halve and retry until
        # it fits; the char cap above is just the fast common-case bound.
        while True:
            cache_key = hashlib.sha256(text.encode()).hexdigest()
            if cache_key in self._embed_cache:
                self._embed_cache.move_to_end(cache_key)
                logger.debug("Embedding cache hit for text hash %s", cache_key[:12])
                return self._embed_cache[cache_key]
            try:
                embedding = await self._embed_post(text)
            except VectorStoreError as exc:
                if "context length" in str(exc).lower() and len(text) > _MIN_EMBED_CHARS:
                    new_len = max(_MIN_EMBED_CHARS, len(text) // 2)
                    logger.warning(
                        "Embedding input too long (%d chars) — shrinking to %d and retrying",
                        len(text), new_len,
                    )
                    text = text[:new_len]
                    continue
                raise
            self._cache_put(cache_key, embedding)
            return embedding

    async def _embed_post(self, text: str) -> list[float]:
        """POST one text to the embeddings endpoint (SP0 A2 durability contract):
        retry transient failures (transport errors, 5xx) with exponential
        backoff; 4xx and parse errors raise VectorStoreError immediately (an
        input-too-long 400 is handled by _embed's shrink-to-fit loop)."""
        url = f"{self._llm_base_url}/embeddings"
        headers = {}
        if self._llm_api_key:
            headers["Authorization"] = f"Bearer {self._llm_api_key}"

        attempts = self._embed_retry_attempts
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                response = await self._http_client.post(
                    url,
                    json={
                        "model": self._embedding_model,
                        "input": text,
                    },
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                return data["data"][0]["embedding"]
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise VectorStoreError(
                        f"Embedding endpoint returned {exc.response.status_code}: "
                        f"{exc.response.text}"
                    ) from exc
                last_exc = exc
            except httpx.RequestError as exc:
                last_exc = exc
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise VectorStoreError(
                    f"Failed to generate embedding: {exc}"
                ) from exc
            if attempt < attempts - 1:
                delay = 0.5 * (2 ** attempt)
                logger.warning(
                    "Embedding attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1, attempts, last_exc, delay,
                )
                await asyncio.sleep(delay)

        if isinstance(last_exc, httpx.HTTPStatusError):
            raise VectorStoreError(
                f"Embedding endpoint returned {last_exc.response.status_code} "
                f"after {attempts} attempts: {last_exc.response.text}"
            ) from last_exc
        raise VectorStoreError(
            f"Failed to generate embedding after {attempts} attempts: {last_exc}"
        ) from last_exc

    async def set_feedback(
        self,
        memory_id: str,
        useful: bool,
        comment: str | None,
        timestamp: str,
    ) -> None:
        """Accumulate feedback counters on a memory point.

        Counters, not last-write-wins: the original implementation wrote three
        flat fields (feedback_useful/comment/timestamp), so a second thumb
        OVERWROTE the first and the ranking layer had nothing to aggregate —
        the signal existed but was structurally unusable, and nothing consumed
        it. The counters feed the recall-time feedback multiplier
        (engine/rag.py, FEEDBACK_* settings) through the same Beta-shrink OWM
        uses. Read-modify-write without a transaction: two racing thumbs can
        lose one count, the same accepted "benign undercount" contract as the
        recall access counters (workers/memory_agent.py flush passes).

        Raises if the point does not exist — the caller decides whether a
        missing id is an error (the REST route logs and continues).
        """
        points = await self._client.retrieve(
            collection_name=self._collection,
            ids=[memory_id],
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            raise VectorStoreError(f"Memory {memory_id} not found")
        payload = points[0].payload or {}
        useful_count = int(payload.get("feedback_useful_count", 0) or 0)
        not_useful_count = int(payload.get("feedback_not_useful_count", 0) or 0)
        if useful:
            useful_count += 1
        else:
            not_useful_count += 1
        update: dict[str, Any] = {
            "feedback_useful_count": useful_count,
            "feedback_not_useful_count": not_useful_count,
            "feedback_last_at": timestamp,
        }
        if comment:
            update["feedback_last_comment"] = comment[:500]
        await self._client.set_payload(
            collection_name=self._collection,
            payload=update,
            points=[memory_id],
        )

    async def get_embedding_info(self) -> dict:
        """Return embedding model info: model name, dimensions, cache size, total vectors."""
        collection_info = await self._client.get_collection(self._collection)
        return {
            "model": self._embedding_model,
            "dimensions": self._embedding_dim,
            "cache_size": len(self._embed_cache),
            "cache_max_size": _EMBED_CACHE_MAX_SIZE,
            "total_vectors": collection_info.points_count,
        }

    def clear_cache(self) -> None:
        """Clear the embedding cache."""
        self._embed_cache.clear()

    # ------------------------------------------------------------------
    # Knowledge lifecycle methods
    # ------------------------------------------------------------------

    async def update_status(
        self,
        memory_id: str,
        status: str,
        superseded_by: str | None = None,
        reason: str | None = None,
        count_as_contradiction: bool = True,
    ) -> None:
        """Update memory lifecycle status and optionally set superseded_by.

        SP0 B2: contradiction also persists a recomputed `confidence` so GC's
        composite eviction score reads reality instead of the 0.5 default.

        ``count_as_contradiction=False`` supersedes WITHOUT bumping
        ``contradicted_count`` or re-deriving confidence downward. It exists
        because ``contradiction.py`` decides supersession on cosine similarity
        alone — a near-duplicate RESTATEMENT and a genuine correction reach
        this method by the identical path, and the restatement is the common
        case. Recording "this memory was contradicted" for a memory that was
        merely said twice is a false claim, and it is not cosmetic: it feeds
        `compute_confidence`, the `(1+confirmed)/(1+contradicted)` factor in
        recall scoring, and the memory-agent's tie-breaks. Proven live —
        storing a fact and then the SAME fact reworded left the first with
        ``status=superseded, contradicted_count=1``, with nothing having
        contradicted it.

        Archiving through this path is a HUMAN act, so it records
        ``archive_source="manual"`` and no ``purge_eligible_at``: GC's purge
        pass only ever deletes archives it created itself, and a manual archive
        must not acquire a deletion deadline as a side effect of being
        archived. ``archived_from_status`` is what lets restore_memory put the
        memory back where it was rather than guessing "active".
        """
        payload: dict[str, Any] = {"status": status}
        if superseded_by:
            payload["superseded_by"] = superseded_by
        if reason:
            payload["status_reason"] = reason
        if status == "superseded":
            # The autopilot digest counts supersessions by this stamp. Before
            # it existed the digest could only guess "superseded by a memory
            # written in the window", which misses every supersession under a
            # pre-existing keeper (nightly deep pass, contested verdicts).
            payload["superseded_at"] = datetime.now(timezone.utc).isoformat()
        if status == "archived":
            points = await self._client.retrieve(
                self._collection, [memory_id], with_payload=True
            )
            previous = (
                (points[0].payload or {}).get("status", "active")
                if points
                else "active"
            )
            payload.update(
                {
                    "archived_at": datetime.now(timezone.utc).isoformat(),
                    "archived_from_status": (
                        previous if previous != "archived" else "active"
                    ),
                    "archive_source": "manual",
                    "archive_reason": reason,
                    "purge_eligible_at": None,
                }
            )
        if status == "superseded" and count_as_contradiction:
            # Increment contradicted_count and persist recomputed confidence
            points = await self._client.retrieve(self._collection, [memory_id], with_payload=True)
            if points:
                current = points[0].payload.get("contradicted_count", 0)
                confirmed = points[0].payload.get("confirmed_count", 0)
                payload["contradicted_count"] = current + 1
                from app.confidence import compute_confidence
                payload["confidence"] = compute_confidence(
                    confirmed_count=confirmed,
                    contradicted_count=current + 1,
                )
        await self._client.set_payload(
            collection_name=self._collection,
            payload=payload,
            points=[memory_id],
        )

    async def confirm_memory(self, memory_id: str) -> bool:
        """Confirm a memory is still valid — bump confirmed_count, update last_confirmed_at.

        SP0 B2: also persists a recomputed `confidence` payload field.

        Confirming also refreshes `timestamp` to the confirmation instant. Age
        is what the archive scorer measures, and a memory a human has just
        vouched for is not old evidence — leaving the original timestamp would
        keep re-nominating it for archival on every pass.
        """
        points = await self._client.retrieve(self._collection, [memory_id], with_payload=True)
        if not points:
            return False
        current_count = points[0].payload.get("confirmed_count", 0)
        contradicted = points[0].payload.get("contradicted_count", 0)
        confirmed_at = datetime.now(timezone.utc).isoformat()
        from app.confidence import compute_confidence
        await self._client.set_payload(
            collection_name=self._collection,
            payload={
                "confirmed_count": current_count + 1,
                "last_confirmed_at": confirmed_at,
                "timestamp": confirmed_at,
                "confidence": compute_confidence(
                    confirmed_count=current_count + 1,
                    contradicted_count=contradicted,
                ),
            },
            points=[memory_id],
        )
        return True

    async def restore_memory(self, memory_id: str) -> bool:
        """Bring an archived memory back, clearing its archive provenance.

        The memory returns to the status it held before it was archived
        (``archived_from_status``); legacy archives written before that field
        existed fall back to "active" rather than being guessed at. Returns
        False — with no write at all — for a missing point or one that is not
        archived, so a restore of something already live is a no-op rather than
        a status rewrite.

        `timestamp` is reset to the restore instant, mirroring confirm_memory:
        a memory a human has just pulled back out of the archive would
        otherwise still carry the age that got it archived and be re-archived
        on the very next GC pass.
        """
        points = await self._client.retrieve(
            self._collection, [memory_id], with_payload=True
        )
        if not points:
            return False
        payload = points[0].payload or {}
        if payload.get("status") != "archived":
            return False

        previous = payload.get("archived_from_status") or "active"
        if previous == "archived":
            previous = "active"
        restored_at = datetime.now(timezone.utc).isoformat()
        await self._client.set_payload(
            collection_name=self._collection,
            payload={
                "status": previous,
                "restored_at": restored_at,
                "timestamp": restored_at,
                **{k: None for k in _ARCHIVE_PROVENANCE_KEYS},
            },
            points=[memory_id],
        )
        return True

    async def get_lifecycle_states(
        self, memory_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return lifecycle state for each id that still exists in the store.

        Missing ids are simply absent from the result — the caller distinguishes
        "archived" from "no longer there", and both are reasons to drop a graph
        row that claims to be backed by this vector. Qdrant is authoritative for
        lifecycle, so this is the read the graph retrieval leg admits rows
        against; a store failure degrades to an empty map (fail closed: nothing
        is admitted) rather than propagating and taking recall down with it.

        ``project`` / ``workspace_id`` / ``namespace`` are projected alongside
        lifecycle because Qdrant is authoritative for SCOPE too, and the graph
        leg has no scope filter of its own: ``query_related`` takes neither a
        project nor a workspace_id, so a project-scoped recall was answered by
        a vector leg that honoured the scope and a graph leg that ignored it.
        The caller (``RAGEngine._scope_verdict``) uses these to gate the rows
        that name a vector memory; a row naming none is graph-owned knowledge
        and stays admitted, exactly as it does for lifecycle.
        """
        if not memory_ids:
            return {}
        try:
            points = await self._client.retrieve(
                self._collection, list(memory_ids), with_payload=True
            )
        except Exception as exc:
            logger.warning("Lifecycle state lookup failed: %s", exc)
            return {}
        states: dict[str, dict[str, Any]] = {}
        for point in points or []:
            payload = point.payload or {}
            states[str(point.id)] = {
                "id": str(point.id),
                "status": payload.get("status", "active"),
                "timestamp": payload.get("timestamp", ""),
                "memory_type": payload.get("memory_type", "episodic"),
                "confirmed_count": payload.get("confirmed_count", 0),
                "contradicted_count": payload.get("contradicted_count", 0),
                "project": payload.get("project"),
                "workspace_id": payload.get("workspace_id"),
                "namespace": payload.get("namespace"),
            }
        return states

    async def get_memory(self, memory_id: str) -> dict | None:
        """Retrieve a single memory point with its payload."""
        try:
            points = await self._client.retrieve(self._collection, [memory_id], with_payload=True)
        except Exception:
            return None
        if not points:
            return None
        p = points[0]
        return {
            "id": str(p.id),
            "text": p.payload.get("text", ""),
            "status": p.payload.get("status", "active"),
            "confirmed_count": p.payload.get("confirmed_count", 0),
            "contradicted_count": p.payload.get("contradicted_count", 0),
            "last_confirmed_at": p.payload.get("last_confirmed_at"),
            "superseded_by": p.payload.get("superseded_by"),
            "metadata": {k: v for k, v in p.payload.items() if k not in ("text", "status", "confirmed_count", "contradicted_count", "last_confirmed_at", "superseded_by")},
        }

    async def find_similar(
        self,
        text: str,
        namespace: str = "default",
        domain: str | None = None,
        threshold: float = 0.85,
        top_k: int = 3,
        *,
        workspace_id: str,
    ) -> list[dict]:
        """Find similar active memories for contradiction detection.

        ``workspace_id`` is required (identity-v2 D4): this is the query that
        decides what an ordinary ``/memory/learn`` is allowed to supersede, and
        without a tenancy filter a near-duplicate written in one workspace
        could mark another workspace's memory superseded. The ONLY caller is
        ``contradiction.detect_and_supersede``, which always has the verified
        principal's workspace to pass.

        Raises:
            ValueError: workspace_id is falsy — there is no unscoped search.
        """
        if not workspace_id:
            raise ValueError("find_similar requires a non-empty workspace_id")
        embedding = await self._embed(text)
        results = await self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            query_filter=_similarity_filter(
                namespace=namespace, domain=domain, workspace_id=workspace_id
            ),
            limit=top_k,
            with_payload=True,
        )
        matches = []
        for point in results.points:
            if point.score >= threshold:
                matches.append({
                    "id": str(point.id),
                    "score": point.score,
                    "text": point.payload.get("text", ""),
                    "domain": point.payload.get("domain", ""),
                    "namespace": point.payload.get("namespace", "default"),
                    "metadata": point.payload,
                })
        return matches

    async def delete_by_chain_id(self, chain_id: str) -> bool:
        """Delete a Qdrant point by memory_chain_id.

        Used when invalidating a memory — removes it from recall results.
        Returns True if a point was deleted, False otherwise.
        """
        try:
            from qdrant_client.models import FilterSelector
            await self._client.delete(
                collection_name=self._collection,
                points_selector=FilterSelector(
                    filter=Filter(must=[
                        FieldCondition(key="memory_chain_id", match=MatchValue(value=chain_id)),
                    ]),
                ),
            )
            return True
        except Exception as e:
            logger.warning("delete_by_chain_id(%s) failed: %s", chain_id, e)
            return False

    def _cache_put(self, key: str, value: list[float]) -> None:
        """Insert into the embedding cache with LRU eviction."""
        if key in self._embed_cache:
            self._embed_cache[key] = value
            self._embed_cache.move_to_end(key)
            return
        if len(self._embed_cache) >= _EMBED_CACHE_MAX_SIZE:
            self._embed_cache.popitem(last=False)
        self._embed_cache[key] = value
