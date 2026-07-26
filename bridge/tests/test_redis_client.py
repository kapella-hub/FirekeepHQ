"""Tests for Redis client socket-timeout hardening (SP0 D7)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_get_redis_sets_socket_timeouts():
    import app.redis_client as rc

    rc._redis = None
    fake = MagicMock()
    with patch("app.redis_client.aioredis.from_url", return_value=fake) as mock_from_url:
        client = await rc.get_redis()

    assert client is fake
    kwargs = mock_from_url.call_args.kwargs
    assert kwargs["decode_responses"] is True
    assert kwargs["socket_timeout"] == 5
    assert kwargs["socket_connect_timeout"] == 5
    assert kwargs["health_check_interval"] == 30
    rc._redis = None  # don't leak the fake into other tests
