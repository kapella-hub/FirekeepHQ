"""Regression guards for the Living Procedures defects found in review.

Grouped by DEFECT, not by module, because several of them are one root cause
reaching three surfaces. The clearest is the Redis client: `app.state.redis_client`
is built with no `decode_responses` (`app/main.py`), and every procedures fixture
in the suite was `FakeRedis(decode_responses=True)` — the one shape production
does not use. Under the real client the execution record was destroyed on every
write, the receipts endpoint could never find its own rows, and a dismiss
reported success while writing to `proc:proposals:b'<id>'`.

The other theme is the stale-reset promise. `store.write_proposals`' own
docstring, `harden.run_pass`' comment and spec §4 Stage 5 all claim OWM's shape
("a proposal with no supporting evidence in the window must DISAPPEAR"), and the
named precedent (`owm.py`) implements the second half this pass omitted: a sweep
over what was written LAST time. Iterating `tallies` can only ever revisit skills
that still have evidence, which is precisely the set that does not need clearing.
"""
from __future__ import annotations

import json

import fakeredis.aioredis as fr
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agent_gateway.models import Action, ActionBeforeRequest
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.procedures import harden, match, store
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
    """Applies must-FieldCondition MatchValue filters, and PAGINATES.

    Pagination is what the pre-existing doubles cannot express: they return
    `pts[:limit], None`, so a caller that ignores the offset cursor looks
    correct against them at any corpus size.
    """

    def __init__(self, points, page_size=None):
        self._points = list(points)
        self._page = page_size
        self.scrolls = 0

    async def scroll(self, *, collection_name, scroll_filter=None, limit=1000,
                     offset=None, **kw):
        self.scrolls += 1
        pts = self._points
        if scroll_filter is not None:
            for cond in scroll_filter.must or []:
                pts = [p for p in pts
                       if (p.payload or {}).get(cond.key) == cond.match.value]
        size = min(limit, self._page or limit)
        start = int(offset or 0)
        page = pts[start:start + size]
        nxt = start + size if start + size < len(pts) else None
        return page, nxt


class _Vector:
    def __init__(self, points=(), page_size=None):
        self._client = _FilterHonouringQdrant(points, page_size)


def _skill(pid, steps, *, load_bearing=(), status="active", trigger="release",
           content=None, kind="file_glob"):
    # The body quotes every step by default, so the fixture carries no spec
    # drift unless a test asks for it.
    return _Point(pid, {
        "memory_type": "skill", "skill_status": status, "trigger": trigger,
        "content": content if content is not None
        else "## Steps\n" + "\n".join(steps),
        "step_specs": [
            {"id": sid, "text": sid, "kind": kind,
             "pattern": f"{sid}.py" if kind == "file_glob" else "",
             "load_bearing": sid in load_bearing}
            for sid in steps
        ],
    })


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


@pytest.fixture
def raw_r():
    """The client PRODUCTION builds: `redis.asyncio.from_url(REDIS_URL)` with no
    `decode_responses`, so every read comes back as bytes."""
    return fr.FakeRedis()


async def _exec(client, session, skill, observed, agent="ag", settings=None):
    s = settings or _Settings()
    for step_id in observed:
        await store.record_observation(
            client, s, session_id=session, skill_id=skill, step_id=step_id,
            action_id="x", target="t", agent_id=agent, adapter="shell-hook")


async def _no_outcome(replay_r, sid):
    return None


# ---------------------------------------------------------------------------
# The Redis client production actually hands this module
# ---------------------------------------------------------------------------

class TestTheStoreDoesNotAssumeADecodingRedisClient:
    @pytest.mark.asyncio
    async def test_a_second_observation_extends_the_same_execution(self, raw_r):
        """`raw.get("exec_id")` on a bytes-keyed dict is None, so every
        observation minted a fresh exec_id and overwrote `observed` with the one
        step it had just seen — the corpus the whole feature rests on."""
        s = _Settings()
        e1 = await store.record_observation(
            raw_r, s, session_id="sess", skill_id="s1", step_id="a",
            action_id="act1", target="a.py", agent_id="ag", adapter="shell-hook")
        e2 = await store.record_observation(
            raw_r, s, session_id="sess", skill_id="s1", step_id="b",
            action_id="act2", target="b.py", agent_id="ag", adapter="shell-hook")
        assert e1 == e2
        ex = await store.get_execution(raw_r, "sess", "s1")
        assert set(ex["observed"]) == {"a", "b"}
        assert ex["skill_id"] == "s1"

    @pytest.mark.asyncio
    async def test_the_receipts_endpoint_can_find_its_own_executions(self, raw_r):
        await _exec(raw_r, "sess", "s1", ["a"])
        recs = await store.iter_executions(raw_r)
        assert [rec.get("skill_id") for rec in recs] == ["s1"]

    @pytest.mark.asyncio
    async def test_dismiss_actually_removes_the_proposal(self, raw_r):
        """It returned True — and wrote the surviving list to
        `proc:proposals:b's1'`, leaving the real key untouched."""
        await store.write_proposals(raw_r, "s1", [
            {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
             "detail": "d"},
        ])
        assert await store.dismiss_proposal(raw_r, "p1") is True
        assert await store.list_proposals(raw_r, "s1") == []
        assert [k for k in await raw_r.keys("proc:proposals:*")
                if b"b'" in k] == []

    @pytest.mark.asyncio
    async def test_listing_every_proposal_resolves_its_owners(self, raw_r):
        await store.write_proposals(raw_r, "s1", [
            {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
             "detail": "d"},
        ])
        assert [p["id"] for p in await store.list_proposals(raw_r)] == ["p1"]

    @pytest.mark.asyncio
    async def test_an_observed_earlier_step_produces_no_warning(self, raw_r):
        """The user-visible end of it: a false 'you skipped a load-bearing step'
        for a step the agent performed a moment earlier in the same session."""
        await raw_r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "dep change", "step_id": "a",
             "step_text": "regenerate the lock", "pattern": "*.lock",
             "load_bearing": True, "order": 0},
            {"skill_id": "s1", "skill_trigger": "dep change", "step_id": "b",
             "step_text": "edit requirements", "pattern": "requirements.txt",
             "load_bearing": False, "order": 1},
        ]))
        obs = ProcedureObserver(get_redis=lambda: raw_r,
                                settings_fn=lambda: _Settings())
        await obs.observe(_req(target="poetry.lock"))
        assert await obs.observe(_req(target="requirements.txt")) == []


def _req(target="requirements.txt", type_="edit_file", session="sess"):
    return ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type=type_, target=target),
    )


# ---------------------------------------------------------------------------
# G2 — the index must not be silently truncated
# ---------------------------------------------------------------------------

class TestTheIndexIsNotSilentlyCapped:
    @pytest.mark.asyncio
    async def test_more_than_one_page_of_skills_is_indexed(self, r):
        points = [_skill(f"s{i}", ["a"]) for i in range(1200)]
        vector = _Vector(points, page_size=500)
        n = await store.rebuild_index(vector, r, _Settings())
        assert n == 1200
        assert len(await store.load_index(r)) == 1200
        assert vector._client.scrolls > 1


# ---------------------------------------------------------------------------
# G1 — the receipt must be joinable to the replay event
# ---------------------------------------------------------------------------

class TestTheObservationReceiptCarriesTheActionId:
    @pytest.mark.asyncio
    async def test_the_minted_action_id_reaches_the_execution_record(self, r):
        await r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "b",
             "step_text": "edit requirements", "pattern": "requirements.txt",
             "load_bearing": False, "order": 0},
        ]))

        class _Decision:
            action = "allow"
            risk_score = 0.0
            reasons: list = []
            signals: dict = {}

        class _Engine:
            async def evaluate(self, ctx):
                return _Decision()

        async def _no(*a, **k):
            return False

        async def _emit(**kw):
            return None

        svc = AgentGatewayService(
            policy_engine=_Engine(), recent_failure_check=_no,
            fastpath_check=_no, session_touched_check=_no,
            replay_emitter=_emit, rethink_counter=RethinkCounter(r),
            prediction_redis=r, fastpath_redis=r, policy_decision_redis=r,
            procedure_observer=ProcedureObserver(
                get_redis=lambda: r, settings_fn=lambda: _Settings()),
        )
        resp = await svc.decide(_req())
        ex = await store.get_execution(r, "sess", "s1")
        assert ex["observed"]["b"][0]["action_id"] == resp.action_id
        assert resp.action_id


# ---------------------------------------------------------------------------
# G3 — one warn-once mechanism, not two
# ---------------------------------------------------------------------------

class TestThereIsOnlyOneWarnOnceMechanism:
    @pytest.mark.asyncio
    async def test_the_execution_record_carries_no_dead_warned_field(self, r):
        await _exec(r, "sess", "s1", ["a"])
        raw = await r.hgetall(store.exec_key("sess", "s1"))
        assert "warned" not in raw
        assert "warned" not in await store.get_execution(r, "sess", "s1")


# ---------------------------------------------------------------------------
# G4 — spec_drift, the third proposal kind
# ---------------------------------------------------------------------------

class TestSpecDrift:
    @pytest.mark.asyncio
    async def test_a_spec_whose_text_left_the_body_is_proposed_for_recompile(
            self, r, monkeypatch):
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        point = _Point("s1", {
            "memory_type": "skill", "skill_status": "active", "trigger": "release",
            "content": "## Steps\n1. bump the version\n2. tag the release",
            "step_specs": [
                {"id": "a", "text": "bump the version", "kind": "file_glob",
                 "pattern": "*.toml", "load_bearing": False},
                {"id": "b", "text": "regenerate the lock", "kind": "file_glob",
                 "pattern": "*.lock", "load_bearing": False},
            ],
        })
        await harden.run_pass(r, None, _Vector([point]), _Settings())
        props = await store.list_proposals(r, "s1")
        drift = [p for p in props if p["kind"] == "spec_drift"]
        assert [p["step_id"] for p in drift] == ["b"], props

    @pytest.mark.asyncio
    async def test_drift_is_reported_for_an_all_unobservable_procedure_too(
            self, r, monkeypatch):
        """Nothing about drift needs a matcher: the body was edited without the
        specs whether or not round 1 can watch the steps."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        point = _Point("s9", {
            "memory_type": "skill", "skill_status": "active", "trigger": "deploy",
            "content": "## Steps\n1. ssh to the box",
            "step_specs": [
                {"id": "z", "text": "run the migration", "kind": "unobservable",
                 "pattern": "", "load_bearing": False},
            ],
        })
        await harden.run_pass(r, None, _Vector([point]), _Settings())
        assert [p["kind"] for p in await store.list_proposals(r, "s9")] == ["spec_drift"]

    @pytest.mark.asyncio
    async def test_drift_needs_no_executions_at_all(self, r, monkeypatch):
        """Tier A is available from the first execution — and spec_drift from
        none, because it compares two stored things and asks nothing of a run."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        result = await harden.run_pass(
            r, None,
            _Vector([_skill("s1", ["regenerate-the-lockfile"],
                            content="## Steps\n1. bump the version")]),
            _Settings())
        assert result["executions"] == 0
        assert [p["kind"] for p in await store.list_proposals(r, "s1")] == ["spec_drift"]


# ---------------------------------------------------------------------------
# G5 — the run record, and the closed-Tier-B state reaching a human
# ---------------------------------------------------------------------------

class TestTheRunRecordReachesAHuman:
    @pytest.mark.asyncio
    async def test_the_pass_persists_what_it_did(self, r, monkeypatch):
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await _exec(r, "s-1", "s1", ["a"])
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a"])]), _Settings())
        run = await store.get_run(r)
        assert run["tier_b"] == "insufficient outcome signal"
        assert run["last_run"]
        assert run["health"] == "ok"

    @pytest.mark.asyncio
    async def test_the_rollup_surfaces_it(self, r, monkeypatch):
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await _exec(r, "s-1", "s1", ["a"])
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a"])]), _Settings())
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert body["run"]["tier_b"] == "insufficient outcome signal"

    @pytest.mark.asyncio
    async def test_a_deployment_where_the_pass_never_ran_says_so(self, r):
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert body["run"]["health"] == "unknown"
        assert body["run"]["last_run"] is None


def _client_for(client_r):
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: client_r, get_vector=lambda: None,
        settings_fn=lambda: _Settings(),
    ))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# ---------------------------------------------------------------------------
# G6 — verdicts decay to neutral, they do not ratchet
# ---------------------------------------------------------------------------

class TestVerdictsDecayToNeutral:
    @pytest.mark.asyncio
    async def test_a_procedure_that_went_quiet_loses_its_proposals_and_counts(
            self, r, monkeypatch):
        """The skill is still indexed; only its evidence aged out. `tallies`
        never contains it again, so nothing in the write loop could reach it."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await store.write_proposals(r, "s1", [
            {"id": "old", "kind": "dead_step", "skill_id": "s1", "step_id": "a",
             "detail": "skipped in 11 of 12 executions. Remove it?"},
        ])
        await store.write_step_stats(r, _Settings(), "s1", {
            "a": {"observed": 1, "skipped": 11, "executions": 12},
        })
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a"])]), _Settings())
        assert await store.list_proposals(r, "s1") == []
        assert await store.get_step_stats(r, "s1") == {}

    @pytest.mark.asyncio
    async def test_a_skill_that_left_the_index_leaves_no_orphans(
            self, r, monkeypatch):
        """Deleted, drafted, or its specs removed — the index is derived from
        Qdrant, so the skill simply stops appearing and its keys were stranded
        with no TTL and no sweep."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        await store.write_proposals(r, "gone", [
            {"id": "p9", "kind": "dead_step", "skill_id": "gone", "step_id": "a",
             "detail": "d"},
        ])
        await store.write_step_stats(r, _Settings(), "gone", {
            "a": {"observed": 4, "skipped": 0, "executions": 4},
        })
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a"])]), _Settings())
        assert await store.list_proposals(r, "gone") == []
        assert await store.get_step_stats(r, "gone") == {}
        assert await store.list_proposals(r) == []


# ---------------------------------------------------------------------------
# Tier B: knowability is not discrimination
# ---------------------------------------------------------------------------

class TestTierBNeedsASignalThatDiscriminates:
    @pytest.mark.asyncio
    async def test_a_uniformly_successful_corpus_keeps_the_gate_shut(
            self, r, monkeypatch):
        """`post_tool` defaults `success` to True and the reconcile emit now
        stamps it, so `_failure_rate` is 0.0 and EVERY session resolves as a
        success. Counting knowable outcomes then opens the gate on a signal that
        distinguishes nothing, and the efficacy comparison degenerates into a
        Beta-prior artefact of bucket size — which is exactly the 'every step
        looks dead' harm the two-tier split exists to prevent (spec §F1)."""
        await _exec(r, "s-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "s-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "s-3", "s1", ["b"], agent="ag3")
        await _exec(r, "s-4", "s1", ["b"], agent="ag4")

        async def _all_good(replay_r, sid):
            return True

        monkeypatch.setattr(harden, "_resolve_outcome", _all_good)
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a", "b"])]), _Settings())

        assert result["tier_b"] == "uniform outcome signal"
        kinds = {p["kind"] for p in await store.list_proposals(r, "s1")}
        assert not kinds & {"dead_step", "load_bearing"}, kinds

    @pytest.mark.asyncio
    async def test_a_signal_with_both_outcomes_still_opens_it(self, r, monkeypatch):
        await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
        await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
        await _exec(r, "bad-1", "s1", ["b"], agent="ag3")
        await _exec(r, "bad-2", "s1", ["b"], agent="ag4")

        async def _outcome(replay_r, sid):
            return sid.startswith("ok")

        monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a", "b"])]), _Settings())
        assert result["tier_b"] == "open"


# ---------------------------------------------------------------------------
# I2 must be checked against the steps the procedure HAS
# ---------------------------------------------------------------------------

class TestStaleStepIdsCannotFabricateSkips:
    @pytest.mark.asyncio
    async def test_an_execution_naming_only_dead_step_ids_votes_on_nothing(
            self, r, monkeypatch):
        """A spec edit re-keys the steps (the ids are minted server-side and no
        surface ever returns them), so every stored execution names ids the
        procedure no longer has. I2 read 'this execution observed something',
        which those satisfy — and then every CURRENT step was tallied skipped."""
        monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
        for i in range(40):
            await _exec(r, f"s-{i}", "s1", ["retired-id"])
        await harden.run_pass(r, None, _Vector([_skill("s1", ["a", "b"])]),
                              _Settings())
        stats = await store.get_step_stats(r, "s1")
        assert stats.get("a", {}).get("skipped", 0) == 0
        assert stats.get("b", {}).get("skipped", 0) == 0


# ---------------------------------------------------------------------------
# I5 — the pre-edit path is bounded
# ---------------------------------------------------------------------------

class TestMatchingIsBoundedInTargetLength:
    def test_a_pathological_target_does_not_rebuild_every_suffix(self, monkeypatch):
        """`"/".join(parts[i:])` inside a loop over every segment is O(n^2) per
        index entry, on the blocking pre-edit path, against a `target` that
        carries no max_length (its sibling `preview` is capped at 2048).
        Measured before the fix: 20k segments took 2.75s for ONE entry."""
        import fnmatch as _fn

        calls = []
        real = _fn.fnmatch

        def _counting(name, pat):
            calls.append(1)
            return real(name, pat)

        monkeypatch.setattr(match.fnmatch, "fnmatch", _counting)
        idx = [{"skill_id": "s1", "step_id": "a", "pattern": "client/pyproject.toml",
                "step_text": "t", "load_bearing": False, "order": 0}]
        assert match.match_target(idx, "a/" * 5000 + "pyproject.toml") == []
        assert len(calls) <= 40, len(calls)

    def test_a_repo_relative_pattern_still_matches_an_absolute_path(self):
        idx = [{"skill_id": "s1", "step_id": "a", "pattern": "client/pyproject.toml",
                "step_text": "t", "load_bearing": False, "order": 0}]
        assert match.match_target(
            idx, "E:/Documents/Projects/Firekeep/client/pyproject.toml")


# ---------------------------------------------------------------------------
# The rollup must describe the deployment it is looking at
# ---------------------------------------------------------------------------

class TestTheRollupDescribesEveryProcedure:
    @pytest.mark.asyncio
    async def test_an_all_unobservable_procedure_is_present_not_absent(self, r):
        """H2 says the product must say '0 of 7 steps observable'. Deriving the
        rows from the matcher index means a procedure with no file_glob spec
        produces no row and specs_total 0 — and the dashboard then renders the
        cold-start message at a human who has just compiled the specs."""
        await store.rebuild_index(
            _Vector([_skill("s9", ["x", "y"], kind="unobservable")]), r, _Settings())
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert body["specs_total"] == 2
        row = body["procedures"][0]
        assert row["skill_id"] == "s9"
        assert row["observable_steps"] == 0
        assert row["spec_count"] == 2

    @pytest.mark.asyncio
    async def test_the_execution_count_is_the_live_one_not_last_nights(self, r):
        """`GET /procedures/{id}/executions` reads the records; the rollup read
        the nightly stats blob, so for up to a full schedule interval the two
        endpoints answered 'has this ever run?' differently — and the rollup was
        the one that was wrong."""
        await store.rebuild_index(_Vector([_skill("s1", ["a"])]), r, _Settings())
        await _exec(r, "s-1", "s1", ["a"])
        await _exec(r, "s-2", "s1", ["a"])
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert body["procedures"][0]["executions"] == 2


# ---------------------------------------------------------------------------
# Spec §4 Stage 2: the unjoinable drop "is counted and surfaced, not hidden"
# ---------------------------------------------------------------------------

class TestUnjoinableWorkIsCounted:
    @pytest.mark.asyncio
    async def test_a_recognised_edit_under_an_unusable_session_is_counted(self, r):
        await r.set(store.INDEX_KEY, json.dumps([
            {"skill_id": "s1", "skill_trigger": "t", "step_id": "a",
             "step_text": "x", "pattern": "requirements.txt",
             "load_bearing": False, "order": 0},
        ]))
        obs = ProcedureObserver(get_redis=lambda: r, settings_fn=lambda: _Settings())
        assert await obs.observe(_req(session="unknown")) == []
        assert await obs.observe(_req(session="")) == []
        async with _client_for(r) as client:
            body = (await client.get("/procedures")).json()
        assert body["unjoinable_edits"] == 2

    @pytest.mark.asyncio
    async def test_a_cold_deployment_writes_nothing_on_the_edit_path(self, r):
        """The counter must not turn every edit on a spec-less deployment into a
        Redis write: the drop is only interesting once work was RECOGNISED."""
        obs = ProcedureObserver(get_redis=lambda: r, settings_fn=lambda: _Settings())
        assert await obs.observe(_req(session="unknown")) == []
        assert await r.keys("proc:*") == []


# ---------------------------------------------------------------------------
# One execution's observation list is bounded
# ---------------------------------------------------------------------------

class TestTheObservationListIsBounded:
    @pytest.mark.asyncio
    async def test_a_long_session_does_not_grow_the_hash_without_limit(self, r):
        """The whole blob is HGETALL'd, parsed, appended to and re-serialised on
        every matching edit, so the pre-edit cost is quadratic in the number of
        matches in one session. The values feed nothing — every consumer reads
        the KEYS — so the cap loses nothing, provided the true count survives."""
        s = _Settings()
        for i in range(200):
            await store.record_observation(
                r, s, session_id="sess", skill_id="s1", step_id="a",
                action_id=f"act{i}", target=f"f{i}.py", agent_id="ag",
                adapter="shell-hook")
        ex = await store.get_execution(r, "sess", "s1")
        assert len(ex["observed"]["a"]) <= store.MAX_OBSERVATIONS_PER_STEP
        assert ex["observed_counts"]["a"] == 200


class TestAnOpenGateIsNotTheSameAsAReachableVerdict:
    @pytest.mark.asyncio
    async def test_the_run_record_says_how_many_steps_could_be_decided(
            self, r, monkeypatch):
        """`PROCEDURE_AGENT_CAP` is spent across BOTH buckets while a verdict
        needs `PROCEDURE_MIN_EXECUTIONS` in EACH, and both default to 5 — so no
        step can be decided by fewer than two distinct agent identities. That is
        deliberate (the cap exists so one identity cannot decide a team's
        procedure) but it was invisible: 'open, zero proposals, forever' read
        exactly like 'open and nothing found'."""
        await _exec(r, "ok-1", "s1", ["a", "b"], agent="solo")
        await _exec(r, "bad-1", "s1", ["b"], agent="solo")

        async def _outcome(replay_r, sid):
            return sid.startswith("ok")

        monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
        result = await harden.run_pass(
            r, None, _Vector([_skill("s1", ["a", "b"])]), _Settings())
        assert result["tier_b"] == "open"
        assert result["verdict_ready_steps"] == 0
        assert (await store.get_run(r))["verdict_ready_steps"] == 0

    @pytest.mark.asyncio
    async def test_a_record_written_before_the_removal_drops_it_on_read(self, r):
        """One shape, not two: a legacy hash still carries the field, and a
        reader that sees it sometimes is a reader that can come to trust it."""
        await _exec(r, "sess", "s1", ["a"])
        await r.hset(store.exec_key("sess", "s1"), "warned", '{"a": "2026-01-01"}')
        assert "warned" not in await store.get_execution(r, "sess", "s1")
