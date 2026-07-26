"""Webhook payload formatters for Slack, Discord, and generic HTTP."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

# Event type → human-readable summary prefix
_EVENT_LABELS = {
    "memory.learned": "Memory stored",
    "memory.recalled": "Memory recalled",
    "stream.ingested": "Events ingested",
    "gc.pruned": "Memory GC",
    "session.completed": "Session completed",
    "session.abandoned": "Session abandoned",
    "eval.computed": "Eval computed",
    "sentinel.alert": "Alert",
    "agent.merged": "Duplicates merged",
    "agent.orphan_cleaned": "Orphans cleaned",
    "agent.contradiction_found": "Contradiction found",
    "agent.backlinks_added": "Backlinks added",
    "agent.confidence_decayed": "Confidence decayed",
    "agent.reclassified": "Reclassified",
    "test": "Test event",
}

# Severity/event → color codes
_SLACK_COLORS = {
    "error": "#f87171",
    "critical": "#dc2626",
    "warning": "#fbbf24",
    "success": "#34d399",
    "info": "#60a5fa",
}

_DISCORD_COLORS = {
    "error": 0xF87171,
    "critical": 0xDC2626,
    "warning": 0xFBBF24,
    "success": 0x34D399,
    "info": 0x60A5FA,
}

VALID_FORMATS = frozenset({"generic", "slack", "discord"})


def _summarize(body: dict[str, Any]) -> str:
    """Build a human-readable summary from a webhook body."""
    event = body.get("event", "unknown")
    payload = body.get("payload", {})
    label = _EVENT_LABELS.get(event, event)

    parts = [label]

    # Add useful payload details
    if payload.get("action"):
        parts.append(str(payload["action"])[:120])
    elif payload.get("summary"):
        parts.append(str(payload["summary"])[:120])
    elif payload.get("goal"):
        parts.append(str(payload["goal"])[:120])
    elif payload.get("query"):
        parts.append(f'query: "{payload["query"][:80]}"')
    elif payload.get("message"):
        parts.append(str(payload["message"])[:120])
    elif payload.get("session_id"):
        parts.append(f'session: {payload["session_id"][:12]}')

    if payload.get("session_id") and not any("session" in p for p in parts[1:]):
        parts.append(f'(session {payload["session_id"][:12]})')

    return " — ".join(parts)


def _event_color(body: dict[str, Any]) -> str:
    """Determine color key from event type."""
    event = body.get("event", "")
    payload = body.get("payload", {})
    severity = payload.get("severity", "")

    if severity in ("error", "critical"):
        return severity
    if "alert" in event or "failure" in event or "contradiction" in event:
        return "error"
    if "warning" in severity:
        return "warning"
    if "completed" in event or "learned" in event or "success" in event:
        return "success"
    return "info"


def format_generic(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Standard JSON payload."""
    return (
        json.dumps(body, default=str).encode("utf-8"),
        {"Content-Type": "application/json"},
    )


def format_slack(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Slack Block Kit payload for incoming webhooks."""
    summary = _summarize(body)
    event = body.get("event", "unknown")
    color_key = _event_color(body)
    color = _SLACK_COLORS.get(color_key, _SLACK_COLORS["info"])
    namespace = body.get("namespace", "default")
    ts = body.get("timestamp", datetime.now(timezone.utc).isoformat())

    payload_data = body.get("payload", {})
    detail_lines = []
    for k, v in list(payload_data.items())[:6]:
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)[:100]
        detail_lines.append(f"*{k}:* {v}")

    slack_body = {
        "attachments": [{
            "color": color,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Firekeep — {event}*\n{summary}"},
                },
            ],
            "fallback": summary,
        }],
    }

    if detail_lines:
        slack_body["attachments"][0]["blocks"].append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(detail_lines)},
        })

    slack_body["attachments"][0]["blocks"].append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"namespace: {namespace} | {ts[:19]}"}],
    })

    return (
        json.dumps(slack_body, default=str).encode("utf-8"),
        {"Content-Type": "application/json"},
    )


def format_discord(body: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Discord webhook embed payload."""
    summary = _summarize(body)
    event = body.get("event", "unknown")
    color_key = _event_color(body)
    color = _DISCORD_COLORS.get(color_key, _DISCORD_COLORS["info"])
    namespace = body.get("namespace", "default")
    ts = body.get("timestamp", datetime.now(timezone.utc).isoformat())

    payload_data = body.get("payload", {})
    fields = []
    for k, v in list(payload_data.items())[:6]:
        if isinstance(v, (dict, list)):
            v = json.dumps(v, default=str)[:100]
        fields.append({"name": k, "value": str(v)[:200], "inline": True})

    discord_body = {
        "embeds": [{
            "title": f"Firekeep: {event}",
            "description": summary,
            "color": color,
            "fields": fields,
            "footer": {"text": f"namespace: {namespace}"},
            "timestamp": ts,
        }],
    }

    return (
        json.dumps(discord_body, default=str).encode("utf-8"),
        {"Content-Type": "application/json"},
    )


_FORMATTERS = {
    "generic": format_generic,
    "slack": format_slack,
    "discord": format_discord,
}


def format_payload(body: dict[str, Any], fmt: str = "generic") -> tuple[bytes, dict[str, str]]:
    """Format a webhook payload for the given format.

    Returns (body_bytes, headers).
    """
    formatter = _FORMATTERS.get(fmt, format_generic)
    return formatter(body)
