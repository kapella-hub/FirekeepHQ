"""Outcome-Weighted Memory: recall ranked by whether the sessions that recalled a
memory SUCCEEDED — the join of replay memory_read events (which sessions saw which
memories) to session outcomes (auto-evals + Bridge status), shrunk toward neutral
so small N can never swing rankings.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.owm import compute_efficacy, run_pass, session_success


# --------------------------------------------------------------------------- #
# Efficacy math: Bayesian shrinkage toward neutral 0.5                        #
# --------------------------------------------------------------------------- #


def test_efficacy_neutral_at_zero_evidence():
    assert compute_efficacy(0, 0, prior_n=5) == 0.5


def test_efficacy_shrinks_small_samples_toward_neutral():
    # 1/1 successful is nowhere near 1.0 with a prior of 5
    e = compute_efficacy(1, 1, prior_n=5)
    assert 0.5 < e < 0.62


def test_efficacy_converges_with_evidence():
    assert compute_efficacy(20, 20, prior_n=5) > 0.85
    assert compute_efficacy(0, 20, prior_n=5) < 0.15


# --------------------------------------------------------------------------- #
# Session success signal                                                      #
# --------------------------------------------------------------------------- #


def test_abandoned_session_is_failure_regardless_of_metrics():
    ev = {"task_result": "success", "task_result_source": "self_reported"}
    assert session_success(ev, "abandoned") is False


def test_grades_come_from_the_recognized_pair_only():
    g = {"task_result_source": "self_reported"}
    assert session_success({"task_result": "success", **g}, "completed") is True
    assert session_success({"task_result": "failure", **g}, "completed") is False
    assert session_success({"task_result": "partial", **g}, "completed") is None


def test_a_sourceless_grade_is_not_evidence():
    assert session_success({"task_result": "success"}, "completed") is None
    assert session_success({"task_result": "success",
                            "task_result_source": "vibes"}, "completed") is None


def test_legacy_records_are_unknown_never_success():
    legacy = {"outcome": "success", "metrics": {"failure_rate": 0.0}}
    assert session_success(legacy, "completed") is None
    assert session_success({"metrics": {}}, None) is None


# --------------------------------------------------------------------------- #
# The batch join                                                              #
# --------------------------------------------------------------------------- #


def _redis_with(evals: dict, index: list):
    r = AsyncMock()
    r.zrangebyscore = AsyncMock(return_value=list(index))
    async def _get(key):
        sid = key.split("rp:eval:", 1)[-1]
        data = evals.get(sid)
        return json.dumps(data) if data is not None else None
    r.get = AsyncMock(side_effect=_get)
    return r


def _vector(point_types=None, stale_scored_ids=()):
    """point_types: id -> payload dict returned by retrieve (memory_type/source
    checks); stale_scored_ids: ids the scroll-for-scored pass reports."""
    v = MagicMock()
    v._client = MagicMock()
    v._client.set_payload = AsyncMock()
    v._client.delete_payload = AsyncMock()
    v.close = AsyncMock()

    async def _retrieve(collection_name, ids, with_payload=True, with_vectors=False):
        out = []
        for pid in ids:
            pt = MagicMock()
            pt.id = pid
            pt.payload = (point_types or {}).get(pid, {"memory_type": "episodic"})
            out.append(pt)
        return out
    v._client.retrieve = AsyncMock(side_effect=_retrieve)

    async def _scroll(collection_name, scroll_filter=None, limit=1000,
                      offset=None, with_payload=False, with_vectors=False):
        pts = []
        for pid in stale_scored_ids:
            pt = MagicMock()
            pt.id = pid
            pts.append(pt)
        return (pts, None)
    v._client.scroll = AsyncMock(side_effect=_scroll)
    return v


def _settings(**over):
    s = MagicMock()
    s.OWM_PRIOR_N = 5
    s.OWM_WINDOW_DAYS = 30
    s.OWM_AGENT_CAP = 5
    s.QDRANT_COLLECTION = "memories"
    for k, val in over.items():
        setattr(s, k, val)
    return s


def _events_fn(events_by_sid):
    """Task 4 (outcome truth PR2 D3): mirrors the events_fn seam's real
    return — a plain list of event dicts for the whole (capped) session, no
    envelope. `run_pass` applies the memory_read filter itself; this fixture
    serves the FULL per-session event list, unfiltered, matching what a real
    events_fn (get_session_event_ids + get_event_batch) would hand back."""
    async def fn(r, sid):
        return events_by_sid.get(sid, [])
    return fn


@pytest.mark.asyncio
async def test_run_pass_joins_reads_to_outcomes_and_writes_payloads():
    evals = {
        "s-good": {"task_result": "success", "task_result_source": "self_reported"},
        "s-bad": {"task_result": "failure", "task_result_source": "self_reported"},
    }
    events = {
        "s-good": [{"event_type": "memory_read",
                    "payload": {"memory_ids": ["m1", "m2"]}}],
        "s-bad": [{"event_type": "memory_read",
                   "payload": {"memory_ids": ["m1"]}}],
    }
    v = _vector()
    out = await run_pass(_redis_with(evals, ["s-good", "s-bad"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))

    assert out["sessions_joined"] == 2
    assert out["memories_scored"] == 2
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    # m1: 1 success / 2 sessions -> shrunk below neutral-ish midpoint
    assert writes["m1"]["owm_n"] == 2
    assert abs(writes["m1"]["owm_efficacy"] - (1 + 2.5) / (2 + 5)) < 1e-9
    # m2: 1/1 success -> above neutral, shrunk
    assert writes["m2"]["owm_n"] == 1
    assert writes["m2"]["owm_efficacy"] > 0.5
    assert "owm_updated_at" in writes["m1"]


@pytest.mark.asyncio
async def test_run_pass_counts_a_session_once_per_memory():
    """One session recalling the same memory five times is ONE observation."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [
        {"event_type": "memory_read", "payload": {"memory_ids": ["m1"]}},
        {"event_type": "memory_read", "payload": {"memory_ids": ["m1", "m1"]}},
    ]}
    v = _vector()
    await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                   bridge_statuses={}, events_fn=_events_fn(events))
    payload = v._client.set_payload.call_args_list[0].kwargs["payload"]
    assert payload["owm_n"] == 1


@pytest.mark.asyncio
async def test_run_pass_skips_dangling_index_and_legacy_events():
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {
        "s1": [{"event_type": "memory_read", "payload": {"query": "old event, no ids"}}],
        "s-expired": [{"event_type": "memory_read", "payload": {"memory_ids": ["mX"]}}],
    }
    v = _vector()
    out = await run_pass(_redis_with(evals, ["s1", "s-expired"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    # s-expired's eval GET returns nil (30d TTL) -> skipped; s1 has no ids
    assert out["memories_scored"] == 0
    assert not v._client.set_payload.called


@pytest.mark.asyncio
async def test_run_pass_excludes_ambiguous_sessions():
    evals = {"s-mid": {"task_result": "partial", "task_result_source": "self_reported"}}
    events = {"s-mid": [{"event_type": "memory_read", "payload": {"memory_ids": ["m1"]}}]}
    v = _vector()
    out = await run_pass(_redis_with(evals, ["s-mid"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 0


@pytest.mark.asyncio
async def test_run_pass_bridge_abandoned_overrides_good_metrics():
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read", "payload": {"memory_ids": ["m1"]}}]}
    v = _vector()
    await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                   bridge_statuses={"s1": "abandoned"}, events_fn=_events_fn(events))
    payload = v._client.set_payload.call_args_list[0].kwargs["payload"]
    assert payload["owm_efficacy"] < 0.5  # counted as a failure observation


@pytest.mark.asyncio
async def test_run_pass_survives_a_set_payload_error(caplog):
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read", "payload": {"memory_ids": ["gone", "m2"]}}]}
    v = _vector()
    async def boom(collection_name, payload, points):
        if points == ["gone"]:
            raise RuntimeError("point deleted")
    v._client.set_payload = AsyncMock(side_effect=boom)
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1  # m2 still written
    assert out["write_errors"] == 1


# --------------------------------------------------------------------------- #
# Review hardening (wf_51dd7c4e)                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stale_scored_memories_reset_to_neutral():
    """A memory scored last month but absent from this run's evidence must have
    its OWM keys DELETED (back to neutral) — otherwise a bad week becomes a
    permanent, self-reinforcing penalty (recall downrank -> never recalled ->
    never earns recovery evidence)."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read", "payload": {"memory_ids": ["m1"]}}]}
    v = _vector(stale_scored_ids=("m1", "m-old"))
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    assert out["stale_reset"] == 1
    del_call = v._client.delete_payload.call_args_list[0].kwargs
    assert del_call["points"] == ["m-old"]
    assert set(del_call["keys"]) == {"owm_efficacy", "owm_n", "owm_updated_at"}


@pytest.mark.asyncio
async def test_corpus_and_skill_points_are_never_scored():
    """Corpus chunks and skills surface through the same vector search, so their
    ids land in memory_ids — but outcome-scoring playbooks/documents by ambient
    session failure is meaningless and pollutes their payloads."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read",
                      "payload": {"memory_ids": ["m1", "sk1", "c1"]}}]}
    v = _vector(point_types={
        "m1": {"memory_type": "episodic"},
        "sk1": {"memory_type": "skill"},
        "c1": {"source": "corpus"},
    })
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    written = [c.kwargs["points"][0] for c in v._client.set_payload.call_args_list]
    assert written == ["m1"]


@pytest.mark.asyncio
async def test_single_agent_contribution_is_capped_per_memory():
    """One identity's failing loop (CI bot, 30 bad sessions overnight) must not
    be able to bury a shared memory: per (memory, agent) observations cap at
    OWM_AGENT_CAP."""
    evals = {f"s{i}": {"task_result": "failure", "task_result_source": "self_reported"}
             for i in range(10)}
    events = {f"s{i}": [{"event_type": "memory_read", "agent_id": "ci-bot",
                         "payload": {"memory_ids": ["m1"]}}] for i in range(10)}
    v = _vector()
    s = _settings(OWM_AGENT_CAP=3)
    await run_pass(_redis_with(evals, [f"s{i}" for i in range(10)]), v, s,
                   bridge_statuses={}, events_fn=_events_fn(events))
    payload = v._client.set_payload.call_args_list[0].kwargs["payload"]
    assert payload["owm_n"] == 3  # capped, not 10


@pytest.mark.asyncio
async def test_run_pass_late_memory_read_beyond_old_1000_window():
    """Task 4 (outcome truth PR2 D3): the old get_session_timeline(
    event_type="memory_read", limit=1000) fetch applied the type filter AFTER
    pagination, so a memory_read past the oldest-1000 window never joined.
    Seed >1000 filler events + a late memory_read + a recognized grade, and
    confirm the late read's memory still reaches the join output."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    filler = [{"event_type": "ctx_update", "payload": {}} for _ in range(1200)]
    late_read = {"event_type": "memory_read", "payload": {"memory_ids": ["late-mem"]}}
    events = {"s1": filler + [late_read]}
    v = _vector()
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    written = [c.kwargs["points"][0] for c in v._client.set_payload.call_args_list]
    assert written == ["late-mem"]
