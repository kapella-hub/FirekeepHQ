"""SP1b-server: the 4 outbound briefing sections (mocked upstreams)."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import MagicMock

from app.briefing import sections as S


class _Resp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._json


class _FakeClient:
    """Routes GET by URL substring to a canned _Resp; optional per-host hang."""
    def __init__(self, routes: dict[str, _Resp], hang_hosts: set[str] | None = None):
        self.routes = routes
        self.hang_hosts = hang_hosts or set()
        self.calls = []

    async def get(self, url, headers=None, params=None):
        self.calls.append((url, headers, params))
        for host in self.hang_hosts:
            if host in url:
                await asyncio.sleep(5)  # exceed the test timeout
        # Fold params into the match key so /sessions?status=paused vs
        # ?status=active (same URL, different query) route to distinct canned
        # responses — mirrors how the real Bridge call is disambiguated.
        key = url + ("?" + "&".join(f"{k}={v}" for k, v in (params or {}).items()) if params else "")
        for frag, resp in self.routes.items():
            if frag in key:
                return resp
        return _Resp(404, {})


_SETTINGS = MagicMock(
    SENTINEL_URL="http://sentinel:8060",
    RELAY_URL="http://relay:8050",
    BRIDGE_URL="http://bridge:8070",
    FIREKEEP_INTERNAL_KEY="nxs_internal",
)


# --- environment (fans in /environment + /events) --------------------------

@pytest.mark.asyncio
async def test_environment_ok_with_errors():
    client = _FakeClient({
        "/environment": _Resp(200, {"status": "ok", "collectors": {"docker": True, "git": True, "files": True},
                                     "event_count": 812, "healthy": True, "containers": {}, "container_count": 0}),
        "/events": _Resp(200, {"events": [
            {"summary": "docker restart", "source": "docker", "severity": "error", "timestamp": "t"}]}),
    })
    sec = await S.environment_section(client, _SETTINGS)
    assert sec["status"] == "ok"
    assert sec["data"]["event_count"] == 812
    assert sec["data"]["recent_errors"][0]["source"] == "docker"
    # derived summary (Sentinel doesn't send a prebuilt 'summary' field)
    assert sec["data"]["summary"] == "Events: 812. All collectors healthy."
    # internal key attached on outbound calls
    assert all(h.get("X-API-Key") == "nxs_internal" for _, h, _ in client.calls)


@pytest.mark.asyncio
async def test_environment_unavailable_on_health_failure():
    client = _FakeClient({"/environment": _Resp(503, {}), "/events": _Resp(200, {"events": []})})
    with pytest.raises(Exception):
        await S.environment_section(client, _SETTINGS)


# --- tasks / bulletins -----------------------------------------------------

@pytest.mark.asyncio
async def test_tasks_ok_and_empty():
    client = _FakeClient({"/tasks": _Resp(200, {"tasks": [
        {"task_id": "t1", "title": "fix", "priority": "high", "assigner": "bob", "created_at": "c"}]})})
    sec = await S.tasks_section(client, _SETTINGS, agent_id="moganes", caller_api_key="nxs_caller")
    assert sec["status"] == "ok" and sec["data"]["count"] == 1
    assert client.calls[0][1]["X-API-Key"] == "nxs_caller"
    client2 = _FakeClient({"/tasks": _Resp(200, {"tasks": []})})
    sec2 = await S.tasks_section(client2, _SETTINGS, agent_id="moganes", caller_api_key="nxs_caller")
    assert sec2["status"] == "empty"


@pytest.mark.asyncio
async def test_bulletins_ok():
    client = _FakeClient({"/bulletin": _Resp(200, {"posts": [
        {"author": "alice", "content": "deploying", "timestamp": "t"}]})})
    sec = await S.bulletins_section(client, _SETTINGS, caller_api_key="nxs_caller")
    assert client.calls[0][1]["X-API-Key"] == "nxs_caller"
    assert sec["status"] == "ok"
    assert sec["data"]["posts"][0]["author"] == "alice"


# --- resumable_sessions (Bridge + Relay presence fan-in) -------------------

@pytest.mark.asyncio
async def test_resumable_paused_session():
    client = _FakeClient({
        "status=paused": _Resp(200, {"sessions": [
            {"session_id": "p1", "goal": "old work", "status": "paused",
             "updated_at": "2026-07-09T10:00:00+00:00", "agent_id": "moganes"}]}),
        "status=active": _Resp(200, {"sessions": []}),
        "/presence/": _Resp(404, {}),
    })
    sec = await S.resumable_sessions_section(
        client, _SETTINGS, agent_id="moganes", caller_api_key="nxs_caller")
    assert sec["status"] == "ok"
    reasons = {s["reason"] for s in sec["data"]["sessions"]}
    assert "paused" in reasons
    assert sec["data"]["crash_check"]["performed"] is True


# --- degraded-under-timeout at the handler level ---------------------------

@pytest.mark.asyncio
async def test_hung_upstream_degrades_only_that_section():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.briefing.api import create_briefing_router
    import fakeredis.aioredis

    hung = _FakeClient({"/tasks": _Resp(200, {"tasks": []}),
                        "/bulletin": _Resp(200, {"posts": []}),
                        "status=paused": _Resp(200, {"sessions": []}),
                        "status=active": _Resp(200, {"sessions": []}),
                        "/presence/": _Resp(404, {})},
                       hang_hosts={"sentinel"})
    app = FastAPI()
    app.include_router(create_briefing_router(section_timeout=0.1))
    app.state.replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.vector_client = MagicMock()
    app.state.vector_client._client = MagicMock()

    async def _scroll(**_k):
        return ([], None)
    app.state.vector_client._client.scroll = _scroll
    app.state.redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.http_client = hung

    body = TestClient(app).get("/briefing?agent_id=moganes&goal=g").json()
    assert body["sections"]["environment"]["status"] == "unavailable"
    assert "timeout" in body["sections"]["environment"]["error"]
    assert body["degraded"] is True
    # other outbound sections still resolved
    assert body["sections"]["tasks"]["status"] == "empty"
    assert body["sections"]["bulletins"]["status"] == "empty"


# --- F1 (Cortex half): the briefing is not a confused deputy -----------------
# `agent_id` is caller-supplied. With FIREKEEP_INTERNAL_KEY on these fetches,
# Bob's GET /briefing?agent_id=alice read Alice's sessions and presence with a
# workspace service credential. Every user-scoped outbound call must present
# the LIVE caller's key, and none may ever present the internal key.


@pytest.mark.asyncio
async def test_resumable_sessions_present_the_callers_key_never_the_internal_key():
    client = _FakeClient({
        "status=paused": _Resp(200, {"sessions": []}),
        "status=active": _Resp(200, {"sessions": []}),
        "/presence/": _Resp(404, {}),
    })
    await S.resumable_sessions_section(
        client, _SETTINGS, agent_id="alice", caller_api_key="nxs_bob")
    urls = [url for url, _, _ in client.calls]
    assert any("/sessions" in u for u in urls) and any("/presence/alice" in u for u in urls)
    for _, headers, _ in client.calls:
        assert headers.get("X-API-Key") == "nxs_bob"
        assert _SETTINGS.FIREKEEP_INTERNAL_KEY not in (headers or {}).values()


@pytest.mark.asyncio
async def test_user_sections_send_no_key_at_all_when_auth_is_off():
    """Auth-disabled: the caller is anonymous and forwards nothing — and the
    internal key is not substituted for the missing one."""
    client = _FakeClient({
        "/tasks": _Resp(200, {"tasks": []}),
        "/bulletin": _Resp(200, {"posts": []}),
        "status=paused": _Resp(200, {"sessions": []}),
        "status=active": _Resp(200, {"sessions": []}),
        "/presence/": _Resp(404, {}),
    })
    await S.tasks_section(client, _SETTINGS, agent_id="local", caller_api_key=None)
    await S.bulletins_section(client, _SETTINGS, caller_api_key=None)
    await S.resumable_sessions_section(client, _SETTINGS, agent_id="local", caller_api_key=None)
    assert client.calls
    for _, headers, _ in client.calls:
        assert "X-API-Key" not in (headers or {})


def test_user_scoped_sections_cannot_be_called_without_naming_the_key():
    """A builder that forgets the caller key fails loudly, not by falling back."""
    import inspect

    for fn in (S.tasks_section, S.bulletins_section, S.resumable_sessions_section):
        param = inspect.signature(fn).parameters["caller_api_key"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is inspect.Parameter.empty
