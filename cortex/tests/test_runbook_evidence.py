"""Enforced Runbooks Phase A — evidence is scored by SUCCESS, not permission.

Review disposition 1: round 1 committed observations at gateway-allow, which
is unsound for commands — a permitted-but-failed backup must not unlock the
deploy. So a command observation is PENDING at decide() and commits only when
the reconcile carries success AND a real exit status of 0. Nonzero, unknown,
absent, and never-reconciled all satisfy NOTHING.

Also here: execution boundaries (terminal COMMAND step commits close the
execution; the next match opens execution_no+1 with a fresh evidence scope),
workspace isolation of the whole path, and the round-1 file_glob behaviours
that must survive byte-identically.
"""
from __future__ import annotations

import json

import pytest
import fakeredis.aioredis as fr

from app.agent_gateway.models import (
    Action,
    ActionAfterRequest,
    ActionBeforeRequest,
    Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import enforce, store
from app.procedures.observe import ProcedureObserver

WS = "workspace-local"  # the deployment workspace (FIREKEEP_WORKSPACE_ID unset)



# Review 2026-08-15: every genuinely-evaluated verdict now carries the
# `runbook_evaluated` receipt (empty message) as its FIRST advisory — the
# client's block-mode branch refuses a bare allow without it. These helpers
# split the receipt from the human-facing advisories so each is pinned for
# what it is.
def _human(resp):
    return [a for a in resp.advisories if a.code != "runbook_evaluated"]


def _codes(resp):
    return [a.code for a in _human(resp)]


def _evaluated(resp):
    return any(a.code == "runbook_evaluated" for a in resp.advisories)


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_WARN_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_INDEX_CACHE_SECONDS = 0
    PROCEDURE_MAX_SPECS = 50
    AGENT_RECONCILE_DEADLINE_SECONDS = 300
    QDRANT_COLLECTION = "c"


class _Decision:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _Engine:
    def __init__(self, action="allow"):
        self._action = action

    async def evaluate(self, ctx):
        class _D(_Decision):
            pass

        _D.action = self._action
        return _D()


def _service(r, engine=None):
    async def _no(*a, **k):
        return False

    async def _emit(**kw):
        return None

    return AgentGatewayService(
        policy_engine=engine or _Engine(), recent_failure_check=_no,
        fastpath_check=_no, session_touched_check=_no, replay_emitter=_emit,
        rethink_counter=RethinkCounter(r), prediction_redis=r,
        fastpath_redis=r, policy_decision_redis=r,
        procedure_observer=ProcedureObserver(
            get_redis=lambda: r, settings_fn=lambda: _Settings()),
    )


def _cmd_req(command, session="sess", ws=WS, member="member-owner",
             cwd=None):
    req = ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type="run_command", target=command, cwd=cwd),
    )
    # What the REST router stamps from the auth principal (the tests call the
    # service directly, so the stamp is applied here).
    req._verified_workspace = ws
    req._verified_member = member
    return req


def _after(action_id, *, success=True, exit_status=0, ws=WS):
    req = ActionAfterRequest(
        action_id=action_id, outcome=Outcome(success=success),
        exit_status=exit_status,
    )
    req._verified_workspace = ws
    return req


def _entry(skill="rb1", step="backup", pattern="bash backup.sh*", order=0,
           lb=False, ws="", kind="command", trigger="vps deploy"):
    return {"skill_id": skill, "skill_trigger": trigger, "step_id": step,
            "step_text": step, "kind": kind, "pattern": pattern,
            "load_bearing": lb, "order": order, "workspace_id": ws}


async def _seed_runbook(r, ws=""):
    """backup (command, load-bearing, order 0) -> deploy (command, terminal,
    order 1)."""
    await r.set(store.INDEX_KEY, json.dumps([
        _entry(step="backup", pattern="bash backup.sh*", order=0, lb=True,
               ws=ws),
        _entry(step="deploy", pattern="bash deploy.sh*", order=1, ws=ws),
    ]))
    await r.set(store.COVERAGE_KEY, json.dumps({
        "rb1": {"trigger": "vps deploy", "spec_count": 2, "observable": 2,
                "workspace_id": ws},
    }))


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


class TestPendingThenCommit:
    @pytest.mark.asyncio
    async def test_decide_writes_pending_in_the_spec_shape(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full", cwd="/srv"))
        assert resp.decision == "allow"
        raw = await r.get(f"proc:pending:{resp.action_id}")
        assert raw, "decide(run_command) with a step match must write pending"
        pending = json.loads(raw)
        assert pending["workspace"] == WS
        assert pending["session"] == "sess"
        assert pending["skill"] == "rb1"
        assert pending["step_id"] == "backup"
        assert pending["execution_no"] == 1
        assert pending["command_hash"] == enforce.command_hash(
            "bash backup.sh --full")
        assert pending["created"]
        assert pending["cwd"] == "/srv"  # audit only
        # TTL = the gateway's existing reconcile deadline.
        ttl = await r.ttl(f"proc:pending:{resp.action_id}")
        assert 0 < ttl <= _Settings.AGENT_RECONCILE_DEADLINE_SECONDS
        # PENDING is not evidence: nothing observed yet.
        assert await store.get_execution(r, "sess", "rb1", WS) is None

    @pytest.mark.asyncio
    async def test_exit_zero_commits_the_evidence(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, success=True, exit_status=0))
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert "backup" in ex["observed"]
        assert ex["observed"]["backup"][0]["action_id"] == resp.action_id
        # The pending record is consumed: a second reconcile settles nothing.
        assert await r.get(f"proc:pending:{resp.action_id}") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("success,exit_status", [
        (True, 1),        # nonzero
        (True, 137),      # nonzero (killed)
        (True, None),     # unknown / absent — NOT success
        (False, 0),       # exit 0 but the caller says failure
        (False, None),
    ])
    async def test_anything_but_success_and_exit_zero_satisfies_nothing(
            self, r, success, exit_status):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, success=success,
                                exit_status=exit_status))
        assert await store.get_execution(r, "sess", "rb1", WS) is None
        # ...but the attempt is retained for the ledger/audit.
        attempt = await store.get_attempt(r, resp.action_id)
        assert attempt is not None
        assert attempt["outcome"]["exit_status"] == exit_status
        assert await r.get(f"proc:pending:{resp.action_id}") is None

    @pytest.mark.asyncio
    async def test_expiry_satisfies_nothing(self, r):
        """No reconcile before the TTL: the pending is gone, and a late
        reconcile finds nothing to commit."""
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await r.delete(f"proc:pending:{resp.action_id}")  # the TTL, forced
        await svc.record(_after(resp.action_id, success=True, exit_status=0))
        assert await store.get_execution(r, "sess", "rb1", WS) is None

    @pytest.mark.asyncio
    async def test_a_refused_command_pends_nothing(self, r):
        """Two-phase discipline: pending is written only once the decision
        settles on allow — a blocked command will not run, so nothing about
        it may pend."""
        await _seed_runbook(r)
        await store.set_mode(r, WS, "rb1", "block", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now"))
        assert resp.decision == "block"
        assert await r.get(f"proc:pending:{resp.action_id}") is None

    @pytest.mark.asyncio
    async def test_an_unjoinable_session_pends_nothing(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        for sid in ("unknown", ""):
            resp = await svc.decide(
                _cmd_req("bash backup.sh --full", session=sid))
            assert resp.decision == "allow"
            assert await r.get(f"proc:pending:{resp.action_id}") is None

    @pytest.mark.asyncio
    async def test_a_cross_workspace_reconcile_commits_nothing(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full"))
        await svc.record(_after(resp.action_id, ws="workspace-other"))
        assert await store.get_execution(r, "sess", "rb1", WS) is None


class TestExecutionBoundaries:
    async def _run(self, svc, command, *, exit_status=0):
        resp = await svc.decide(_cmd_req(command))
        assert resp.decision == "allow"
        await svc.record(_after(resp.action_id, exit_status=exit_status))
        return resp

    @pytest.mark.asyncio
    async def test_terminal_command_commit_closes_the_execution(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        await self._run(svc, "bash backup.sh --full")
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert not ex.get("closed_at")
        await self._run(svc, "bash deploy.sh --now")
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert ex.get("closed_at")
        assert set(ex["observed"]) == {"backup", "deploy"}

    @pytest.mark.asyncio
    async def test_the_next_match_opens_a_fresh_execution(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        await self._run(svc, "bash backup.sh --full")
        await self._run(svc, "bash deploy.sh --now")     # closes execution 1
        await self._run(svc, "bash backup.sh --full")    # opens execution 2
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert int(ex.get("execution_no")) == 2
        assert set(ex["observed"]) == {"backup"}         # fresh evidence scope
        assert not ex.get("closed_at")

    @pytest.mark.asyncio
    async def test_the_closed_execution_is_archived_not_lost(self, r):
        """The hardening pass and the receipts endpoint read
        iter_executions; a completed run must stay visible to both."""
        await _seed_runbook(r)
        svc = _service(r)
        await self._run(svc, "bash backup.sh --full")
        await self._run(svc, "bash deploy.sh --now")
        await self._run(svc, "bash backup.sh --full")
        recs = [rec for rec in await store.iter_executions(r)
                if rec.get("skill_id") == "rb1"]
        assert len(recs) == 2
        by_no = {int(rec.get("execution_no") or 1): rec for rec in recs}
        assert set(by_no[1]["observed"]) == {"backup", "deploy"}
        assert set(by_no[2]["observed"]) == {"backup"}

    @pytest.mark.asyncio
    async def test_a_failed_terminal_step_does_not_close(self, r):
        await _seed_runbook(r)
        svc = _service(r)
        await self._run(svc, "bash backup.sh --full")
        await self._run(svc, "bash deploy.sh --now", exit_status=1)
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert not ex.get("closed_at")
        assert set(ex["observed"]) == {"backup"}

    @pytest.mark.asyncio
    async def test_a_stale_reconcile_cannot_write_into_the_next_execution(
            self, r):
        """Evidence pended for execution N must not commit once the record
        has moved past N: the run it belonged to is over."""
        await _seed_runbook(r)
        svc = _service(r)
        # Pend a backup against execution 1, but do not reconcile it yet.
        stale = await svc.decide(_cmd_req("bash backup.sh --full"))
        # Meanwhile execution 1 completes and closes...
        await self._run(svc, "bash backup.sh --full")
        await self._run(svc, "bash deploy.sh --now")
        # ...and execution 2 opens.
        await self._run(svc, "bash backup.sh --full")
        # The stale reconcile arrives (within TTL) — it must satisfy nothing
        # in execution 2 beyond what execution 2 already has.
        await svc.record(_after(stale.action_id, exit_status=0))
        ex = await store.get_execution(r, "sess", "rb1", WS)
        assert int(ex.get("execution_no")) == 2
        counts = ex["observed_counts"]
        assert counts.get("backup") == 1  # execution 2's own, not the stale one


class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_another_workspaces_runbook_is_invisible_to_my_actions(
            self, r):
        """Workspace A's runbooks must not advise, challenge, block, or
        collect evidence from workspace B's commands."""
        await _seed_runbook(r, ws="workspace-other")
        await store.set_mode(r, "workspace-other", "rb1", "block", "human")
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash deploy.sh --now", ws=WS))
        assert resp.decision == "allow"
        assert _human(resp) == []
        assert await r.get(f"proc:pending:{resp.action_id}") is None

    @pytest.mark.asyncio
    async def test_the_owning_workspace_still_gets_the_verdict(self, r):
        await _seed_runbook(r, ws="workspace-other")
        await store.set_mode(r, "workspace-other", "rb1", "block", "human")
        svc = _service(r)
        req = _cmd_req("bash deploy.sh --now", ws="workspace-other",
                       member="member-other")
        resp = await svc.decide(req)
        assert resp.decision == "block"

    @pytest.mark.asyncio
    async def test_evidence_is_keyed_by_workspace(self, r):
        """The same (session, skill) in two workspaces holds two separate
        evidence scopes — round-2 tenancy, clean break from the round-1
        machine-global keys."""
        await _seed_runbook(r)  # ws="" → visible to the deployment workspace
        svc = _service(r)
        resp = await svc.decide(_cmd_req("bash backup.sh --full", ws=WS))
        await svc.record(_after(resp.action_id, ws=WS))
        assert await store.get_execution(r, "sess", "rb1", WS) is not None
        assert await store.get_execution(r, "sess", "rb1") is None
        assert await store.get_execution(
            r, "sess", "rb1", "workspace-other") is None


class TestFileGlobRoundOneByteIdentical:
    """Round-1 file behaviour through the round-2 service, workspace stamped:
    the same sequences must produce the same advisories, the same latch
    behaviour, and executions that never close on a file commit."""

    def _file_req(self, target, session="sess", ws=WS):
        req = ActionBeforeRequest(
            session_id=session, agent_id="ag", adapter="shell-hook",
            action=Action(type="edit_file", target=target),
        )
        req._verified_workspace = ws
        req._verified_member = "member-owner"
        return req

    async def _seed_files(self, r, ws=""):
        await r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "dependency change",
             "step_id": "a", "step_text": "regenerate the lock",
             "pattern": "*.lock", "load_bearing": True, "order": 0,
             "workspace_id": ws},
            {"skill_id": "s1", "skill_trigger": "dependency change",
             "step_id": "b", "step_text": "edit requirements",
             "pattern": "requirements.txt", "load_bearing": False, "order": 1,
             "workspace_id": ws},
        ]))
        await r.set(store.COVERAGE_KEY, json.dumps({
            "s1": {"trigger": "dependency change", "spec_count": 2,
                   "observable": 2, "workspace_id": ws},
        }))

    @pytest.mark.asyncio
    async def test_a_match_observes_and_warns_exactly_as_round_one(self, r):
        await self._seed_files(r)
        svc = _service(r)
        resp = await svc.decide(self._file_req("requirements.txt"))
        assert resp.decision == "allow"
        assert _codes(resp) == ["procedure_step_missing"]
        assert "regenerate the lock" in _human(resp)[0].message
        ex = await store.get_execution(r, "sess", "s1", WS)
        assert "b" in ex["observed"]

    @pytest.mark.asyncio
    async def test_the_warn_latch_still_fires_once_per_execution(self, r):
        await self._seed_files(r)
        svc = _service(r)
        first = await svc.decide(self._file_req("requirements.txt"))
        assert len(_human(first)) == 1
        second = await svc.decide(self._file_req("requirements.txt"))
        assert _human(second) == []

    @pytest.mark.asyncio
    async def test_a_terminal_file_commit_never_closes_the_execution(self, r):
        """THE file/command asymmetry, pinned: closing on a terminal file edit
        would re-arm the warn latch round 1 promises fires once. `b` is s1's
        terminal step (spec_count 2, order 1) and its commit must not close."""
        await self._seed_files(r)
        svc = _service(r)
        await svc.decide(self._file_req("requirements.txt"))
        ex = await store.get_execution(r, "sess", "s1", WS)
        assert not ex.get("closed_at")
        assert int(ex.get("execution_no") or 1) == 1
        # And the latch consequence: a third edit still does not re-warn.
        third = await svc.decide(self._file_req("requirements.txt"))
        assert _human(third) == []

    @pytest.mark.asyncio
    async def test_an_observed_earlier_step_still_silences_the_warn(self, r):
        await self._seed_files(r)
        svc = _service(r)
        await svc.decide(self._file_req("poetry.lock"))       # step a
        resp = await svc.decide(self._file_req("requirements.txt"))
        assert _human(resp) == []

    @pytest.mark.asyncio
    async def test_run_command_still_takes_the_file_stage_nowhere(self, r):
        """Round 1 ignored run_command entirely on the file stage; with no
        command-kind entries in the index, a run_command action must still
        produce nothing at all."""
        await self._seed_files(r)
        svc = _service(r)
        resp = await svc.decide(_cmd_req("rm -rf *.lock"))
        assert resp.decision == "allow"
        assert _human(resp) == []
        assert await r.get(f"proc:pending:{resp.action_id}") is None
