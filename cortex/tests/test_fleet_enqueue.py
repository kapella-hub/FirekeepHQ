"""The nightly fleet enqueue: state-based dedup, member-private exclusion, caps.

The fake Qdrant evaluates `must` (equality) and `must_not` (equality or
IsEmpty) because those are exactly the conditions this pass issues; anything
richer would be testing qdrant-client.
"""
from __future__ import annotations

import json

import fakeredis
import pytest
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue

from app.fleet import enqueue, ledger


class _Point:
    def __init__(self, pid, payload):
        self.id, self.payload = pid, payload


def _match(cond, payload) -> bool:
    if isinstance(cond, FieldCondition):
        return payload.get(cond.key) == cond.match.value
    if isinstance(cond, IsEmptyCondition):
        return payload.get(cond.is_empty.key) in (None, "", [], {})
    raise AssertionError(f"unsupported condition {cond!r}")


class _FakeQdrant:
    def __init__(self, points):
        self._points = points
        self.closed = False

    def scroll(self, collection_name, scroll_filter=None, limit=1000,
               with_payload=True, with_vectors=False, offset=None):
        out = []
        for p in self._points:
            ok = all(_match(c, p.payload) for c in (scroll_filter.must or []))
            ok = ok and not any(_match(c, p.payload) for c in (scroll_filter.must_not or []))
            if ok:
                out.append(p)
        return out[:limit], None

    def close(self):
        self.closed = True


class _Settings:
    QDRANT_COLLECTION = "test"
    FLEET_ENQUEUE_ENABLED = True
    FLEET_ENQUEUE_MAX_PER_RUN = 20
    RELAY_URL = "http://relay:8050"
    FIREKEEP_INTERNAL_KEY = "nxs_test"


def _stale_skill(pid="s1", **extra):
    return _Point(pid, {"memory_type": "skill", "skill_status": "active", "stale": True,
                        "trigger": "T", "symptoms": "S", "content": "C", "domain": "d",
                        "workspace_id": "ws", **extra})


def _pair(a="m1", b="m2", **extra):
    return [
        _Point(a, {"status": "active", "contested": True, "contested_with": b, "text": "A",
                   "contested_at": "2026-09-01T00:00:00+00:00", "workspace_id": "ws", **extra}),
        _Point(b, {"status": "active", "contested": True, "contested_with": a, "text": "B",
                   "contested_at": "2026-09-01T00:00:00+00:00", "workspace_id": "ws"}),
    ]


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


class _Recorder:
    def __init__(self, ok=True):
        self.ok, self.tasks = ok, []

    def __call__(self, settings, task):
        self.tasks.append(task)
        return self.ok


def _run(points, redis, post=None, settings=None):
    post = post or _Recorder()
    out = enqueue.fleet_enqueue_pass(client=_FakeQdrant(points), settings=settings or _Settings(),
                                     redis_client=redis, post=post)
    return out, post


def test_disabled_before_any_io(redis):
    s = _Settings(); s.FLEET_ENQUEUE_ENABLED = False
    q = _FakeQdrant([_stale_skill()])
    rec = _Recorder()
    out = enqueue.fleet_enqueue_pass(client=q, settings=s, redis_client=redis, post=rec)
    assert out == {"status": "disabled"} and rec.tasks == []


def test_stale_skill_becomes_a_reauthor_task_with_context(redis):
    out, post = _run([_stale_skill(access_count=3, skill_efficacy=0.4, skill_efficacy_n=6)], redis)
    assert out["reauthor_enqueued"] == 1
    t = post.tasks[0]
    assert t["title"] == "reauthor_stale_skill" and t["assigner"] == "cortex-fleet"
    assert t["description"] == "skill_id=s1 workspace_id=ws"
    ctx = json.loads(t["context"])
    assert ctx["skill_id"] == "s1" and ctx["trigger"] == "T" and ctx["content"] == "C"
    assert ctx["access_count"] == 3 and ctx["skill_efficacy_n"] == 6
    assert redis.exists(ledger.live_marker_key("reauthor_stale_skill", "s1")) == 1


def test_contested_pair_becomes_one_verdict_task(redis):
    out, post = _run(_pair(), redis)
    assert out["verdict_enqueued"] == 1 and len(post.tasks) == 1
    t = post.tasks[0]
    assert t["title"] == "propose_contested_verdict"
    assert t["description"] == "pair=m1,m2 workspace_id=ws"
    ctx = json.loads(t["context"])
    assert {ctx["a"]["id"], ctx["b"]["id"]} == {"m1", "m2"}
    assert ctx["a"]["text"] in {"A", "B"} and ctx["contested_at"]


def test_member_private_points_are_never_enqueued(redis):
    pts = [_stale_skill(visibility="member")] + _pair(a="p1", b="p2", visibility="member")
    out, post = _run(pts, redis)
    assert post.tasks == []
    assert out["reauthor_enqueued"] == 0 and out["verdict_enqueued"] == 0
    # The pair's other side is workspace-visible but its partner is private → unpaired.
    assert out["skipped_unpaired"] == 1


def test_pending_reauthor_draft_blocks_re_enqueue(redis):
    draft = _Point("d1", {"memory_type": "skill", "skill_status": "draft", "reauthor_of": "s1"})
    out, post = _run([_stale_skill(), draft], redis)
    assert post.tasks == [] and out["skipped_pending"] == 1


def test_existing_proposal_blocks_re_enqueue(redis):
    pts = _pair()
    pts[0].payload["proposed_verdict"] = {"action": "coexist", "winner_id": None}
    out, post = _run(pts, redis)
    assert post.tasks == [] and out["skipped_pending"] == 1


def test_rejected_marker_blocks_re_enqueue(redis):
    redis.set(ledger.rejected_reauthor_key("s1"), "1")
    out, post = _run([_stale_skill()], redis)
    assert post.tasks == [] and out["skipped_rejected"] == 1


def test_live_marker_blocks_double_post_and_expires_with_the_task(redis):
    out1, post1 = _run([_stale_skill()], redis)
    assert len(post1.tasks) == 1
    ttl = redis.ttl(ledger.live_marker_key("reauthor_stale_skill", "s1"))
    assert 0 < ttl <= ledger.LIVE_MARKER_TTL_SECONDS
    out2, post2 = _run([_stale_skill()], redis)
    assert post2.tasks == [] and out2["skipped_inflight"] == 1


def test_failed_post_releases_the_marker_and_counts_failed(redis):
    out, _ = _run([_stale_skill()], redis, post=_Recorder(ok=False))
    assert out["failed"] == 1 and out["reauthor_enqueued"] == 0
    assert redis.exists(ledger.live_marker_key("reauthor_stale_skill", "s1")) == 0


def test_cap_bounds_a_night(redis):
    s = _Settings(); s.FLEET_ENQUEUE_MAX_PER_RUN = 2
    pts = [_stale_skill(f"s{i}") for i in range(5)]
    out, post = _run(pts, redis, settings=s)
    assert len(post.tasks) == 2 and out["capped"] == 3


def test_context_is_truncated(redis):
    out, post = _run([_stale_skill(content="x" * 10000)], redis)
    assert len(json.loads(post.tasks[0]["context"])["content"]) == enqueue.SKILL_CONTENT_CAP


def test_post_relay_task_uses_rest_and_internal_key(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 201

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(enqueue.httpx, "post", fake_post)
    assert enqueue.post_relay_task(_Settings(), {"title": "t"}) is True
    assert seen["url"] == "http://relay:8050/tasks" and seen["json"] == {"title": "t"}
    assert seen["headers"] == {"X-API-Key": "nxs_test"} and seen["timeout"] == 10.0


def test_post_relay_task_swallows_transport_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("relay down")
    monkeypatch.setattr(enqueue.httpx, "post", boom)
    assert enqueue.post_relay_task(_Settings(), {"title": "t"}) is False
