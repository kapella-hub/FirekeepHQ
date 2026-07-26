"""Embedding administration router for FirekeepCortex.

Provides endpoints to inspect embedding status and trigger re-embedding
of all vectors with a new model.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.db.vector import VectorClient

logger = logging.getLogger(__name__)


def create_embedding_router(vector: VectorClient) -> APIRouter:
    """Create and return the embedding admin router."""
    router = APIRouter(prefix="/admin/embeddings", tags=["admin"])

    @router.get("/status")
    async def embedding_status() -> dict[str, Any]:
        """Return current embedding model name, vector dimensions, cache size, total vectors."""
        try:
            info = await vector.get_embedding_info()
            return info
        except Exception as exc:
            logger.error("Failed to get embedding info: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/reembed")
    async def trigger_reembed(model: str | None = None) -> dict[str, Any]:
        """Trigger re-embedding of all vectors. Returns the Celery task ID."""
        from app.workers.reembed import reembed_all

        settings = get_settings()
        effective_model = model or settings.EMBEDDING_MODEL
        batch_size = settings.REEMBED_BATCH_SIZE

        task = reembed_all.delay(new_model=effective_model, batch_size=batch_size)

        return {
            "status": "started",
            "task_id": task.id,
            "model": effective_model,
        }

    @router.get("/reembed/{task_id}")
    async def reembed_progress(task_id: str) -> dict[str, Any]:
        """Check re-embedding task progress."""
        from app.workers.reembed import celery_app

        result = celery_app.AsyncResult(task_id)

        if result.state == "PROGRESS":
            meta = result.info or {}
            return {
                "status": "PROGRESS",
                "current": meta.get("current", 0),
                "total": meta.get("total", 0),
            }
        elif result.state == "SUCCESS":
            info = result.result or {}
            return {
                "status": "SUCCESS",
                "current": info.get("reembedded", 0),
                "total": info.get("reembedded", 0),
            }
        elif result.state == "FAILURE":
            return {
                "status": "FAILURE",
                "error": str(result.info),
            }
        else:
            return {
                "status": result.state,
                "current": 0,
                "total": 0,
            }

    return router
