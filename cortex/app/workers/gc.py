"""Garbage Collection worker — periodic ARCHIVE-FIRST memory maintenance.

Scheduled maintenance never hard-deletes on first contact. A qualifying active
memory is ARCHIVED (status/archive_source/archived_at/purge_eligible_at written
onto the Qdrant payload), which removes it from recall while leaving it visible
and restorable in the dashboard. Only records GC archived ITSELF, whose recorded
recovery window has elapsed, are ever purged — and only when GC_PURGE_ENABLED is
explicitly on. Manual and legacy archives are never guessed at.

`preview_memories` reports the same candidate set without writing anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio
from qdrant_client import QdrantClient

from app.config import get_settings
from app.workers.sleep_cycle import celery_app, _get_neo4j_driver, _get_redis_client

logger = logging.getLogger(__name__)

# Redis list holding recent memory-maintenance audit entries (capped at 1000).
# Every entry carries an `action` ("archived" | "purged" | "would_archive" |
# "would_purge") and an `occurred_at`. Pre-archive-first entries have neither and
# are read as legacy purges by the dashboard.
GC_EVICTION_LOG_KEY = "gc:eviction:log"

# Redis hash accumulating recall access counts (flushed to Qdrant payloads
# by the memory-agent periodic pass; see app.workers.memory_agent).
ACCESS_COUNTS_KEY = "memory:access_counts"

# Redis hash mapping memory_id -> last-recall ISO timestamp (HSET on the recall
# hot path, flushed to the Qdrant `last_recalled_at` payload by the same
# memory-agent pass). Feeds the skill staleness sweep. Last-writer-wins on
# "now"; a lost entry is a benign undercount of a best-effort signal.
LAST_RECALLED_KEY = "memory:last_recalled"

# `archive_source` value stamped on archives GC created. Purge considers nothing
# else: an archive this task did not make carries a decision it cannot re-derive.
ARCHIVE_SOURCE_GC = "gc"

# Used whenever GC_ARCHIVE_GRACE_DAYS is missing or not a real number. Mirrors the
# config default rather than collapsing to 0 — a bad setting must not shorten the
# recovery window.
DEFAULT_ARCHIVE_GRACE_DAYS = 90

# The only lifecycle state automatic aging may archive. `deprecated`/`superseded`
# already carry a human/lifecycle decision, and re-stamping them as GC archives
# would make somebody else's decision look purge-eligible.
_ARCHIVABLE_STATUS = "active"

_HALF_LIFE_MAP: dict[str, float] = {
    "reference": float("inf"),
    "procedural": 180.0,
    "episodic": 90.0,
    "transient": 14.0,
    "skill": float("inf"),  # SP0 B2: crystallized skills are never age-evicted
}

# The same DECAY_*_DAYS settings that drive recall ranking drive archive
# eligibility. Falling back to the legacy table (rather than to a literal) is what
# keeps a deployment that never set these behaving exactly as it did before.
_DECAY_SETTING_FOR: dict[str, str] = {
    "reference": "DECAY_REFERENCE_DAYS",
    "procedural": "DECAY_PROCEDURAL_DAYS",
    "episodic": "DECAY_EPISODIC_DAYS",
    "transient": "DECAY_TRANSIENT_DAYS",
}


def _bool_setting(settings: Any, name: str, default: bool) -> bool:
    """Read a real bool or fall back to ``default``.

    Deliberately strict: a settings object that merely *has* the attribute (a
    stub, a partially-populated override) must not be able to switch on deletion
    by being truthy. Only an actual bool speaks for the operator.
    """
    value = getattr(settings, name, None)
    return value if isinstance(value, bool) else default


def _int_setting(settings: Any, name: str, default: int | None) -> int | None:
    """Read a real number or fall back to ``default`` (strict, see `_bool_setting`)."""
    value = getattr(settings, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)


def _parse_ts(raw: Any) -> datetime | None:
    """Parse an ISO timestamp, or None if it is absent/malformed."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    half_life_days: float | None = None,
) -> float:
    """Lower score = more valuable. Archive when score > EVICTION_THRESHOLD.

    ``efficacy`` is the OWM outcome-weighted score (app/owm.py): 0.5 is neutral
    and leaves the score bit-identical to the pre-OWM formula (never-scored
    memories default to 0.5 at the call site); persistently misleading memories
    (efficacy -> 0) age out up to 1.5x faster, proven ones (-> 1) up to 2x slower.

    ``half_life_days`` is the configured DECAY_*_DAYS value for this memory type.
    Non-positive means no age decay at all (the `reference` default) and returns
    0.0. ``None`` keeps the legacy per-type table — which is what a caller that
    could not resolve a real configured value must pass, so an unconfigured
    deployment ages exactly as it did before.
    """
    if half_life_days is None:
        half_life = _HALF_LIFE_MAP.get(memory_type, 90.0)
    elif half_life_days <= 0:
        return 0.0
    else:
        half_life = float(half_life_days)

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
    """Run scheduled archive-first memory maintenance.

    1. Nothing at all when GC_ENABLED is off
    2. Archive active memories whose composite eviction score exceeds the threshold
    3. Purge only GC-origin archives past their recovery window, and only when
       GC_PURGE_ENABLED is on — which also gates the Neo4j orphan cleanup
    4. GC_DRY_RUN evaluates and audits without touching Qdrant or Neo4j
    """
    try:
        return _prune()
    except Exception:
        logger.exception("Unhandled error in prune_memories")
        return {"status": "error", "pruned_vector": 0, "pruned_graph": 0}


def _zero_stats() -> dict[str, int]:
    return {
        "archived_vector": 0,
        "pruned_vector": 0,
        "would_archive_vector": 0,
        "would_purge_vector": 0,
    }


def _normalize_vector_stats(result: Any) -> dict[str, int]:
    """Accept either the stats dict or the legacy pruned-count int."""
    stats = _zero_stats()
    if isinstance(result, dict):
        stats.update({k: int(result.get(k, 0)) for k in stats})
    elif isinstance(result, int):
        stats["pruned_vector"] = result
    return stats


def _prune() -> dict[str, Any]:
    """Core maintenance logic — kill switches first, then vector, then graph."""
    settings = get_settings()

    if not _bool_setting(settings, "GC_ENABLED", True):
        logger.info("Memory maintenance skipped: GC_ENABLED is false")
        return {"status": "disabled", "dry_run": False, "pruned_graph": 0, **_zero_stats()}

    dry_run = _bool_setting(settings, "GC_DRY_RUN", False)
    purge_enabled = _bool_setting(settings, "GC_PURGE_ENABLED", False)

    stats = _normalize_vector_stats(
        _prune_qdrant(settings, return_stats=True, dry_run=dry_run)
    )

    # Orphan deletion is unconditional destruction with no archive tier of its
    # own, so it rides the same explicit purge switch — and never runs in dry run.
    pruned_graph = _prune_neo4j_orphans() if (purge_enabled and not dry_run) else 0

    logger.info(
        "Memory maintenance completed (dry_run=%s, purge_enabled=%s): "
        "archived %d, purged %d, would-archive %d, would-purge %d, graph orphans %d",
        dry_run, purge_enabled,
        stats["archived_vector"], stats["pruned_vector"],
        stats["would_archive_vector"], stats["would_purge_vector"],
        pruned_graph,
    )

    if not dry_run and (stats["archived_vector"] or stats["pruned_vector"] or pruned_graph):
        _fire_gc_webhook(settings, stats, pruned_graph)

    return {
        "status": "completed",
        "dry_run": dry_run,
        "pruned_graph": pruned_graph,
        **stats,
    }


def _fire_gc_webhook(settings: Any, stats: dict[str, int], pruned_graph: int) -> None:
    """Best-effort `gc.pruned` notification; never fails the run."""
    try:
        from app.webhooks import fire_webhooks

        async def _fire():
            r = redis.asyncio.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                await fire_webhooks(
                    r, "gc.pruned",
                    {
                        "archived_vector": stats["archived_vector"],
                        "pruned_vector": stats["pruned_vector"],
                        "pruned_graph": pruned_graph,
                    },
                )
            finally:
                await r.aclose()

        asyncio.run(_fire())
    except Exception:
        logger.warning("Failed to fire gc.pruned webhook")


def _pending_access_counts() -> dict[str, int]:
    """Unflushed access counts from the recall hot path, merged with the persisted
    payload value so a recently-used memory isn't archived for looking idle."""
    try:
        raw = _get_redis_client().hgetall(ACCESS_COUNTS_KEY)
        return {k: int(v) for k, v in raw.items()}
    except Exception:
        logger.warning("Could not read %s; using payload access counts only", ACCESS_COUNTS_KEY)
        return {}


def _purge_boundary(payload: dict, grace_days: int) -> datetime | None:
    """When this GC-origin archive becomes purge-eligible, or None if it never does.

    A RECORDED `purge_eligible_at` wins over the current setting: the recovery
    window a memory was archived under is a promise to whoever might restore it,
    and lowering GC_ARCHIVE_GRACE_DAYS afterwards must not retroactively expire
    archives that were made under the longer window. An unparseable boundary or
    archive date is never guessed at — it means "never purge".
    """
    if "purge_eligible_at" in payload:
        return _parse_ts(payload.get("purge_eligible_at"))
    archived_at = _parse_ts(payload.get("archived_at"))
    return None if archived_at is None else archived_at + timedelta(days=grace_days)


def _age_days(payload: dict, now: datetime) -> int:
    """DELIBERATE: age from `timestamp` (last-seen; identical-text re-learn
    refreshes it) so actively re-learned memories reset their aging clock — LRU
    semantics. `created_at` is only the fallback for points that never carried one."""
    created = _parse_ts(payload.get("timestamp") or payload.get("created_at"))
    return 0 if created is None else (now - created).days


def _scan_candidates(
    client: Any,
    settings: Any,
    *,
    purge_enabled: bool,
    pending_access: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scroll the collection and return (archive candidates, purge candidates).

    Pure evaluation — writes nothing. Shared by the real pass and `preview_memories`
    so a preview cannot drift from what the run would actually do.
    """
    collection = settings.QDRANT_COLLECTION
    threshold = settings.EVICTION_THRESHOLD
    grace_days = _int_setting(settings, "GC_ARCHIVE_GRACE_DAYS", DEFAULT_ARCHIVE_GRACE_DAYS)
    owm_enabled = bool(getattr(settings, "OWM_ENABLED", True))
    now = datetime.now(timezone.utc)

    archive_recs: list[dict[str, Any]] = []
    purge_recs: list[dict[str, Any]] = []

    offset = None
    batch_size = 100
    while True:
        points, next_offset = client.scroll(
            collection_name=collection,
            limit=batch_size,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )

        for point in points:
            payload = point.payload or {}

            # Confirmed memories are never touched by automatic aging.
            if (payload.get("confirmed_count") or 0) > 0:
                continue

            # Corpus chunks have their own lifecycle (re-ingest / corpus_delete).
            if payload.get("source") == "corpus":
                continue

            # Top-level memory_type (written since SP0; backfilled by the
            # promote_memory_type migration), nested fallback for any point that
            # predates both.
            memory_type = (
                payload.get("memory_type")
                or (payload.get("metadata") or {}).get("memory_type")
                or "episodic"
            )
            if memory_type == "skill":
                continue

            status = str(payload.get("status") or _ARCHIVABLE_STATUS)

            if status == "archived":
                if not purge_enabled:
                    continue
                if payload.get("archive_source") != ARCHIVE_SOURCE_GC:
                    continue
                boundary = _purge_boundary(payload, grace_days)
                if boundary is None or boundary > now:
                    continue
                purge_recs.append({
                    "id": str(point.id),
                    "memory_type": memory_type,
                    "archived_at": payload.get("archived_at"),
                    "purge_eligible_at": boundary.isoformat(),
                    "text_preview": str(payload.get("text", ""))[:120],
                })
                continue

            if status != _ARCHIVABLE_STATUS:
                continue

            age_days = _age_days(payload, now)
            access_count = int(payload.get("access_count") or 0) + pending_access.get(
                str(point.id), 0
            )
            decay_setting = _DECAY_SETTING_FOR.get(memory_type)
            eviction_score = compute_eviction_score(
                age_days=float(age_days),
                memory_type=memory_type,
                access_count=access_count,
                confidence=float(payload.get("confidence") or 0.5),
                efficacy=owm_efficacy_for_eviction(payload, owm_enabled),
                half_life_days=(
                    _int_setting(settings, decay_setting, None) if decay_setting else None
                ),
            )
            if eviction_score > threshold:
                archive_recs.append({
                    "id": str(point.id),
                    "memory_type": memory_type,
                    "age_days": age_days,
                    "access_count": access_count,
                    "eviction_score": round(eviction_score, 4),
                    "text_preview": str(payload.get("text", ""))[:120],
                })

        if next_offset is None or not points:
            break
        offset = next_offset

    return archive_recs, purge_recs


def _archive_points(
    client: Any, settings: Any, ids: list[str], now: datetime, grace_days: int
) -> None:
    """Stamp the archive tier onto the payload. `purge_eligible_at` is written HERE,
    at archive time, so the recovery window travels with the record."""
    client.set_payload(
        collection_name=settings.QDRANT_COLLECTION,
        payload={
            "status": "archived",
            "archive_source": ARCHIVE_SOURCE_GC,
            "archived_at": now.isoformat(),
            "purge_eligible_at": (now + timedelta(days=grace_days)).isoformat(),
        },
        points=ids,
    )


def _purge_points(client: Any, settings: Any, ids: list[str]) -> None:
    from qdrant_client.models import PointIdsList

    client.delete(
        collection_name=settings.QDRANT_COLLECTION,
        points_selector=PointIdsList(points=ids),
    )


def _write_audit(entries: list[dict[str, Any]]) -> None:
    """Maintenance audit trail — loud on failure, never silent."""
    try:
        pipe = _get_redis_client().pipeline()
        for entry in entries:
            pipe.lpush(GC_EVICTION_LOG_KEY, json.dumps(entry))
        pipe.ltrim(GC_EVICTION_LOG_KEY, 0, 999)
        pipe.execute()
    except Exception:
        logger.exception(
            "Acted on %d memories but FAILED to write %s audit entries: %s",
            len(entries), GC_EVICTION_LOG_KEY, [e.get("id") for e in entries],
        )


def _prune_qdrant(
    settings: Any, *, return_stats: bool = False, dry_run: bool = False
) -> int | dict[str, int]:
    """Archive qualifying active memories; purge only expired GC-origin archives.

    Returns the stats dict when ``return_stats``, else the legacy purged count —
    which is 0 on an archive-only deployment, because an archive-first first pass
    deletes nothing.
    """
    client = _get_qdrant_client()
    stats = _zero_stats()
    purge_enabled = _bool_setting(settings, "GC_PURGE_ENABLED", False)

    try:
        archive_recs, purge_recs = _scan_candidates(
            client,
            settings,
            purge_enabled=purge_enabled,
            pending_access=_pending_access_counts(),
        )

        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        grace_days = _int_setting(
            settings, "GC_ARCHIVE_GRACE_DAYS", DEFAULT_ARCHIVE_GRACE_DAYS
        )
        audit: list[dict[str, Any]] = []

        if dry_run:
            # "Evaluate and audit without changing Qdrant or Neo4j" — Redis is
            # neither, so the operator still gets a record of what would happen.
            stats["would_archive_vector"] = len(archive_recs)
            stats["would_purge_vector"] = len(purge_recs)
            audit = [
                {**rec, "action": "would_archive", "occurred_at": now_iso}
                for rec in archive_recs
            ] + [
                {**rec, "action": "would_purge", "occurred_at": now_iso}
                for rec in purge_recs
            ]
        else:
            if archive_recs:
                _archive_points(
                    client, settings, [rec["id"] for rec in archive_recs], now, grace_days
                )
                stats["archived_vector"] = len(archive_recs)
                audit += [
                    {
                        **rec,
                        "action": "archived",
                        "occurred_at": now_iso,
                        "archived_at": now_iso,
                    }
                    for rec in archive_recs
                ]
            if purge_recs:
                _purge_points(client, settings, [rec["id"] for rec in purge_recs])
                stats["pruned_vector"] = len(purge_recs)
                audit += [
                    {
                        **rec,
                        "action": "purged",
                        "occurred_at": now_iso,
                        "evicted_at": now_iso,
                    }
                    for rec in purge_recs
                ]

        if audit:
            _write_audit(audit)

    except Exception:
        logger.exception("Error during Qdrant memory maintenance")

    finally:
        client.close()

    return stats if return_stats else stats["pruned_vector"]


def preview_memories(settings: Any, limit: int = 50) -> dict[str, Any]:
    """No-write report of what the next maintenance pass would do.

    Runs the identical evaluation as the real pass — same protections, same
    scoring, same purge-eligibility rules — but writes nothing: no `set_payload`,
    no `delete`, no audit entries. The counts are the FULL candidate totals;
    ``limit`` only bounds the returned `candidates` list (`truncated` says so).
    """
    client = _get_qdrant_client()
    archive_recs: list[dict[str, Any]] = []
    purge_recs: list[dict[str, Any]] = []

    try:
        archive_recs, purge_recs = _scan_candidates(
            client,
            settings,
            purge_enabled=_bool_setting(settings, "GC_PURGE_ENABLED", False),
            pending_access=_pending_access_counts(),
        )
    except Exception:
        logger.exception("Error building memory-maintenance preview")
    finally:
        client.close()

    candidates = [
        {**rec, "action": "would_archive"} for rec in archive_recs
    ] + [
        {**rec, "action": "would_purge"} for rec in purge_recs
    ]
    bound = max(0, int(limit))

    return {
        "status": "preview",
        "would_archive_vector": len(archive_recs),
        "would_purge_vector": len(purge_recs),
        "candidates": candidates[:bound],
        "truncated": len(candidates) > bound,
    }


def _prune_neo4j_orphans() -> int:
    """Delete orphaned Neo4j nodes (nodes with no relationships).

    Destructive with no archive tier — the caller gates this on GC_PURGE_ENABLED.
    """
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
