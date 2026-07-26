"""Persistence helpers for policy evaluation decisions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

POLICY_DECISION_KEY = "policy:decisions"
POLICY_DECISION_MAXLEN = 500


async def record_policy_decision(
    redis_client,
    *,
    file_path: str,
    agent_id: str,
    session_id: str,
    action: str,
    risk_score: float,
    reasons: list[str],
    signals: dict[str, Any],
) -> dict[str, Any]:
    """Persist one policy decision to Redis."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "file_path": file_path,
        "agent_id": agent_id,
        "session_id": session_id,
        "action": action,
        "risk_score": risk_score,
        "reasons": reasons,
        "signals": signals,
    }
    await redis_client.lpush(POLICY_DECISION_KEY, json.dumps(entry))
    await redis_client.ltrim(POLICY_DECISION_KEY, 0, POLICY_DECISION_MAXLEN - 1)
    return entry


async def get_policy_decisions(
    redis_client,
    *,
    limit: int = 50,
    action: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return recent policy decisions with optional filtering."""
    raw_items = await redis_client.lrange(POLICY_DECISION_KEY, 0, max(limit * 4, limit) - 1)
    decisions: list[dict[str, Any]] = []

    for raw in raw_items:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            item = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue

        if action and item.get("action") != action:
            continue
        if agent_id and item.get("agent_id") != agent_id:
            continue
        decisions.append(item)
        if len(decisions) >= limit:
            break

    return decisions


def summarize_policy_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize a set of policy decisions."""
    counts = {"allow": 0, "warn": 0, "rethink": 0, "block": 0}
    unique_agents: set[str] = set()
    unique_sessions: set[str] = set()

    for item in decisions:
        action = item.get("action")
        if action in counts:
            counts[action] += 1
        if item.get("agent_id"):
            unique_agents.add(item["agent_id"])
        if item.get("session_id"):
            unique_sessions.add(item["session_id"])

    return {
        "counts": counts,
        "total": len(decisions),
        "unique_agents": len(unique_agents),
        "unique_sessions": len(unique_sessions),
    }
