"""End-to-end integration test — full replay + eval flow against real Redis.

Tests the complete lifecycle:
1. Emit a session's worth of trace events
2. Query the timeline
3. Compute eval metrics
4. Run narrowing on a failure event
5. Store and retrieve context snapshots
"""

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter, store_context_snapshot
from replay.reader import get_session_timeline, get_session_summary, get_event, get_context_at
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
async def setup_emitter(redis_client):
    settings = ReplaySettings(
        ENABLED=True,
        REDIS_URL="redis://localhost:6379/6",
        STREAM_MAXLEN=10000,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield redis_client
    await close_emitter()


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, setup_emitter):
        r = setup_emitter
        session_id = "e2e-session-1"
        agent_id = "test-agent"

        # 1. Session start
        await emit("session_start", session_id, agent_id, {"goal": "fix auth bug"})

        # 2. Memory recall
        await emit(
            "memory_read", session_id, agent_id,
            {"query": "auth middleware", "result_count": 3, "top_score": 0.85},
            outcome="success",
        )

        # Get the event ID from the stream for later use
        entries = await r.xrevrange("rp:events", count=1)
        _, fields = entries[0]
        recall_event_id = fields["id"]

        # 3. Context update (decision point — should get snapshot)
        shadow_content = "## Plan\n- [x] Check middleware\n- [ ] Fix CORS"
        ctx_ref = await store_context_snapshot(shadow_content)
        assert ctx_ref is not None

        await emit(
            "ctx_update", session_id, agent_id,
            {"category": "decision", "content_length": 42},
            context_ref=ctx_ref,
            trace_links=[{
                "target_event_id": recall_event_id,
                "link_type": "declared",
                "relationship": "informed_by",
                "confidence": 0.9,
            }],
        )

        # 4. Memory write
        await emit(
            "memory_write", session_id, agent_id,
            {"action_summary": "Fixed CORS ordering", "memory_type": "episodic"},
            outcome="success",
        )

        # 5. A failure event
        await emit(
            "ctx_update", session_id, agent_id,
            {"category": "progress", "content_length": 20},
            outcome="failure",
            error="Tests still failing after fix",
            trace_links=[{
                "target_event_id": recall_event_id,
                "link_type": "inferred",
                "relationship": "informed_by",
                "confidence": 0.6,
            }],
        )

        # 6. Session end
        await emit(
            "session_end", session_id, agent_id,
            {"outcome": "partial", "distilled": True},
            outcome="partial",
        )

        # ---- Now query everything ----

        # Timeline
        timeline = await get_session_timeline(r, session_id)
        assert timeline["total"] == 6
        events = timeline["events"]
        assert events[0]["event_type"] == "session_start"
        assert events[-1]["event_type"] == "session_end"

        # Summary
        summary = await get_session_summary(r, session_id)
        assert summary["event_count"] == 6
        assert summary["has_failures"] is True
        assert "memory_read" in summary["event_type_counts"]
        assert len(summary["agents"]) == 1

        # Single event lookup (uses the O(1) index)
        event = await get_event(r, recall_event_id)
        assert event is not None
        assert event["event_type"] == "memory_read"
        assert event["payload"]["top_score"] == 0.85

        # Context at decision point
        decision_events = [e for e in events if e.get("context_ref")]
        assert len(decision_events) >= 1
        ctx = await get_context_at(r, session_id, decision_events[0]["id"])
        assert ctx["snapshot_type"] == "exact"
        assert "Plan" in ctx["context"]

        # Narrowing from the failure event
        failure_events = [e for e in events if e.get("outcome") == "failure"]
        assert len(failure_events) >= 1
        failure_id = failure_events[0]["id"]

        result = await narrow(r, session_id, failure_id)
        assert len(result["suspects"]) >= 1
        # The recall event should be a suspect (declared link from failure)
        suspect_ids = [s["event"]["id"] for s in result["suspects"]]
        assert recall_event_id in suspect_ids

    @pytest.mark.asyncio
    async def test_eval_computation(self, setup_emitter):
        """Test that eval metrics are correctly computed from replay events."""
        r = setup_emitter
        session_id = "e2e-eval-1"

        # Emit some events
        await emit("session_start", session_id, "agent", {"goal": "test"})
        await emit("memory_read", session_id, "agent",
                    {"query": "q1", "top_score": 0.9}, outcome="success")
        await emit("memory_read", session_id, "agent",
                    {"query": "q2", "top_score": 0.7}, outcome="success")
        await emit("ctx_update", session_id, "agent",
                    {"category": "plan"}, outcome="success")
        await emit("memory_write", session_id, "agent",
                    {"action_summary": "wrote"}, outcome="failure")
        await emit("session_end", session_id, "agent",
                    {"outcome": "partial"}, outcome="partial")

        # Compute eval with CORTEX's scorers — this is the one test proving the
        # replay stream and the eval layer agree on event shape. `app` is
        # cortex's package, not an installed distribution, and this suite runs
        # from the repo root (CLAUDE.md's documented invocation), so the path
        # is added explicitly. It had never actually executed anywhere: CI's
        # shared-modules job has no Redis (the fixture skips), and every local
        # run failed on this import since the initial commit.
        import sys
        from pathlib import Path
        cortex_dir = str(Path(__file__).resolve().parents[2] / "cortex")
        if cortex_dir not in sys.path:
            sys.path.insert(0, cortex_dir)
        from app.evals.scorers import compute_tier1_metrics
        timeline = await get_session_timeline(r, session_id, limit=100)
        events = timeline["events"]
        assert len(events) == 6

        metrics = compute_tier1_metrics(events)
        assert metrics["event_count"] == 6.0
        assert metrics["memory_read_count"] == 2.0
        assert metrics["memory_write_count"] == 1.0
        assert metrics["memory_freshness_at_recall"] == 0.8  # avg(0.9, 0.7)
        # 3 success, 1 failure, 1 partial = 5 with outcomes, 3/5 = 0.6
        assert metrics["tool_success_rate"] == 0.6
        assert metrics["failure_rate"] == 0.2  # 1/5

    @pytest.mark.asyncio
    async def test_idempotency_across_session(self, setup_emitter):
        """Duplicate events with same idempotency key are silently dropped."""
        r = setup_emitter

        id1 = await emit(
            "memory_write", "s1", "a1", {"x": 1},
            idempotency_key="idem-test-1",
        )
        id2 = await emit(
            "memory_write", "s1", "a1", {"x": 2},
            idempotency_key="idem-test-1",
        )
        assert id1 is not None
        assert id2 is None  # Duplicate dropped

        timeline = await get_session_timeline(r, "s1")
        assert timeline["total"] == 1

    @pytest.mark.asyncio
    async def test_multi_session_isolation(self, setup_emitter):
        """Events from different sessions don't leak into each other's timelines."""
        r = setup_emitter

        await emit("session_start", "sess-A", "a1", {"goal": "A"})
        await emit("memory_read", "sess-A", "a1", {"query": "A"})

        await emit("session_start", "sess-B", "a2", {"goal": "B"})
        await emit("memory_read", "sess-B", "a2", {"query": "B"})
        await emit("memory_write", "sess-B", "a2", {"action": "B"})

        timeline_a = await get_session_timeline(r, "sess-A")
        timeline_b = await get_session_timeline(r, "sess-B")

        assert timeline_a["total"] == 2
        assert timeline_b["total"] == 3
        assert all(e["session_id"] == "sess-A" for e in timeline_a["events"])
        assert all(e["session_id"] == "sess-B" for e in timeline_b["events"])
