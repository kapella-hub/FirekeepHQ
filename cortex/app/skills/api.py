"""Skill CRUD REST API — mounted on Cortex :8100."""
from __future__ import annotations

import logging
import uuid
import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from qdrant_client.models import (
    FieldCondition, MatchAny, MatchValue, PointIdsList, PointStruct
)

from app.config import get_settings, Settings
from app.db.vector import VectorClient
from app.db.visibility import workspace_condition
from app.migration_gate import require_not_frozen
from app.skills.search import search_skill_points
from app.models import (
    SkillRequest, SkillResponse, SkillPatchRequest, SkillEvaluateRequest
)
from auth import keys as _auth_keys
from auth.middleware import require_any_scope
from auth.principal import deployment_workspace_id

logger = logging.getLogger(__name__)

ACCESS_COUNTS_KEY = "memory:access_counts"
LAST_RECALLED_KEY = "memory:last_recalled"

# PATCHing any of these changes what the skill MEANS, so the stored vector stops
# describing the stored text and the point silently drops out of semantic
# matching. Everything else (skill_status, stale, needs_rereview) is lifecycle
# bookkeeping the embedding never encoded, and stays a cheap payload-only write.
SEMANTIC_PATCH_FIELDS = ("content", "trigger", "symptoms")

# Every write route on this router is authenticated by the global key
# middleware, and until 2026-10-01 that was ALL it was: any valid key -- the
# narrowest service key included -- could DELETE any Qdrant point by id (a
# member-private memory as easily as a skill, since nothing checked
# memory_type), and could PATCH its own poisoned draft to `active`, which IS
# the human approval act. `admin` is kept alongside `memory:write` for the
# reason given in docs/guides/replay-evals-patterns.md: `scopes_allow` treats
# only "*" as a superset, so a literal ["admin"] key would otherwise lose a
# route it could always reach.
_skill_write = require_any_scope("memory:write", "admin")
_skill_read = require_any_scope("memory:read", "admin")
# POST /skill/evaluate queues a session for scoring + synthesis. It rides the
# same ctx_complete_session path as POST /evals/sessions/{id}/compute (bridge
# sends the caller's own key to both), so it takes the same gate.
_skill_evaluate = require_any_scope("eval:write", "admin")

# PATCH fields that record a REVIEW decision -- what the dashboard's review
# queue sends, and nothing else does. An agent key may author and refine a
# draft; deciding that a skill is fit to be shown to every agent (or retiring
# one, or clearing a flag the ladder/staleness sweep raised for a human) is the
# human act, so it needs review authority, not memory:write.
_REVIEW_PATCH_FIELDS = ("skill_status", "needs_rereview", "stale")


def _has_review_authority(identity: dict[str, Any]) -> bool:
    """Admin (or "*") when auth is enforced; anyone when it is not.

    The auth-disabled branch is deliberate and narrow: with AUTH_ENABLED=false
    no key middleware is installed, every caller -- the dashboard and every
    agent alike -- IS the anonymous deployment-owner principal, and there is no
    identity left to tell a human from an agent. Refusing here would only break
    the dashboard review queue on those boxes while protecting nothing, which
    is why this is NOT require_scope("admin") (whose auth-off branch refuses
    admin to everyone, correctly, for secrets). Recorded as a residual in
    docs/THREAT-MODEL.md section 5.10.
    """
    if not _auth_keys._AUTH_ENABLED:
        return True
    return _auth_keys.scopes_allow(identity.get("scopes", []), "admin")


def _require_review_authority(identity: dict[str, Any], what: str) -> None:
    if not _has_review_authority(identity):
        raise HTTPException(
            status_code=403,
            detail=(
                f"{what} is a skill review decision and requires 'admin' "
                f"(the dashboard review queue); key has {identity.get('scopes', [])}"
            ),
        )


async def _verify_caller_can_read_session(
    settings: Any, request: Request, identity: dict[str, Any], session_id: str,
) -> None:
    """Refuse (404) a caller who cannot read ``session_id`` in Bridge.

    Before 2026-10-05 POST /skill/evaluate queued synthesis for ANY session id:
    the worker then read that session with the internal key and wrote a draft
    built from it into the review queue every memory:read holder lists, so a
    member could publish a teammate's session as a skill draft
    (docs/THREAT-MODEL.md §5.16).

    The caller's OWN key is presented to Bridge's GET /sessions/{id}, so the
    answer is Bridge's ``session_owned_by`` verbatim — the owner passes; a
    workspace-wide reader (FIREKEEP_INTERNAL_KEY, the "*" dashboard/owner keys
    via session:read:workspace) passes within its workspace; a legacy session
    passes only for the deployment owner; anyone else gets Bridge's 404, which
    is reported as 404 so a guessed id discloses nothing. Bridge's own call
    (ctx_complete_session forwards the completing caller's key, #47) is the
    owner's. An ``admin`` key passes without the round trip: it administers
    the review queue this route feeds.

    Fail closed: Bridge unreachable or erroring is 503, never a pass. With auth
    disabled every caller is the deployment owner and nothing is checked.
    """
    if not _auth_keys._AUTH_ENABLED:
        return
    if _auth_keys.scopes_allow(identity.get("scopes", []), "admin"):
        return
    caller_key = (request.headers.get("X-API-Key") or "").strip()
    if not identity.get("authenticated") or not caller_key:
        raise HTTPException(status_code=401, detail="Verified caller credential is unavailable")

    from app.session_owner import bridge_client, bridge_session_url

    try:
        async with bridge_client() as client:
            response = await client.get(
                bridge_session_url(settings.BRIDGE_URL, session_id),
                headers={"X-API-Key": caller_key},
            )
    except Exception as exc:  # noqa: BLE001 — unreachable must not mean "allowed"
        logger.warning("POST /skill/evaluate: Bridge session check failed for %s: %s",
                       session_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Session authorization is temporarily unavailable; nothing was queued",
        ) from exc
    if response.status_code == 200:
        return
    if response.status_code in (401, 403, 404):
        raise HTTPException(status_code=404, detail="Session not found")
    logger.warning("POST /skill/evaluate: Bridge answered HTTP %d for %s",
                   response.status_code, session_id)
    raise HTTPException(
        status_code=503,
        detail="Session authorization is temporarily unavailable; nothing was queued",
    )


async def _load_owned_skill(
    vector: VectorClient, settings: Any, skill_id: str, identity: dict[str, Any],
) -> Any:
    """The skill point `skill_id`, or 404 -- never another kind of point.

    A point that is not `memory_type == "skill"` (a memory, a corpus chunk, a
    dream, another member's private document) or that lives in another
    workspace is reported exactly like a missing one, so an id guessed or
    lifted from a recall result discloses nothing. A point with NO recorded
    workspace belongs to the deployment's own -- the rule
    workspace_migration.backfill_memories and procedures/api.py already apply.

    A lookup FAILURE refuses (503): without the point there is no way to verify
    what the id names, and the destructive routes must not act on a guess.
    """
    try:
        points = await vector._client.retrieve(
            collection_name=settings.QDRANT_COLLECTION,
            ids=[skill_id],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill %s lookup failed: %s", skill_id, exc)
        raise HTTPException(
            status_code=503,
            detail="Skill store unavailable; nothing was changed",
        ) from exc
    if not points:
        raise HTTPException(status_code=404, detail="Skill not found")
    payload = points[0].payload or {}
    if payload.get("memory_type") != "skill":
        raise HTTPException(status_code=404, detail="Skill not found")
    owner_ws = payload.get("workspace_id") or deployment_workspace_id()
    if owner_ws != identity.get("workspace_id"):
        raise HTTPException(status_code=404, detail="Skill not found")
    return points[0]


def create_skills_router(
    get_settings_fn: Callable[[], Settings] | None = None,
) -> APIRouter:
    settings_fn = get_settings_fn or get_settings

    router = APIRouter(prefix="", tags=["skills"])

    from app.main import get_vector  # imported here to avoid circular at module load

    @router.post("/skill/evaluate", status_code=202)
    async def evaluate_session(
        req: SkillEvaluateRequest,
        request: Request,
        background: BackgroundTasks,
        identity: dict = Depends(_skill_evaluate),
        vector: VectorClient = Depends(get_vector),
    ):
        """Score a session; trigger Celery synthesis task if above threshold."""
        settings = settings_fn()
        if not settings.SKILL_SYNTHESIS_ENABLED:
            return {"status": "disabled"}
        # The synthesis worker reads the session with FIREKEEP_INTERNAL_KEY
        # (session:read:workspace) after this returns, so the CALLER's right
        # to read it is proven here, synchronously, or a guessed teammate
        # session id turns the worker into a confused deputy.
        await _verify_caller_can_read_session(settings, request, identity, req.session_id)
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
        identity: dict = Depends(_skill_read),
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        # status is never allowed to fall through unfiltered — an explicit
        # `?status=` (empty string) must not become a "return all statuses"
        # escape hatch, since that would leak drafts just like the old
        # no-arg default did. Treat falsy as the safe default.
        status = status or "active"
        # `recallable` is the one alias: what an agent may be shown — active plus
        # trial (spec 2026-09-03 decision 1). Plain `active` stays active-only so
        # dashboards and the staleness sweep keep their exact meaning.
        if status == "recallable":
            status_cond = FieldCondition(key="skill_status", match=MatchAny(any=["active", "trial"]))
        else:
            status_cond = FieldCondition(key="skill_status", match=MatchValue(value=status))
        must = [
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            status_cond,
            # Tenancy: the caller's workspace only (legacy = the deployment's),
            # the rule _load_owned_skill applies to a single id.
            workspace_condition(identity.get("workspace_id") or ""),
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

        if status == "recallable":
            # Actives first, trials last; stable within a tier so the semantic
            # ranking survives inside each group.
            results.sort(key=lambda r: 0 if r.skill_status == "active" else 1)

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
                from auth.principal import request_principal

                sid = request.headers.get("X-Session-Id", "unknown")
                aid = request.headers.get("X-Agent-Id", "unknown")
                principal = request_principal(request)
                await _replay_emit(
                    "memory_read", session_id=sid, agent_id=aid,
                    workspace_id=principal.get("workspace_id"),
                    member_id=principal.get("member_id"),
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
        identity: dict = Depends(_skill_read),
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        # Same guard as the write routes: this returned ANY point's content by
        # id, so a member-private memory id read back through here verbatim.
        point = await _load_owned_skill(vector, settings, skill_id, identity)
        return _point_to_response(point)

    @router.post("/skills", response_model=SkillResponse, status_code=201,
                dependencies=[Depends(_skill_write), Depends(require_not_frozen)])
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
        if req.reauthor_of:
            # The original must exist and belong to the caller's workspace — a
            # Night Shift worker enrolled elsewhere fails here, visibly, instead of
            # drafting across the tenancy boundary (spec decision 6). Same
            # resolver as GET/PATCH/DELETE /skills/{id}: a point that is not a
            # skill, or an unattributed one outside the deployment workspace,
            # is 404 -- the bare retrieve here accepted both.
            try:
                await _load_owned_skill(vector, settings, req.reauthor_of, principal)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                # Keep the documented wire text (fleet-as-gpu plan, Task 8).
                raise HTTPException(
                    status_code=404, detail="reauthor_of skill not found") from exc
            payload["reauthor_of"] = req.reauthor_of
        if req.origin_job:
            payload["origin_job"] = req.origin_job
        await vector._client.upsert(
            collection_name=settings.QDRANT_COLLECTION,
            points=[PointStruct(id=skill_id, vector=embedding, payload=payload)],
        )
        if req.origin_job and req.status == "draft":
            from app.fleet import ledger as _ledger
            await _ledger.record(getattr(request.app.state, "redis_client", None),
                                 req.origin_job, "produced")
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
            origin_job=payload.get("origin_job"),
            reauthor_of=payload.get("reauthor_of"),
        )

    @router.patch("/skills/{skill_id}", response_model=SkillResponse,
                 dependencies=[Depends(require_not_frozen)])
    async def patch_skill(
        skill_id: str,
        req: SkillPatchRequest,
        request: Request,
        identity: dict = Depends(_skill_write),
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        points = [await _load_owned_skill(vector, settings, skill_id, identity)]
        current = points[0].payload or {}
        # Review decisions need review authority. Checked BEFORE any write and
        # against the request as a whole, so a refused PATCH changes nothing.
        review_fields = [f for f in _REVIEW_PATCH_FIELDS if getattr(req, f) is not None]
        if req.clear_duplicate_of:
            review_fields.append("clear_duplicate_of")
        if review_fields:
            _require_review_authority(identity, "Setting " + ", ".join(review_fields))
        # Rewriting what an APPROVED (or trial/deprecated) skill says is
        # approval by the back door: the text agents are shown changes and no
        # human saw it. A draft is still its author's to refine. step_specs are
        # deliberately NOT here -- skill_add_step_specs compiles them onto
        # existing skills over MCP with the caller's key, and arming a
        # procedure is already admin-only (PUT /procedures/{id}/mode).
        semantic = [f for f in SEMANTIC_PATCH_FIELDS if getattr(req, f) is not None]
        if semantic and current.get("skill_status", "draft") != "draft":
            _require_review_authority(
                identity,
                f"Editing {', '.join(semantic)} of a {current.get('skill_status')} skill",
            )
        updates: dict[str, Any] = {}
        if req.skill_status is not None:
            updates["skill_status"] = req.skill_status
            if req.skill_status != current.get("skill_status"):
                # Every status change opens a fresh evidence window for the ladder
                # (spec decision 4): promotions never ride evidence from a previous life.
                updates["ladder_since"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if req.skill_status == "active":
                    # Server-decided, never client-asserted: reaching this route
                    # at all IS the human act. PR2's ladder writes "ladder"
                    # in-process via set_payload and never comes through here.
                    updates["approved_by"] = "human"
            # Promoting to active is a human blessing — stamp freshness so the
            # staleness sweep gives the newly-active skill a full window. Without
            # this, a draft that aged past SKILL_STALE_AFTER_DAYS in the review
            # queue would be flagged STALE on the very next sweep after approval
            # (its only timestamp is the old synthesis time).
            if req.skill_status == "active":
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                updates["stale_reviewed_at"] = now_iso
                if current.get("skill_status") != "active":
                    # The REAL approval timestamp (Fleet-as-GPU spec decision 7):
                    # stamped once, on the draft->active transition only.
                    updates["approved_at"] = now_iso
                    if current.get("origin_job") and not current.get("approved_at"):
                        from app.fleet import ledger as _ledger
                        await _ledger.record(getattr(request.app.state, "redis_client", None),
                                             current["origin_job"], "approved")
        if req.content is not None:
            updates["content"] = req.content
        if req.trigger is not None:
            updates["trigger"] = req.trigger
        if req.symptoms is not None:
            updates["symptoms"] = req.symptoms
        if req.needs_rereview is not None:
            updates["needs_rereview"] = req.needs_rereview
        if req.clear_duplicate_of:
            # Un-parks a draft the ladder stamped as a probable duplicate.
            # `PARKED_FIELDS` is present-AND-truthy, so writing None lifts the
            # block while leaving the key on the payload as a record that the
            # pass once matched it. A PATCH carrying only this is still a real
            # change: `updates` is non-empty, so the `elif updates` set_payload
            # branch below fires.
            updates["duplicate_of"] = None
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

    @router.delete("/skills/{skill_id}", status_code=204,
                  dependencies=[Depends(require_not_frozen)])
    async def delete_skill(
        skill_id: str,
        request: Request,
        identity: dict = Depends(_skill_write),
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        # Deleting is a review decision (the dashboard's Reject / Delete); no
        # agent-facing tool deletes a skill. This route used to delete whatever
        # point id it was handed -- skill or not, any workspace -- and went
        # ahead even when the lookup failed. Now the point is verified first,
        # and an unverifiable one is refused (503), never deleted blind.
        _require_review_authority(identity, "Deleting a skill")
        point = await _load_owned_skill(vector, settings, skill_id, identity)
        # Deleting a fleet DRAFT is the human saying "no" — the only rejection
        # signal that exists, and it vanishes with the point, so record it first.
        current = point.payload or {}
        if current.get("origin_job") and current.get("skill_status") == "draft":
            from app.fleet import ledger as _ledger
            _r = getattr(request.app.state, "redis_client", None)
            await _ledger.record(_r, current["origin_job"], "rejected")
            if current["origin_job"] == _ledger.JOB_REAUTHOR and current.get("reauthor_of"):
                await _ledger.mark_rejected_reauthor(_r, current["reauthor_of"])
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
        skill_efficacy=p.get("skill_efficacy"),
        skill_efficacy_n=p.get("skill_efficacy_n"),
        skill_efficacy_updated_at=p.get("skill_efficacy_updated_at"),
        step_specs=p.get("step_specs"),
        origin_job=p.get("origin_job"),
        reauthor_of=p.get("reauthor_of"),
        approved_at=p.get("approved_at"),
        ladder_since=p.get("ladder_since"),
        approved_by=p.get("approved_by"),
        ladder_shadow=p.get("ladder_shadow"),
        ladder_history=p.get("ladder_history"),
        demoted_at=p.get("demoted_at"),
        demotion_reason=p.get("demotion_reason"),
        ladder_rewrite_requested_at=p.get("ladder_rewrite_requested_at"),
        trial_expired_at=p.get("trial_expired_at"),
        duplicate_of=p.get("duplicate_of"),
        superseded_by=p.get("superseded_by"),
    )


async def _dispatch_synthesis(session_id: str, skill_worthy: bool, settings: Any) -> None:
    """Background task: dispatch Celery skill synthesis task."""
    try:
        from app.workers.skill_synthesis import synthesize_skill_for_session
        synthesize_skill_for_session.delay(session_id, skill_worthy)
    except Exception:
        logger.exception("Failed to dispatch skill synthesis task for session %s", session_id)
