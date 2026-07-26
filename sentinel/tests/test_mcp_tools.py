"""Tests for MCP tool functions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.store import push_event


pytestmark = pytest.mark.asyncio


async def _noop():
    pass


async def _patch_redis_and_call(redis, coro):
    """Call an MCP tool function with our fake redis injected."""
    with patch("app.mcp_server.get_redis", return_value=redis), \
         patch("app.mcp_server._ensure_collectors", side_effect=_noop):
        return await coro


async def test_sentinel_get_health_newest_state_wins(redis):
    """The newest event per container should determine its state."""
    from app.mcp_server import sentinel_get_health

    # Push older event: container stopped
    await push_event(redis, "docker", "container.exited", "stopped",
                     {"container": "web", "state": "exited", "status": "Exited (0)"})
    # Push newer event: container running
    await push_event(redis, "docker", "container.running", "running",
                     {"container": "web", "state": "running", "status": "Up 5 min"})

    result = await _patch_redis_and_call(redis, sentinel_get_health())

    assert result["containers"]["web"]["state"] == "running"
    assert result["healthy"] is True


async def test_sentinel_get_health_empty(redis):
    from app.mcp_server import sentinel_get_health

    result = await _patch_redis_and_call(redis, sentinel_get_health())
    assert result["healthy"] is None
    assert result["container_count"] == 0


async def test_sentinel_push_event_ok(redis):
    from app.mcp_server import sentinel_push_event

    result = await _patch_redis_and_call(
        redis,
        sentinel_push_event(source="test", event_type="test.event", summary="hello"),
    )
    assert result["status"] == "accepted"
    assert "event_id" in result


async def test_sentinel_push_event_source_too_long(redis):
    from app.mcp_server import sentinel_push_event

    result = await _patch_redis_and_call(
        redis,
        sentinel_push_event(source="x" * 501, event_type="t", summary="s"),
    )
    assert "error" in result
    assert "source" in result["error"]


async def test_sentinel_push_event_event_type_too_long(redis):
    from app.mcp_server import sentinel_push_event

    result = await _patch_redis_and_call(
        redis,
        sentinel_push_event(source="s", event_type="x" * 201, summary="s"),
    )
    assert "error" in result
    assert "event_type" in result["error"]


async def test_sentinel_push_event_summary_too_long(redis):
    from app.mcp_server import sentinel_push_event

    result = await _patch_redis_and_call(
        redis,
        sentinel_push_event(source="s", event_type="t", summary="x" * 10001),
    )
    assert "error" in result
    assert "summary" in result["error"]


async def test_sentinel_push_event_invalid_severity(redis):
    from app.mcp_server import sentinel_push_event

    result = await _patch_redis_and_call(
        redis,
        sentinel_push_event(source="s", event_type="t", summary="s", severity="bad"),
    )
    assert "error" in result


async def test_sentinel_get_events(redis):
    from app.mcp_server import sentinel_get_events

    await push_event(redis, "test", "t", "event one")
    await push_event(redis, "test", "t", "event two")

    result = await _patch_redis_and_call(redis, sentinel_get_events(limit=10))
    assert result["returned"] == 2
    assert result["total_in_stream"] == 2
    assert len(result["events"]) == 2
