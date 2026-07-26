"""Tests for collector error handling and health status tracking."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from app.collectors.docker import run_docker_collector
from app.collectors.git import run_git_collector
from app.collectors.files import run_file_collector


pytestmark = pytest.mark.asyncio


def _make_settings(**overrides):
    """Create a mock settings object with sensible defaults."""
    defaults = {
        "DOCKER_SOCKET": "/var/run/docker.sock",
        "POLL_INTERVAL_DOCKER": 1,
        "POLL_INTERVAL_GIT": 1,
        "POLL_INTERVAL_FILES": 1,
        "WATCH_PATHS": "",
        "EVENT_MAXLEN": 10000,
    }
    defaults.update(overrides)
    settings = MagicMock()
    for k, v in defaults.items():
        setattr(settings, k, v)
    return settings


async def test_docker_collector_logs_on_error(caplog):
    """Docker collector should log warnings on errors, not silently pass."""
    stop_event = asyncio.Event()
    redis_mock = AsyncMock()

    settings = _make_settings()

    # Make httpx raise to simulate Docker socket unavailable
    with patch("app.collectors.docker.httpx.AsyncHTTPTransport", side_effect=Exception("socket gone")):
        # Run for one iteration then stop
        async def stop_after_brief():
            await asyncio.sleep(0.05)
            stop_event.set()

        with caplog.at_level(logging.WARNING):
            await asyncio.gather(
                run_docker_collector(redis_mock, settings, stop_event),
                stop_after_brief(),
            )

    assert any("docker" in r.message.lower() for r in caplog.records)


async def test_git_collector_logs_on_error(caplog):
    """Git collector should log warnings on errors."""
    stop_event = asyncio.Event()
    redis_mock = AsyncMock()
    # smembers raises to simulate Redis failure
    redis_mock.smembers = AsyncMock(side_effect=Exception("redis down"))

    settings = _make_settings()

    async def stop_after_brief():
        await asyncio.sleep(0.05)
        stop_event.set()

    with caplog.at_level(logging.WARNING):
        await asyncio.gather(
            run_git_collector(redis_mock, settings, stop_event),
            stop_after_brief(),
        )

    assert any("git" in r.message.lower() for r in caplog.records)


async def test_file_collector_logs_on_error(caplog):
    """File collector should log warnings on errors."""
    stop_event = asyncio.Event()
    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(side_effect=Exception("redis down"))

    settings = _make_settings()

    async def stop_after_brief():
        await asyncio.sleep(0.05)
        stop_event.set()

    with caplog.at_level(logging.WARNING):
        await asyncio.gather(
            run_file_collector(redis_mock, settings, stop_event),
            stop_after_brief(),
        )

    assert any("files" in r.message.lower() for r in caplog.records)


async def test_docker_collector_healthy_flag():
    """Collector healthy flag should be False after an error."""
    from app.collectors.docker import get_collector
    collector = get_collector()
    # Reset
    collector.healthy = True

    stop_event = asyncio.Event()
    redis_mock = AsyncMock()
    settings = _make_settings()

    with patch("app.collectors.docker.httpx.AsyncHTTPTransport", side_effect=Exception("fail")):
        async def stop_after_brief():
            await asyncio.sleep(0.05)
            stop_event.set()

        await asyncio.gather(
            run_docker_collector(redis_mock, settings, stop_event),
            stop_after_brief(),
        )

    assert collector.healthy is False


async def test_git_collector_healthy_flag():
    """Git collector healthy flag should be False after an error."""
    from app.collectors.git import get_collector
    collector = get_collector()
    collector.healthy = True

    stop_event = asyncio.Event()
    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(side_effect=Exception("fail"))
    settings = _make_settings()

    async def stop_after_brief():
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.gather(
        run_git_collector(redis_mock, settings, stop_event),
        stop_after_brief(),
    )

    assert collector.healthy is False


async def test_file_collector_healthy_flag():
    """File collector healthy flag should be False after an error."""
    from app.collectors.files import get_collector
    collector = get_collector()
    collector.healthy = True

    stop_event = asyncio.Event()
    redis_mock = AsyncMock()
    redis_mock.smembers = AsyncMock(side_effect=Exception("fail"))
    settings = _make_settings()

    async def stop_after_brief():
        await asyncio.sleep(0.05)
        stop_event.set()

    await asyncio.gather(
        run_file_collector(redis_mock, settings, stop_event),
        stop_after_brief(),
    )

    assert collector.healthy is False
