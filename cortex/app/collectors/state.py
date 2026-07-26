"""Redis-backed collector state: per-source version map + run record.
Decode-agnostic (app.state.redis_client lacks decode_responses; F2 lesson)."""
from __future__ import annotations

from datetime import datetime, timezone


def _s(v):
    return v.decode("utf-8") if isinstance(v, bytes) else v


class CollectorState:
    @staticmethod
    def _vkey(name: str) -> str:
        return f"collector:versions:{name}"

    @staticmethod
    def _rkey(name: str) -> str:
        return f"collector:run:{name}"

    @classmethod
    async def seen_version(cls, name: str, page_id: str, redis) -> int:
        raw = await redis.hget(cls._vkey(name), page_id)
        if raw is None:
            return 0
        try:
            return int(_s(raw))
        except (ValueError, TypeError):
            return 0

    @classmethod
    async def record_version(cls, name: str, page_id: str, version: int, redis) -> None:
        await redis.hset(cls._vkey(name), page_id, str(int(version)))

    @classmethod
    async def record_run(cls, name: str, *, seen: int, ingested: int, skipped: int,
                         errors: int, health: str, redis) -> None:
        await redis.hset(cls._rkey(name), mapping={
            "last_run": datetime.now(timezone.utc).isoformat(),
            "pages_seen": str(int(seen)), "pages_ingested": str(int(ingested)),
            "pages_skipped": str(int(skipped)), "errors": str(int(errors)),
            "health": health,
        })

    @classmethod
    async def get_run(cls, name: str, redis) -> dict | None:
        raw = await redis.hgetall(cls._rkey(name))
        if not raw:
            return None
        d = {_s(k): _s(v) for k, v in raw.items()}
        for k in ("pages_seen", "pages_ingested", "pages_skipped", "errors"):
            try:
                d[k] = int(d.get(k, 0) or 0)
            except (ValueError, TypeError):
                d[k] = 0
        return d
