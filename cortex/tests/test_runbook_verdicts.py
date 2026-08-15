"""Enforced runbooks round 2 — verdicts through the real decide()/record() path.

Spec: docs/superpowers/specs/2026-08-15-enforced-runbooks-design.md. The
helpers here (settings/engine/service builders, the deploy-runbook seed) are
imported by the other test_runbook_* files, the way test_skill_step_specs
imports from test_skill_api — one app under test, not five subtly different
ones.
"""
from __future__ import annotations

import json

import fakeredis.aioredis as fr
import pytest

from app.agent_gateway.models import (
    Action,
    ActionAfterRequest,
    ActionBeforeRequest,
    Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import store
from app.procedures.observe import ProcedureObserver

WS_A = "ws-a"
WS_B = "ws-b"



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
    PROCEDURE_INDEX_CACHE_SECONDS = 0  # no memoisation in tests
    PROCEDURE_MAX_SPECS = 50
    AGENT_RECONCILE_DEADLINE_SECONDS = 300
    QDRANT_COLLECTION = "c"


class _Decision:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _Engine:
    """Returns whatever `action` it is told to, one verdict per call."""

    def __init__(self, *actions):
        self._actions = list(actions) or ["allow"]

    async def evaluate(self, ctx):
        action = self._actions.pop(0) if len(self._actions) > 1 else self._actions[0]

        class _D(_Decision):
            pass

        _D.action = action
        return _D()


def _service(r, engine=None, settings=None):
    async def _no(*a, **k):
        return False

    async def _emit(**kw):
        return None

    s = settings or _Settings()
    return AgentGatewayService(
        policy_engine=engine or _Engine("allow"), recent_failure_check=_no,
        fastpath_check=_no, session_touched_check=_no, replay_emitter=_emit,
        rethink_counter=RethinkCounter(r), prediction_redis=r, fastpath_redis=r,
        policy_decision_redis=r,
        procedure_observer=ProcedureObserver(
            get_redis=lambda: r, settings_fn=lambda: s),
    )


def _entry(skill, step, pattern, order, *, kind="command", load_bearing=False,
           ws=WS_A, trigger="vps deploy"):
    return {"skill_id": skill, "skill_trigger": trigger, "step_id": step,
            "step_text": step, "kind": kind, "pattern": pattern,
            "load_bearing": load_bearing, "order": order, "workspace_id": ws}


# The dogfood shape from the rollout gates: backup (load-bearing) -> update.
def deploy_entries(ws=WS_A):
    return [
        _entry("dep", "backup", "bash backup.sh*", 0, load_bearing=True, ws=ws),
        _entry("dep", "update", "bash update.sh*", 1, ws=ws),
    ]


def deploy_coverage(ws=WS_A):
    return {"dep": {"trigger": "vps deploy", "spec_count": 2, "observable": 2,
                    "workspace_id": ws}}


async def seed(r, entries, coverage=None):
    await r.set(store.INDEX_KEY, json.dumps(entries))
    if coverage is not None:
        await r.set(store.COVERAGE_KEY, json.dumps(coverage))


def cmd_req(command, *, session="sess", ws=WS_A, member="m1",
            type_="run_command"):
    req = ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type=type_, target=command),
    )
    # What the REST router stamps from the auth principal; set directly here
    # because these tests drive the service, not the HTTP app.
    req._verified_workspace = ws
    req._verified_member = member
    return req


def after_req(action_id, *, success=True, exit_status=0, ws=WS_A):
    req = ActionAfterRequest(
        action_id=action_id, outcome=Outcome(success=success),
        exit_status=exit_status,
    )
    req._verified_workspace = ws
    return req


async def run_ok(svc, command, **kw):
    """decide + reconcile(exit 0): one successfully completed command."""
    resp = await svc.decide(cmd_req(command, **kw))
    assert resp.decision == "allow", [a.message for a in resp.advisories]
    await svc.record(after_req(resp.action_id, ws=kw.get("ws", WS_A)))
    return resp


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


# ---------------------------------------------------------------------------
# advise — allow + advisory; the warning text reaches the agent
# ---------------------------------------------------------------------------

class TestAdvise:
    @pytest.mark.asyncio
    async def test_missing_load_bearing_predecessor_advises_but_allows(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        resp = await _service(r).decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert _codes(resp) == ["procedure_step_missing"]
        assert "backup" in _human(resp)[0].message

    @pytest.mark.asyncio
    async def test_satisfied_predecessor_stays_silent(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        svc = _service(r)
        await run_ok(svc, "bash backup.sh full")
        resp = await svc.decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert _human(resp) == []

    @pytest.mark.asyncio
    async def test_a_non_matching_command_adds_zero_work(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        resp = await _service(r).decide(cmd_req("ls -la"))
        assert resp.decision == "allow"
        assert _human(resp) == []
        assert await r.get(store.pending_key(resp.action_id)) is None


# ---------------------------------------------------------------------------
# block — refuse while a load-bearing predecessor lacks SUCCESSFUL evidence
# ---------------------------------------------------------------------------

class TestBlock:
    @pytest.mark.asyncio
    async def test_missing_load_bearing_predecessor_blocks(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r).decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "block"
        assert _codes(resp) == ["runbook_blocked"]
        assert "backup" in _human(resp)[0].message
        # Two-phase: a refused command writes NOTHING — no pending, no
        # execution record.
        assert await r.get(store.pending_key(resp.action_id)) is None
        assert await store.get_execution(r, "sess", "dep", WS_A) is None

    @pytest.mark.asyncio
    async def test_successful_predecessor_unblocks(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        svc = _service(r)
        await run_ok(svc, "bash backup.sh full")
        resp = await svc.decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert _human(resp) == []

    @pytest.mark.asyncio
    async def test_a_permitted_but_failed_backup_does_not_unlock_the_deploy(self, r):
        """Review disposition 1, the flagship case: allow is not success. The
        backup RAN (gateway allowed it) and exited 1 — its evidence must not
        exist, and block mode must still refuse the update."""
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        svc = _service(r)
        resp = await svc.decide(cmd_req("bash backup.sh full"))
        assert resp.decision == "allow"
        await svc.record(after_req(resp.action_id, success=True, exit_status=1))
        blocked = await svc.decide(cmd_req("bash update.sh --prod"))
        assert blocked.decision == "block"

    @pytest.mark.asyncio
    async def test_one_command_satisfying_both_steps_is_not_blocked_by_itself(self, r):
        """A single command can match a load-bearing step AND a later step: it
        either succeeds for both or fails for both, so the later entry must
        not be refused over the sibling the same command performs."""
        entries = [
            _entry("dep", "backup", "deploy-all*", 0, load_bearing=True),
            _entry("dep", "update", "deploy-all*", 1),
        ]
        await seed(r, entries, deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r).decide(cmd_req("deploy-all now"))
        assert resp.decision == "allow"
        assert _human(resp) == []

    @pytest.mark.asyncio
    async def test_block_aggregates_over_a_policy_rethink(self, r):
        """Existing precedence: block over rethink."""
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r, _Engine("rethink")).decide(
            cmd_req("bash update.sh --prod"))
        assert resp.decision == "block"


# ---------------------------------------------------------------------------
# Tenancy — the verified workspace scopes every lookup
# ---------------------------------------------------------------------------

class TestWorkspaceIsolation:
    @pytest.mark.asyncio
    async def test_another_workspaces_runbook_never_touches_my_commands(self, r):
        """Workspace A's runbooks are invisible to B's actions — even in block
        mode with every predecessor missing."""
        await seed(r, deploy_entries(ws=WS_A), deploy_coverage(ws=WS_A))
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r).decide(
            cmd_req("bash update.sh --prod", ws=WS_B))
        assert resp.decision == "allow"
        assert _human(resp) == []
        assert await r.get(store.pending_key(resp.action_id)) is None

    @pytest.mark.asyncio
    async def test_evidence_is_scoped_to_the_workspace_that_earned_it(self, r):
        """Same session id, same skill id, different workspace: A's committed
        backup is not evidence for B."""
        entries = deploy_entries(ws=WS_A) + deploy_entries(ws=WS_B)
        coverage = {"dep": {"trigger": "vps deploy", "spec_count": 2,
                            "observable": 2, "workspace_id": WS_A}}
        await seed(r, entries, coverage)
        svc = _service(r)
        await run_ok(svc, "bash backup.sh full", ws=WS_A)
        assert await store.get_execution(r, "sess", "dep", WS_A) is not None
        assert await store.get_execution(r, "sess", "dep", WS_B) is None

    @pytest.mark.asyncio
    async def test_the_workspace_comes_from_the_private_attr_not_the_payload(self):
        """The wire schema must not admit a client-supplied workspace: the
        PrivateAttr is invisible to validation, so a hostile payload naming it
        changes nothing."""
        req = ActionBeforeRequest.model_validate({
            "session_id": "s", "agent_id": "a", "adapter": "mcp",
            "action": {"type": "run_command", "target": "x"},
            "_verified_workspace": "ws-evil",
            "verified_workspace": "ws-evil",
        })
        assert req._verified_workspace == ""
        assert "_verified_workspace" not in ActionBeforeRequest.model_fields
        assert "_verified_workspace" not in req.model_dump()


# ---------------------------------------------------------------------------
# Round-1 file path — byte-identical with command entries alongside
# ---------------------------------------------------------------------------

class TestFileGlobRoundOneUnchanged:
    @pytest.mark.asyncio
    async def test_command_entries_do_not_perturb_file_matching(self, r):
        """A file edit against an index that now carries command entries
        behaves exactly as round 1: same advisory, same observation, and the
        command entries contribute nothing to the file match."""
        entries = [
            {"skill_id": "s1", "skill_trigger": "dependency change",
             "step_id": "a", "step_text": "regenerate the lock",
             "pattern": "*.lock", "load_bearing": True, "order": 0,
             "workspace_id": ""},
            {"skill_id": "s1", "skill_trigger": "dependency change",
             "step_id": "b", "step_text": "edit requirements",
             "pattern": "requirements.txt", "load_bearing": False, "order": 1,
             "workspace_id": ""},
            _entry("s1", "c", "pip-compile*", 2, ws=""),
        ]
        await seed(r, entries)
        svc = _service(r)
        req = ActionBeforeRequest(
            session_id="sess", agent_id="ag", adapter="shell-hook",
            action=Action(type="edit_file", target="requirements.txt"))
        resp = await svc.decide(req)
        assert resp.decision == "allow"
        assert _codes(resp) == ["procedure_step_missing"]
        ex = await store.get_execution(r, "sess", "s1")
        assert set(ex["observed"]) == {"b"}
        # Warn latch: same step warns once per execution, exactly as round 1.
        again = await svc.decide(ActionBeforeRequest(
            session_id="sess", agent_id="ag", adapter="shell-hook",
            action=Action(type="edit_file", target="requirements.txt")))
        assert _human(again) == []

    @pytest.mark.asyncio
    async def test_a_file_commit_never_closes_an_execution(self, r):
        """file_glob semantics unchanged (spec, 'What round 1 keeps'): even
        the LAST spec's file edit leaves the execution open — closing is a
        terminal COMMAND step's successful reconcile only."""
        entries = [
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "a",
             "step_text": "a", "pattern": "*.lock", "load_bearing": False,
             "order": 0, "workspace_id": ""},
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "b",
             "step_text": "b", "pattern": "requirements.txt",
             "load_bearing": False, "order": 1, "workspace_id": ""},
        ]
        await seed(r, entries, {"s1": {"trigger": "t", "spec_count": 2,
                                       "observable": 2, "workspace_id": ""}})
        svc = _service(r)
        req = ActionBeforeRequest(
            session_id="sess", agent_id="ag", adapter="shell-hook",
            action=Action(type="edit_file", target="requirements.txt"))
        await svc.decide(req)  # terminal spec position, file kind
        ex = await store.get_execution(r, "sess", "s1")
        assert ex is not None and not ex.get("closed_at")


# ---------------------------------------------------------------------------
# Sessions that cannot carry evidence
# ---------------------------------------------------------------------------

class TestUnjoinableSessions:
    @pytest.mark.asyncio
    async def test_an_unknown_session_is_never_enforced_and_never_pends(self, r):
        """No evidence scope can exist for a session that cannot be joined to
        an outcome — block mode degrading to always-block there would refuse
        every command forever."""
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        for sid in ("", "unknown"):
            resp = await _service(r).decide(
                cmd_req("bash update.sh --prod", session=sid))
            assert resp.decision == "allow"
            assert _human(resp) == []
            assert await r.get(store.pending_key(resp.action_id)) is None

    @pytest.mark.asyncio
    async def test_disabled_feature_does_nothing_at_all(self, r):
        class Off(_Settings):
            PROCEDURE_ENABLED = False

        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r, settings=Off()).decide(
            cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert _human(resp) == []


# ---------------------------------------------------------------------------
# The pre-tool path cannot raise
# ---------------------------------------------------------------------------

class TestNeverRaises:
    @pytest.mark.asyncio
    async def test_a_dead_redis_degrades_to_allow(self, r):
        class Dead:
            def __getattr__(self, name):
                async def boom(*a, **k):
                    raise ConnectionError("redis is down")
                return boom

        svc = AgentGatewayService(
            policy_engine=_Engine("allow"),
            recent_failure_check=(lambda *a, **k: _f(False)),
            fastpath_check=(lambda *a, **k: _f(False)),
            session_touched_check=(lambda *a, **k: _f(False)),
            replay_emitter=(lambda **kw: _f(None)),
            rethink_counter=RethinkCounter(r), prediction_redis=r,
            procedure_observer=ProcedureObserver(
                get_redis=lambda: Dead(), settings_fn=lambda: _Settings()),
        )
        resp = await svc.decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"

    @pytest.mark.asyncio
    async def test_a_corrupt_index_degrades_to_allow(self, r):
        await r.set(store.INDEX_KEY, "{not json")
        resp = await _service(r).decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"


async def _f(value):
    return value


class TestEvaluatedMarker:
    """Review 2026-08-15: the positive-evaluation receipt. A block-mode client
    lowers its exit code only on an allow that carries `runbook_evaluated`;
    every path below pins WHERE the receipt may and may not appear, because a
    marker on a degraded path would convert server failure into an
    authenticated allow — the exact hole the receipt exists to close."""

    @pytest.mark.asyncio
    async def test_no_match_is_evaluated_and_marked(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        resp = await _service(r).decide(cmd_req("ls -la"))
        assert resp.decision == "allow"
        assert _human(resp) == []
        assert _evaluated(resp), "a readable index with no match IS an evaluation"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_marked_so_stale_block_bundles_drain(self, r):
        class Off:
            PROCEDURE_ENABLED = False
        await seed(r, deploy_entries(), deploy_coverage())
        await store.set_mode(r, WS_A, "dep", "block", "human")
        resp = await _service(r, settings=Off()).decide(
            cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert _evaluated(resp), (
            "feature-off is an evaluation result; without the marker a client "
            "holding a stale block bundle would fail closed forever")

    @pytest.mark.asyncio
    async def test_unreadable_index_is_not_marked(self, r):
        from app.procedures import enforce

        class S:
            PROCEDURE_ENABLED = True
            AGENT_RECONCILE_DEADLINE_SECONDS = 300

        req = cmd_req("bash update.sh --prod")
        result = await enforce.evaluate(
            r, S(), req=req, workspace=WS_A,
            member="m1", action_id="a1", index=[], index_ok=False)
        assert result.decision == "allow"
        assert result.advisories == [], (
            "an index that could not be READ evaluated nothing — no receipt, "
            "and the block-mode client stays failed-closed")

    @pytest.mark.asyncio
    async def test_internal_failure_is_not_marked(self, r, monkeypatch):
        from app.procedures import match as match_mod
        def boom(*a, **kw):
            raise RuntimeError("index walk exploded")
        monkeypatch.setattr(match_mod, "match_command", boom)
        await seed(r, deploy_entries(), deploy_coverage())
        resp = await _service(r).decide(cmd_req("bash update.sh --prod"))
        assert resp.decision == "allow"
        assert not _evaluated(resp), (
            "an internal exception degrades to allow WITHOUT the receipt — "
            "the client, not the broken server, decides what that means")

    @pytest.mark.asyncio
    async def test_unjoinable_session_is_not_marked(self, r):
        await seed(r, deploy_entries(), deploy_coverage())
        resp = await _service(r).decide(
            cmd_req("bash update.sh --prod", session="unknown"))
        assert not _evaluated(resp), (
            "no evidence scope means enforcement cannot have run")
