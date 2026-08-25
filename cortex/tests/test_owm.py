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


def _vector(point_types=None, stale_scored_ids=(), stale_scored_skill_ids=()):
    """point_types: id -> payload dict returned by retrieve (memory_type/source
    checks); stale_scored_ids: ids the owm_n scroll-for-scored pass reports;
    stale_scored_skill_ids: ids the skill_efficacy_n scroll-for-scored pass
    reports (the two sweeps use different filters, so the fixture inspects
    scroll_filter to route to the right set)."""
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
        try:
            key = scroll_filter.must[0].key
        except Exception:
            key = None
        ids = stale_scored_skill_ids if key == "skill_efficacy_n" else stale_scored_ids
        pts = []
        for pid in ids:
            pt = MagicMock()
            pt.id = pid
            pts.append(pt)
        return (pts, None)
    v._client.scroll = AsyncMock(side_effect=_scroll)
    return v


def _settings(**over):
    s = MagicMock()
    s.OWM_ENABLED = True
    s.OWM_PRIOR_N = 5
    s.OWM_WINDOW_DAYS = 30
    s.OWM_AGENT_CAP = 5
    s.SKILL_OWM_ENABLED = True
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
async def test_corpus_points_are_never_scored_skills_get_a_distinct_field():
    """Corpus chunks and skills surface through the same vector search. Corpus
    stays fully excluded (outcome-scoring a document by ambient session
    failure is meaningless). Skills are no longer dropped (PR3, D2 —
    superseding the old "skills are never scored" assumption this test used
    to assert): they route into a DISTINCT skill_efficacy field so the RAG
    lifecycle scorer, which reads owm_efficacy with no memory_type guard
    (rag.py:1187-1192), never silently re-ranks a skill."""
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
    assert out["skills_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert set(writes) == {"m1", "sk1"}  # c1 (corpus) never written
    assert "owm_efficacy" in writes["m1"]
    assert "skill_efficacy" in writes["sk1"]
    assert "owm_efficacy" not in writes["sk1"]
    assert "skill_efficacy" not in writes["m1"]


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
    """Task 4 (outcome truth PR2 D3): run_pass now applies the memory_read
    type filter in Python over the WHOLE per-session event list (the old
    get_session_timeline(event_type="memory_read", limit=1000) applied it AFTER
    pagination, so a memory_read past the oldest-1000 window never joined).
    This test drives that Python filter via an injected events_fn returning a
    >1000-event list with a late memory_read, and confirms the late read's
    memory reaches the join output. It does NOT exercise the real
    _default_events_fn fetch/cap (get_session_event_ids + get_event_batch,
    _METRIC_SCAN_MAX) — that primitive is covered by the compute.py and
    patterns/extractor.py sibling tests whose fakes honor `limit`."""
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


# --------------------------------------------------------------------------- #
# Skill efficacy (outcome truth PR3, D2): skills route into a DISTINCT        #
# skill_efficacy field instead of being dropped, gated independently by       #
# SKILL_OWM_ENABLED.                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_pass_scores_skill_into_skill_efficacy_field():
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read",
                      "payload": {"memory_ids": ["sk1"]}}]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}})
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["skills_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert writes["sk1"]["skill_efficacy"] == round(compute_efficacy(1, 1, 5), 4)
    assert writes["sk1"]["skill_efficacy_n"] == 1
    assert "skill_efficacy_updated_at" in writes["sk1"]


@pytest.mark.asyncio
async def test_skill_payload_never_carries_owm_efficacy():
    """CRITICAL invariant: rag.py:1187-1192 reads owm_efficacy with NO
    memory_type guard, so a skill point carrying it would have its general-RAG
    recall silently re-ranked. Skills get ONLY skill_efficacy* keys."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read",
                      "payload": {"memory_ids": ["sk1"]}}]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}})
    await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                   bridge_statuses={}, events_fn=_events_fn(events))
    skill_calls = [c for c in v._client.set_payload.call_args_list
                   if c.kwargs["points"][0] == "sk1"]
    assert len(skill_calls) == 1
    assert set(skill_calls[0].kwargs["payload"]) == {
        "skill_efficacy", "skill_efficacy_n", "skill_efficacy_updated_at"}


@pytest.mark.asyncio
async def test_memory_and_skill_scored_independently_in_same_run():
    """A memory id in the same run as a skill id still gets owm_efficacy — the
    memory path is untouched by the new skill path."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read",
                      "payload": {"memory_ids": ["m1", "sk1"]}}]}
    v = _vector(point_types={
        "m1": {"memory_type": "episodic"},
        "sk1": {"memory_type": "skill"},
    })
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    assert out["skills_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert "owm_efficacy" in writes["m1"] and "skill_efficacy" not in writes["m1"]
    assert "skill_efficacy" in writes["sk1"] and "owm_efficacy" not in writes["sk1"]


@pytest.mark.asyncio
async def test_skill_owm_disabled_flag_is_bit_neutral_for_skills():
    """SKILL_OWM_ENABLED=false: no skill write, no skill stale-reset — the
    skill path must be bit-neutral while the memory path is untouched."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read",
                      "payload": {"memory_ids": ["m1", "sk1"]}}]}
    v = _vector(point_types={
        "m1": {"memory_type": "episodic"},
        "sk1": {"memory_type": "skill"},
    }, stale_scored_skill_ids=("sk1", "sk-old"))
    out = await run_pass(_redis_with(evals, ["s1"]), v,
                         _settings(SKILL_OWM_ENABLED=False),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    assert out["skills_scored"] == 0
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert "m1" in writes
    assert "sk1" not in writes
    assert not v._client.delete_payload.called  # no skill stale-reset sweep at all


@pytest.mark.asyncio
async def test_stale_scored_skills_reset_to_neutral():
    """Mirrors test_stale_scored_memories_reset_to_neutral for the skill side:
    a skill scored last pass but absent from this run's evidence has its
    skill_efficacy* keys deleted (back to neutral)."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [{"event_type": "memory_read", "payload": {"memory_ids": ["sk1"]}}]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}},
               stale_scored_skill_ids=("sk1", "sk-old"))
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["skills_scored"] == 1
    assert out["skill_stale_reset"] == 1
    assert out["stale_reset"] == 0  # skill sweep no longer bumps the memory counter
    del_call = v._client.delete_payload.call_args_list[0].kwargs
    assert del_call["points"] == ["sk-old"]
    assert set(del_call["keys"]) == {
        "skill_efficacy", "skill_efficacy_n", "skill_efficacy_updated_at"}


# --------------------------------------------------------------------------- #
# memory_feedback applied signal -> SKILL tally ONLY (outcome truth PR3, D3). #
# PR2's memory_feedback replay receipt ({memory_ids, useful, ...}) is a       #
# direct judgment on the recalled artifact. Memories already consume it via  #
# the set_feedback Qdrant counter (rag.py:1194+), so it must feed the SKILL  #
# tally exclusively -- routing it into `stats`/`scorable` too would          #
# double-count the same thumb for memories.                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_memory_feedback_merges_into_skill_tally():
    """A skill graded 1/1 by exposure (memory_read + successful session) plus
    a useful:false memory_feedback thumb must land at s=1, n=2 -- strictly
    lower than the exposure-only 1/1 efficacy."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [
        {"event_type": "memory_read", "payload": {"memory_ids": ["sk1"]}},
        {"event_type": "memory_feedback",
         "payload": {"memory_ids": ["sk1"], "useful": False}},
    ]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}})
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["skills_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert writes["sk1"]["skill_efficacy"] == round(compute_efficacy(1, 2, 5), 4)
    assert writes["sk1"]["skill_efficacy_n"] == 2
    assert writes["sk1"]["skill_efficacy"] < round(compute_efficacy(1, 1, 5), 4)


@pytest.mark.asyncio
async def test_memory_feedback_double_count_guard_owm_efficacy_unchanged():
    """CRITICAL invariant: a memory with BOTH exposure (memory_read, 1/1
    success) AND a memory_feedback thumb must have an owm_efficacy IDENTICAL
    to the exposure-only figure -- proving the feedback event never reaches
    `scorable` (it's already counted for memories via the set_feedback
    Qdrant counter, rag.py:1194+)."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [
        {"event_type": "memory_read", "payload": {"memory_ids": ["m1"]}},
        {"event_type": "memory_feedback",
         "payload": {"memory_ids": ["m1"], "useful": False}},
    ]}
    v = _vector(point_types={"m1": {"memory_type": "episodic"}})
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["memories_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert writes["m1"]["owm_efficacy"] == round(compute_efficacy(1, 1, 5), 4)
    assert writes["m1"]["owm_n"] == 1


@pytest.mark.asyncio
async def test_useful_true_feedback_raises_skill_tally_vs_exposure_failure():
    """An exposure-graded failure (0/1) with a useful:true thumb on top must
    score STRICTLY higher than the same exposure-failure with no feedback."""
    evals = {"s1": {"task_result": "failure", "task_result_source": "self_reported"}}
    control_events = {"s1": [
        {"event_type": "memory_read", "payload": {"memory_ids": ["sk1"]}},
    ]}
    fb_events = {"s1": [
        {"event_type": "memory_read", "payload": {"memory_ids": ["sk1"]}},
        {"event_type": "memory_feedback",
         "payload": {"memory_ids": ["sk1"], "useful": True}},
    ]}

    v_control = _vector(point_types={"sk1": {"memory_type": "skill"}})
    await run_pass(_redis_with(evals, ["s1"]), v_control, _settings(),
                   bridge_statuses={}, events_fn=_events_fn(control_events))
    control_writes = {c.kwargs["points"][0]: c.kwargs["payload"]
                      for c in v_control._client.set_payload.call_args_list}

    v_fb = _vector(point_types={"sk1": {"memory_type": "skill"}})
    await run_pass(_redis_with(evals, ["s1"]), v_fb, _settings(),
                   bridge_statuses={}, events_fn=_events_fn(fb_events))
    fb_writes = {c.kwargs["points"][0]: c.kwargs["payload"]
                for c in v_fb._client.set_payload.call_args_list}

    assert fb_writes["sk1"]["skill_efficacy"] > control_writes["sk1"]["skill_efficacy"]


@pytest.mark.asyncio
async def test_feedback_only_skill_scored_via_union():
    """A skill with in-window feedback but NO memory_read exposure this pass
    still gets retrieved and scored -- the retrieve driver is the union of
    `stats` and `feedback_stats` keys, not `stats` alone."""
    evals = {"s1": {"task_result": "success", "task_result_source": "self_reported"}}
    events = {"s1": [
        {"event_type": "memory_feedback",
         "payload": {"memory_ids": ["sk1"], "useful": True}},
    ]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}})
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["skills_scored"] == 1
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert writes["sk1"]["skill_efficacy"] == round(compute_efficacy(1, 1, 5), 4)
    assert writes["sk1"]["skill_efficacy_n"] == 1


@pytest.mark.asyncio
async def test_memory_feedback_counts_even_when_session_is_ungraded():
    """Fix round 1 (D3 review): the applied signal is 'distinct from the
    session-outcome inference' per spec -- the `useful` bit must NOT require
    a recognized grade. A session with an UNRECOGNIZED grade (here:
    "partial", which session_success() returns None for -- excluded from the
    exposure join) that emits memory_feedback for a skill must still have
    that feedback land in skill_efficacy. Before the fix, this session was
    skipped in full (the feedback loop sat after the grade gate), so
    skills_scored would be 0 and sk1 would never be written."""
    evals = {"s1": {"task_result": "partial", "task_result_source": "self_reported"}}
    events = {"s1": [
        {"event_type": "memory_feedback",
         "payload": {"memory_ids": ["sk1"], "useful": False}},
    ]}
    v = _vector(point_types={"sk1": {"memory_type": "skill"}})
    out = await run_pass(_redis_with(evals, ["s1"]), v, _settings(),
                         bridge_statuses={}, events_fn=_events_fn(events))
    assert out["skills_scored"] == 1
    assert out["sessions_joined"] == 0  # no recognized-grade exposure join happened
    writes = {c.kwargs["points"][0]: c.kwargs["payload"]
              for c in v._client.set_payload.call_args_list}
    assert writes["sk1"]["skill_efficacy"] == round(compute_efficacy(0, 1, 5), 4)
    assert writes["sk1"]["skill_efficacy_n"] == 1
