"""Redis layer for Living Procedures. Uses a filter-HONOURING Qdrant double —
every pre-existing fake in this suite ignores scroll_filter, so a test against
one would pass while the index silently included drafts and non-skills."""

import pytest
import fakeredis.aioredis as fr

from app.procedures import store


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
                key = cond.key
                want = cond.match.value
                pts = [p for p in pts if (p.payload or {}).get(key) == want]
        return pts[:limit], None


class _Vector:
    def __init__(self, points):
        self._client = _FilterHonouringQdrant(points)


class _Settings:
    QDRANT_COLLECTION = "c"
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50


@pytest.fixture
def redis_client():
    return fr.FakeRedis(decode_responses=True)


def _skill(pid, trigger, specs, status="active", mtype="skill"):
    return _Point(pid, {
        "memory_type": mtype, "skill_status": status,
        "trigger": trigger, "step_specs": specs,
    })


@pytest.mark.asyncio
async def test_index_holds_only_active_skills_file_glob_specs(redis_client):
    vector = _Vector([
        _skill("s1", "release", [
            {"id": "a", "text": "bump", "kind": "file_glob", "pattern": "p.toml", "load_bearing": True},
            {"id": "b", "text": "ask", "kind": "unobservable", "pattern": "", "load_bearing": False},
        ]),
        _skill("s2", "draft one", [
            {"id": "c", "text": "x", "kind": "file_glob", "pattern": "*.py", "load_bearing": False},
        ], status="draft"),
    ])
    n = await store.rebuild_index(vector, redis_client, _Settings())
    assert n == 1
    idx = await store.load_index(redis_client)
    assert [e["step_id"] for e in idx] == ["a"]
    assert idx[0]["skill_id"] == "s1"
    assert idx[0]["order"] == 0
    assert idx[0]["load_bearing"] is True


@pytest.mark.asyncio
async def test_order_is_the_spec_list_position_not_the_filtered_position(redis_client):
    """'Earlier step' means earlier in step_specs. An unobservable step still
    occupies its position — dropping it from the index must not renumber the
    observable ones, or the earlier-step check compares the wrong steps."""
    vector = _Vector([_skill("s1", "t", [
        {"id": "a", "text": "0", "kind": "unobservable", "pattern": "", "load_bearing": False},
        {"id": "b", "text": "1", "kind": "file_glob", "pattern": "x", "load_bearing": False},
    ])])
    await store.rebuild_index(vector, redis_client, _Settings())
    idx = await store.load_index(redis_client)
    assert idx[0]["step_id"] == "b"
    assert idx[0]["order"] == 1


@pytest.mark.asyncio
async def test_load_index_on_a_cold_store_is_empty_not_an_error(redis_client):
    assert await store.load_index(redis_client) == []


@pytest.mark.asyncio
async def test_observation_opens_then_extends_one_execution(redis_client):
    s = _Settings()
    e1 = await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="a",
        action_id="act1", target="p.toml", agent_id="ag", adapter="shell-hook")
    e2 = await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="b",
        action_id="act2", target="q.txt", agent_id="ag", adapter="shell-hook")
    assert e1 == e2  # same execution
    ex = await store.get_execution(redis_client, "sess", "s1")
    assert set(ex["observed"]) == {"a", "b"}
    assert ex["observed"]["a"][0]["action_id"] == "act1"
    assert await redis_client.ttl(store.exec_key("sess", "s1")) > 0


@pytest.mark.asyncio
async def test_warn_is_claimed_once_per_execution_and_step(redis_client):
    s = _Settings()
    await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="a",
        action_id="x", target="t", agent_id="ag", adapter="shell-hook")
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="z") is True
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="z") is False
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="y") is True


@pytest.mark.asyncio
async def test_proposals_round_trip_and_dismiss(redis_client):
    await store.write_proposals(redis_client, "s1", [
        {"id": "p1", "kind": "dead_step", "step_id": "a", "detail": "d"},
    ])
    got = await store.list_proposals(redis_client, "s1")
    assert len(got) == 1
    assert await store.dismiss_proposal(redis_client, "p1") is True
    assert await store.list_proposals(redis_client, "s1") == []
    assert await store.dismiss_proposal(redis_client, "nope") is False


@pytest.mark.asyncio
async def test_rebuild_replaces_rather_than_appends(redis_client):
    s = _Settings()
    v1 = _Vector([_skill("s1", "t", [
        {"id": "a", "text": "x", "kind": "file_glob", "pattern": "1", "load_bearing": False}])])
    await store.rebuild_index(v1, redis_client, s)
    v2 = _Vector([_skill("s1", "t", [
        {"id": "b", "text": "y", "kind": "file_glob", "pattern": "2", "load_bearing": False}])])
    await store.rebuild_index(v2, redis_client, s)
    idx = await store.load_index(redis_client)
    assert [e["step_id"] for e in idx] == ["b"]


@pytest.mark.asyncio
async def test_a_corrupt_index_reads_as_empty_never_raises(redis_client):
    await redis_client.set(store.INDEX_KEY, "{not json")
    assert await store.load_index(redis_client) == []
