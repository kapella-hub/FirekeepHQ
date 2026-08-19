"""Shared test fixtures for FirekeepBridge."""

from __future__ import annotations

import os
import sys

# Ensure shared modules (auth, replay) are importable when running tests
# outside Docker (mirrors cortex/tests/conftest.py and the Dockerfile COPY layout).
_FIREKEEP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _FIREKEEP_ROOT not in sys.path:
    sys.path.insert(0, _FIREKEEP_ROOT)

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def disable_prior_art(monkeypatch):
    """Prior art off unless a test asks for it.

    `ctx_start_session` assembles prior art on every call, and one of its two
    legs is an HTTP POST to Cortex — so leaving it on would make every existing
    session-start test reach for the network to learn nothing. The prior-art
    tests re-enable it explicitly (`monkeypatch.setattr(mcp_server.settings,
    "PRIOR_ART_ENABLED", True)`), which also keeps them honest about the flag
    being the gate.
    """
    try:
        from app import mcp_server
    except Exception:  # pragma: no cover — suites that never import the server
        return
    monkeypatch.setattr(mcp_server.settings, "PRIOR_ART_ENABLED", False)


@pytest.fixture
def mock_redis():
    """Mock async Redis client."""
    r = AsyncMock()
    r.hset = AsyncMock()
    r.hget = AsyncMock(return_value=None)
    r.hgetall = AsyncMock(return_value={})
    r.set = AsyncMock()
    r.get = AsyncMock(return_value=None)
    r.delete = AsyncMock()
    r.exists = AsyncMock(return_value=0)
    r.expire = AsyncMock()
    r.lpush = AsyncMock()
    r.lrange = AsyncMock(return_value=[])
    r.llen = AsyncMock(return_value=0)
    r.ltrim = AsyncMock()
    r.rpop = AsyncMock()
    r.zadd = AsyncMock()
    r.zrangebyscore = AsyncMock(return_value=[])
    r.zrevrangebyscore = AsyncMock(return_value=[])
    r.zcard = AsyncMock(return_value=0)
    r.zrem = AsyncMock()
    r.zrange = AsyncMock(return_value=[])
    r.eval = AsyncMock(return_value="")
    r.persist = AsyncMock()

    # Pipeline support
    mock_pipe = AsyncMock()
    mock_pipe.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_pipe.__aexit__ = AsyncMock(return_value=False)
    mock_pipe.execute = AsyncMock(return_value=[])
    r.pipeline = MagicMock(return_value=mock_pipe)
    r._pipeline = mock_pipe

    return r
