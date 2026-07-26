"""Safe draft-skill reconciliation (SP3): on re-classification, sweep this
source's DRAFT skills whose procedure vanished from the fresh title set.
Active (human-approved) skills are NEVER deleted — auto-flagging them on a
vanished procedure is deferred (needs classification-stability gating)."""
from __future__ import annotations

import logging
from app.config import get_settings
from qdrant_client.models import FieldCondition, Filter, MatchValue

logger = logging.getLogger(__name__)


async def reconcile_source_skills(source_name: str, new_titles: set[str], vector) -> dict:
    settings = get_settings()
    deleted = 0
    points, _ = await vector._client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="source_doc", match=MatchValue(value=source_name)),
        ]),
        limit=1000, with_payload=True, with_vectors=False,
    )
    for p in points:
        payload = p.payload or {}
        if payload.get("procedure_title") in new_titles:
            continue
        if payload.get("skill_status") == "draft":
            await vector._client.delete(
                collection_name=settings.QDRANT_COLLECTION, points_selector=[p.id],
            )
            deleted += 1
        # active / other statuses: left untouched (auto-flag deferred)
    logger.info("Reconciled skills for '%s': deleted %d stale draft(s)", source_name, deleted)
    return {"deleted": deleted}
