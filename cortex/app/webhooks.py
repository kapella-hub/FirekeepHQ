"""Webhook / Event Push system for FirekeepCortex.

Provides webhook registration, management, and async event firing
with optional HMAC-SHA256 signature verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import redis.asyncio
from fastapi import APIRouter, Depends, HTTPException

from app.knowledge.crawler import is_safe_url
from auth.middleware import require_scope
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Redis key for storing webhook registrations as a hash (id -> JSON)
WEBHOOKS_REDIS_KEY = "firekeep:webhooks"

# Module-level httpx client singleton for webhook delivery
_webhook_client: httpx.AsyncClient | None = None
_webhook_client_lock = asyncio.Lock()


async def _get_webhook_client() -> httpx.AsyncClient:
    """Return a shared httpx client for webhook delivery, lazily initialized."""
    global _webhook_client
    async with _webhook_client_lock:
        if _webhook_client is None or _webhook_client.is_closed:
            _webhook_client = httpx.AsyncClient(timeout=5.0)
    return _webhook_client

# Valid event types
VALID_EVENTS = frozenset({
    "memory.learned",
    "memory.recalled",
    "stream.ingested",
    "gc.pruned",
    "session.completed",
    "session.abandoned",
    "eval.computed",
    "sentinel.alert",
    "agent.merged",
    "agent.orphan_cleaned",
    "agent.contradiction_found",
    "agent.backlinks_added",
    "agent.confidence_decayed",
    "agent.reclassified",
    "test",
})

# Valid webhook payload formats
VALID_FORMATS = frozenset({"generic", "slack", "discord"})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class WebhookRegistration(BaseModel):
    """Stored webhook configuration."""

    id: str
    url: str
    events: list[str]
    namespace: str = "default"
    secret: str | None = None
    format: str = "generic"  # generic, slack, discord
    active: bool = True
    created_at: str


class WebhookCreateRequest(BaseModel):
    """Request body for creating a webhook."""

    url: str = Field(..., min_length=1, max_length=2000)
    events: list[str] = Field(..., min_length=1, max_length=10)
    namespace: str = Field(default="default", min_length=1, max_length=200)
    secret: str | None = Field(default=None, max_length=500)
    format: str = Field(default="generic", description="Payload format: generic, slack, or discord")

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("Webhook URL must start with http:// or https://")
        return v

    @field_validator("format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        if v not in VALID_FORMATS:
            raise ValueError(f"Invalid format '{v}'. Must be one of: {sorted(VALID_FORMATS)}")
        return v


class WebhookResponse(BaseModel):
    """Response for webhook operations."""

    id: str
    url: str
    events: list[str]
    namespace: str
    format: str
    active: bool
    created_at: str


# ---------------------------------------------------------------------------
# Webhook firing
# ---------------------------------------------------------------------------


async def fire_webhooks(
    redis_client: redis.asyncio.Redis,
    event_type: str,
    payload: dict[str, Any],
    namespace: str = "default",
) -> None:
    """Fire all registered webhooks matching the event type and namespace.

    For each matching webhook, POST to url with:
      - JSON body: {"event": event_type, "payload": payload, "timestamp": ..., "namespace": namespace}
      - X-FirekeepCortex-Signature header: HMAC-SHA256 of body using webhook secret (if set)
    Uses httpx with 5s timeout, fire-and-forget (don't block the main request).
    Log failures but don't raise.
    """
    try:
        webhooks = await _get_all_webhooks(redis_client)
    except Exception:
        logger.exception("Failed to fetch webhooks for firing")
        return

    matching = [
        wh
        for wh in webhooks
        if wh.active
        and event_type in wh.events
        and wh.namespace == namespace
    ]

    if not matching:
        return

    body = {
        "event": event_type,
        "payload": payload,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
    }

    tasks = [_send_webhook(wh, body) for wh in matching]
    # Fire-and-forget: gather but don't block callers
    await asyncio.gather(*tasks, return_exceptions=True)


async def _send_webhook(webhook: WebhookRegistration, body: dict[str, Any]) -> None:
    """Send a single webhook POST request."""
    try:
        from app.webhook_formatters import format_payload

        body_bytes, headers = format_payload(body, webhook.format)

        if webhook.secret:
            signature = hmac.new(
                webhook.secret.encode("utf-8"),
                body_bytes,
                hashlib.sha256,
            ).hexdigest()
            headers["X-FirekeepCortex-Signature"] = signature

        client = await _get_webhook_client()
        response = await client.post(
            webhook.url,
            content=body_bytes,
            headers=headers,
        )
        logger.info(
            "Webhook %s fired to %s: status %d",
            webhook.id,
            webhook.url,
            response.status_code,
        )
    except Exception:
        logger.exception(
            "Failed to fire webhook %s to %s", webhook.id, webhook.url
        )


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


async def _get_all_webhooks(
    redis_client: redis.asyncio.Redis,
) -> list[WebhookRegistration]:
    """Fetch all webhooks from Redis."""
    raw = await redis_client.hgetall(WEBHOOKS_REDIS_KEY)
    webhooks = []
    for _id, data in raw.items():
        try:
            wh = WebhookRegistration(**json.loads(data))
            webhooks.append(wh)
        except Exception:
            logger.warning("Skipping malformed webhook: %s", _id)
    return webhooks


async def _get_webhook(
    redis_client: redis.asyncio.Redis, webhook_id: str
) -> WebhookRegistration | None:
    """Fetch a single webhook by ID from Redis."""
    raw = await redis_client.hget(WEBHOOKS_REDIS_KEY, webhook_id)
    if raw is None:
        return None
    try:
        return WebhookRegistration(**json.loads(raw))
    except Exception:
        return None


async def _save_webhook(
    redis_client: redis.asyncio.Redis, webhook: WebhookRegistration
) -> None:
    """Save a webhook to Redis."""
    await redis_client.hset(
        WEBHOOKS_REDIS_KEY, webhook.id, webhook.model_dump_json()
    )


async def _delete_webhook(
    redis_client: redis.asyncio.Redis, webhook_id: str
) -> bool:
    """Delete a webhook from Redis. Returns True if deleted."""
    result = await redis_client.hdel(WEBHOOKS_REDIS_KEY, webhook_id)
    return result > 0


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_webhook_router(redis_client: redis.asyncio.Redis) -> APIRouter:
    """Create a FastAPI router for webhook management endpoints.

    Args:
        redis_client: Async Redis client for storing webhook registrations.

    Returns:
        An APIRouter with webhook CRUD + test endpoints.
    """
    router = APIRouter(prefix="/webhooks", tags=["webhooks"])

    @router.post("/", response_model=WebhookResponse, status_code=201)
    async def register_webhook(
        req: WebhookCreateRequest,
        identity: dict = Depends(require_scope("admin")),
    ) -> WebhookResponse:
        """Register a new webhook.

        Admin-scoped: a registration is a PERSISTENT forward of every matching event —
        `memory.learned` carries the memory body — plus an arbitrary outbound-POST
        primitive from inside the trust boundary. That is strictly more powerful than
        the one-shot `GET /memory/export`, which is admin-gated.
        """
        # Validate event types
        invalid_events = set(req.events) - VALID_EVENTS
        if invalid_events:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid event types: {sorted(invalid_events)}. "
                f"Valid types: {sorted(VALID_EVENTS)}",
            )

        # SSRF: the model validates SCHEME only, so the cloud-metadata address and any
        # internal host were accepted and then fed every matching event. cortex already
        # ships this guard for the crawler; the same fail-fast-at-the-endpoint pattern
        # is used there. DNS-rebinding TOCTOU remains accepted, as it is for the crawler.
        ok, reason = is_safe_url(str(req.url))
        if not ok:
            raise HTTPException(status_code=400, detail=f"URL rejected: {reason}")

        webhook = WebhookRegistration(
            id=str(uuid.uuid4()),
            url=req.url,
            events=req.events,
            namespace=req.namespace,
            secret=req.secret,
            format=req.format,
            active=True,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await _save_webhook(redis_client, webhook)

        return WebhookResponse(
            id=webhook.id,
            url=webhook.url,
            events=webhook.events,
            namespace=webhook.namespace,
            format=webhook.format,
            active=webhook.active,
            created_at=webhook.created_at,
        )

    @router.get("/", response_model=list[WebhookResponse])
    async def list_webhooks(
        identity: dict = Depends(require_scope("memory:read")),
    ) -> list[WebhookResponse]:
        """List all registered webhooks."""
        webhooks = await _get_all_webhooks(redis_client)
        return [
            WebhookResponse(
                id=wh.id,
                url=wh.url,
                events=wh.events,
                namespace=wh.namespace,
                format=wh.format,
                active=wh.active,
                created_at=wh.created_at,
            )
            for wh in webhooks
        ]

    @router.get("/events")
    async def list_valid_events(
        identity: dict = Depends(require_scope("memory:read")),
    ) -> dict[str, list[str]]:
        """List all valid webhook event types and formats."""
        return {"events": sorted(VALID_EVENTS), "formats": sorted(VALID_FORMATS)}

    @router.get("/{webhook_id}", response_model=WebhookResponse)
    async def get_webhook(
        webhook_id: str,
        identity: dict = Depends(require_scope("memory:read")),
    ) -> WebhookResponse:
        """Get details of a specific webhook."""
        wh = await _get_webhook(redis_client, webhook_id)
        if wh is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
        return WebhookResponse(
            id=wh.id,
            url=wh.url,
            events=wh.events,
            namespace=wh.namespace,
            format=wh.format,
            active=wh.active,
            created_at=wh.created_at,
        )

    @router.delete("/{webhook_id}", status_code=204)
    async def delete_webhook(
        webhook_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> None:
        """Delete a webhook."""
        deleted = await _delete_webhook(redis_client, webhook_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Webhook not found")

    @router.post("/{webhook_id}/test", status_code=200)
    async def test_webhook(
        webhook_id: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, str]:
        """Send a test event to a specific webhook. Admin-scoped: it fires an outbound
        POST to an operator-supplied URL on demand, and `_send_webhook` only logs the
        status, so the caller cannot see the response — a blind request primitive."""
        wh = await _get_webhook(redis_client, webhook_id)
        if wh is None:
            raise HTTPException(status_code=404, detail="Webhook not found")

        test_payload = {
            "event": "test",
            "payload": {"message": "This is a test webhook event from FirekeepCortex"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "namespace": wh.namespace,
        }

        await _send_webhook(wh, test_payload)
        return {"status": "test_sent", "webhook_id": webhook_id}

    @router.post("/internal/fire", status_code=202)
    async def internal_fire_webhook(
        event_type: str,
        payload: dict[str, Any] = {},
        namespace: str = "default",
        # memory:write, not admin — this is the service-to-service route Sentinel and
        # Bridge POST to with FIREKEEP_INTERNAL_KEY, whose scope set is
        # memory:write,session:read,eval:read,eval:write. Ungated it let any caller
        # fabricate an arbitrary event payload to every registered endpoint.
        identity: dict = Depends(require_scope("memory:write")),
    ) -> dict[str, str]:
        """Internal endpoint for cross-service webhook firing.

        Other services (Sentinel, Bridge) POST here to trigger webhooks
        without needing direct access to Cortex's Redis.
        """
        asyncio.create_task(fire_webhooks(redis_client, event_type, payload, namespace))
        return {"status": "accepted", "event_type": event_type}

    return router
