"""Tests for auth.keys — fastapi-free key primitives (SP1a Task 1)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import fakeredis.aioredis
import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestFastapiFree:
    def test_keys_imports_with_fastapi_blocked(self):
        """auth.keys must import in an interpreter where fastapi is unavailable.

        bridge/requirements.txt ships no fastapi — the ASGI validator
        (auth/asgi.py, Task 2) depends on this module being importable there.
        """
        code = (
            "import sys\n"
            "class _BlockFastapi:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'fastapi' or name.startswith('fastapi.'):\n"
            "            raise ImportError('fastapi blocked by test')\n"
            "sys.meta_path.insert(0, _BlockFastapi())\n"
            "import auth.keys\n"
            "print('KEYS_IMPORT_OK')\n"
        )
        res = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        assert res.returncode == 0, res.stderr
        assert "KEYS_IMPORT_OK" in res.stdout


class TestValidateKeyExplicitClient:
    @pytest.mark.asyncio
    async def test_valid_key_with_explicit_client(self, redis):
        from auth import keys

        await keys.init_auth(redis_client=redis, enabled=True)
        created = await keys.create_key("morgan", ["replay:read"])
        # Clear module globals: the explicit client must be sufficient.
        await keys.init_auth(redis_client=None, enabled=False)

        identity = await keys.validate_key(created["api_key"], redis_client=redis)
        assert identity == {
            "workspace_id": "workspace-local",
            "member_id": "member-owner",
            "credential_id": created["key_id"],
            "scopes": ["replay:read"],
            "authenticated": True,
        }

    @pytest.mark.asyncio
    async def test_invalid_key_returns_none(self, redis):
        from auth import keys

        assert await keys.validate_key("nxs_" + "0" * 48, redis_client=redis) is None

    @pytest.mark.asyncio
    async def test_expired_key_returns_none(self, redis):
        from auth import keys

        api_key = "nxs_" + "ab" * 24
        h = keys._hash_key(api_key)
        await redis.hset(
            f"auth:key:{h}",
            mapping={
                "agent_id": "old",
                "scopes": json.dumps(["*"]),
                "created_at": "2020-01-01T00:00:00+00:00",
                "key_id": h[:16],
                "expires_at": "2020-06-01T00:00:00+00:00",
            },
        )
        assert await keys.validate_key(api_key, redis_client=redis) is None

    @pytest.mark.asyncio
    async def test_falls_back_to_module_global_client(self, redis):
        from auth import keys

        await keys.init_auth(redis_client=redis, enabled=True)
        try:
            created = await keys.create_key("global-path", ["admin"])
            identity = await keys.validate_key(created["api_key"])
            assert identity is not None
            assert identity["member_id"] == "member-owner"
            assert "agent_id" not in identity
        finally:
            await keys.init_auth(redis_client=None, enabled=False)


class TestMiddlewareReExports:
    def test_middleware_reexports_primitives(self):
        """External callers (auth/api.py, cortex/app/main.py) import from
        auth.middleware — the re-exports must be the SAME objects."""
        from auth import keys, middleware

        assert middleware.validate_key is keys.validate_key
        assert middleware.create_key is keys.create_key
        assert middleware.generate_api_key is keys.generate_api_key
        assert middleware._hash_key is keys._hash_key
        assert middleware.init_auth is keys.init_auth
        assert middleware.SCOPES is keys.SCOPES
        assert middleware._ANONYMOUS_IDENTITY is keys._ANONYMOUS_IDENTITY
