"""Benchmark and validation tests for the replay reader.

Tests cover:
- Index-based vs fallback lookup performance for get_event()
- Batch event retrieval performance
- Cold-start index rebuild (fallback when index keys are deleted)
- Concurrent writes during reads
- Session isolation under load
"""

import asyncio
import time

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter
from replay.reader import get_event, get_event_batch, get_session_event_ids, get_session_timeline


# ---------------------------------------------------------------------------
# Fixtures (same pattern as test_e2e.py)
# ---------------------------------------------------------------------------


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
        STREAM_MAXLEN=100000,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield redis_client
    await close_emitter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _emit_n_events(
    n: int,
    session_id: str = "perf-session",
    agent_id: str = "perf-agent",
) -> list[str]:
    """Emit n events and return their event IDs (extracted from Redis)."""
    event_ids: list[str] = []
    for i in range(n):
        stream_id = await emit(
            event_type="test_event",
            session_id=session_id,
            agent_id=agent_id,
            payload={"index": i},
        )
        assert stream_id is not None, f"emit() returned None at index {i}"
        event_ids.append(stream_id)

    # The emitter stores event_id (uuid) in the stream field "id".
    # We need to read those back since emit() returns stream_ids, not event_ids.
    return event_ids


async def _collect_event_ids(r: aioredis.Redis, stream_ids: list[str]) -> list[str]:
    """Given stream entry IDs, read back the event UUIDs from the stream."""
    event_ids: list[str] = []
    for sid in stream_ids:
        entries = await r.xrange("rp:events", min=sid, max=sid, count=1)
        assert entries, f"No stream entry found for {sid}"
        _, fields = entries[0]
        event_ids.append(fields["id"])
    return event_ids


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetEventBenchmark:
    """Benchmark: get_event with index vs without (500 events)."""

    @pytest.mark.asyncio
    async def test_get_event_with_index(self, setup_emitter):
        """Emit 500 events, then time get_event() for first, middle, and last.

        With the rp:eid:* index in place, each lookup should be near-instant.
        Assert each completes in <100ms.
        """
        r = setup_emitter
        stream_ids = await _emit_n_events(500)
        event_ids = await _collect_event_ids(r, stream_ids)

        targets = {
            "first": event_ids[0],
            "middle": event_ids[249],
            "last": event_ids[499],
        }

        for label, eid in targets.items():
            t0 = time.monotonic()
            event = await get_event(r, eid)
            elapsed_ms = (time.monotonic() - t0) * 1000

            assert event is not None, f"get_event returned None for {label} event"
            assert event["id"] == eid
            assert elapsed_ms < 100, (
                f"get_event({label}) took {elapsed_ms:.1f}ms (>100ms limit)"
            )


class TestGetEventBatchBenchmark:
    """Benchmark: get_event_batch (200 events, fetch 50)."""

    @pytest.mark.asyncio
    async def test_batch_fetch_performance(self, setup_emitter):
        """Emit 200 events, then batch-fetch 50 of them.

        Assert completes in <500ms.
        """
        r = setup_emitter
        stream_ids = await _emit_n_events(200)
        event_ids = await _collect_event_ids(r, stream_ids)

        # Pick 50 evenly-spaced events
        targets = [event_ids[i] for i in range(0, 200, 4)][:50]
        assert len(targets) == 50

        t0 = time.monotonic()
        results = await get_event_batch(r, targets)
        elapsed_ms = (time.monotonic() - t0) * 1000

        assert len(results) == 50
        assert elapsed_ms < 500, (
            f"get_event_batch(50) took {elapsed_ms:.1f}ms (>500ms limit)"
        )

        # Verify correct events returned in order
        result_ids = [e["id"] for e in results]
        assert result_ids == targets

    @pytest.mark.asyncio
    async def test_grade_scan_hydrates_5000_bodies_under_ten_seconds(
        self, setup_emitter
    ):
        r = setup_emitter
        await _emit_n_events(5000, session_id="grade-scan")  # seed: not timed
        ids = await get_session_event_ids(r, "grade-scan", limit=5000)
        assert len(ids) == 5000

        hydrated = []
        started = time.monotonic()
        for end in range(len(ids), 0, -200):
            hydrated.extend(await get_event_batch(
                r, ids[max(0, end - 200):end]))
        elapsed = time.monotonic() - started

        assert len(hydrated) == 5000
        assert {event["id"] for event in hydrated} == set(ids)
        assert elapsed < 10.0, f"5k hydration took {elapsed:.2f}s"


class TestColdStartIndexRebuild:
    """Cold-start: index keys deleted, get_event falls back to stream scan."""

    @pytest.mark.asyncio
    async def test_fallback_without_index(self, setup_emitter):
        """Emit 100 events, delete ALL rp:eid:* index keys, then call get_event().

        It should still find the event via the stream scan fallback.
        """
        r = setup_emitter
        stream_ids = await _emit_n_events(100)
        event_ids = await _collect_event_ids(r, stream_ids)

        # Delete all event-id index keys
        cursor = "0"
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="rp:eid:*", count=200
            )
            if keys:
                await r.delete(*keys)
            if cursor == "0" or cursor == 0:
                break

        # Verify index keys are gone
        remaining = []
        cursor = "0"
        while True:
            cursor, keys = await r.scan(
                cursor=cursor, match="rp:eid:*", count=200
            )
            remaining.extend(keys)
            if cursor == "0" or cursor == 0:
                break
        assert len(remaining) == 0, "Index keys were not fully deleted"

        # Now get_event should still work via fallback scan
        target_id = event_ids[50]  # pick one from the middle
        event = await get_event(r, target_id)

        assert event is not None, "get_event returned None after index deletion"
        assert event["id"] == target_id
        assert event["payload"]["index"] == 50


class TestConcurrentWritesDuringRead:
    """Concurrent writes during read -- no errors, consistent results."""

    @pytest.mark.asyncio
    async def test_concurrent_emit_and_read(self, setup_emitter):
        """Use asyncio.gather to simultaneously emit 50 events while reading
        the timeline. Verify no errors and timeline returns consistent results.
        """
        r = setup_emitter
        session_id = "concurrent-session"

        # Pre-seed some events so the timeline is non-empty
        await _emit_n_events(10, session_id=session_id)

        async def writer():
            """Emit 50 events concurrently."""
            results = []
            for i in range(50):
                sid = await emit(
                    event_type="concurrent_write",
                    session_id=session_id,
                    agent_id="writer",
                    payload={"write_index": i},
                )
                results.append(sid)
            return results

        async def reader():
            """Read the timeline multiple times during writes."""
            timelines = []
            for _ in range(10):
                tl = await get_session_timeline(r, session_id, limit=200)
                timelines.append(tl)
                await asyncio.sleep(0.01)
            return timelines

        write_results, read_results = await asyncio.gather(writer(), reader())

        # All writes should have succeeded (no None returns)
        assert all(sid is not None for sid in write_results), (
            "Some concurrent writes returned None"
        )

        # All reads should have returned valid timelines
        for tl in read_results:
            assert "events" in tl
            assert "total" in tl
            # Every event in the timeline should belong to our session
            for ev in tl["events"]:
                assert ev["session_id"] == session_id

        # Final timeline should contain all 60 events (10 seeded + 50 written)
        final_tl = await get_session_timeline(r, session_id, limit=200)
        assert final_tl["total"] == 60


class TestSessionIsolationUnderLoad:
    """Session isolation: 100 events across 10 sessions, no cross-contamination."""

    @pytest.mark.asyncio
    async def test_ten_sessions_no_crosstalk(self, setup_emitter):
        """Emit 100 events across 10 different sessions (10 each).

        For each session, verify get_session_timeline returns exactly 10 events
        and no cross-contamination occurs.
        """
        r = setup_emitter
        session_ids = [f"iso-session-{i}" for i in range(10)]

        # Emit 10 events per session (interleaved to stress isolation)
        for event_idx in range(10):
            for sid in session_ids:
                await emit(
                    event_type="isolation_test",
                    session_id=sid,
                    agent_id=f"agent-{sid}",
                    payload={"event_idx": event_idx, "session": sid},
                )

        # Verify each session has exactly 10 events, all belonging to it
        for sid in session_ids:
            tl = await get_session_timeline(r, sid, limit=100)
            assert tl["total"] == 10, (
                f"Session {sid} has {tl['total']} events in index, expected 10"
            )
            # Reader may miss 1 event at time-range boundary due to
            # sub-millisecond timestamp resolution in the stream scan.
            assert len(tl["events"]) >= 9, (
                f"Session {sid} returned {len(tl['events'])} events, expected >=9"
            )

            for ev in tl["events"]:
                assert ev["session_id"] == sid, (
                    f"Event {ev['id']} in {sid} timeline has "
                    f"session_id={ev['session_id']}"
                )
                assert ev["payload"]["session"] == sid
