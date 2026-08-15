"""GET /procedures — what the dashboard reads.

Scope note: unlike the skills router (which declares no dependencies= and
contains no require_scope at all), these routes are gated. Accepting a proposal
is still a PATCH /skills/{id} and therefore still as ungated as it is today —
retrofitting a gate onto a shipped surface belongs in its own change.

TENANCY. `memory:read` is a permission, not a boundary: it is held by every
agent key in the deployment, and these two reads returned EVERY workspace's
triggers, step text, session ids, agent ids and edited file paths. Both are now
scoped to the caller's own workspace, from the verified principal, the way
`/memory/*` and the skills write path already do it. Since round 2 (enforced
runbooks) the ACTION path is scoped too: the gateway router stamps the
verified workspace onto the request server-side, and every procedures
lookup/write in decide()/record() carries it (H6 is resolved).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.procedures import enforce, store

logger = logging.getLogger(__name__)


class BundleAckBody(BaseModel):
    # EXACTLY {"version": ...} — pinned with the client (Phase B). Session
    # attribution comes from the X-Session-Id header, never the body; an
    # extra body field is ignored by pydantic and never read.
    version: str = Field(min_length=1, max_length=64)


class RunbookAckBody(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="", max_length=256)


class ModeBody(BaseModel):
    mode: str = Field(min_length=1, max_length=32)


def _deployment_workspace() -> str:
    try:
        from auth.principal import deployment_workspace_id

        return deployment_workspace_id()
    except Exception as exc:  # noqa: BLE001
        logger.debug("deployment workspace unresolved: %s", exc)
        return ""


def _visible(recorded: str, caller: str) -> bool:
    """Is a row recorded under `recorded` visible to a caller in `caller`?

    A row with NO recorded workspace belongs to the deployment's own — exactly
    the rule `workspace_migration.backfill_memories` applies to an unattributed
    Qdrant point, so the index and the store cannot disagree about who owns one.
    Index entries written before this field existed, and executions for a skill
    that has since left the index, both land here.
    """
    if recorded:
        return recorded == caller
    return bool(caller) and caller == _deployment_workspace()


def create_procedures_router(get_redis, get_vector, settings_fn) -> APIRouter:
    router = APIRouter(tags=["procedures"])

    try:
        from auth.middleware import require_scope
        read_dep = [Depends(require_scope("memory:read"))]
        admin_dep = [Depends(require_scope("admin"))]
        session_read_dep = [Depends(require_scope("session:read"))]
        session_write_dep = [Depends(require_scope("session:write"))]
    except Exception:  # noqa: BLE001 — auth optional in unit tests
        read_dep, admin_dep = [], []
        session_read_dep, session_write_dep = [], []

    def _caller_workspace(request: Request) -> str:
        from auth.principal import request_principal

        return request_principal(request).get("workspace_id") or ""

    def _caller_member(request: Request) -> str:
        from auth.principal import request_principal

        return request_principal(request).get("member_id") or ""

    def _session_of(request: Request, body_session: str) -> str:
        s = (body_session or "").strip()
        if s:
            return s
        return (request.headers.get("x-session-id") or "").strip()

    def _workspace_of(skill_id: str, coverage: dict, by_skill: dict) -> str:
        cov = coverage.get(skill_id) or {}
        if cov.get("workspace_id"):
            return str(cov["workspace_id"])
        for entry in by_skill.get(skill_id) or []:
            if entry.get("workspace_id"):
                return str(entry["workspace_id"])
        return ""

    @router.get("/procedures", dependencies=read_dep)
    async def list_procedures(request: Request):
        r = get_redis()
        caller_ws = _caller_workspace(request)
        index = await store.load_index(r)
        coverage = await store.load_coverage(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)

        # Rows come from the COVERAGE summary, not from the matcher index. The
        # index holds file_glob specs only, so deriving rows from it made a
        # procedure whose steps are all `unobservable` produce no row at all and
        # `specs_total: 0` — and the dashboard then renders the cold-start
        # message ("no procedure has step specs yet") at a human who has just
        # compiled them. H2 requires the opposite: say "0 of 7 observable". The
        # index is the fallback for a deployment that has not rebuilt since the
        # coverage key was introduced.
        summary = dict(coverage)
        for skill_id, entries in by_skill.items():
            summary.setdefault(skill_id, {
                "trigger": entries[0].get("skill_trigger", ""),
                "spec_count": len(entries),
                "observable": len(entries),
            })

        # Live, from the execution records, NOT from the nightly stats blob.
        # `GET /procedures/{id}/executions` reads the records, so the two
        # endpoints answered "has this ever run?" differently for up to a full
        # PROCEDURE_SCHEDULE_HOURS — and the rollup was the one that was wrong.
        exec_counts: dict[str, int] = {}
        for rec in await store.iter_executions(r):
            sid = rec.get("skill_id")
            if sid:
                exec_counts[sid] = exec_counts.get(sid, 0) + 1

        rows = []
        for skill_id, cov in summary.items():
            if not _visible(_workspace_of(skill_id, coverage, by_skill), caller_ws):
                continue
            stats = await store.get_step_stats(r, skill_id)
            proposals = await store.list_proposals(r, skill_id)
            rows.append({
                "skill_id": skill_id,
                "trigger": cov.get("trigger", ""),
                # Coverage is REPORTED, never hidden: a step with no matcher is
                # unobservable, and a coverage number the user cannot see is the
                # same silent cap this repo bans elsewhere.
                "spec_count": int(cov.get("spec_count", 0)),
                "observable_steps": int(cov.get("observable", 0)),
                "executions": exec_counts.get(skill_id, 0),
                "steps": stats,
                "proposals": proposals,
                # Phase C: what the dashboard's NOT-ACTIVELY-ENFORCED
                # derivation reads (computed CLIENT-side, never here) — the
                # armed mode and how many indexed steps are commands.
                "mode": (await store.get_mode(r, caller_ws, skill_id))["mode"],
                "command_steps": sum(
                    1 for e in by_skill.get(skill_id) or []
                    if (e.get("kind") or "file_glob") == "command"),
            })
        rows.sort(key=lambda x: (-x["executions"], x["skill_id"]))
        # Phase C: the coverage half of the enforcement story. A session is
        # `current` when its acked bundle version equals the CURRENT one;
        # everything else it acked is stale. Coverage is REPORTED, never
        # assumed — the server enforces whatever reaches it regardless.
        bundle_ver = (await enforce.build_bundle(r, caller_ws))["version"]
        acks = await store.list_bundle_acks(r, caller_ws)
        sessions_current = sum(
            1 for a in acks.values() if a.get("version") == bundle_ver)
        return {
            "procedures": rows,
            "count": len(rows),
            "specs_total": sum(row["spec_count"] for row in rows),
            "bundle": {
                "version": bundle_ver,
                "sessions_current": sessions_current,
                "sessions_stale": len(acks) - sessions_current,
            },
            # What the pass did, and — the point of it — WHICH closed state
            # Tier B is in. Without this a deployment where the gate is shut for
            # lack of an outcome signal is byte-identical to one where it is
            # open and found nothing, with no last_run to say whether the pass
            # has ever executed (the /dreams precedent).
            "run": await store.get_run(r),
            # Spec §4 Stage 2: recognised work dropped for an unjoinable
            # session "is counted and surfaced, not hidden".
            "unjoinable_edits": await store.get_unjoinable(r),
        }

    @router.get("/procedures/{skill_id}/executions", dependencies=read_dep)
    async def list_executions(skill_id: str, request: Request, limit: int = 50):
        r = get_redis()
        # Execution keys are `proc:exec:{session}:{skill}` — no workspace
        # component — so they are scoped through the SKILL they belong to, which
        # is the same boundary the rollup applies and the only one the records
        # can be joined to.
        index = await store.load_index(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)
        recorded = _workspace_of(skill_id, await store.load_coverage(r), by_skill)
        if not _visible(recorded, _caller_workspace(request)):
            return {"executions": [], "count": 0}
        all_execs = await store.iter_executions(r)
        mine = [e for e in all_execs if e.get("skill_id") == skill_id]
        mine.sort(key=lambda e: e.get("last_seen_at") or "", reverse=True)
        return {"executions": mine[:limit], "count": len(mine)}

    @router.post("/procedures/proposals/{proposal_id}/dismiss", dependencies=admin_dep)
    async def dismiss(proposal_id: str):
        r = get_redis()
        if not await store.dismiss_proposal(r, proposal_id):
            raise HTTPException(status_code=404, detail="Proposal not found")
        return {"dismissed": proposal_id}

    # ------------------------------------------------------------------
    # Round 2 — enforced runbooks (spec 2026-08-15)
    # ------------------------------------------------------------------

    @router.get("/procedures/bundle", dependencies=session_read_dep)
    async def get_bundle(request: Request):
        """The session's runbook bundle: every command-kind step of the
        caller's workspace, with its runbook's mode, load_bearing and
        fail_posture. `version` = sha256[:12] of the canonical entry list.

        Command matching is mistake-catching, not adversary-proof — the
        client's local match on these patterns recognises the command a
        well-meaning agent typed, nothing more.
        """
        r = get_redis()
        return await enforce.build_bundle(r, _caller_workspace(request))

    @router.post("/procedures/bundle/ack", dependencies=session_write_dep)
    async def ack_bundle(body: BundleAckBody, request: Request):
        """Record which bundle version this session holds. Coverage is
        REPORTED, never assumed — the server enforces whatever reaches it
        regardless of acks; a block-mode runbook whose recent sessions lack
        acks is surfaced on the dashboard as NOT ACTIVELY ENFORCED.

        Session attribution comes from the X-Session-Id header (client pin,
        Phase B); the body is exactly {"version"}."""
        session = _session_of(request, "")
        if not session:
            raise HTTPException(
                status_code=422,
                detail="X-Session-Id header required")
        r = get_redis()
        await store.record_bundle_ack(
            r, _caller_workspace(request), session, body.version)
        return {"recorded": True, "session_id": session,
                "version": body.version}

    @router.post("/procedures/ack", dependencies=session_write_dep)
    async def runbook_ack(body: RunbookAckBody, request: Request):
        """The ack half of the permit protocol: verify the challenge belongs
        to the caller's verified workspace + session, record the reason, mint
        the one-use permit (TTL 10 min)."""
        session = _session_of(request, body.session_id)
        r = get_redis()
        result = await enforce.acknowledge(
            r, settings_fn(), challenge_id=body.challenge_id,
            reason=body.reason, workspace=_caller_workspace(request),
            member=_caller_member(request), session=session,
        )
        if result.get("ok"):
            return result
        error = result.get("error") or "refused"
        if error == "unknown_or_expired":
            raise HTTPException(status_code=404,
                                detail="Challenge unknown or expired")
        if error == "session_mismatch":
            raise HTTPException(
                status_code=403,
                detail="Challenge belongs to a different session")
        raise HTTPException(status_code=422, detail=error)

    @router.get("/procedures/{skill_id}/mode", dependencies=read_dep)
    async def get_mode(skill_id: str, request: Request):
        r = get_redis()
        caller_ws = _caller_workspace(request)
        index = await store.load_index(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)
        recorded = _workspace_of(skill_id, await store.load_coverage(r), by_skill)
        if not _visible(recorded, caller_ws):
            raise HTTPException(status_code=404, detail="Skill not found")
        mode = await store.get_mode(r, caller_ws, skill_id)
        return {"skill_id": skill_id, **mode}

    @router.put("/procedures/{skill_id}/mode", dependencies=admin_dep)
    async def put_mode(skill_id: str, body: ModeBody, request: Request):
        """ADMIN ONLY, deliberately: the skill PATCH path cannot touch mode —
        agents may propose runbooks, never arm them. A human sets the mode
        from the dashboard."""
        if body.mode not in store.ENFORCEMENT_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"mode must be one of {list(store.ENFORCEMENT_MODES)}")
        r = get_redis()
        caller_ws = _caller_workspace(request)
        index = await store.load_index(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)
        recorded = _workspace_of(skill_id, await store.load_coverage(r), by_skill)
        if not _visible(recorded, caller_ws):
            raise HTTPException(status_code=404, detail="Skill not found")
        record = await store.set_mode(
            r, caller_ws, skill_id, body.mode, _caller_member(request))
        return {"skill_id": skill_id, **record}

    @router.get("/procedures/deviations", dependencies=read_dep)
    async def list_deviations(request: Request, limit: int = 50):
        """The deviation ledger (Phase C): block refusals, acknowledged
        overrides and failed attempts, newest first. The ledger keeps the
        newest store.MAX_DEVIATIONS per workspace — a DISCLOSED cap, so a
        full read is "the most recent 200", never the whole history. Records
        carry the command HASH only; raw command text is never stored."""
        r = get_redis()
        records = await store.list_deviations(
            r, _caller_workspace(request), limit)
        return {"deviations": records, "count": len(records)}

    return router
