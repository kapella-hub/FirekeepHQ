"""Tier A (frequency, no outcome needed) and Tier B (efficacy, gated hard).

The gate matters more than the arithmetic: measured on this repo, no production
emitter passes outcome= to replay except Bridge's session lifecycle, so
_failure_rate is 0.0 and effectively every session reads as a success. A pass
that trusted that would find every step dead and propose deleting the procedure.

The Qdrant double here HONOURS its scroll_filter. Every pre-existing fake in
this suite ignores it, and the pass rebuilds the matcher index from Qdrant on
every run — so against a filter-ignoring fake a draft skill would silently enter
the index and the test would pass while proving nothing.
"""
import json

import pytest
import fakeredis.aioredis as fr

from app.procedures import harden, store


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_MIN_EXECUTIONS = 2
    PROCEDURE_PRIOR_N = 5
    PROCEDURE_EFFICACY_DELTA = 0.15
    PROCEDURE_WINDOW_DAYS = 30
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_AGENT_CAP = 5
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FilterHonouringQdrant:
    """Applies must-FieldCondition MatchValue filters for real."""

    def __init__(self, points):
        self._points = points

    async def scroll(self, *, collection_name, scroll_filter=None, limit=1000, **kw):
        pts = self._points
        if scroll_filter is not None:
            for cond in scroll_filter.must or []:
                pts = [p for p in pts
                       if (p.payload or {}).get(cond.key) == cond.match.value]
        return pts[:limit], None


class _Vector:
    def __init__(self, points=()):
        self._client = _FilterHonouringQdrant(list(points))


def _skill(pid, steps, *, load_bearing=(), status="active", trigger="release",
           content=None):
    # The body QUOTES every step, so this fixture carries no spec_drift. Without
    # a `content` key the pass correctly reports every spec as drifted (its text
    # cannot appear in a body that does not exist), which is a true finding about
    # a fixture rather than about the behaviour these cases are asserting.
    return _Point(pid, {
        "memory_type": "skill", "skill_status": status, "trigger": trigger,
        "content": content if content is not None else "## Steps\n" + "\n".join(steps),
        "step_specs": [
            {"id": sid, "text": sid, "kind": "file_glob",
             "pattern": f"{sid}.py", "load_bearing": sid in load_bearing}
            for sid in steps
        ],
    })


def _vector(steps, **kw):
    """The pass derives its index from Qdrant, so the fixture that used to seed
    proc:index directly cannot be used: run_pass rebuilds (and therefore
    overwrites) the index before it reads it. Seed the store instead."""
    return _Vector([_skill("s1", steps, **kw)])


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


async def _exec(r, session, skill, observed, agent="ag"):
    s = _Settings()
    for step_id in observed:
        await store.record_observation(
            r, s, session_id=session, skill_id=skill, step_id=step_id,
            action_id="x", target="t", agent_id=agent, adapter="shell-hook")


async def _no_outcome(replay_r, sid):
    return None


@pytest.mark.asyncio
async def test_tier_a_counts_without_any_outcome_signal(r, monkeypatch):
    await _exec(r, "s-1", "s1", ["a", "b"])
    await _exec(r, "s-2", "s1", ["a"])          # b skipped, a observed => I2 satisfied

    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)

    result = await harden.run_pass(r, None, _vector(["a", "b"]), _Settings())
    stats = await store.get_step_stats(r, "s1")
    assert stats["a"]["observed"] == 2
    assert stats["b"]["observed"] == 1
    assert stats["b"]["skipped"] == 1
    assert stats["a"]["executions"] == 2
    assert result["tier_b"] == "insufficient outcome signal"


@pytest.mark.asyncio
async def test_i2_an_execution_with_no_sibling_evidence_counts_no_skips(r, monkeypatch):
    """The kiro / shell-only / personal-mode case: nothing was observed, so
    nothing was skipped. Without this, those sessions vote to delete every step."""
    key = store.exec_key("s-1", "s1")
    await r.hset(key, mapping={"exec_id": "e", "skill_id": "s1", "session_id": "s-1",
                               "agent_id": "ag", "adapter": "shell-hook",
                               "observed": "{}", "warned": "{}"})
    await r.sadd("proc:exec:__index", key)

    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await harden.run_pass(r, None, _vector(["a", "b"]), _Settings())

    stats = await store.get_step_stats(r, "s1")
    assert stats.get("a", {}).get("skipped", 0) == 0
    assert stats.get("b", {}).get("skipped", 0) == 0


@pytest.mark.asyncio
async def test_tier_b_stays_closed_without_enough_knowable_outcomes(r, monkeypatch):
    await _exec(r, "s-1", "s1", ["a"])
    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    result = await harden.run_pass(r, None, _vector(["a", "b"]), _Settings())
    assert await store.list_proposals(r, "s1") == []
    assert result["tier_b"] == "insufficient outcome signal"


@pytest.mark.asyncio
async def test_tier_b_proposes_load_bearing_when_skipping_predicts_failure(r, monkeypatch):
    # a observed + success, twice; a skipped + failure, twice
    await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
    await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
    await _exec(r, "bad-1", "s1", ["b"], agent="ag3")
    await _exec(r, "bad-2", "s1", ["b"], agent="ag4")

    async def _outcome(replay_r, sid):
        return True if sid.startswith("ok") else False

    monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
    await harden.run_pass(r, None, _vector(["a", "b"]), _Settings())

    props = await store.list_proposals(r, "s1")
    kinds = {(p["kind"], p["step_id"]) for p in props}
    assert ("load_bearing", "a") in kinds


@pytest.mark.asyncio
async def test_a_step_already_declared_load_bearing_is_not_proposed_again(r, monkeypatch):
    await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
    await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
    await _exec(r, "bad-1", "s1", ["b"], agent="ag3")
    await _exec(r, "bad-2", "s1", ["b"], agent="ag4")

    async def _outcome(replay_r, sid):
        return sid.startswith("ok")

    monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
    await harden.run_pass(
        r, None, _vector(["a", "b"], load_bearing=("a",)), _Settings())

    props = await store.list_proposals(r, "s1")
    assert all(p["kind"] != "load_bearing" for p in props), props


@pytest.mark.asyncio
async def test_one_agent_cannot_decide_a_procedure(r, monkeypatch):
    """PROCEDURE_AGENT_CAP: a CI identity looping must not bury a step."""
    for i in range(20):
        await _exec(r, f"bot-{i}", "s1", ["b"], agent="ci-bot")

    async def _outcome(replay_r, sid):
        return False

    monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
    await harden.run_pass(r, None, _vector(["a", "b"]), _Settings())

    stats = await store.get_step_stats(r, "s1")
    assert stats["a"]["skipped_scored"] <= _Settings.PROCEDURE_AGENT_CAP
    # Tier A is uncapped on purpose: "skipped in 20 of 20 executions" is a true
    # frequency statement. Only the efficacy vote is rate-limited.
    assert stats["a"]["skipped"] == 20


@pytest.mark.asyncio
async def test_proposals_are_replaced_not_accumulated(r, monkeypatch):
    await store.write_proposals(r, "s1", [{"id": "old", "kind": "dead_step",
                                           "step_id": "z", "detail": "d"}])
    await _exec(r, "s-1", "s1", ["a"])
    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await harden.run_pass(r, None, _vector(["a"]), _Settings())
    assert all(p["id"] != "old" for p in await store.list_proposals(r, "s1"))


@pytest.mark.asyncio
async def test_disabled_returns_immediately(r):
    class Off(_Settings):
        PROCEDURE_ENABLED = False
    assert (await harden.run_pass(r, None, _Vector(), Off()))["status"] == "disabled"


@pytest.mark.asyncio
async def test_the_pass_rebuilds_the_index_and_drafts_never_enter_it(r, monkeypatch):
    """The write-path rebuild fires only when step_specs are touched, so a
    draft->active PATCH changes index membership without triggering one. The
    pass must rebuild unconditionally — and must not admit the draft."""
    await r.set(store.INDEX_KEY, json.dumps([]))
    vector = _Vector([
        _skill("s1", ["a"]),
        _skill("s2", ["c"], status="draft"),
    ])
    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await _exec(r, "s-1", "s1", ["a"])
    await _exec(r, "s-2", "s2", ["c"])

    await harden.run_pass(r, None, vector, _Settings())

    assert (await store.get_step_stats(r, "s1")).get("a", {}).get("observed") == 1
    assert await store.get_step_stats(r, "s2") == {}


@pytest.mark.asyncio
async def test_executions_older_than_the_window_are_ignored(r, monkeypatch):
    from datetime import datetime, timedelta, timezone

    await _exec(r, "old", "s1", ["a"])
    stale = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    await r.hset(store.exec_key("old", "s1"),
                 mapping={"opened_at": stale, "last_seen_at": stale})

    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await harden.run_pass(r, None, _vector(["a"]), _Settings())
    assert await store.get_step_stats(r, "s1") == {}


class _Eval:
    def __init__(self, data):
        self._data = data

    def model_dump(self):
        return self._data


@pytest.mark.asyncio
async def test_i4_a_session_with_no_outcome_bearing_event_is_excluded(monkeypatch):
    """_failure_rate returns 0.0 when nothing carries an outcome, and 0.0 reads
    as success — so the eval must not even be consulted."""
    asked: list = []

    async def _timeline(rr, sid, **kw):
        return {"events": [{"outcome": None}, {"outcome": ""}]}

    async def _get_eval(rr, sid):
        asked.append(sid)
        return _Eval({"metrics": {"failure_rate": 0.0}, "outcome": None})

    monkeypatch.setattr("replay.reader.get_session_timeline", _timeline)
    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)

    assert await harden._resolve_outcome(object(), "sess") is None
    assert asked == []


@pytest.mark.asyncio
async def test_i4_one_outcome_bearing_event_admits_the_session(monkeypatch):
    async def _timeline(rr, sid, **kw):
        return {"events": [{"outcome": "success"}]}

    async def _get_eval(rr, sid):
        return _Eval({"metrics": {"failure_rate": 0.0}, "outcome": None})

    monkeypatch.setattr("replay.reader.get_session_timeline", _timeline)
    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)

    assert await harden._resolve_outcome(object(), "sess") is True


@pytest.mark.asyncio
async def test_i4_no_replay_client_means_no_outcome():
    assert await harden._resolve_outcome(None, "sess") is None


@pytest.mark.asyncio
async def test_resolve_outcome_never_raises(monkeypatch):
    async def _boom(rr, sid, **kw):
        raise RuntimeError("replay is down")

    monkeypatch.setattr("replay.reader.get_session_timeline", _boom)
    assert await harden._resolve_outcome(object(), "sess") is None
