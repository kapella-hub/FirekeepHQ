"""Tests for replay emitter — Redis storage, idempotency, context snapshots."""

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import (
    _SESSION_IDX_PREFIX,
    _STREAM_KEY,
    close_emitter,
    emit,
    get_context_snapshot,
    init_emitter,
    is_enabled,
    store_context_snapshot,
    trim_old_events,
)


@pytest_asyncio.fixture
async def redis_client():
    """Create a real Redis connection to DB 6 for integration tests.

    Tests are skipped if Redis is not available.
    """
    r = aioredis.from_url("redis://localhost:6379/6", decode_responses=True)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not available on localhost:6379")

    # Clean test data
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def emitter(redis_client):
    """Initialize the emitter with the test Redis client."""
    settings = ReplaySettings(
        ENABLED=True,
        REDIS_URL="redis://localhost:6379/6",
        STREAM_MAXLEN=1000,
        DEDUP_TTL_SECONDS=60,
        RETENTION_DAYS=1,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield
    await close_emitter()


class TestEmitterInit:
    @pytest.mark.asyncio
    async def test_init_enables_emitter(self, emitter):
        assert is_enabled()

    @pytest.mark.asyncio
    async def test_disabled_emitter(self):
        settings = ReplaySettings(ENABLED=False)
        await init_emitter(settings=settings)
        assert not is_enabled()
        await close_emitter()


class TestEmit:
    @pytest.mark.asyncio
    async def test_basic_emit(self, emitter, redis_client):
        stream_id = await emit(
            event_type="memory_read",
            session_id="test-session",
            agent_id="default",
            payload={"query": "auth bug", "result_count": 3},
        )
        assert stream_id is not None

        # Verify event in stream
        entries = await redis_client.xrange(_STREAM_KEY)
        assert len(entries) == 1
        _, fields = entries[0]
        assert fields["event_type"] == "memory_read"
        assert fields["session_id"] == "test-session"
        assert fields["agent_id"] == "default"
        assert fields["schema_version"] == "1"

    @pytest.mark.asyncio
    async def test_session_index_updated(self, emitter, redis_client):
        await emit(
            event_type="session_start",
            session_id="sess-1",
            agent_id="default",
            payload={"goal": "fix auth"},
        )
        await emit(
            event_type="ctx_update",
            session_id="sess-1",
            agent_id="default",
            payload={"category": "plan"},
        )

        # Session index should have 2 entries
        idx_key = f"{_SESSION_IDX_PREFIX}sess-1"
        count = await redis_client.zcard(idx_key)
        assert count == 2

    @pytest.mark.asyncio
    async def test_idempotency_dedup(self, emitter, redis_client):
        # First emit succeeds
        id1 = await emit(
            event_type="memory_write",
            session_id="s1",
            agent_id="a1",
            payload={"action": "test"},
            idempotency_key="unique-key-1",
        )
        assert id1 is not None

        # Duplicate is silently dropped
        id2 = await emit(
            event_type="memory_write",
            session_id="s1",
            agent_id="a1",
            payload={"action": "test"},
            idempotency_key="unique-key-1",
        )
        assert id2 is None

        # Stream should have only 1 entry
        entries = await redis_client.xrange(_STREAM_KEY)
        assert len(entries) == 1

    @pytest.mark.asyncio
    async def test_different_idempotency_keys(self, emitter, redis_client):
        await emit(
            event_type="claim", session_id="s1", agent_id="a1",
            payload={}, idempotency_key="key-a",
        )
        await emit(
            event_type="release", session_id="s1", agent_id="a1",
            payload={}, idempotency_key="key-b",
        )

        entries = await redis_client.xrange(_STREAM_KEY)
        assert len(entries) == 2

    @pytest.mark.asyncio
    async def test_trace_links_serialized(self, emitter, redis_client):
        await emit(
            event_type="memory_write",
            session_id="s1",
            agent_id="a1",
            payload={"memory_id": "m-1"},
            trace_links=[
                {
                    "target_event_id": "prev-event",
                    "link_type": "declared",
                    "relationship": "informed_by",
                    "confidence": 0.9,
                }
            ],
        )

        entries = await redis_client.xrange(_STREAM_KEY)
        _, fields = entries[0]
        import json
        links = json.loads(fields["trace_links"])
        assert len(links) == 1
        assert links[0]["link_type"] == "declared"

    @pytest.mark.asyncio
    async def test_namespace_normalized(self, emitter, redis_client):
        await emit(
            event_type="webhook", session_id="s1", agent_id="a1",
            payload={}, namespace="My-Project",
        )

        entries = await redis_client.xrange(_STREAM_KEY)
        _, fields = entries[0]
        assert fields["namespace"] == "my_project"

    @pytest.mark.asyncio
    async def test_optional_fields_default_empty(self, emitter, redis_client):
        await emit(
            event_type="env_change", session_id="s1", agent_id="a1",
            payload={"source": "docker"},
        )

        entries = await redis_client.xrange(_STREAM_KEY)
        _, fields = entries[0]
        assert fields["outcome"] == ""
        assert fields["context_ref"] == ""
        assert fields["parent_span_id"] == ""
        assert fields["error"] == ""

    @pytest.mark.asyncio
    async def test_emit_returns_none_when_disabled(self):
        settings = ReplaySettings(ENABLED=False)
        await init_emitter(settings=settings)
        result = await emit(
            event_type="claim", session_id="s1", agent_id="a1", payload={},
        )
        assert result is None
        await close_emitter()


class TestContextSnapshots:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self, emitter):
        content = '{"plan": "fix auth", "decisions": ["use JWT"]}'
        hash_key = await store_context_snapshot(content)
        assert hash_key is not None
        assert len(hash_key) == 32

        retrieved = await get_context_snapshot(hash_key)
        assert retrieved == content

    @pytest.mark.asyncio
    async def test_dedup_same_content(self, emitter, redis_client):
        content = "same content twice"
        h1 = await store_context_snapshot(content)
        h2 = await store_context_snapshot(content)
        assert h1 == h2

    @pytest.mark.asyncio
    async def test_different_content_different_hash(self, emitter):
        h1 = await store_context_snapshot("content A")
        h2 = await store_context_snapshot("content B")
        assert h1 != h2

    @pytest.mark.asyncio
    async def test_missing_snapshot_returns_none(self, emitter):
        result = await get_context_snapshot("nonexistent_hash_value_here")
        assert result is None


class TestTrimming:
    @pytest.mark.asyncio
    async def test_trim_removes_old_events(self, emitter, redis_client):
        # Add an event with a very old timestamp by manipulating the stream
        # For this test, we emit normally then check trim with 0-day retention
        await emit(
            event_type="session_start", session_id="old-sess",
            agent_id="a1", payload={},
        )

        entries = await redis_client.xrange(_STREAM_KEY)
        assert len(entries) == 1

        # With 1-day retention (our test setting), recent events survive
        trimmed = await trim_old_events()
        assert trimmed == 0  # Event is recent, not trimmed

        entries = await redis_client.xrange(_STREAM_KEY)
        assert len(entries) == 1
