"""Tests for replay emitter Redis socket-timeout hardening (SP0 D7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from replay.config import ReplaySettings


def _settings() -> ReplaySettings:
    return ReplaySettings(ENABLED=True, REDIS_URL="redis://localhost:6379/6")


@pytest.mark.asyncio
async def test_init_emitter_with_url_sets_socket_timeouts():
    import replay.emitter as em

    em._redis = None
    fake = MagicMock()
    with patch("replay.emitter.aioredis.from_url", return_value=fake) as mock_from_url:
        await em.init_emitter(redis_url="redis://localhost:6379/6", settings=_settings())

    kwargs = mock_from_url.call_args.kwargs
    assert kwargs["socket_timeout"] == 5
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["health_check_interval"] == 30
    em._redis = None


@pytest.mark.asyncio
async def test_init_emitter_default_url_sets_socket_timeouts():
    import replay.emitter as em

    em._redis = None
    fake = MagicMock()
    with patch("replay.emitter.aioredis.from_url", return_value=fake) as mock_from_url:
        await em.init_emitter(settings=_settings())

    kwargs = mock_from_url.call_args.kwargs
    assert kwargs["socket_timeout"] == 5
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["health_check_interval"] == 30
    em._redis = None
