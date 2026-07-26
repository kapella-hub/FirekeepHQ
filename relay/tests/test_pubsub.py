"""Tests for FirekeepRelay pubsub module."""

import pytest

from app.pubsub import broadcast, get_backlog


@pytest.mark.asyncio
async def test_broadcast_and_get_backlog_roundtrip(redis):
    """Messages broadcast to a channel appear in the backlog."""
    await broadcast(redis, "test-chan", "hello world", "agent-1", ["tag1"], backlog_size=50)
    await broadcast(redis, "test-chan", "second msg", "agent-2", [], backlog_size=50)

    messages = await get_backlog(redis, "test-chan", limit=10)
    assert len(messages) == 2
    # Newest first (lpush order)
    assert messages[0]["content"] == "second msg"
    assert messages[1]["content"] == "hello world"
    assert messages[0]["sender"] == "agent-2"
    assert messages[1]["tags"] == ["tag1"]


@pytest.mark.asyncio
async def test_backlog_trimming(redis):
    """Backlog is trimmed to backlog_size."""
    for i in range(10):
        await broadcast(redis, "trim-chan", f"msg-{i}", "agent", [], backlog_size=5)

    messages = await get_backlog(redis, "trim-chan", limit=20)
    assert len(messages) == 5
    # Most recent messages kept
    assert messages[0]["content"] == "msg-9"
    assert messages[4]["content"] == "msg-5"


@pytest.mark.asyncio
async def test_backlog_ttl_is_set(redis):
    """Backlog keys get a TTL after broadcast."""
    await broadcast(redis, "ttl-chan", "msg", "agent", [], backlog_size=50, backlog_ttl_seconds=3600)

    ttl = await redis.ttl("nr:backlog:ttl-chan")
    assert 0 < ttl <= 3600


@pytest.mark.asyncio
async def test_get_backlog_empty_channel(redis):
    """Getting backlog from non-existent channel returns empty list."""
    messages = await get_backlog(redis, "nonexistent", limit=10)
    assert messages == []


@pytest.mark.asyncio
async def test_get_backlog_corrupt_data(redis):
    """Corrupt JSON in backlog is skipped gracefully."""
    await redis.lpush("nr:backlog:corrupt-chan", "not-json")
    await redis.lpush("nr:backlog:corrupt-chan", '{"content": "valid", "sender": "a", "tags": [], "timestamp": 1.0}')

    messages = await get_backlog(redis, "corrupt-chan", limit=10)
    assert len(messages) == 1
    assert messages[0]["content"] == "valid"
