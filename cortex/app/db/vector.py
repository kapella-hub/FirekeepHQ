"""Qdrant vector database client for FirekeepCortex.

Handles embedding generation, vector upserts, and semantic search
for the RAG engine's vector retrieval path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from typing import TYPE_CHECKING, Any

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.exceptions import VectorStoreError

if TYPE_CHECKING:
    from app.config import Settings

logger = logging.getLogger(__name__)

# Deterministic namespace for content-based UUIDs (uuid5).
FIREKEEP_UUID_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

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
_PROMOTED_PAYLOAD_KEYS = {"agent_id", "session_id", "project"}

# Keys excluded from the nested ``metadata`` sub-dict in the Qdrant payload —
# either because they already live at the top level (source/tags/domain/timestamp)
# or because they were just promoted (_PROMOTED_PAYLOAD_KEYS). Single source of
# truth: extend _PROMOTED_PAYLOAD_KEYS and this set updates automatically.
_EXCLUDED_FROM_NESTED_METADATA = (
    {"source", "tags", "domain", "timestamp"} | _PROMOTED_PAYLOAD_KEYS
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
    if not payload:
        return {}
    return {
        # "id" was added by Task 8 (access-count HINCRBY reads it) — this
        # replacement MUST keep it.
        "id": point_id,
        "source": payload.get("source", ""),
        "tags": payload.get("tags", []),
        "domain": payload.get("domain", ""),
        "timestamp": payload.get("timestamp", ""),
        **(payload.get("metadata", {})),
        # Team continuity: who wrote this, in what session, for what project.
        **{k: payload[k] for k in _PROMOTED_PAYLOAD_KEYS if k in payload},
        # Lifecycle fields last so the top-level payload is authoritative —
        # recall scoring reads these (SP0 C2).
        "status": payload.get("status", "active"),
        "confirmed_count": payload.get("confirmed_count", 0),
        "contradicted_count": payload.get("contradicted_count", 0),
        "superseded_by": payload.get("superseded_by"),
        # OWM (app/owm.py): outcome-weighted efficacy, read by the RAG
        # lifecycle scorer. Absent -> neutral.
        "owm_efficacy": payload.get("owm_efficacy"),
        "owm_n": payload.get("owm_n"),
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
        # LRU embedding cache: hash(text) -> embedding vector
        self._embed_cache: OrderedDict[str, list[float]] = OrderedDict()
        # TTL cache for get_stats()
        self._stats_cache: dict | None = None
        self._stats_cache_time: float = 0.0
        self._STATS_CACHE_TTL = 60.0

    async def initialize(self) -> None:
        """Create the Qdrant collection if it doesn't already exist."""
        try:
            collections = await self._client.get_collections()
            existing = {c.name for c in collections.collections}
            if self._collection not in existing:
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
            else:
                logger.info(
                    "Qdrant collection '%s' already exists",
                    self._collection,
                )
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to initialize Qdrant collection: {exc}"
            ) from exc

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

    def _list_filter(self, view: str, namespace: str | None) -> Filter | None:
        """Build the lifecycle/namespace filter shared by both list_memories legs.

        ``view="all"`` with no namespace yields None — byte-identical to the
        pre-lifecycle behaviour, so the default listing is untouched.
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
        if not must and not must_not:
            return None
        return Filter(must=must or None, must_not=must_not or None)

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
    ) -> list[dict]:
        """List memories with optional search.

        If query is provided, do semantic search. Otherwise scroll through points.
        If namespace is provided, filter by domain.

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
        query_filter = self._list_filter(view, namespace)

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

    async def upsert(self, text: str, metadata: dict[str, Any], namespace: str = "default") -> str:
        """Embed text and upsert into Qdrant.

        Args:
            text: The text content to embed and store.
            metadata: Must include 'source', 'tags', 'domain'. Team-continuity
                      keys ('agent_id', 'session_id', 'project') are promoted
                      to top-level payload fields when present, so endpoints
                      like /memory/contributors can filter/group on them.
                      Other keys are stored under 'metadata'.
            namespace: Tenant namespace for multi-tenant isolation.

        Returns:
            The generated point ID as a string.
        """
        try:
            vector = await self._embed(text)
            point_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, text))

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
        namespace: str = "default",
        include_archived: bool = False,
        project: str | None = None,
        score_threshold: float | None = None,
    ) -> list[dict[str, Any]]:
        """Embed query and search Qdrant for similar vectors.

        Args:
            query: The search query text.
            top_k: Maximum number of results to return.
            filter_tags: Optional list of tags to filter on (match any).
            namespace: Tenant namespace for multi-tenant filtering.

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
            if namespace != "default":
                filter_conditions.append(
                    FieldCondition(
                        key="namespace",
                        match=MatchValue(value=namespace),
                    )
                )
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
            query_filter = (
                Filter(
                    must=filter_conditions or None,
                    must_not=must_not_conditions or None,
                )
                if filter_conditions or must_not_conditions
                else None
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
        """Update a memory point's payload with feedback metadata.

        Args:
            memory_id: The Qdrant point ID to update.
            useful: Whether the memory was useful.
            comment: Optional feedback comment.
            timestamp: ISO-format timestamp of the feedback.
        """
        await self._client.set_payload(
            collection_name=self._collection,
            payload={
                "feedback_useful": useful,
                "feedback_comment": comment,
                "feedback_timestamp": timestamp,
            },
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
    ) -> None:
        """Update memory lifecycle status and optionally set superseded_by.

        SP0 B2: contradiction also persists a recomputed `confidence` so GC's
        composite eviction score reads reality instead of the 0.5 default.

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
        if status == "superseded":
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

    async def find_similar(self, text: str, namespace: str = "default", domain: str | None = None, threshold: float = 0.85, top_k: int = 3) -> list[dict]:
        """Find similar active memories for contradiction detection."""
        embedding = await self._embed(text)
        conditions = [FieldCondition(key="status", match=MatchValue(value="active"))]
        if namespace != "default":
            conditions.append(FieldCondition(key="namespace", match=MatchValue(value=namespace)))
        if domain:
            conditions.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
        results = await self._client.query_points(
            collection_name=self._collection,
            query=embedding,
            query_filter=Filter(must=conditions),
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
