"""Garbage Collection worker — periodic pruning of old, low-value memories.

Removes expired memories from Qdrant and orphaned nodes from Neo4j
based on configurable age and feedback thresholds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

import redis.asyncio
from qdrant_client import QdrantClient

from app.config import get_settings
from app.workers.sleep_cycle import celery_app, _get_neo4j_driver, _get_redis_client

logger = logging.getLogger(__name__)

# Redis list holding recent eviction audit entries (capped at 1000).
GC_EVICTION_LOG_KEY = "gc:eviction:log"

# Redis hash accumulating recall access counts (flushed to Qdrant payloads
# by the memory-agent periodic pass; see app.workers.memory_agent).
ACCESS_COUNTS_KEY = "memory:access_counts"

# Redis hash mapping memory_id -> last-recall ISO timestamp (HSET on the recall
# hot path, flushed to the Qdrant `last_recalled_at` payload by the same
# memory-agent pass). Feeds the skill staleness sweep. Last-writer-wins on
# "now"; a lost entry is a benign undercount of a best-effort signal.
LAST_RECALLED_KEY = "memory:last_recalled"

_HALF_LIFE_MAP: dict[str, float] = {
    "reference": float("inf"),
    "procedural": 180.0,
    "episodic": 90.0,
    "transient": 14.0,
    "skill": float("inf"),  # SP0 B2: crystallized skills are never age-evicted
}


def owm_efficacy_for_eviction(payload: dict, enabled: bool) -> float:
    """OWM kill switch must neutralize GC too: disabled -> neutral 0.5 even if
    stale payload values remain. isinstance (not `or`): a stored 0.0 is the
    MAXIMUM penalty, not a falsy neutral (wf_51dd7c4e)."""
    if not enabled:
        return 0.5
    v = payload.get("owm_efficacy")
    return float(v) if isinstance(v, (int, float)) else 0.5


def compute_eviction_score(
    age_days: float,
    memory_type: str,
    access_count: int,
    confidence: float,
    efficacy: float = 0.5,
) -> float:
    """Lower score = more valuable. Prune when score > EVICTION_THRESHOLD.

    ``efficacy`` is the OWM outcome-weighted score (app/owm.py): 0.5 is neutral
    and leaves the score bit-identical to the pre-OWM formula (never-scored
    memories default to 0.5 at the call site); persistently misleading memories
    (efficacy -> 0) age out up to 1.5x faster, proven ones (-> 1) up to 2x slower.
    """
    half_life = _HALF_LIFE_MAP.get(memory_type, 90.0)
    if half_life == float("inf"):
        return 0.0
    age_ratio = age_days / half_life
    access_weight = 1.0 / (1.0 + math.log(access_count + 1))
    confidence_weight = max(0.0, 1.0 - confidence)
    efficacy_weight = 0.5 + (1.0 - efficacy)
    return age_ratio * access_weight * confidence_weight * efficacy_weight


def _get_qdrant_client() -> QdrantClient:
    """Create a synchronous Qdrant client from settings."""
    s = get_settings()
    return QdrantClient(host=s.QDRANT_HOST, port=s.QDRANT_PORT)


@celery_app.task(name="app.workers.gc.prune_memories")
def prune_memories() -> dict[str, Any]:
    """Prune low-value memories using composite eviction scoring.

    1. Scroll all Qdrant points and compute eviction_score per memory
    2. Skip confirmed memories (confirmed_count > 0) unconditionally
    3. Delete points where eviction_score > EVICTION_THRESHOLD
    4. Delete orphaned Neo4j nodes (nodes with no relationships)
    5. Return {"status": "completed", "pruned_vector": N, "pruned_graph": M}
    """
    try:
        return _prune()
    except Exception:
        logger.exception("Unhandled error in prune_memories")
        return {"status": "error", "pruned_vector": 0, "pruned_graph": 0}


def _prune() -> dict[str, Any]:
    """Core pruning logic."""
    settings = get_settings()

    pruned_vector = _prune_qdrant(settings)
    pruned_graph = _prune_neo4j_orphans()

    logger.info(
        "GC completed: pruned %d vector points, %d orphaned graph nodes",
        pruned_vector,
        pruned_graph,
    )

    # Fire gc.pruned webhook
    if pruned_vector > 0 or pruned_graph > 0:
        try:
            from app.webhooks import fire_webhooks

            async def _fire():
                r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
                try:
                    await fire_webhooks(
                        r, "gc.pruned",
                        {"pruned_vector": pruned_vector, "pruned_graph": pruned_graph},
                    )
                finally:
                    await r.aclose()

            asyncio.run(_fire())
        except Exception:
            logger.warning("Failed to fire gc.pruned webhook")

    return {
        "status": "completed",
        "pruned_vector": pruned_vector,
        "pruned_graph": pruned_graph,
    }


def _prune_qdrant(settings: Any) -> int:
    """Delete Qdrant points whose composite eviction score exceeds the threshold.

    SP0 B2 (defect #4):
      - memory_type read from the top-level payload, falling back to the
        legacy nested metadata.memory_type for pre-migration points.
      - skill memories and corpus chunks are never evicted.
      - confirmed memories (confirmed_count > 0) are never evicted (unchanged).
      - every eviction appends an audit entry to gc:eviction:log (LTRIM 1000).
    """
    client = _get_qdrant_client()
    collection = settings.QDRANT_COLLECTION
    pruned = 0

    # Pending (unflushed) access counts from the recall hot path — merged with
    # the persisted payload value so recently-used memories aren't evicted.
    pending_access: dict[str, int] = {}
    try:
        raw = _get_redis_client().hgetall(ACCESS_COUNTS_KEY)
        pending_access = {k: int(v) for k, v in raw.items()}
    except Exception:
        logger.warning("Could not read %s; using payload access counts only", ACCESS_COUNTS_KEY)

    try:
        offset = None
        batch_size = 100
        ids_to_delete: list[str] = []
        audit_entries: list[dict[str, Any]] = []

        while True:
            results = client.scroll(
                collection_name=collection,
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            points, next_offset = results

            for point in points:
                payload = point.payload or {}

                # Never evict confirmed memories
                confirmed = (payload.get("confirmed_count") or 0) > 0
                if confirmed:
                    continue

                # Never evict corpus chunks — corpus has its own lifecycle
                # (explicit re-ingest / corpus_delete).
                if payload.get("source") == "corpus":
                    continue

                # Top-level memory_type (written since SP0; backfilled by the
                # promote_memory_type migration), nested fallback for any
                # point that predates both.
                memory_type = (
                    payload.get("memory_type")
                    or (payload.get("metadata") or {}).get("memory_type")
                    or "episodic"
                )
                if memory_type == "skill":
                    continue

                # DELIBERATE: age from `timestamp` (last-seen; Task 4 refreshes
                # it on identical-text re-learn) so actively re-learned memories
                # reset their eviction clock — LRU semantics. `created_at` is
                # only the fallback for points that never carried a timestamp.
                created_str = payload.get("timestamp") or payload.get("created_at") or ""
                try:
                    created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - created).days
                except (ValueError, AttributeError):
                    age_days = 0

                access_count = int(payload.get("access_count") or 0) + pending_access.get(
                    str(point.id), 0
                )

                eviction_score = compute_eviction_score(
                    age_days=float(age_days),
                    memory_type=memory_type,
                    access_count=access_count,
                    confidence=float(payload.get("confidence") or 0.5),
                    efficacy=owm_efficacy_for_eviction(payload, bool(getattr(settings, "OWM_ENABLED", True))),
                )
                if eviction_score > settings.EVICTION_THRESHOLD:
                    ids_to_delete.append(str(point.id))
                    audit_entries.append({
                        "id": str(point.id),
                        "memory_type": memory_type,
                        "age_days": age_days,
                        "access_count": access_count,
                        "eviction_score": round(eviction_score, 4),
                        "text_preview": str(payload.get("text", ""))[:120],
                        "evicted_at": datetime.now(timezone.utc).isoformat(),
                    })

            if next_offset is None or not points:
                break
            offset = next_offset

        # Delete collected point IDs
        if ids_to_delete:
            from qdrant_client.models import PointIdsList
            client.delete(
                collection_name=collection,
                points_selector=PointIdsList(points=ids_to_delete),
            )
            pruned = len(ids_to_delete)

            # Eviction audit trail — loud on failure, never silent.
            try:
                pipe = _get_redis_client().pipeline()
                for entry in audit_entries:
                    pipe.lpush(GC_EVICTION_LOG_KEY, json.dumps(entry))
                pipe.ltrim(GC_EVICTION_LOG_KEY, 0, 999)
                pipe.execute()
            except Exception:
                logger.exception(
                    "Evicted %d memories but FAILED to write %s audit entries: %s",
                    pruned, GC_EVICTION_LOG_KEY, [e["id"] for e in audit_entries],
                )

    except Exception:
        logger.exception("Error pruning Qdrant points")

    finally:
        client.close()

    return pruned


def _prune_neo4j_orphans() -> int:
    """Delete orphaned Neo4j nodes (nodes with no relationships)."""
    try:
        driver = _get_neo4j_driver()
        with driver.session() as session:
            result = session.run(
                "MATCH (n) "
                "WHERE (n:Domain OR n:Concept OR n:Action "
                "       OR n:Outcome OR n:Resolution OR n:EventStream) "
                "  AND NOT (n)--() "
                "WITH n LIMIT 1000 "
                "DETACH DELETE n "
                "RETURN count(n) AS cnt"
            )
            record = result.single()
            return record["cnt"] if record else 0
    except Exception:
        logger.exception("Error pruning orphaned Neo4j nodes")
        return 0
