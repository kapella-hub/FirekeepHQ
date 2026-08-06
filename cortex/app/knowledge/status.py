"""Per-source ingest-status store for the async docs->skills pipeline (SP2.1).

Records the state of the deferred classify+draft work for one ingested
source so GET /knowledge/sources and the dashboard can show progress.
Decode-agnostic: Cortex's app.state.redis_client is built WITHOUT
decode_responses=True (see the F2 fix in corpus/store.list_sources), so
every read normalizes bytes -> str.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import get_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "knowledge:ingest_status:"

_VALID_STATUS = {"queued", "classifying", "classified", "failed", "corpus_only"}


def _s(v) -> str:
    return v.decode("utf-8") if isinstance(v, bytes) else v


async def set_ingest_status(
    source_name: str,
    status: str,
    *,
    disposition: str = "",
    skills_queued: int = 0,
    note: str = "",
    redis_client,
) -> None:
    """Write (overwrite) the ingest-status hash for a source, with TTL refresh."""
    if redis_client is None:
        return
    if status not in _VALID_STATUS:
        raise ValueError(f"invalid ingest status: {status!r}")
    key = f"{_KEY_PREFIX}{source_name}"
    mapping = {
        "status": status,
        "disposition": disposition or "",
        "skills_queued": str(int(skills_queued)),
        "note": note or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    await redis_client.hset(key, mapping=mapping)
    await redis_client.expire(key, get_settings().KNOWLEDGE_STATUS_TTL_SECONDS)


async def record_draft_outcome(
    source_name: str, *, ok: bool, redis_client, note: str = ""
) -> None:
    """Count one finished ``draft_skill_from_doc`` against its source.

    WHY. ``classify_and_draft_from_doc`` writes ``classified`` with
    ``skills_queued=N`` and then fans out N independent Celery tasks whose
    outcomes were never recorded anywhere. So ``GET /knowledge/sources``
    reported ``status=classified, skills_queued=1`` for a source that had
    produced zero drafts and could never produce one — measured on the live
    deployment for "Runbook: Restart stuck Celery worker" (queued 1, drafted 0,
    unchanged since 2026-07-12). "Enqueued" was being rendered as "succeeded".

    HINCRBY rather than a rewrite because the N tasks run independently and a
    read-modify-write would lose counts. The stored ``status`` is deliberately
    NOT changed: classification really did succeed, and overwriting it would
    lose that. The honest verdict is DERIVED at read time — see
    ``app/knowledge/api.py::_effective_status``.
    """
    if redis_client is None:
        return
    key = f"{_KEY_PREFIX}{source_name}"
    field = "skills_drafted" if ok else "skills_failed"
    try:
        await redis_client.hincrby(key, field, 1)
        mapping = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if note:
            mapping["last_draft_error" if not ok else "last_draft_note"] = note[:300]
        await redis_client.hset(key, mapping=mapping)
        await redis_client.expire(key, get_settings().KNOWLEDGE_STATUS_TTL_SECONDS)
    except Exception:
        # Bookkeeping must never fail a draft that otherwise succeeded.
        logger.warning("Failed to record draft outcome for %s", source_name, exc_info=True)


async def get_ingest_status(source_name: str, redis_client) -> dict | None:
    """Read the ingest-status hash for a source, or None if absent. Bytes-safe."""
    if redis_client is None:
        return None
    raw = await redis_client.hgetall(f"{_KEY_PREFIX}{source_name}")
    if not raw:
        return None
    data = {_s(k): _s(v) for k, v in raw.items()}
    for counter in ("skills_queued", "skills_drafted", "skills_failed"):
        try:
            data[counter] = int(data.get(counter, 0) or 0)
        except (ValueError, TypeError):
            data[counter] = 0
    data.setdefault("status", "unknown")
    data.setdefault("disposition", "")
    data.setdefault("note", "")
    data.setdefault("updated_at", "")
    return data
