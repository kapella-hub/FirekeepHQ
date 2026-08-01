"""Skill CRUD REST API — mounted on Cortex :8100."""
from __future__ import annotations

import logging
import uuid
import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from qdrant_client.models import (
    FieldCondition, MatchValue, PointIdsList, PointStruct
)

from app.config import get_settings, Settings
from app.db.vector import VectorClient
from app.skills.search import search_skill_points
from app.models import (
    SkillRequest, SkillResponse, SkillPatchRequest, SkillEvaluateRequest
)

logger = logging.getLogger(__name__)


def create_skills_router(
    get_settings_fn: Callable[[], Settings] | None = None,
) -> APIRouter:
    settings_fn = get_settings_fn or get_settings

    router = APIRouter(prefix="", tags=["skills"])

    from app.main import get_vector  # imported here to avoid circular at module load

    @router.post("/skill/evaluate", status_code=202)
    async def evaluate_session(
        req: SkillEvaluateRequest,
        background: BackgroundTasks,
        vector: VectorClient = Depends(get_vector),
    ):
        """Score a session; trigger Celery synthesis task if above threshold."""
        settings = settings_fn()
        if not settings.SKILL_SYNTHESIS_ENABLED:
            return {"status": "disabled"}
        background.add_task(_dispatch_synthesis, req.session_id, req.skill_worthy, settings)
        return {"status": "queued", "session_id": req.session_id}

    @router.get("/skills", response_model=list[SkillResponse])
    async def list_skills(
        status: str = "active",
        project: str | None = None,
        domain: str | None = None,
        q: str | None = None,
        stale: bool | None = None,
        limit: int = 50,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        # status is never allowed to fall through unfiltered — an explicit
        # `?status=` (empty string) must not become a "return all statuses"
        # escape hatch, since that would leak drafts just like the old
        # no-arg default did. Treat falsy as the safe default.
        status = status or "active"
        must = [
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value=status)),
        ]
        if project:
            must.append(FieldCondition(key="project", match=MatchValue(value=project.lower())))
        if domain:
            must.append(FieldCondition(key="domain", match=MatchValue(value=domain)))
        # Stale review queue: ?stale=true filters to flagged skills. Points that
        # predate the first sweep lack the field and simply don't match true
        # (same semantics as the skill_status precedent) — accept one sweep-cycle
        # latency rather than a client-side missing-field heuristic.
        if stale is not None:
            must.append(FieldCondition(key="stale", match=MatchValue(value=stale)))

        # Two paths, one filter. `must` above is handed over VERBATIM: dropping
        # memory_type=skill would return plain memories as empty-trigger skills
        # (silently, since _point_to_response defaults trigger to ""), and rebuilding
        # the stale condition would break its three-state append-only semantics.
        points, semantic = await search_skill_points(
            vector, settings, must=must, query=q, limit=limit,
        )
        results = [_point_to_response(p) for p in points]

        # THE FIX. On the semantic path the points are already cosine-ranked and
        # floored, so the legacy substring narrowing must NOT run — re-applying it
        # would reinstate the original bug on top of a working matcher. On every
        # degraded path (no query, embed failure, nothing above the floor) `semantic`
        # is False and behaviour is byte-identical to before.
        if q and not semantic:
            ql = q.lower()
            results = [
                r for r in results
                if ql in r.trigger.lower() or ql in r.domain.lower()
            ]
        return results

    @router.get("/skills/{skill_id}", response_model=SkillResponse)
    async def get_skill(
        skill_id: str,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        points = await vector._client.retrieve(
            collection_name=settings.QDRANT_COLLECTION,
            ids=[skill_id],
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            raise HTTPException(status_code=404, detail="Skill not found")
        return _point_to_response(points[0])

    @router.post("/skills", response_model=SkillResponse, status_code=201)
    async def create_skill(
        req: SkillRequest,
        request: Request,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        full_content = (
            f"trigger: {req.trigger}\n"
            f"symptoms: {req.symptoms}\n"
            f"domain: {req.domain}\n"
            f"verified_on: {datetime.date.today().isoformat()}\n"
            "---\n"
            f"## Steps\n{req.steps}\n\n"
            f"## Gotchas\n{req.gotchas}"
        )
        embedding = await vector._embed(full_content)
        skill_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {
            "memory_type": "skill", "skill_status": req.status,
            "trigger": req.trigger, "symptoms": req.symptoms,
            "content": full_content, "domain": req.domain,
            # Provenance from the identity headers (Night Shift + shim identity
            # tap attribute skills to the ORIGINATING session, not the caller
            # process). Absent headers keep the pre-0.1.23 null behavior.
            "skill_score": 0.0,
            "source_session_id": request.headers.get("X-Session-Id") or None,
            "project": req.project,
            "agent_id": request.headers.get("X-Agent-Id") or None,
            "namespace": req.namespace, "timestamp": now,
            "source_type": "manual",
        }
        await vector._client.upsert(
            collection_name=settings.QDRANT_COLLECTION,
            points=[PointStruct(id=skill_id, vector=embedding, payload=payload)],
        )
        return SkillResponse(
            id=skill_id,
            trigger=payload["trigger"],
            symptoms=payload["symptoms"],
            content=payload["content"],
            skill_status=payload["skill_status"],
            skill_score=payload["skill_score"],
            source_session_id=payload["source_session_id"],
            domain=payload["domain"],
            project=payload["project"],
            agent_id=payload["agent_id"],
            namespace=payload["namespace"],
            created_at=now,
            source_type=payload["source_type"],
        )

    @router.patch("/skills/{skill_id}", response_model=SkillResponse)
    async def patch_skill(
        skill_id: str,
        req: SkillPatchRequest,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        points = await vector._client.retrieve(
            collection_name=settings.QDRANT_COLLECTION,
            ids=[skill_id],
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            raise HTTPException(status_code=404, detail="Skill not found")
        updates: dict[str, Any] = {}
        if req.skill_status is not None:
            updates["skill_status"] = req.skill_status
            # Promoting to active is a human blessing — stamp freshness so the
            # staleness sweep gives the newly-active skill a full window. Without
            # this, a draft that aged past SKILL_STALE_AFTER_DAYS in the review
            # queue would be flagged STALE on the very next sweep after approval
            # (its only timestamp is the old synthesis time).
            if req.skill_status == "active":
                updates["stale_reviewed_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
        if req.content is not None:
            updates["content"] = req.content
        if req.trigger is not None:
            updates["trigger"] = req.trigger
        if req.symptoms is not None:
            updates["symptoms"] = req.symptoms
        if req.needs_rereview is not None:
            updates["needs_rereview"] = req.needs_rereview
        if req.stale is not None:
            updates["stale"] = req.stale
            # A human clearing the flag ("Still valid") is an acknowledgment the
            # staleness sweep must honor as freshness, or it re-flags the skill
            # next cycle. Stamp a distinct reviewed marker (NOT last_recalled_at,
            # which would falsify recall activity) — buys one more stale window.
            if req.stale is False:
                updates["stale_reviewed_at"] = datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat()
        if updates:
            await vector._client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload=updates,
                points=[skill_id],
            )
        # Re-fetch updated point
        updated = await vector._client.retrieve(
            collection_name=settings.QDRANT_COLLECTION,
            ids=[skill_id], with_payload=True, with_vectors=False,
        )
        return _point_to_response(updated[0])

    @router.delete("/skills/{skill_id}", status_code=204)
    async def delete_skill(
        skill_id: str,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        await vector._client.delete(
            collection_name=settings.QDRANT_COLLECTION,
            points_selector=PointIdsList(points=[skill_id]),
        )

    return router


def _point_to_response(point: Any) -> SkillResponse:
    p = point.payload or {}
    return SkillResponse(
        id=str(point.id),
        trigger=p.get("trigger", ""),
        symptoms=p.get("symptoms", ""),
        content=p.get("content", ""),
        skill_status=p.get("skill_status", "draft"),
        skill_score=float(p.get("skill_score") or 0.0),
        source_session_id=p.get("source_session_id"),
        domain=p.get("domain", ""),
        project=p.get("project"),
        agent_id=p.get("agent_id"),
        namespace=p.get("namespace", "default"),
        created_at=p.get("timestamp"),
        source_type=p.get("source_type", "session"),
        content_class=p.get("content_class"),
        source_doc=p.get("source_doc"),
        procedure_title=p.get("procedure_title"),
        needs_rereview=p.get("needs_rereview", False),
        stale=p.get("stale", False),
        stale_detected_at=p.get("stale_detected_at"),
        stale_reviewed_at=p.get("stale_reviewed_at"),
        last_recalled_at=p.get("last_recalled_at"),
    )


async def _dispatch_synthesis(session_id: str, skill_worthy: bool, settings: Any) -> None:
    """Background task: dispatch Celery skill synthesis task."""
    try:
        from app.workers.skill_synthesis import synthesize_skill_for_session
        synthesize_skill_for_session.delay(session_id, skill_worthy)
    except Exception:
        logger.exception("Failed to dispatch skill synthesis task for session %s", session_id)
