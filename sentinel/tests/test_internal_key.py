"""Sentinel threads FIREKEEP_INTERNAL_KEY as X-API-Key onto its outbound
alert-broadcast (->Relay) and webhook (->Cortex) calls under office AUTH.

Tests the outbound fns DIRECTLY (not via push_event, which dispatches them
through asyncio.create_task — racy to assert). A stub httpx.AsyncClient
captures the headers actually sent.
"""
from __future__ import annotations

import pytest

import app.store as store_mod

pytestmark = pytest.mark.asyncio


class _CapturingClient:
    """Stub replacing httpx.AsyncClient; records the post() headers."""
    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, **kwargs):
        _CapturingClient.captured = {"url": url, "headers": kwargs.get("headers") or {}}
        return None  # neither outbound fn calls .raise_for_status()


def test_internal_key_headers_present_and_absent():
    assert store_mod._internal_key_headers("nxs_abc") == {"X-API-Key": "nxs_abc"}
    assert store_mod._internal_key_headers(None) == {}
    assert store_mod._internal_key_headers("") == {}


async def test_broadcast_alert_carries_key_and_preserves_accept(monkeypatch):
    monkeypatch.setattr(store_mod.httpx, "AsyncClient", _CapturingClient)
    await store_mod._broadcast_alert(
        "http://relay:8050", "git", "error", "boom", internal_key="nxs_secret"
    )
    hdrs = _CapturingClient.captured["headers"]
    assert hdrs["X-API-Key"] == "nxs_secret"
    # Existing MCP Accept header must survive the merge (not be clobbered)
    assert hdrs["Accept"] == "application/json, text/event-stream"
    assert _CapturingClient.captured["url"] == "http://relay:8050/mcp"


async def test_broadcast_alert_omits_key_when_unset(monkeypatch):
    monkeypatch.setattr(store_mod.httpx, "AsyncClient", _CapturingClient)
    await store_mod._broadcast_alert("http://relay:8050", "git", "error", "boom")
    hdrs = _CapturingClient.captured["headers"]
    assert "X-API-Key" not in hdrs
    assert hdrs["Accept"] == "application/json, text/event-stream"


async def test_fire_cortex_webhook_carries_key(monkeypatch):
    monkeypatch.setattr(store_mod.httpx, "AsyncClient", _CapturingClient)
    await store_mod._fire_cortex_webhook(
        "git", "error", "boom", "commit.new", internal_key="nxs_secret"
    )
    assert _CapturingClient.captured["headers"]["X-API-Key"] == "nxs_secret"


async def test_fire_cortex_webhook_omits_key_when_unset(monkeypatch):
    monkeypatch.setattr(store_mod.httpx, "AsyncClient", _CapturingClient)
    await store_mod._fire_cortex_webhook("git", "error", "boom", "commit.new")
    assert "X-API-Key" not in _CapturingClient.captured["headers"]
