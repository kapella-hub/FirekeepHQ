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


async def get_ingest_status(source_name: str, redis_client) -> dict | None:
    """Read the ingest-status hash for a source, or None if absent. Bytes-safe."""
    if redis_client is None:
        return None
    raw = await redis_client.hgetall(f"{_KEY_PREFIX}{source_name}")
    if not raw:
        return None
    data = {_s(k): _s(v) for k, v in raw.items()}
    try:
        data["skills_queued"] = int(data.get("skills_queued", 0) or 0)
    except (ValueError, TypeError):
        data["skills_queued"] = 0
    data.setdefault("status", "unknown")
    data.setdefault("disposition", "")
    data.setdefault("note", "")
    data.setdefault("updated_at", "")
    return data
