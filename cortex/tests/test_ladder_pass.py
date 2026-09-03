"""The nightly skill-ladder pass (shadow mode) — `app.skills.ladder.run_ladder_impl`.

PR1's whole point is a SHADOW run: it gathers evidence, asks `ladder_rules` for
decisions, and records what it *would* do — never mutating `skill_status`,
never touching the fleet ledger, never enqueueing anything. Every test here
either pins that contract directly or exercises one wiring rule (stamping,
parking, duplicate detection, caps, the lock, the disabled gate, fault
isolation) that sits between the pure evidence reader/rules modules (already
covered by their own suites) and this orchestrator.

The Qdrant double is deliberately dumb — filter-matching scroll + a
`set_payload` that mutates the underlying point in place, mirroring
`test_autopilot_api.py`'s `_FakeQdrant`. `dup_fn` is injected in most tests;
a few specifically exercise the REAL default duplicate-check helper
(`_default_dup_fn`) against a fake `query_points` — an empty active/trial set
(the fresh-Keep bootstrap case, fix round 1), a real 0.95 hit, and an embed
failure.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis as fr
import pytest_asyncio

from app.skills import ladder
from app.skills.ladder import run_ladder_impl
from tests.skill_payloads import real_skill_payload

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                            #
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def redis():
    r = fr.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest_asyncio.fixture
async def replay_r():
    r = fr.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class _Settings:
    def __init__(self, **over):
        self.SKILL_LADDER_ENABLED = True
        self.SKILL_LADDER_MODE = "shadow"
        self.SKILL_LADDER_SCHEDULE_HOURS = 24
        self.SKILL_LADDER_PROMOTE_MIN_SUCCESSES = 3
        self.SKILL_LADDER_PROMOTE_MIN_AGENTS = 2
        self.SKILL_LADDER_TRIAL_TTL_DAYS = 60
        self.OWM_PRIOR_N = 5
        self.OWM_WINDOW_DAYS = 30
        self.QDRANT_COLLECTION = "memories"
        for k, v in over.items():
            setattr(self, k, v)


def _settings(**over) -> _Settings:
    return _Settings(**over)


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


def _skill_point(pid, *, status, trigger="", symptoms="", steps=None, domain="",
                 timestamp=None, ladder_since=None, **extra) -> _Point:
    """A skill point shaped the way `app/skills/api.py` actually stores one
    (tests/skill_payloads.py).

    `steps` go into `content` under `## Steps`, NOT into a `steps` payload key
    — no writer has ever stored one, and inventing it here is what let C1 ship.
    `steps=None` therefore still yields the incomplete shape (heading present,
    body empty), and a point with no `timestamp` still has none, so the
    oldest-first ordering test is unchanged.
    """
    if steps is None:
        steps_text = ""
    elif isinstance(steps, (list, tuple)):
        steps_text = "\n".join(str(s) for s in steps)
    else:
        steps_text = str(steps)
    payload = real_skill_payload(
        trigger=trigger, symptoms=symptoms, domain=domain,
        steps=steps_text, status=status,
    )
    payload.pop("timestamp", None)
    if timestamp is not None:
        payload["timestamp"] = timestamp
    if ladder_since is not None:
        payload["ladder_since"] = ladder_since
    payload.update(extra)
    return _Point(pid, payload)


def _matches(payload: dict, scroll_filter) -> bool:
    if scroll_filter is None:
        return True
    for cond in scroll_filter.must or []:
        match = cond.match
        any_values = getattr(match, "any", None)
        if any_values is not None:
            if payload.get(cond.key) not in any_values:
                return False
        else:
            if payload.get(cond.key) != match.value:
                return False
    return True


class _ScoredPoint:
    def __init__(self, pid, score):
        self.id = pid
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points):
        self.points = list(points)


class _FakeQdrant:
    """Filter-matching scroll + payload-mutating set_payload — enough to pin
    what this module actually issues. `query_points` returns
    `query_points_result` (default: empty — the fresh-Keep bootstrap case),
    mirroring the real Qdrant response shape (a `.points` list of scored
    points)."""

    def __init__(self, points=None):
        self.points = list(points or [])
        self.set_payload_calls: list[dict] = []
        self.query_points_result: list = []

    async def scroll(self, collection_name, scroll_filter=None, limit=200,
                     offset=None, with_payload=True, with_vectors=False):
        matched = [p for p in self.points if _matches(p.payload, scroll_filter)]
        start = int(offset or 0)
        page = matched[start:start + limit]
        nxt = start + limit if start + limit < len(matched) else None
        return page, nxt

    async def set_payload(self, collection_name, payload, points):
        self.set_payload_calls.append({"payload": dict(payload), "points": list(points)})
        for pid in points:
            for p in self.points:
                if str(p.id) == str(pid):
                    p.payload.update(payload)

    async def query_points(self, **kwargs):
        return _FakeQueryResponse(self.query_points_result)


class _FakeVector:
    def __init__(self, client):
        self._client = client
        self._embed = AsyncMock(return_value=[0.1] * 8)


async def _no_dup(vector, settings, payload):
    return None


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


def _skill_recall(skill_id, agent_id="agent-1"):
    return {"event_type": "memory_read", "agent_id": agent_id,
            "payload": {"trigger": "skill_recall", "memory_ids": [skill_id]}}


def _feedback(skill_id, useful, agent_id="agent-1"):
    return {"event_type": "memory_feedback", "agent_id": agent_id,
            "payload": {"memory_ids": [skill_id], "useful": useful}}


# --------------------------------------------------------------------------- #
# Case 1: shadow changes nothing                                             #
# --------------------------------------------------------------------------- #


async def test_shadow_contract_five_decisions_and_no_status_writes(redis, replay_r):
    trial_promote = _skill_point("trial-promote", status="trial", domain="other",
                                 ladder_since=iso(10))
    trial_demote = _skill_point("trial-demote", status="trial", domain="other",
                                ladder_since=iso(10))
    trial_expire = _skill_point("trial-expire", status="trial", domain="other",
                                ladder_since=iso(90))
    active_flag = _skill_point("active-flag", status="active", domain="other",
                               ladder_since=iso(10))
    draft_admit = _skill_point("draft-admit", status="draft", trigger="t",
                               symptoms="s", steps=["a"], domain="d1",
                               timestamp=iso(1), ladder_since=iso(1))

    fake = _FakeQdrant([trial_promote, trial_demote, trial_expire, active_flag, draft_admit])
    vector = _FakeVector(fake)

    events = {}
    for i, agent in enumerate(["agent-a", "agent-b", "agent-c"]):
        sid = f"promote-{i}"
        events[sid] = [_skill_recall("trial-promote", agent_id=agent)]
        await _store_eval(replay_r, sid, task_result="success")
    for i in range(5):
        sid = f"demote-{i}"
        events[sid] = [_feedback("trial-demote", False)]
        await _store_eval(replay_r, sid, task_result="failure")
    for i in range(5):
        sid = f"flag-{i}"
        events[sid] = [_feedback("active-flag", False)]
        await _store_eval(replay_r, sid, task_result="failure")

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn(events), bridge_statuses={},
                                dup_fn=_no_dup)

    _ALLOWED_KEYS = {"ladder_since", "ladder_shadow", "duplicate_of"}
    for call in fake.set_payload_calls:
        assert set(call["payload"]) <= _ALLOWED_KEYS

    decisions_raw = await redis.lrange(ladder.DECISIONS_KEY, 0, -1)
    decisions = [json.loads(d) for d in decisions_raw]
    assert len(decisions) == 5
    assert {d["action"] for d in decisions} == {"promote", "demote", "expire", "flag", "admit"}
    for d in decisions:
        assert d["mode"] == "shadow"

    assert trial_promote.payload["ladder_shadow"]["action"] == "promote"
    assert trial_demote.payload["ladder_shadow"]["action"] == "demote"
    assert trial_expire.payload["ladder_shadow"]["action"] == "expire"
    assert active_flag.payload["ladder_shadow"]["action"] == "flag"
    assert draft_admit.payload["ladder_shadow"]["action"] == "admit"

    assert out["expired"] == 1
    assert out["demoted"] == 1
    assert out["flagged"] == 1
    assert out["promoted"] == 1
    assert out["admitted"] == 1
    assert out["skipped_duplicate"] == 0
    assert out["skipped_capped"] == 0
    assert out["skipped_parked"] == 0
    assert out["skipped_incomplete"] == 0
    assert out["stamped_since"] == 0
    assert out["trial_count"] == 3
    assert out["errors"] == []
    assert out["mode"] == "shadow"

    assert await redis.exists("fleet:ledger:ladder") == 0

    # The record Task 8's autopilot surface reads is a DIFFERENT object than
    # the one returned here — assert it directly rather than trusting they
    # stay in sync.
    persisted = json.loads(await redis.get(ladder.LAST_RUN_KEY))
    assert persisted["mode"] == "shadow"
    assert persisted["expired"] == 1
    assert persisted["demoted"] == 1
    assert persisted["flagged"] == 1
    assert persisted["promoted"] == 1
    assert persisted["admitted"] == 1
    assert persisted["trial_count"] == 3
    assert set(persisted["reach_by_tier"]) == {"active", "trial"}
    assert "at" in persisted


# --------------------------------------------------------------------------- #
# Case 2: ladder_since stamped once                                          #
# --------------------------------------------------------------------------- #


async def test_ladder_since_stamped_once(redis, replay_r):
    trial = _skill_point("trial-1", status="trial", domain="other", timestamp=iso(90))
    fake = _FakeQdrant([trial])
    vector = _FakeVector(fake)
    settings = _settings()

    out1 = await run_ladder_impl(vector, replay_r, redis, settings, now=NOW,
                                 events_fn=_events_fn({}), bridge_statuses={})
    assert out1["stamped_since"] == 1
    assert trial.payload["ladder_since"] == iso(90)
    assert any("ladder_since" in call["payload"] for call in fake.set_payload_calls)

    calls_before_second_run = len(fake.set_payload_calls)
    out2 = await run_ladder_impl(vector, replay_r, redis, settings, now=NOW,
                                 events_fn=_events_fn({}), bridge_statuses={})
    assert out2["stamped_since"] == 0
    new_calls = fake.set_payload_calls[calls_before_second_run:]
    assert all("ladder_since" not in call["payload"] for call in new_calls)


# --------------------------------------------------------------------------- #
# Case 3: parked drafts are never admitted                                   #
# --------------------------------------------------------------------------- #


async def test_parked_draft_is_never_admitted(redis, replay_r):
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1),
                         ladder_since=iso(1), demoted_at=iso(5))
    fake = _FakeQdrant([draft])
    vector = _FakeVector(fake)
    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=_no_dup)
    assert out["admitted"] == 0
    assert out["skipped_parked"] == 1
    assert await redis.lrange(ladder.DECISIONS_KEY, 0, -1) == []


# --------------------------------------------------------------------------- #
# Case 4: duplicate draft                                                    #
# --------------------------------------------------------------------------- #


async def test_duplicate_draft_is_parked_not_admitted(redis, replay_r):
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([draft])
    vector = _FakeVector(fake)

    async def dup_fn(vector, settings, payload):
        return ("active-1", 0.95)

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=dup_fn)
    assert out["admitted"] == 0
    assert out["skipped_duplicate"] == 1
    assert draft.payload["duplicate_of"] == "active-1"


# --------------------------------------------------------------------------- #
# Case 5: domain cap and per-run cap                                         #
# --------------------------------------------------------------------------- #


async def test_domain_cap_skips_admission(redis, replay_r):
    trials = [_skill_point(f"trial-{i}", status="trial", domain="d", ladder_since=iso(10))
             for i in range(10)]
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([*trials, draft])
    vector = _FakeVector(fake)
    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=_no_dup)
    assert out["admitted"] == 0
    assert out["skipped_capped"] == 1


async def test_admit_per_run_cap_skips_extra_drafts(redis, replay_r):
    drafts = [_skill_point(f"draft-{i}", status="draft", trigger="t", symptoms="s",
                           steps=["a"], domain=f"d{i}", timestamp=iso(i + 1),
                           ladder_since=iso(i + 1))
             for i in range(21)]
    fake = _FakeQdrant(list(drafts))
    vector = _FakeVector(fake)
    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=_no_dup)
    assert out["admitted"] == 20
    assert out["skipped_capped"] == 1


# --------------------------------------------------------------------------- #
# Case 6: lock                                                               #
# --------------------------------------------------------------------------- #


async def test_second_concurrent_run_is_locked(redis, replay_r):
    await redis.set(ladder.LOCK_KEY, "1", nx=True, ex=3600)

    class _BrokenQdrant:
        async def scroll(self, *a, **k):
            raise AssertionError("must not touch Qdrant while locked")

    vector = _FakeVector(_BrokenQdrant())
    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={})
    assert out == {"status": "locked"}


# --------------------------------------------------------------------------- #
# Case 7: disabled                                                           #
# --------------------------------------------------------------------------- #


async def test_disabled_returns_before_any_io(redis, replay_r):
    class _BrokenQdrant:
        async def scroll(self, *a, **k):
            raise AssertionError("must not touch Qdrant when disabled")

    vector = _FakeVector(_BrokenQdrant())
    settings = _settings(SKILL_LADDER_ENABLED=False)
    out = await run_ladder_impl(vector, replay_r, redis, settings, now=NOW)
    assert out == {"status": "disabled"}
    assert await redis.exists(ladder.LOCK_KEY) == 0


# --------------------------------------------------------------------------- #
# Case 8: enforce mode still runs shadow                                     #
# --------------------------------------------------------------------------- #


async def test_enforce_mode_still_runs_shadow(redis, replay_r):
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([draft])
    vector = _FakeVector(fake)
    settings = _settings(SKILL_LADDER_MODE="enforce")
    out = await run_ladder_impl(vector, replay_r, redis, settings, now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=_no_dup)
    assert out["mode"] == "shadow"
    assert out["warning"] == "enforce mode ships in PR2 — ran shadow"
    assert out["admitted"] == 1

    persisted = json.loads(await redis.get(ladder.LAST_RUN_KEY))
    assert persisted["mode"] == "shadow"
    assert persisted["warning"] == "enforce mode ships in PR2 — ran shadow"


# --------------------------------------------------------------------------- #
# Case 9: fault isolation                                                    #
# --------------------------------------------------------------------------- #


async def test_dup_fn_failure_for_one_draft_does_not_block_others(redis, replay_r):
    good = _skill_point("draft-good", status="draft", trigger="t1", symptoms="s",
                        steps=["a"], domain="d1", timestamp=iso(1), ladder_since=iso(1))
    bad = _skill_point("draft-bad", status="draft", trigger="t2", symptoms="s",
                       steps=["a"], domain="d2", timestamp=iso(2), ladder_since=iso(2))
    fake = _FakeQdrant([good, bad])
    vector = _FakeVector(fake)

    async def dup_fn(vector, settings, payload):
        if payload.get("trigger") == "t2":
            raise RuntimeError("dup backend down")
        return None

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=dup_fn)
    assert out["admitted"] == 1
    assert any(e["skill_id"] == "draft-bad" and e["stage"] == "admit"
               for e in out["errors"])


async def test_evidence_gather_failure_records_error_and_continues_to_admission(redis):
    trial = _skill_point("trial-1", status="trial", domain="other", ladder_since=iso(10))
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d2", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([trial, draft])
    vector = _FakeVector(fake)

    class _BrokenReplay:
        async def zrangebyscore(self, *a, **k):
            raise RuntimeError("replay store unreachable")

    out = await run_ladder_impl(vector, _BrokenReplay(), redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={}, dup_fn=_no_dup)
    assert any(e["skill_id"] is None and e["stage"] == "evidence" for e in out["errors"])
    assert out["admitted"] == 1


# --------------------------------------------------------------------------- #
# Controller ruling 2: duplicate check unavailable never admits              #
# --------------------------------------------------------------------------- #


async def test_dup_check_unavailable_blocks_admission_and_records_error(redis, replay_r):
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([draft])
    vector = _FakeVector(fake)
    vector._embed = AsyncMock(side_effect=RuntimeError("embed backend down"))

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={})
    assert out["admitted"] == 0
    assert any(e["skill_id"] == "draft-1" and e["stage"] == "admit"
               and e["error"] == "dup_check_unavailable" for e in out["errors"])


# --------------------------------------------------------------------------- #
# Fix round 1, Important #1: the real _default_dup_fn must never treat "no    #
# active/trial skills to compare against" as "the duplicate check is broken" #
# --------------------------------------------------------------------------- #


async def test_default_dup_fn_admits_when_no_active_or_trial_skills_exist(redis, replay_r):
    """The ordinary bootstrap state of a fresh Keep: no active/trial skills
    exist yet, so a genuinely clean draft's semantic search legitimately
    finds nothing. That must admit, not deadlock as 'unavailable'."""
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([draft])
    # query_points_result defaults to [] — no active/trial skill to match.
    vector = _FakeVector(fake)

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={})
    assert out["admitted"] == 1
    assert out["errors"] == []
    assert draft.payload["ladder_shadow"]["action"] == "admit"


async def test_default_dup_fn_detects_real_duplicate_via_query_points(redis, replay_r):
    draft = _skill_point("draft-1", status="draft", trigger="t", symptoms="s",
                         steps=["a"], domain="d", timestamp=iso(1), ladder_since=iso(1))
    fake = _FakeQdrant([draft])
    fake.query_points_result = [_ScoredPoint("active-1", 0.95)]
    vector = _FakeVector(fake)

    out = await run_ladder_impl(vector, replay_r, redis, _settings(), now=NOW,
                                events_fn=_events_fn({}), bridge_statuses={})
    assert out["admitted"] == 0
    assert out["skipped_duplicate"] == 1
    assert draft.payload["duplicate_of"] == "active-1"
