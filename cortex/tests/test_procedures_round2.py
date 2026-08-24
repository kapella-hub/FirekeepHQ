"""Round-2 regression guards for Living Procedures.

Every defect here survived a fully green suite, which is the point: each one is
a property nothing measured. Grouped by defect, with the code fact that makes it
real stated in the docstring — a guard whose reason lives only in a commit
message is a guard the next reader deletes.
"""
from __future__ import annotations

import json

import fakeredis.aioredis as fr
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agent_gateway.models import Action, ActionBeforeRequest
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import harden, store
from app.procedures.api import create_procedures_router
from app.procedures.observe import ProcedureObserver


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_WARN_ENABLED = True
    PROCEDURE_MIN_EXECUTIONS = 2
    PROCEDURE_PRIOR_N = 5
    PROCEDURE_EFFICACY_DELTA = 0.15
    PROCEDURE_WINDOW_DAYS = 30
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_AGENT_CAP = 5
    PROCEDURE_MAX_SPECS = 50
    PROCEDURE_INDEX_CACHE_SECONDS = 0
    QDRANT_COLLECTION = "c"


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FilterHonouringQdrant:
    def __init__(self, points):
        self._points = list(points)

    async def scroll(self, *, collection_name, scroll_filter=None, limit=1000,
                     offset=None, **kw):
        pts = self._points
        if scroll_filter is not None:
            for cond in scroll_filter.must or []:
                pts = [p for p in pts
                       if (p.payload or {}).get(cond.key) == cond.match.value]
        start = int(offset or 0)
        page = pts[start:start + limit]
        nxt = start + limit if start + limit < len(pts) else None
        return page, nxt


class _Vector:
    def __init__(self, points=()):
        self._client = _FilterHonouringQdrant(points)


def _skill(pid, steps, *, load_bearing=(), status="active", trigger="release",
           content=None, workspace_id=None):
    payload = {
        "memory_type": "skill", "skill_status": status, "trigger": trigger,
        "content": content if content is not None
        else "## Steps\n" + "\n".join(steps),
        "step_specs": [
            {"id": sid, "text": sid, "kind": "file_glob",
             "pattern": f"{sid}.py", "load_bearing": sid in load_bearing}
            for sid in steps
        ],
    }
    if workspace_id is not None:
        payload["workspace_id"] = workspace_id
    return _Point(pid, payload)


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


async def _exec(client, session, skill, observed, agent="ag"):
    s = _Settings()
    for step_id in observed:
        await store.record_observation(
            client, s, session_id=session, skill_id=skill, step_id=step_id,
            action_id="x", target="t", agent_id=agent, adapter="shell-hook")


async def _no_outcome(replay_r, sid, bridge_status=None):
    return None


def _req(target="b.py", type_="edit_file", session="sess"):
    return ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type=type_, target=target),
    )


# ---------------------------------------------------------------------------
# R1 — the observation is a claim that the edit HAPPENED
# ---------------------------------------------------------------------------

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


def _service(r, engine):
    async def _no(*a, **k):
        return False

    async def _emit(**kw):
        return None

    return AgentGatewayService(
        policy_engine=engine, recent_failure_check=_no, fastpath_check=_no,
        session_touched_check=_no, replay_emitter=_emit,
        rethink_counter=RethinkCounter(r), prediction_redis=r, fastpath_redis=r,
        policy_decision_redis=r,
        procedure_observer=ProcedureObserver(
            get_redis=lambda: r, settings_fn=lambda: _Settings()),
    )


async def _seed_index(r, load_bearing=True):
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "a",
         "step_text": "regenerate the lock", "pattern": "*.lock",
         "load_bearing": load_bearing, "order": 0, "workspace_id": ""},
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "b",
         "step_text": "edit requirements", "pattern": "b.py",
         "load_bearing": False, "order": 1, "workspace_id": ""},
    ]))


class TestOnlyAnEditThatHappenedIsObserved:
    @pytest.mark.asyncio
    async def test_a_blocked_edit_writes_no_execution(self, r):
        """PathDenyRule blocks BEFORE the observer runs today, so the edit that
        the customer never made was written into `observed` — inflating Tier A
        frequency with work that did not occur."""
        await _seed_index(r)
        resp = await _service(r, _Engine("block")).decide(_req())
        assert resp.decision == "block"
        assert await store.get_execution(r, "sess", "s1") is None

    @pytest.mark.asyncio
    async def test_a_rethink_writes_no_execution(self, r):
        """On rethink the agent RESUBMITS the same edit, so recording it here
        double-counts one edit."""
        await _seed_index(r)
        resp = await _service(r, _Engine("rethink")).decide(_req())
        assert resp.decision == "rethink"
        assert await store.get_execution(r, "sess", "s1") is None

    @pytest.mark.asyncio
    async def test_the_escalation_to_block_is_seen_by_the_observer(self, r):
        """The rethink counter can turn an `allow`-shaped path into a `block`
        AFTER the observer has run. Nothing may be recorded for it."""
        from app.config import get_settings

        limit = get_settings().AGENT_RETHINK_MAX_LOOPS
        svc = _service(r, _Engine("rethink"))
        await _seed_index(r)
        for _ in range(limit):
            resp = await svc.decide(_req())
        assert resp.decision == "block"
        assert await store.get_execution(r, "sess", "s1") is None

    @pytest.mark.asyncio
    async def test_a_rethink_does_not_burn_the_warn_for_the_resubmission(self, r):
        """The warn latch is once-per-(execution, step). Claiming it for an edit
        that never happened means the agent's actual edit is never warned."""
        await _seed_index(r)
        rethought = await _service(r, _Engine("rethink")).decide(_req())
        assert [a.code for a in rethought.advisories] == ["procedure_step_missing"]

        allowed = await _service(r, _Engine("allow")).decide(_req())
        assert [a.code for a in allowed.advisories] == ["procedure_step_missing"]

    @pytest.mark.asyncio
    async def test_an_allowed_edit_is_still_recorded_exactly_once(self, r):
        await _seed_index(r)
        svc = _service(r, _Engine("allow"))
        resp = await svc.decide(_req())
        assert resp.decision == "allow"
        ex = await store.get_execution(r, "sess", "s1")
        assert ex["observed_counts"] == {"b": 1}
        assert ex["observed"]["b"][0]["action_id"] == resp.action_id

    @pytest.mark.asyncio
    async def test_the_procedure_advisory_keeps_its_place_in_the_list(self, r):
        """Ordering is part of the contract: the client joins `message` with
        '; ', so moving the whole stage after the escalation block would reorder
        what the human reads and put the procedure advisory after
        `rethink_limit`."""
        from app.config import get_settings

        await _seed_index(r)
        svc = _service(r, _Engine("rethink"))
        for _ in range(get_settings().AGENT_RETHINK_MAX_LOOPS):
            resp = await svc.decide(_req())
        assert [a.code for a in resp.advisories] == [
            "procedure_step_missing", "rethink_limit",
        ]

    @pytest.mark.asyncio
    async def test_one_edit_matching_two_steps_does_not_warn_about_itself(self, r):
        """Two globs of ONE procedure can match a single file. While the write
        happened inline, the second matched entry read the first one's step as
        already observed; deferring the write must carry that forward or the
        edit warns that it skipped a step it performed in the same call."""
        await r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "a",
             "step_text": "the load-bearing one", "pattern": "b.py",
             "load_bearing": True, "order": 0, "workspace_id": ""},
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "b",
             "step_text": "the later one", "pattern": "*.py",
             "load_bearing": False, "order": 1, "workspace_id": ""},
        ]))
        resp = await _service(r, _Engine("allow")).decide(_req())
        assert resp.advisories == []
        ex = await store.get_execution(r, "sess", "s1")
        assert set(ex["observed"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_an_unjoinable_edit_is_counted_only_when_it_happened(self, r):
        """The unjoinable counter measures recognised work that could not be
        joined to an outcome. A blocked edit is not work."""
        await _seed_index(r)
        await _service(r, _Engine("block")).decide(_req(session="unknown"))
        assert await store.get_unjoinable(r) == 0
        await _service(r, _Engine("allow")).decide(_req(session="unknown"))
        assert await store.get_unjoinable(r) == 1


# ---------------------------------------------------------------------------
# R2 — the discrimination check must hold where the comparison is made
# ---------------------------------------------------------------------------

class TestAPerStepVerdictNeedsAPerStepSignal:
    @pytest.mark.asyncio
    async def test_one_failure_elsewhere_does_not_authorise_a_dead_step(
            self, r, monkeypatch):
        """The gate summed outcome_success/outcome_failure across EVERY
        execution of EVERY skill, so a single failing session anywhere in the
        deployment opened it. `_tier_b_proposals` was then applied to a step
        whose own scored buckets are uniformly successful — where
        `compute_efficacy` returns the identical Beta-prior value for both, so
        `eff_skip >= eff_obs - delta` holds exactly and a `dead_step` ("remove
        it?") is emitted on a signal that separated nothing."""
        await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "ok-3", "s1", ["b"], agent="ag3")
        await _exec(r, "ok-4", "s1", ["b"], agent="ag4")
        await _exec(r, "bad-1", "s2", ["c"], agent="ag5")  # elsewhere, unrelated

        async def _outcome(replay_r, sid, bridge_status=None):
            return sid.startswith("ok")

        monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a", "b"]), _skill("s2", ["c"])]),
            _Settings())

        # The pass-level string is kept for the operator: the deployment really
        # does carry both outcome classes.
        assert result["tier_b"] == "open"
        kinds = {(p["kind"], p["step_id"])
                 for p in await store.list_proposals(r, "s1")}
        assert not {k for k in kinds if k[0] in ("dead_step", "load_bearing")}, kinds

    @pytest.mark.asyncio
    async def test_a_step_whose_own_buckets_discriminate_still_gets_a_verdict(
            self, r, monkeypatch):
        """The fix must not close Tier B — only move the check to where the
        comparison happens."""
        await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "bad-1", "s1", ["b"], agent="ag3")
        await _exec(r, "bad-2", "s1", ["b"], agent="ag4")

        async def _outcome(replay_r, sid, bridge_status=None):
            return sid.startswith("ok")

        monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a", "b"])]),
                              _Settings())
        kinds = {(p["kind"], p["step_id"])
                 for p in await store.list_proposals(r, "s1")}
        assert ("load_bearing", "a") in kinds

    @pytest.mark.asyncio
    async def test_verdict_ready_counts_steps_that_could_really_be_decided(
            self, r, monkeypatch):
        """It is the operator's answer to 'open, zero proposals, forever', so it
        must count the gates that actually authorise a verdict — including the
        per-step discrimination one."""
        await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "ok-3", "s1", ["b"], agent="ag3")
        await _exec(r, "ok-4", "s1", ["b"], agent="ag4")
        await _exec(r, "bad-1", "s2", ["c"], agent="ag5")

        async def _outcome(replay_r, sid, bridge_status=None):
            return sid.startswith("ok")

        monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a", "b"]), _skill("s2", ["c"])]),
            _Settings())
        assert result["verdict_ready_steps"] == 0


# ---------------------------------------------------------------------------
# R3 — Bridge `abandoned` is one of only two real failure signals
# ---------------------------------------------------------------------------

class _Eval:
    def __init__(self, data):
        self._data = data
        self.task_result = data.get("task_result")
        self.task_result_source = data.get("task_result_source")

    def model_dump(self):
        return self._data


def _clean_session(monkeypatch):
    """A genuinely graded successful session; abandoned still overrides it."""
    async def _get_eval(rr, sid):
        return _Eval({
            "metrics": {},
            "task_result": "success",
            "task_result_source": "self_reported",
        })

    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)


class TestBridgeAbandonedIsNotDropped:
    @pytest.mark.asyncio
    async def test_an_abandoned_session_is_a_failure(self, monkeypatch):
        """`harden` hardcoded `session_success(data, None)` while `owm.py`
        fetches the status map and passes it — so the pass discarded the signal
        most likely to legitimately open its own gate."""
        _clean_session(monkeypatch)
        assert await harden._resolve_outcome(object(), "sess", None) is True
        assert await harden._resolve_outcome(object(), "sess", "abandoned") is False

    @pytest.mark.asyncio
    async def test_the_pass_fetches_the_statuses_owm_already_fetches(
            self, r, monkeypatch):
        """End to end: without the status map this corpus is uniformly
        successful and no step can ever be decided."""
        _clean_session(monkeypatch)
        await _exec(r, "s-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "s-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "s-3", "s1", ["b"], agent="ag3")
        await _exec(r, "s-4", "s1", ["b"], agent="ag4")

        async def _statuses(settings):
            return {"s-3": "abandoned", "s-4": "abandoned"}

        monkeypatch.setattr("app.owm._fetch_bridge_statuses", _statuses)
        result = await harden.run_pass(
            r, object(), _Vector([_skill("s1", ["a", "b"])]), _Settings())

        assert result["outcome_failure"] == 2
        assert result["outcome_success"] == 2
        assert result["tier_b"] == "open"
        kinds = {(p["kind"], p["step_id"])
                 for p in await store.list_proposals(r, "s1")}
        assert ("load_bearing", "a") in kinds

    @pytest.mark.asyncio
    async def test_an_unreachable_bridge_never_fails_the_pass(self, r, monkeypatch):
        _clean_session(monkeypatch)
        await _exec(r, "s-1", "s1", ["a"], agent="ag1")

        async def _boom(settings):
            raise ConnectionError("bridge is down")

        monkeypatch.setattr("app.owm._fetch_bridge_statuses", _boom)
        result = await harden.run_pass(
            r, object(), _Vector([_skill("s1", ["a"])]), _Settings())
        assert result["status"] == "ok"
        assert result["health"] == "ok"


# ---------------------------------------------------------------------------
# R4 — an empty scan is not evidence of deletion
# ---------------------------------------------------------------------------

class TestTheOrphanSweepRefusesAVacuousScan:
    @pytest.mark.asyncio
    async def test_a_scan_that_returns_nothing_clears_nothing(self, r, monkeypatch):
        """`scan_ok` was False only when the scan RAISED. A scan that succeeds
        and returns EMPTY — wrong QDRANT_COLLECTION, a collection restored
        empty, a payload-index rebuild in progress — produced an empty `touched`
        set, and the sweep then cleared every previously-written skill's stats
        and proposals. Silent data loss on a transient infrastructure state."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await store.write_step_stats(r, _Settings(), "s1", {
            "a": {"observed": 9, "skipped": 1, "executions": 10},
        })
        await store.write_proposals(r, "s1", [
            {"id": "p1", "kind": "spec_drift", "skill_id": "s1", "step_id": "a",
             "detail": "d"},
        ])

        result = await harden.run_pass(r, None, _Vector([]), _Settings())

        assert await store.get_step_stats(r, "s1") != {}
        assert await store.list_proposals(r, "s1") != []
        assert result["orphans_cleared"] == 0
        assert result["orphan_sweep"] == "declined: vacuous scan"

    @pytest.mark.asyncio
    async def test_a_real_deletion_is_still_swept(self, r, monkeypatch):
        """The guard is 'empty relative to what was there', not 'never sweep':
        a scan that still returns skills is evidence, and the skill it no longer
        names really has gone."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await store.write_step_stats(r, _Settings(), "gone", {
            "a": {"observed": 4, "skipped": 0, "executions": 4},
        })
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a"])]), _Settings())
        assert await store.get_step_stats(r, "gone") == {}
        assert result["orphans_cleared"] == 1
        assert result["orphan_sweep"] == "ok"

    @pytest.mark.asyncio
    async def test_a_cold_deployment_is_not_a_declined_sweep(self, r, monkeypatch):
        """Nothing was there, so an empty scan says nothing was deleted either."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        result = await harden.run_pass(r, None, _Vector([]), _Settings())
        assert result["orphan_sweep"] == "ok"


# ---------------------------------------------------------------------------
# R6 — the read surface is scoped to the caller's workspace
# ---------------------------------------------------------------------------

def _client_for(client_r):
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: client_r, get_vector=lambda: None,
        settings_fn=lambda: _Settings(),
    ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _entry(skill_id, step_id, workspace_id, pattern="*.py"):
    return {"skill_id": skill_id, "skill_trigger": "t", "step_id": step_id,
            "step_text": step_id, "pattern": pattern, "load_bearing": False,
            "order": 0, "workspace_id": workspace_id}


class TestTheReadSurfaceIsScopedToTheCallersWorkspace:
    @pytest.mark.asyncio
    async def test_the_index_carries_the_workspace_it_came_from(self, r):
        """`scan_active_skills` filtered on memory_type + skill_status only, so
        nothing downstream had a tenancy field to scope on at all."""
        await store.rebuild_index(
            _Vector([_skill("mine", ["a"], workspace_id="workspace-local"),
                     _skill("theirs", ["b"], workspace_id="workspace-other")]),
            r, _Settings())
        index = await store.load_index(r)
        assert {e["skill_id"]: e["workspace_id"] for e in index} == {
            "mine": "workspace-local", "theirs": "workspace-other",
        }
        coverage = await store.load_coverage(r)
        assert coverage["theirs"]["workspace_id"] == "workspace-other"

    @pytest.mark.asyncio
    async def test_another_workspaces_procedure_is_not_listed(self, r):
        """`GET /procedures` returned EVERY workspace's triggers and step text
        to any caller holding `memory:read`."""
        await r.set(store.INDEX_KEY, json.dumps([
            _entry("mine", "a", "workspace-local"),
            _entry("theirs", "b", "workspace-other"),
        ]))
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert [row["skill_id"] for row in body["procedures"]] == ["mine"]
        assert body["specs_total"] == 1

    @pytest.mark.asyncio
    async def test_another_workspaces_receipts_are_not_returned(self, r):
        """The receipts carry session ids, agent ids and edited file paths."""
        await r.set(store.INDEX_KEY, json.dumps([
            _entry("theirs", "b", "workspace-other"),
        ]))
        await _exec(r, "their-session", "theirs", ["b"])
        async with _client_for(r) as client:
            resp = await client.get("/procedures/theirs/executions")
        assert resp.status_code == 200
        assert resp.json() == {"executions": [], "count": 0}

    @pytest.mark.asyncio
    async def test_an_unattributed_procedure_belongs_to_the_deployment(self, r):
        """An index entry written before this field existed carries no
        workspace. `workspace_migration.backfill_memories` assigns exactly those
        points to the deployment's own workspace, so the index follows the same
        rule rather than inventing a second one — and the receipts endpoint
        agrees with the rollup."""
        await r.set(store.INDEX_KEY, json.dumps([_entry("legacy", "a", "")]))
        await _exec(r, "s-1", "legacy", ["a"])
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
            execs = (await client.get("/procedures/legacy/executions")).json()
        assert [row["skill_id"] for row in body["procedures"]] == ["legacy"]
        assert execs["count"] == 1
