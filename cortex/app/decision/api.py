"""POST /decision/synthesize — global-knowledge board homework (SP4)."""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from app.config import get_settings
from app.decision.synthesize import synthesize_board

logger = logging.getLogger(__name__)


class DecisionRequest(BaseModel):
    context: str = Field(..., min_length=1)
    draft_questions: list[str] = []
    agent_id: str = "unknown"


def create_decision_router() -> APIRouter:
    router = APIRouter(prefix="/decision", tags=["decision"])
    from app.main import get_rag_engine

    @router.post("/synthesize")
    async def synthesize(req: DecisionRequest, rag=Depends(get_rag_engine)):
        try:
            board = await synthesize_board(req.context, req.draft_questions,
                                           rag_engine=rag, settings=get_settings())
            board["board_id"] = uuid.uuid4().hex
        except Exception as exc:
            logger.warning("decision synthesize failed, returning minimal degraded board: %s", exc)
            board = {
                "questions": [
                    {"id": f"q{i}", "text": qt, "knowledge_found": False, "evidence": [],
                     "suggested_answers": [], "suggested_actions": []}
                    for i, qt in enumerate(req.draft_questions)
                ],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "degraded": True,
                "note": "synthesize-failed",
                "board_id": uuid.uuid4().hex,
            }
        return board

    return router
