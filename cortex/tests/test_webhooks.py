"""Tests for Webhook / Event Push system (app.webhooks)."""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.webhooks import (
    WEBHOOKS_REDIS_KEY,
    WebhookRegistration,
    _send_webhook,
    create_webhook_router,
    fire_webhooks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_redis():
    """Create an async Redis mock."""
    r = AsyncMock()
    r.hgetall = AsyncMock(return_value={})
    r.hget = AsyncMock(return_value=None)
    r.hset = AsyncMock()
    r.hdel = AsyncMock(return_value=1)
    return r


@pytest.fixture
def app_with_webhooks(mock_redis):
    """Create a FastAPI test app with the webhook router."""
    app = FastAPI()
    # The routes are scope-gated (admin for mutations, memory:read for reads), and that
    # gate bites even on the AUTH_ENABLED=false path. These tests assert the routes' OWN
    # logic, so they build the router with the gate stubbed at CONSTRUCTION time (the
    # dependency is captured at decoration). Patch `app.webhooks.require_scope`, not the
    # source module — webhooks.py holds its own reference from an import-time `from`.
    # Safe only because test_webhook_mutating_routes_refuse_anonymous_when_auth_disabled
    # exercises the real gate. Mirrors app/ops.py's and transfer's precedent.
    with patch("app.webhooks.require_scope", lambda scope: (lambda: _ADMIN)):
        router = create_webhook_router(mock_redis)
    app.include_router(router)
    return app


@pytest.fixture
def client(app_with_webhooks):
    """Create a test client."""
    return TestClient(app_with_webhooks)


def _make_webhook(
    webhook_id: str = "test-id",
    url: str = "https://example.com/hook",
    events: list[str] | None = None,
    namespace: str = "default",
    secret: str | None = None,
    active: bool = True,
) -> WebhookRegistration:
    return WebhookRegistration(
        id=webhook_id,
        url=url,
        events=events or ["memory.learned"],
        namespace=namespace,
        secret=secret,
        active=active,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Webhook Registration (POST)
# ---------------------------------------------------------------------------


class TestWebhookRegistration:
    def test_register_webhook(self, client, mock_redis):
        response = client.post(
            "/webhooks/",
            json={
                "url": "https://example.com/hook",
                "events": ["memory.learned"],
                "namespace": "default",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["url"] == "https://example.com/hook"
        assert data["events"] == ["memory.learned"]
        assert data["namespace"] == "default"
        assert data["active"] is True
        assert "id" in data
        assert "created_at" in data
        mock_redis.hset.assert_called_once()

    def test_register_webhook_with_secret(self, client, mock_redis):
        response = client.post(
            "/webhooks/",
            json={
                "url": "https://example.com/hook",
                "events": ["memory.learned"],
                "secret": "my-secret",
            },
        )
        assert response.status_code == 201
        # Secret should not be in the response model
        mock_redis.hset.assert_called_once()

    def test_register_webhook_invalid_event_type(self, client):
        response = client.post(
            "/webhooks/",
            json={
                "url": "https://example.com/hook",
                "events": ["invalid.event"],
            },
        )
        assert response.status_code == 400
        assert "Invalid event types" in response.json()["detail"]

    def test_register_webhook_multiple_events(self, client, mock_redis):
        response = client.post(
            "/webhooks/",
            json={
                "url": "https://example.com/hook",
                "events": ["memory.learned", "memory.recalled", "gc.pruned"],
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert len(data["events"]) == 3


# ---------------------------------------------------------------------------
# Webhook Listing (GET)
# ---------------------------------------------------------------------------


class TestWebhookListing:
    def test_list_webhooks_empty(self, client, mock_redis):
        mock_redis.hgetall.return_value = {}
        response = client.get("/webhooks/")
        assert response.status_code == 200
        assert response.json() == []

    def test_list_webhooks_with_data(self, client, mock_redis):
        wh = _make_webhook()
        mock_redis.hgetall.return_value = {
            wh.id: wh.model_dump_json(),
        }
        response = client.get("/webhooks/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == wh.id

    def test_get_webhook_by_id(self, client, mock_redis):
        wh = _make_webhook(webhook_id="wh-123")
        mock_redis.hget.return_value = wh.model_dump_json()
        response = client.get("/webhooks/wh-123")
        assert response.status_code == 200
        assert response.json()["id"] == "wh-123"

    def test_get_webhook_not_found(self, client, mock_redis):
        mock_redis.hget.return_value = None
        response = client.get("/webhooks/nonexistent")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Webhook Deletion (DELETE)
# ---------------------------------------------------------------------------


class TestWebhookDeletion:
    def test_delete_webhook(self, client, mock_redis):
        mock_redis.hdel.return_value = 1
        response = client.delete("/webhooks/wh-123")
        assert response.status_code == 204
        mock_redis.hdel.assert_called_once_with(WEBHOOKS_REDIS_KEY, "wh-123")

    def test_delete_webhook_not_found(self, client, mock_redis):
        mock_redis.hdel.return_value = 0
        response = client.delete("/webhooks/nonexistent")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Webhook Firing with HMAC Signature
# ---------------------------------------------------------------------------


class TestWebhookFiring:
    @pytest.mark.asyncio
    async def test_fire_webhooks_with_hmac_signature(self, mock_redis):
        secret = "test-secret-key"
        wh = _make_webhook(
            events=["memory.learned"],
            namespace="default",
            secret=secret,
        )
        mock_redis.hgetall.return_value = {wh.id: wh.model_dump_json()}

        with patch("app.webhooks._send_webhook", new_callable=AsyncMock) as mock_send:
            await fire_webhooks(
                mock_redis,
                event_type="memory.learned",
                payload={"action": "test"},
                namespace="default",
            )
            mock_send.assert_called_once()
            called_webhook = mock_send.call_args[0][0]
            assert called_webhook.secret == secret

    @pytest.mark.asyncio
    async def test_send_webhook_includes_hmac_header(self):
        secret = "my-secret"
        wh = _make_webhook(secret=secret)
        body = {
            "event": "memory.learned",
            "payload": {"test": True},
            "timestamp": "2025-01-01T00:00:00",
            "namespace": "default",
        }

        body_bytes = json.dumps(body, default=str).encode("utf-8")
        expected_sig = hmac_module.new(
            secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()

        with patch("app.webhooks.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await _send_webhook(wh, body)

            call_kwargs = mock_client.post.call_args
            headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
            assert headers.get("X-FirekeepCortex-Signature") == expected_sig


# ---------------------------------------------------------------------------
# Webhook Filtering by Event Type and Namespace
# ---------------------------------------------------------------------------


class TestWebhookFiltering:
    @pytest.mark.asyncio
    async def test_filters_by_event_type(self, mock_redis):
        wh_learn = _make_webhook(webhook_id="wh-1", events=["memory.learned"])
        wh_recall = _make_webhook(webhook_id="wh-2", events=["memory.recalled"])
        mock_redis.hgetall.return_value = {
            "wh-1": wh_learn.model_dump_json(),
            "wh-2": wh_recall.model_dump_json(),
        }

        with patch("app.webhooks._send_webhook", new_callable=AsyncMock) as mock_send:
            await fire_webhooks(
                mock_redis,
                event_type="memory.learned",
                payload={"test": True},
            )
            # Only wh-1 should fire
            mock_send.assert_called_once()
            called_wh = mock_send.call_args[0][0]
            assert called_wh.id == "wh-1"

    @pytest.mark.asyncio
    async def test_filters_by_namespace(self, mock_redis):
        wh_default = _make_webhook(webhook_id="wh-1", namespace="default")
        wh_custom = _make_webhook(webhook_id="wh-2", namespace="custom")
        mock_redis.hgetall.return_value = {
            "wh-1": wh_default.model_dump_json(),
            "wh-2": wh_custom.model_dump_json(),
        }

        with patch("app.webhooks._send_webhook", new_callable=AsyncMock) as mock_send:
            await fire_webhooks(
                mock_redis,
                event_type="memory.learned",
                payload={"test": True},
                namespace="custom",
            )
            mock_send.assert_called_once()
            called_wh = mock_send.call_args[0][0]
            assert called_wh.id == "wh-2"

    @pytest.mark.asyncio
    async def test_inactive_webhooks_not_fired(self, mock_redis):
        wh = _make_webhook(active=False)
        mock_redis.hgetall.return_value = {wh.id: wh.model_dump_json()}

        with patch("app.webhooks._send_webhook", new_callable=AsyncMock) as mock_send:
            await fire_webhooks(
                mock_redis,
                event_type="memory.learned",
                payload={"test": True},
            )
            mock_send.assert_not_called()


# ---------------------------------------------------------------------------
# fire_webhooks with unreachable URL
# ---------------------------------------------------------------------------


class TestWebhookErrorHandling:
    @pytest.mark.asyncio
    async def test_unreachable_url_does_not_raise(self):
        """Sending to an unreachable URL should log but not raise."""
        wh = _make_webhook(url="https://unreachable.invalid/hook")
        body = {"event": "test", "payload": {}, "timestamp": "now", "namespace": "default"}

        with patch("app.webhooks.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = Exception("Connection refused")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            # Should not raise
            await _send_webhook(wh, body)

    @pytest.mark.asyncio
    async def test_fire_webhooks_redis_error_does_not_raise(self, mock_redis):
        """Redis failure when fetching webhooks should not raise."""
        mock_redis.hgetall.side_effect = RuntimeError("Redis down")

        # Should not raise
        await fire_webhooks(
            mock_redis,
            event_type="memory.learned",
            payload={"test": True},
        )


# --- scope gate + SSRF guard (2026-08-02) ----------------------------------
# Found by adversarial review of eded76a: that commit gated /memory/export and
# /memory/import, but the same defect sat one file over on a strictly more
# powerful surface. A registered webhook is a PERSISTENT forward of every memory
# learned (main.py:1306 fires memory.learned with the memory body) plus an
# arbitrary outbound-POST primitive from inside the trust boundary.
_ADMIN = {"agent_id": "test-admin", "scopes": ["*"], "authenticated": True}


def test_webhook_mutating_routes_refuse_anonymous_when_auth_disabled():
    """Registration, deletion and test-fire answered ANY caller: no route declared a
    dependency and main.py:267 registered the router without `dependencies=`. The
    global key middleware only proves a key exists — it performs no scope check — so
    under AUTH_ENABLED=true any non-admin teammate key sufficed, and with auth off no
    key at all."""
    from auth import keys as _keys

    assert _keys._AUTH_ENABLED is False, "this test is about the auth-DISABLED path"
    assert "admin" not in _keys.ANONYMOUS_SCOPES

    test_app = FastAPI()
    test_app.include_router(create_webhook_router(AsyncMock()))  # the REAL gate
    client = TestClient(test_app)

    body = {"url": "https://attacker.example/collect",
            "events": ["memory.learned"], "namespace": "default"}
    for method, path, payload in (
        ("post", "/webhooks/", body),
        ("delete", "/webhooks/abc", None),
        ("post", "/webhooks/abc/test", None),
    ):
        resp = (client.post(path, json=payload) if method == "post"
                else client.delete(path))
        assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"


def test_internal_fire_stays_reachable_for_the_internal_key():
    """`/internal/fire` is deliberately memory:write, NOT admin — it is the
    service-to-service route Sentinel and Bridge POST to, and FIREKEEP_INTERNAL_KEY is
    explicitly minted WITHOUT admin (scopes: memory:write, session:read, eval:read,
    eval:write). Gating it admin would silently break both callers.

    A consequence worth stating rather than hiding: memory:write is in ANONYMOUS_SCOPES,
    so with AUTH_ENABLED=false this route still answers anonymously. That matches the
    documented posture — auth off means everything below admin is open on a
    loopback-bound deployment — and the exfiltration path is closed regardless, because
    REGISTERING an endpoint requires admin. This test guards the Sentinel/Bridge
    contract: it fails if someone tightens this route to admin.

    event_type is a required query param; omitting it 422s before the dependency is
    solved, which would mask the gate rather than exercise it.
    """
    test_app = FastAPI()
    test_app.include_router(create_webhook_router(AsyncMock()))  # the REAL gate
    client = TestClient(test_app)
    resp = client.post("/webhooks/internal/fire?event_type=memory.learned")
    assert resp.status_code != 403, "admin here would break Sentinel and Bridge"


def test_webhook_registration_rejects_internal_addresses(app_with_webhooks):
    """The URL was validated for SCHEME only (webhooks.py:91), so the cloud-metadata
    address was accepted and every subsequent memory forwarded to it. cortex already
    ships an SSRF guard for exactly this — app/knowledge/crawler.py's is_safe_url —
    and this route bypassed it."""
    client = TestClient(app_with_webhooks)
    resp = client.post("/webhooks/", json={
        "url": "http://169.254.169.254/latest/meta-data/",
        "events": ["memory.learned"], "namespace": "default"})
    assert resp.status_code == 400, f"-> {resp.status_code}"
