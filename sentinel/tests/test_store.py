"""Tests for the event store (push_event, get_events, trim_by_age)."""

from __future__ import annotations

import time

import pytest

from app.store import push_event, get_events, get_event_count, trim_by_age


pytestmark = pytest.mark.asyncio


async def test_push_event_returns_id(redis):
    entry_id = await push_event(redis, "test", "test.event", "hello world")
    assert entry_id is not None
    assert isinstance(entry_id, str)


async def test_push_and_get_events(redis):
    await push_event(redis, "src1", "type.a", "first event")
    await push_event(redis, "src2", "type.b", "second event")

    events = await get_events(redis)
    assert len(events) == 2
    # Newest first
    assert events[0]["summary"] == "second event"
    assert events[1]["summary"] == "first event"


async def test_get_events_filter_by_source(redis):
    await push_event(redis, "docker", "container.running", "up")
    await push_event(redis, "git", "commit.new", "new commit")

    events = await get_events(redis, source="docker")
    assert len(events) == 1
    assert events[0]["source"] == "docker"


async def test_get_events_filter_by_event_type(redis):
    await push_event(redis, "docker", "container.running", "up")
    await push_event(redis, "docker", "container.stopped", "down")

    events = await get_events(redis, event_type="container.stopped")
    assert len(events) == 1
    assert events[0]["event_type"] == "container.stopped"


async def test_get_events_filter_by_severity(redis):
    await push_event(redis, "test", "t", "info event", severity="info")
    await push_event(redis, "test", "t", "warning event", severity="warning")

    events = await get_events(redis, severity="warning")
    assert len(events) == 1
    assert events[0]["severity"] == "warning"


async def test_get_events_with_limit(redis):
    for i in range(10):
        await push_event(redis, "test", "t", f"event {i}")

    events = await get_events(redis, limit=3)
    assert len(events) == 3


async def test_get_events_with_since(redis):
    # Push an old event with a manipulated timestamp
    await push_event(redis, "test", "t", "old event")
    # Small delay to separate timestamps in the stream
    before = time.time()
    await push_event(redis, "test", "t", "new event")

    events = await get_events(redis, since=before)
    assert len(events) >= 1
    assert events[0]["summary"] == "new event"


async def test_get_event_count(redis):
    assert await get_event_count(redis) == 0
    await push_event(redis, "test", "t", "one")
    await push_event(redis, "test", "t", "two")
    assert await get_event_count(redis) == 2


async def test_push_event_respects_maxlen(redis):
    for i in range(20):
        await push_event(redis, "test", "t", f"event {i}", maxlen=10)
    count = await get_event_count(redis)
    # approximate trimming means count might be slightly above maxlen
    assert count <= 15


async def test_trim_by_age(redis):
    # Push events and then trim with a negative-hour window (everything is "old")
    await push_event(redis, "test", "t", "event1")
    await push_event(redis, "test", "t", "event2")
    assert await get_event_count(redis) == 2

    # Use a very large negative retention to ensure cutoff is in the future,
    # guaranteeing all events are considered "old" and get deleted.
    # We pass max_age_hours=-1 which makes cutoff = now + 3600 seconds (future).
    deleted = await trim_by_age(redis, max_age_hours=-1)
    assert deleted == 2
    assert await get_event_count(redis) == 0


async def test_trim_by_age_keeps_recent(redis):
    await push_event(redis, "test", "t", "recent event")
    # Trim with a large window should keep everything
    deleted = await trim_by_age(redis, max_age_hours=24)
    assert deleted == 0
    assert await get_event_count(redis) == 1
