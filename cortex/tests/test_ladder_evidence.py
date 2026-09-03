"""Skill-ladder evidence reader: the ONLY place that reads whether a skill was
shown / reached / applied / graded, over the outcome-eval window. Pure read
over the replay store — no writes, no Qdrant, no decision thresholds (those
are Task 6's job)."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import fakeredis.aioredis as fr
import pytest

from app.skills.ladder_evidence import Evidence, efficacy, gather

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis():
    r = fr.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def _settings(**over):
    s = MagicMock()
    s.OWM_WINDOW_DAYS = 30
    s.OWM_PRIOR_N = 5
    for k, v in over.items():
        setattr(s, k, v)
    return s


async def _store_eval(r, sid, *, task_result, task_result_source="self_reported",
                       created_at=None):
    created_at = created_at or NOW
    data = {"task_result": task_result, "task_result_source": task_result_source,
            "created_at": created_at.isoformat()}
    await r.set(f"rp:eval:{sid}", json.dumps(data))
    await r.zadd("rp:eval_index", {sid: created_at.timestamp()})


def _events_fn(events_by_sid):
    async def fn(replay_r, sid):
        return events_by_sid.get(sid, [])
    return fn


def _skill_recall(skill_id, agent_id="agent-1", member_id=None):
    payload = {"trigger": "skill_recall", "memory_ids": [skill_id]}
    if member_id is not None:
        payload["member_id"] = member_id
    return {"event_type": "memory_read", "agent_id": agent_id, "payload": payload}


def _briefing(skill_id, agent_id="agent-1"):
    return {"event_type": "memory_read", "agent_id": agent_id,
            "payload": {"trigger": "briefing", "memory_ids": [skill_id]}}


def _feedback(skill_id, useful, agent_id="agent-1"):
    return {"event_type": "memory_feedback", "agent_id": agent_id,
            "payload": {"memory_ids": [skill_id], "useful": useful}}


# --------------------------------------------------------------------------- #
# Case 1: skill_recall + useful=true feedback + graded success -> success     #
# --------------------------------------------------------------------------- #


async def test_recall_plus_useful_feedback_plus_success_grade_is_a_success(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_skill_recall("sk1"), _feedback("sk1", True)]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.successes == 1
    assert ev.reached == 1
    assert ev.applied == 1


# --------------------------------------------------------------------------- #
# Case 2: skill_recall, no feedback, graded success -> success (reached       #
# fallback)                                                                   #
# --------------------------------------------------------------------------- #


async def test_recall_with_no_feedback_falls_back_to_reached(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_skill_recall("sk1")]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.successes == 1
    assert ev.applied == 0


# --------------------------------------------------------------------------- #
# Case 3: briefing receipt only, graded success -> shown, not a success       #
# --------------------------------------------------------------------------- #


async def test_briefing_only_is_shown_not_success(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_briefing("sk1")]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.shown == 1
    assert ev.reached == 0
    assert ev.successes == 0


# --------------------------------------------------------------------------- #
# Case 4: useful=false + graded success -> not a failure (pairing requires    #
# both useful=false AND a failure grade)                                     #
# --------------------------------------------------------------------------- #


async def test_useful_false_with_success_grade_is_not_a_failure(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_feedback("sk1", False)]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.failures == 0
    assert ev.applied == 1
    assert ev.successes == 0


# --------------------------------------------------------------------------- #
# Case 5: useful=false + graded failure -> a paired failure                   #
# --------------------------------------------------------------------------- #


async def test_useful_false_with_failure_grade_is_a_paired_failure(redis):
    await _store_eval(redis, "s1", task_result="failure")
    events = {"s1": [_feedback("sk1", False)]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.failures == 1
    assert ev.last_failure_sessions == ["s1"]


# --------------------------------------------------------------------------- #
# Case 6: useful=true + bridge status abandoned -> neither success nor        #
# failure (abandoned overrides the grade to False; success needs grade True;  #
# failure needs useful=false)                                                 #
# --------------------------------------------------------------------------- #


async def test_useful_true_with_abandoned_bridge_status_is_neither(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_skill_recall("sk1"), _feedback("sk1", True)]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events),
                       bridge_statuses={"s1": "abandoned"}, now=NOW)

    ev = out["sk1"]
    assert ev.failures == 0
    assert ev.successes == 0


# --------------------------------------------------------------------------- #
# Case 7: ungraded / partial sessions -> nothing beyond shown/reached/applied  #
# --------------------------------------------------------------------------- #


async def test_partial_grade_counts_only_exposure_signals(redis):
    await _store_eval(redis, "s1", task_result="partial")
    events = {"s1": [_skill_recall("sk1"), _feedback("sk1", True)]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.reached == 1
    assert ev.applied == 1
    assert ev.successes == 0
    assert ev.failures == 0


# --------------------------------------------------------------------------- #
# Case 8: per-identity cap                                                    #
# --------------------------------------------------------------------------- #


async def test_successes_are_capped_per_identity(redis):
    events = {}
    for i in range(4):
        sid = f"a{i}"
        await _store_eval(redis, sid, task_result="success")
        events[sid] = [_skill_recall("sk1", agent_id="a")]

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW,
                       per_identity_cap=2)

    ev = out["sk1"]
    assert ev.successes == 2
    assert ev.identities == {"a": 2}

    await _store_eval(redis, "b0", task_result="success")
    events["b0"] = [_skill_recall("sk1", agent_id="b")]

    out2 = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                        events_fn=_events_fn(events), bridge_statuses={}, now=NOW,
                        per_identity_cap=2)

    ev2 = out2["sk1"]
    assert ev2.successes == 3
    assert ev2.identities == {"a": 2, "b": 1}


# --------------------------------------------------------------------------- #
# Case 9: member_id preferred over agent_id                                   #
# --------------------------------------------------------------------------- #


async def test_member_id_preferred_over_agent_id_as_identity(redis):
    await _store_eval(redis, "s1", task_result="success")
    events = {"s1": [_skill_recall("sk1", agent_id="agent-fallback", member_id="member-1")]}

    out = await gather(redis, _settings(), since_by_skill={"sk1": NOW - timedelta(days=10)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.identities == {"member-1": 1}


# --------------------------------------------------------------------------- #
# Case 10: since_by_skill filtering                                          #
# --------------------------------------------------------------------------- #


async def test_evidence_before_ladder_since_is_ignored(redis):
    old_ts = NOW - timedelta(days=20)
    await _store_eval(redis, "s1", task_result="success", created_at=old_ts)
    events = {"s1": [_skill_recall("sk1")]}

    # ladder_since is AFTER this session's timestamp -> ignored entirely
    out = await gather(redis, _settings(),
                       since_by_skill={"sk1": NOW - timedelta(days=5)},
                       events_fn=_events_fn(events), bridge_statuses={}, now=NOW)

    ev = out["sk1"]
    assert ev.successes == 0
    assert ev.reached == 0
    assert ev.shown == 0


# --------------------------------------------------------------------------- #
# Case 11: efficacy                                                          #
# --------------------------------------------------------------------------- #


def test_efficacy_formula():
    assert efficacy(Evidence(successes=3, failures=0), prior_n=5) == pytest.approx((3 + 2.5) / 8)
