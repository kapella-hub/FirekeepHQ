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
