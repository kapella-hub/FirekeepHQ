"""The nightly skill-synthesis catch-all reads Bridge with the internal key.

`_run_pass` listed completed sessions with an UNHEADERED GET /sessions. Under
AUTH_ENABLED=true Bridge answers 401 to that, so the pass returned
`bridge_unavailable` every night and synthesized nothing — silently. It now
presents FIREKEEP_INTERNAL_KEY (session:read + session:read:workspace, so it
lists every session in its workspace), the same header helper the synthesizer
and scorer use; with the key unset (auth-disabled boxes) the request is
unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest

import app.workers.skill_synthesis as skill_synthesis


@pytest.fixture
def bridge(monkeypatch):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("X-API-Key") != "nxs_internal" and seen_auth["on"]:
            return httpx.Response(401, json={"error": "Missing X-API-Key header"})
        return httpx.Response(200, json={"sessions": [{"session_id": "s-1"}]})

    seen_auth = {"on": True}
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **kw: real_client(transport=httpx.MockTransport(handler)),
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(skill_synthesis.redis.asyncio, "from_url", lambda *a, **kw: redis)
    synth = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(skill_synthesis, "_run_synthesis", synth)
    return SimpleNamespace(seen=seen, auth=seen_auth, synth=synth)


def _settings(monkeypatch, key):
    monkeypatch.setattr(skill_synthesis, "get_settings", lambda: SimpleNamespace(
        BRIDGE_URL="http://bridge:8070", FIREKEEP_INTERNAL_KEY=key,
        REDIS_URL="redis://fake/0",
    ))


@pytest.mark.asyncio
async def test_pass_lists_sessions_with_the_internal_key(bridge, monkeypatch):
    _settings(monkeypatch, "nxs_internal")

    result = await skill_synthesis._run_pass()

    assert result == {"status": "completed", "synthesized": 1}
    assert bridge.seen[0].headers.get("X-API-Key") == "nxs_internal"
    bridge.synth.assert_awaited_once_with("s-1", skill_worthy=False)


@pytest.mark.asyncio
async def test_pass_without_an_internal_key_sends_no_header(bridge, monkeypatch):
    """Auth-disabled boxes leave FIREKEEP_INTERNAL_KEY unset: unchanged request."""
    bridge.auth["on"] = False
    _settings(monkeypatch, None)

    result = await skill_synthesis._run_pass()

    assert result["synthesized"] == 1
    assert "X-API-Key" not in bridge.seen[0].headers
