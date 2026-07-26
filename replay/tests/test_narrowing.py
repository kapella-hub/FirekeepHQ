"""Tests for the narrowing algorithm."""


import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter
from replay.narrowing import narrow


@pytest_asyncio.fixture
async def redis_client():
    r = aioredis.from_url("redis://localhost:6379/6", decode_responses=True)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not available on localhost:6379")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def emitter(redis_client):
    settings = ReplaySettings(
        ENABLED=True,
        REDIS_URL="redis://localhost:6379/6",
        STREAM_MAXLEN=1000,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield
    await close_emitter()


async def _emit_and_get_id(redis_client, **kwargs) -> str:
    """Emit an event and return its event ID (not stream ID)."""
    await emit(**kwargs)
    entries = await redis_client.xrevrange("rp:events", count=1)
    _, fields = entries[0]
    return fields["id"]


class TestNarrowing:
    @pytest.mark.asyncio
    async def test_nonexistent_failure_event(self, emitter, redis_client):
        result = await narrow(redis_client, "sess-1", "nonexistent")
        assert result["suspects"] == []
        assert result["total_events_walked"] == 0

    @pytest.mark.asyncio
    async def test_failure_with_no_links(self, emitter, redis_client):
        """A failure event with no trace links should still return
        temporal neighbors as low-confidence suspects."""
        # Emit a sequence of events in the same session
        await emit(
            event_type="memory_read", session_id="sess-1", agent_id="a1",
            payload={"query": "auth"},
        )
        failure_id = await _emit_and_get_id(
            redis_client,
            event_type="ctx_update", session_id="sess-1", agent_id="a1",
            payload={"category": "progress", "content": "failed"},
            outcome="failure",
        )

        result = await narrow(redis_client, "sess-1", failure_id)
        # Should find the memory_read as a temporal neighbor
        assert result["failure_event_id"] == failure_id

    @pytest.mark.asyncio
    async def test_failure_with_declared_link(self, emitter, redis_client):
        """A failure with an explicit declared trace link should rank the
        linked event as highest suspect."""
        # Emit cause event
        cause_id = await _emit_and_get_id(
            redis_client,
            event_type="memory_read", session_id="sess-2", agent_id="a1",
            payload={"query": "stale data", "result_count": 1},
            outcome="success",
        )

        # Emit failure event with declared link to cause
        failure_id = await _emit_and_get_id(
            redis_client,
            event_type="ctx_update", session_id="sess-2", agent_id="a1",
            payload={"category": "decision", "content": "wrong approach"},
            outcome="failure",
            trace_links=[{
                "target_event_id": cause_id,
                "link_type": "declared",
                "relationship": "informed_by",
                "confidence": 0.95,
            }],
        )

        result = await narrow(redis_client, "sess-2", failure_id)
        assert len(result["suspects"]) >= 1
        # The declared link should be the top suspect
        top = result["suspects"][0]
        assert top["event"]["id"] == cause_id
        assert top["depth"] == 1
        assert top["suspicion_score"] > 0.5

    @pytest.mark.asyncio
    async def test_multi_hop_chain(self, emitter, redis_client):
        """Test that narrowing follows multi-hop trace link chains."""
        # Chain: root → middle → failure
        root_id = await _emit_and_get_id(
            redis_client,
            event_type="env_change", session_id="sess-3", agent_id="a1",
            payload={"source": "docker", "summary": "container restarted"},
        )

        middle_id = await _emit_and_get_id(
            redis_client,
            event_type="memory_read", session_id="sess-3", agent_id="a1",
            payload={"query": "recovery"},
            trace_links=[{
                "target_event_id": root_id,
                "link_type": "observed",
                "relationship": "preceded",
                "confidence": 0.7,
            }],
        )

        failure_id = await _emit_and_get_id(
            redis_client,
            event_type="ctx_update", session_id="sess-3", agent_id="a1",
            payload={"category": "progress"},
            outcome="failure",
            trace_links=[{
                "target_event_id": middle_id,
                "link_type": "declared",
                "relationship": "informed_by",
                "confidence": 0.9,
            }],
        )

        result = await narrow(redis_client, "sess-3", failure_id)
        suspect_ids = [s["event"]["id"] for s in result["suspects"]]

        # Both middle and root should be suspects
        assert middle_id in suspect_ids
        assert root_id in suspect_ids

        # Middle should have higher score than root (closer)
        middle_score = next(s["suspicion_score"] for s in result["suspects"] if s["event"]["id"] == middle_id)
        root_score = next(s["suspicion_score"] for s in result["suspects"] if s["event"]["id"] == root_id)
        assert middle_score > root_score

    @pytest.mark.asyncio
    async def test_max_depth_limits_traversal(self, emitter, redis_client):
        result = await narrow(
            redis_client, "sess-1", "nonexistent",
            max_depth=0,
        )
        assert result["total_events_walked"] == 0

    @pytest.mark.asyncio
    async def test_max_results_limits_output(self, emitter, redis_client):
        # Create several events linked to a failure
        event_ids = []
        for i in range(5):
            eid = await _emit_and_get_id(
                redis_client,
                event_type="memory_read", session_id="sess-4", agent_id="a1",
                payload={"query": f"q{i}"},
            )
            event_ids.append(eid)

        failure_id = await _emit_and_get_id(
            redis_client,
            event_type="ctx_update", session_id="sess-4", agent_id="a1",
            payload={}, outcome="failure",
            trace_links=[
                {
                    "target_event_id": eid,
                    "link_type": "observed",
                    "relationship": "preceded",
                    "confidence": 0.5,
                }
                for eid in event_ids
            ],
        )

        result = await narrow(
            redis_client, "sess-4", failure_id,
            max_results=2,
        )
        assert len(result["suspects"]) <= 2
