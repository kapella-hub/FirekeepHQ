"""Shared test fixtures for FirekeepSentinel tests."""

from __future__ import annotations

import os
import sys

# Ensure shared modules (auth, replay) are importable when running tests
# outside Docker (mirrors cortex/tests/conftest.py and the Dockerfile COPY layout).
_FIREKEEP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _FIREKEEP_ROOT not in sys.path:
    sys.path.insert(0, _FIREKEEP_ROOT)

import fakeredis.aioredis
import pytest_asyncio


@pytest_asyncio.fixture
async def redis():
    """Provide a fresh fake Redis instance for each test."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()
