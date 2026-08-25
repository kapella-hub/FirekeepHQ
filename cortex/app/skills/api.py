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

ACCESS_COUNTS_KEY = "memory:access_counts"
LAST_RECALLED_KEY = "memory:last_recalled"

# PATCHing any of these changes what the skill MEANS, so the stored vector stops
# describing the stored text and the point silently drops out of semantic
# matching. Everything else (skill_status, stale, needs_rereview) is lifecycle
# bookkeeping the embedding never encoded, and stays a cheap payload-only write.
SEMANTIC_PATCH_FIELDS = ("content", "trigger", "symptoms")


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
        request: Request,
        status: str = "active",
        project: str | None = None,
        domain: str | None = None,
        q: str | None = None,
        stale: bool | None = None,
        limit: int = 50,
        record_recall: bool = False,
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

        # Usage is recorded only for an EXPLICIT recall (`record_recall=true`, sent
        # by the MCP skill_recall tool) and only for the FINAL response — dashboard
        # browsing, `skill_list` and automatic briefing impressions must not look
        # like a human reaching for the skill, or the staleness sweep measures
        # traffic instead of usefulness.
        if record_recall and results:
            await _record_skill_usage(request, [r.id for r in results])
            try:
                from app.main import _replay_emit

                sid = request.headers.get("X-Session-Id", "unknown")
                aid = request.headers.get("X-Agent-Id", "unknown")
                await _replay_emit(
                    "memory_read", session_id=sid, agent_id=aid,
                    payload={
                        "memory_ids": [r.id for r in results][:50],
                        "result_count": len(results),
                        "trigger": "skill_recall",
                    },
                )
            except Exception as exc:  # noqa: BLE001 — telemetry never fails the recall
                logger.warning("skill_recall replay receipt failed: %s", exc)
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
        # Living Procedures: written only when the author supplied specs, so an
        # ordinary skill carries no key at all and a reader can distinguish "not
        # a procedure" from "a procedure with zero steps".
        if req.step_specs:
            payload["step_specs"] = [s.model_dump() for s in req.step_specs]
        # Tenancy, from the verified principal — NOT optional. VectorClient.search
        # filters workspace_id as a hard `must`, so a skill created without it
        # was stored, listed in the dashboard, and matched by NOTHING: measured
        # live, a freshly created skill ranked 1st at 0.877 with the filter off
        # and disappeared entirely under the caller's real workspace. Only a
        # cortex-api restart healed it, via the migration backfill.
        from auth.principal import request_principal

        principal = request_principal(request)
        payload["workspace_id"] = principal["workspace_id"]
        payload["member_id"] = principal["member_id"]
        await vector._client.upsert(
            collection_name=settings.QDRANT_COLLECTION,
            points=[PointStruct(id=skill_id, vector=embedding, payload=payload)],
        )
        # Keep the pre-edit matcher index fresh. Best-effort: a rebuild failure
        # must not fail the write, and the nightly pass rebuilds unconditionally.
        if req.step_specs is not None:
            try:
                from app.procedures import store as _proc_store

                _r = getattr(request.app.state, "redis_client", None)
                if _r is not None:
                    await _proc_store.rebuild_index(vector, _r, settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("procedure index rebuild skipped: %s", exc)
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
            step_specs=payload.get("step_specs"),
        )

    @router.patch("/skills/{skill_id}", response_model=SkillResponse)
    async def patch_skill(
        skill_id: str,
        req: SkillPatchRequest,
        request: Request,
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
        if req.step_specs is not None:
            # NOT in SEMANTIC_PATCH_FIELDS: specs describe how to OBSERVE the
            # steps, not what the skill means, so they must not trigger a
            # re-embed — that would put an embedding-backend outage in the path
            # of every spec edit, and the re-embed path fails loud by design.
            #
            # Ids are carried forward by text rather than re-minted: a step id is
            # the key its recorded executions are filed under, and no agent-facing
            # surface returns ids, so a wholesale replace silently orphaned a
            # procedure's entire history on every wording fix.
            from app.procedures.models import merge_step_specs

            updates["step_specs"] = merge_step_specs(
                req.step_specs, (points[0].payload or {}).get("step_specs"),
            )
        if any(field in updates for field in SEMANTIC_PATCH_FIELDS):
            # The text changed, so the stored vector no longer describes it. Merge
            # onto the CURRENT payload rather than writing `updates` alone — an
            # upsert replaces the whole point, so anything not carried forward
            # (provenance, staleness stamps, fields added by a later migration)
            # would be silently dropped.
            merged = dict(points[0].payload or {})
            merged.update(updates)
            try:
                embedding = await vector._embed(_skill_embed_text(merged))
            except Exception as exc:
                # Deliberately fail-loud and write NOTHING. A payload-only write
                # here would leave the point readable but semantically stale — the
                # worst outcome, because it looks successful and is undetectable
                # afterwards. The caller can retry once embeddings are back.
                logger.warning(
                    "Skill %s re-embedding failed; no changes written: %s", skill_id, exc
                )
                raise HTTPException(
                    status_code=500,
                    detail="Skill re-embedding failed; no changes were written",
                ) from exc
            await vector._client.upsert(
                collection_name=settings.QDRANT_COLLECTION,
                points=[PointStruct(id=skill_id, vector=embedding, payload=merged)],
            )
        elif updates:
            await vector._client.set_payload(
                collection_name=settings.QDRANT_COLLECTION,
                payload=updates,
                points=[skill_id],
            )
        # Keep the pre-edit matcher index fresh. `is not None` rather than truthy:
        # a PATCH that CLEARS the spec list must evict those steps from the index,
        # or the pre-edit path keeps matching steps the author just deleted.
        if req.step_specs is not None:
            try:
                from app.procedures import store as _proc_store

                _r = getattr(request.app.state, "redis_client", None)
                if _r is not None:
                    await _proc_store.rebuild_index(vector, _r, settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("procedure index rebuild skipped: %s", exc)
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


def _skill_embed_text(payload: dict[str, Any]) -> str:
    """The text a skill point's vector must describe.

    A composite, not just `content`: a PATCH may change only `trigger` or only
    `symptoms`, and embedding `content` alone would then produce a vector that
    ignores the edit entirely. Mirrors the field order the create path bakes into
    its `full_content`, so a re-embedded skill stays comparable with skills that
    were never patched.
    """
    return (
        f"trigger: {payload.get('trigger', '')}\n"
        f"symptoms: {payload.get('symptoms', '')}\n"
        f"domain: {payload.get('domain', '')}\n"
        "---\n"
        f"{payload.get('content', '')}"
    )


async def _record_skill_usage(request: Request, skill_ids: list[str]) -> None:
    """Stamp access count + last-recall time for explicitly recalled skills.

    Best-effort by design and mirrors the `/memory/recall` accumulator in
    `app/main.py`: a Redis hash the memory agent later flushes to Qdrant, so the
    read path never does a write-on-read into the vector store. Feeds
    `skill_staleness_pass`, which would otherwise keep flagging genuinely-used
    skills as stale.

    Reads the client off `app.state` instead of taking `Depends(get_redis)`
    because the skills router is mounted in test apps (and any host app) that
    never set one — a hard dependency would turn "no usage stamp" into "endpoint
    500s". No Redis, or a failing Redis, simply means no stamp.
    """
    if not skill_ids:
        return
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        return
    try:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        pipe = redis_client.pipeline()
        for skill_id in skill_ids:
            pipe.hincrby(ACCESS_COUNTS_KEY, skill_id, 1)
            pipe.hset(LAST_RECALLED_KEY, skill_id, now_iso)
        await pipe.execute()
    except Exception as exc:  # noqa: BLE001 — never fail a recall over bookkeeping
        logger.warning("Failed to record skill recall usage: %s", exc)


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
        step_specs=p.get("step_specs"),
    )


async def _dispatch_synthesis(session_id: str, skill_worthy: bool, settings: Any) -> None:
    """Background task: dispatch Celery skill synthesis task."""
    try:
        from app.workers.skill_synthesis import synthesize_skill_for_session
        synthesize_skill_for_session.delay(session_id, skill_worthy)
    except Exception:
        logger.exception("Failed to dispatch skill synthesis task for session %s", session_id)
