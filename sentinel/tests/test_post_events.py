"""POST /events: the EventIngest model finally wired in, Literal-tight
severity, 4xx on mismatch (never a degrade-to-default).

Follows the ASGI route-test pattern established in test_mcp_auth.py /
test_briefing_routes.py / test_version_route.py: build the real FastMCP
http_app behind the real auth middleware and drive it with httpx's
ASGITransport, with app.mcp_server.get_redis patched to a fakeredis
instance (mirrors test_mcp_tools.py's _patch_redis_and_call).
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from auth.asgi import build_auth_middleware
from auth.config import AuthSettings

import app.mcp_server as mcp_mod


async def _noop():
    pass


def _app(auth_enabled: bool = False):
    return mcp_mod.mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=auth_enabled, REDIS_URL="redis://unused/7"),
            skip_paths=("/health", "/version"),
        ),
        stateless_http=True,
    )


@pytest_asyncio.fixture
async def app_client(redis):
    """Drive the real POST /events route over ASGI with a fake redis behind it."""
    with patch("app.mcp_server.get_redis", return_value=redis), \
         patch("app.mcp_server._ensure_collectors", side_effect=_noop):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(auth_enabled=False)),
            base_url="http://test",
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_post_single_event_stores(app_client):
    resp = await app_client.post("/events", json={
        "source": "firekeep.ai/failure-report",
        "event_type": "install-failure",
        "summary": "install failure: create-venv permission-denied linux-gnu 1.5.2 (n=3)",
        "severity": "warning",
        "details": {"kind": "install", "stage": "create-venv",
                    "error": "permission-denied", "os": "linux-gnu",
                    "arch": "x86_64", "client": "1.5.2", "py": "3.11",
                    "first": True, "count": 3,
                    "batch": "failures.20260822T120000Z-1.log|abc123"},
    })
    assert resp.status_code == 202
    assert resp.json() == {"stored": 1}


@pytest.mark.asyncio
async def test_post_batch_stores_all(app_client):
    events = [{"source": "firekeep.ai/failure-report",
               "event_type": "connectivity-failure",
               "summary": f"s{i}", "severity": "info", "details": {}}
              for i in range(3)]
    resp = await app_client.post("/events", json=events)
    assert resp.status_code == 202 and resp.json() == {"stored": 3}


@pytest.mark.asyncio
async def test_invalid_severity_is_422_not_degraded(app_client):
    resp = await app_client.post("/events", json={
        "source": "x", "event_type": "y", "summary": "z", "severity": "warn"})
    assert resp.status_code == 422
    assert "severity" in resp.text


@pytest.mark.asyncio
async def test_invalid_json_is_400(app_client):
    resp = await app_client.post("/events", content=b"not json",
                                 headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_events_stored_are_retrievable(app_client, redis):
    """Sanity: what POST /events stores is what a reader (get_events) sees
    back -- details land structurally, not just accepted-and-discarded."""
    resp = await app_client.post("/events", json={
        "source": "firekeep.ai/failure-report", "event_type": "install-failure",
        "summary": "s", "severity": "error", "details": {"stage": "create-venv"},
    })
    assert resp.status_code == 202

    from app.store import get_events
    events = await get_events(redis, source="firekeep.ai/failure-report", limit=10)
    assert len(events) == 1
    assert events[0]["details"] == {"stage": "create-venv"}
    assert events[0]["severity"] == "error"


class TestPostEventsAuth:
    """Not in skip_paths -- must 401 like every other custom route when
    AUTH_ENABLED=true and no key is supplied (mirrors
    test_briefing_routes.py's TestSentinelBriefingRoutesAuth)."""

    @pytest.mark.asyncio
    async def test_post_events_401_unauth(self):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(auth_enabled=True)),
            base_url="http://test",
        ) as c:
            resp = await c.post("/events", json={
                "source": "s", "event_type": "t", "summary": "m"})
        assert resp.status_code == 401
