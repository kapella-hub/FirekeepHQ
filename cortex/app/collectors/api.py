"""GET /collectors — per-collector status/health for the dashboard (SP3)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from app.collectors.state import CollectorState
from app.config import get_settings

_KNOWN = [("confluence", "CONFLUENCE_COLLECTOR_ENABLED")]


def create_collectors_router() -> APIRouter:
    router = APIRouter(prefix="/collectors", tags=["collectors"])
    from app.main import get_redis

    @router.get("")
    async def list_collectors(redis_client=Depends(get_redis)):
        settings = get_settings()
        out = []
        for name, flag in _KNOWN:
            rec = await CollectorState.get_run(name, redis_client)
            enabled = bool(settings.COLLECTORS_ENABLED and getattr(settings, flag, False))
            out.append({
                "name": name, "enabled": enabled,
                "last_run": (rec or {}).get("last_run"),
                "pages_seen": (rec or {}).get("pages_seen", 0),
                "pages_ingested": (rec or {}).get("pages_ingested", 0),
                "pages_skipped": (rec or {}).get("pages_skipped", 0),
                "errors": (rec or {}).get("errors", 0),
                "health": (rec or {}).get("health", "unknown"),
            })
        return {"collectors": out, "count": len(out)}

    return router
