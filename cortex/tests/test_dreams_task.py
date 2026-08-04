from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import fakeredis
import httpx
import pytest

from app.dreams import task as dt

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _gate(**kw):
    args = dict(
        enabled=True,
        now=NOW,
        last_write_at=NOW - timedelta(minutes=45),
        idle_minutes=30,
        new_memories=100,
        min_new=25,
    )
    args.update(kw)
    return dt.should_run(**args)


def test_gate_opens_when_idle_and_work_exists():
    ok, reason = _gate()
    assert ok, reason


def test_gate_closed_when_disabled():
    ok, reason = _gate(enabled=False)
    assert not ok and "disabled" in reason


def test_gate_closed_while_recently_active():
    ok, reason = _gate(last_write_at=NOW - timedelta(minutes=2))
    assert not ok and "idle" in reason


def test_gate_closed_without_enough_new_memories():
    ok, reason = _gate(new_memories=3)
    assert not ok and "new" in reason


def test_gate_opens_when_never_written_before():
    ok, reason = _gate(last_write_at=None)
    assert ok, reason


def test_task_is_registered_on_beat_with_matching_name():
    from app.workers.sleep_cycle import celery_app

    name = "app.dreams.task.run_dream_tick"
    assert name in celery_app.tasks
    entry = celery_app.conf.beat_schedule["dream-tick"]
    assert entry["task"] == name
    # Fix-round review minor: this test previously couldn't catch the module
    # being removed from `include` — celery_app.tasks is populated because
    # THIS test module's own top-level `from app.dreams import task as dt`
    # already imported it directly, independent of sleep_cycle.py's
    # `include` list ever being consulted.
    assert "app.dreams.task" in celery_app.conf.include


def test_disabled_task_returns_status_without_building_clients(monkeypatch):
    monkeypatch.setattr(dt, "_build_clients", lambda: (_ for _ in ()).throw(
        AssertionError("must not build clients when disabled")))
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("DREAM_ENABLED", "false")
    out = dt.run_dream_tick()
    assert out["status"] == "disabled"


# ---------------------------------------------------------------------------
# I5 — generation-backend reachability probe (pure predicate + probe tests)
# ---------------------------------------------------------------------------

def test_is_backend_unavailable_detects_connection_and_timeout_errors():
    assert dt._is_backend_unavailable(httpx.ConnectError("refused"))
    assert dt._is_backend_unavailable(httpx.ConnectTimeout("timed out"))


def test_is_backend_unavailable_detects_404():
    request = httpx.Request("GET", "http://x/models")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("not found", request=request, response=response)
    assert dt._is_backend_unavailable(exc)


def test_is_backend_unavailable_false_for_a_reachable_backend_erroring():
    request = httpx.Request("GET", "http://x/models")
    response = httpx.Response(500, request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    assert not dt._is_backend_unavailable(exc)
    assert not dt._is_backend_unavailable(ValueError("unrelated"))


class _Settings:
    LLM_BASE_URL = "http://x/v1"


@pytest.mark.asyncio
async def test_generation_backend_available_true_on_2xx(monkeypatch):
    async def fake_get(self, url, **kw):
        return httpx.Response(200, request=httpx.Request("GET", url), json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert await dt._generation_backend_available(_Settings())


@pytest.mark.asyncio
async def test_generation_backend_available_false_on_connect_error(monkeypatch):
    async def fake_get(self, url, **kw):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert not await dt._generation_backend_available(_Settings())


# ---------------------------------------------------------------------------
# run_one_unit end-to-end wiring — fakes standing in for Qdrant/Redis/LLM.
# Models: tests/test_dreams_profile.py and test_dreams_store.py's FakeVector,
# extended with the ._client.scroll(...) surface run_one_unit reads directly.
# ---------------------------------------------------------------------------

class _FakePoint:
    def __init__(self, id, payload, vector=None):
        self.id = id
        self.payload = payload
        self.vector = vector or [1.0, 0.0]


class _FakeScroll:
    """Single-page stand-in for AsyncQdrantClient.scroll: returns the full
    point list on every call and never sets a next offset. Both the
    vector-less activity scan and the with-vectors candidate scan read the
    SAME points at these test sizes (one page is enough either way)."""
    def __init__(self, points):
        self.points = points

    async def scroll(self, **kwargs):
        return self.points, None


class _FakeVector:
    def __init__(self, points):
        self._client = _FakeScroll(points)
        self.upserted: dict[str, dict] = {}

    async def upsert_point(self, point_id, text, payload):
        self.upserted[point_id] = {"text": text, "payload": payload}
        return point_id

    async def close(self):
        pass


def _dream_settings(**overrides):
    from app.config import get_settings

    base = dict(
        DREAM_ENABLED=True, DREAM_MIN_NEW_MEMORIES=1, DREAM_IDLE_MINUTES=1,
        DREAM_MIN_AGE_DAYS=2, DREAM_MIN_CLUSTER=4, DREAM_CLUSTER_THRESHOLD=0.9,
        DREAM_MAX_CLUSTERS_PER_RUN=5, DREAM_LOCK_TTL_SECONDS=60,
        DREAM_PROFILES_ENABLED=True,
    )
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _candidate_payload(member_id, workspace_id, i, *, namespace="default", project="p"):
    return {
        "status": "active", "source": "action_log", "memory_type": "episodic",
        "confirmed_count": 0,
        "timestamp": (NOW - timedelta(days=10)).isoformat(),
        "workspace_id": workspace_id, "namespace": namespace, "project": project,
        "member_id": member_id,
        "text": f"memory {i} for {member_id} in {workspace_id}",
    }


@pytest.mark.asyncio
async def test_profile_grouping_respects_workspace_tenancy_boundary(monkeypatch):
    """C1 (CRITICAL): a member with memories in TWO different workspaces
    must produce two DISJOINT profile writes, one per workspace — never one
    profile whose synthesis input blends both, and never a scroll-order-
    dependent "which workspace wins" leaving stale duplicate points.
    Each group is below DREAM_MIN_CLUSTER (2 < 4), so no cluster forms and
    both ticks fall straight through to the profile branch."""
    points = (
        [_FakePoint(f"a{i}", _candidate_payload("mem1", "wsA", i)) for i in range(2)]
        + [_FakePoint(f"b{i}", _candidate_payload("mem1", "wsB", i)) for i in range(2)]
    )
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))

    async def fake_synth_profile(member_id, memories, **kw):
        texts = sorted(m["text"] for m in memories)
        return f"PROFILE for {member_id}: " + " | ".join(texts)

    monkeypatch.setattr("app.dreams.profile.synthesize_profile", fake_synth_profile)

    out1 = await dt.run_one_unit()
    assert out1["status"] == "ok" and out1["unit"] == "profile"
    out2 = await dt.run_one_unit()
    assert out2["status"] == "ok" and out2["unit"] == "profile"

    assert len(vector.upserted) == 2, "each workspace must get its OWN point"
    records = list(vector.upserted.values())
    texts = [rec["text"] for rec in records]
    assert texts[0] != texts[1], "the two profiles must be disjoint, not one blended write"
    for text in texts:
        assert not ("wsA" in text and "wsB" in text), \
            "one profile's synthesis input must never mix both workspaces"
    workspaces = {rec["payload"]["workspace_id"] for rec in records}
    assert workspaces == {"wsA", "wsB"}


@pytest.mark.asyncio
async def test_profile_derives_namespace_and_project_from_group(monkeypatch):
    """I2: namespace/project must be read from the (now tenancy-homogeneous)
    candidate group, not hardcoded to "default"/None. project="finance" here
    would render as None if the old hardcoding were still in place."""
    points = [
        _FakePoint(f"c{i}", _candidate_payload(
            "mem2", "wsC", i, namespace="acme", project="finance"))
        for i in range(2)
    ]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.dreams.profile.synthesize_profile", AsyncMock(return_value="a profile"))

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    assert len(vector.upserted) == 1
    payload = next(iter(vector.upserted.values()))["payload"]
    assert payload["namespace"] == "acme"
    assert payload["project"] == "finance"


@pytest.mark.asyncio
async def test_profile_group_missing_workspace_id_skips_synthesis_entirely(monkeypatch):
    """M5 (round 2): the workspace_id guard must run BEFORE synthesize_profile
    is awaited — a group that's always going to be discarded (no real
    workspace could ever match profile_point_id(member_id, "")) must not
    burn a full LLM call first. The original ordering awaited synthesis
    unconditionally and only checked workspace_id afterward."""
    points = [_FakePoint(f"m{i}", _candidate_payload("mem1", "", i)) for i in range(2)]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=True))
    synth_mock = AsyncMock(return_value="should never be produced")
    monkeypatch.setattr("app.dreams.profile.synthesize_profile", synth_mock)

    out = await dt.run_one_unit()
    assert out["status"] == "ok" and out["unit"] == "profile"
    assert out["written"] is False
    synth_mock.assert_not_awaited()
    assert not vector.upserted

    from app.dreams.state import DreamState

    # still marked done so the empty-workspace group isn't retried forever
    assert len(DreamState(r).done_set("profile")) == 1


@pytest.mark.asyncio
async def test_backend_unavailable_skips_unit_without_marking_anything_done(monkeypatch):
    """I5: when the generation backend is unreachable, the tick must not
    walk the backlog marking clusters/profiles done with zero insights, and
    must not stamp completion — there IS a backlog, it just couldn't be
    worked this tick."""
    points = [_FakePoint(f"m{i}", _candidate_payload("mem1", "ws1", i)) for i in range(4)]
    r = fakeredis.FakeStrictRedis()
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    monkeypatch.setattr(dt, "_generation_backend_available", AsyncMock(return_value=False))

    out = await dt.run_one_unit()
    assert out["status"] == "unavailable"

    from app.dreams.state import DreamState

    state = DreamState(r)
    assert state.done_set("cluster") == set()
    assert state.done_set("profile") == set()
    assert state.get_run().get("last_completed_at") is None
    assert not vector.upserted


@pytest.mark.asyncio
async def test_lock_contention_returns_locked_without_touching_data(monkeypatch):
    r = fakeredis.FakeStrictRedis()
    r.set(dt.LOCK_KEY, "1", nx=True, ex=60)  # simulate another tick holding it
    points = [_FakePoint("m0", _candidate_payload("mem1", "ws1", 0))]
    vector = _FakeVector(points)
    settings = _dream_settings()

    async def fake_build_clients():
        return r, vector, settings

    monkeypatch.setattr(dt, "_build_clients", fake_build_clients)
    out = await dt.run_one_unit()
    assert out == {"status": "locked"}
    assert not vector.upserted
