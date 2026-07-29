"""Shared fixtures and test shims for FirekeepRelay tests."""

import asyncio
import json
import os
import sys
import types

# Ensure shared modules (auth, replay) are importable when running tests
# outside Docker (mirrors cortex/tests/conftest.py and the Dockerfile COPY layout).
_FIREKEEP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _FIREKEEP_ROOT not in sys.path:
    sys.path.insert(0, _FIREKEEP_ROOT)

import fakeredis.aioredis
import pytest
import pytest_asyncio


class _FakeFastMCP:
    # **kwargs, not a fixed signature: the real FastMCP takes `instructions=` (the
    # MCP initialize handshake text) and `lifespan=`, and a double that enumerates
    # only the args it happens to know about turns every future constructor kwarg
    # into a collection ERROR rather than a test failure. That is what happened
    # when instructions= was added -- three test modules failed to import.
    def __init__(self, name: str, **_kwargs):
        self.name = name
        self.instructions = _kwargs.get("instructions")

    def tool(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def custom_route(self, *args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

    def run(self, *args, **kwargs):
        return None


if "fastmcp" not in sys.modules:
    fastmcp_module = types.ModuleType("fastmcp")
    fastmcp_module.FastMCP = _FakeFastMCP
    sys.modules["fastmcp"] = fastmcp_module


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def redis():
    """Provide a fresh fakeredis instance per test."""
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def _eval(script, numkeys, *args):
        script = script or ""
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])

        if "local token = redis.call('INCR'" in script:
            lease_key, fence_key = keys
            agent_id, ttl_raw, now = argv
            ttl = int(ttl_raw)
            existing = await r.get(lease_key)
            if existing:
                return [0, existing]
            token = await r.incr(fence_key)
            data = json.dumps({
                "holder_id": agent_id,
                "fencing_token": token,
                "acquired_at": now,
                "ttl_seconds": ttl,
            })
            await r.set(lease_key, data, ex=ttl)
            return [1, data]

        if "local expected_token = tonumber(ARGV[2])" in script and "redis.call('DEL', lease_key)" in script:
            lease_key = keys[0]
            agent_id, expected_token_raw = argv
            expected_token = int(expected_token_raw)
            existing = await r.get(lease_key)
            if not existing:
                return 0
            data = json.loads(existing)
            if data.get("holder_id") != agent_id:
                return -1
            if expected_token > 0 and data.get("fencing_token") != expected_token:
                return -2
            await r.delete(lease_key)
            return 1

        if "redis.call('EXPIRE', lease_key, ttl)" in script:
            lease_key = keys[0]
            agent_id, expected_token_raw, ttl_raw = argv
            expected_token = int(expected_token_raw)
            ttl = int(ttl_raw)
            existing = await r.get(lease_key)
            if not existing:
                return 0
            data = json.loads(existing)
            if data.get("holder_id") != agent_id:
                return -1
            if data.get("fencing_token") != expected_token:
                return -2
            await r.expire(lease_key, ttl)
            return 1

        raise NotImplementedError(f"Unsupported eval script in test shim: {script[:60]!r}")

    r.eval = _eval
    yield r
    await r.aclose()
