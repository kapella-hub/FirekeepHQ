"""Tier 1 metric scorers — compute quality metrics directly from replay traces.

All scorers take a list of parsed replay events (dicts) and return a float.
They require NO external services, NO LLM, and NO user feedback.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def compute_tier1_metrics(events: list[dict[str, Any]]) -> dict[str, float]:
    """Compute all Tier 1 metrics from a session's replay events.

    Returns a dict of metric_name → value. All values are floats in [0, 1]
    where applicable (rates), or raw values (counts, durations).
    """
    if not events:
        return {}

    metrics: dict[str, float] = {}

    metrics["event_count"] = float(len(events))
    metrics["tool_success_rate"] = _tool_success_rate(events)
    metrics["memory_read_count"] = _memory_read_count(events)
    metrics["recall_used_rate"] = _recall_used_rate(events)
    metrics["memory_write_count"] = _memory_write_count(events)
    metrics["memory_freshness_at_recall"] = _memory_freshness_at_recall(events)
    metrics["claim_contention_rate"] = _claim_contention_rate(events)
    metrics["failure_rate"] = _failure_rate(events)
    metrics["session_duration_ms"] = _session_duration_ms(events)
    metrics["unique_event_types"] = _unique_event_types(events)
    metrics["context_snapshot_count"] = _context_snapshot_count(events)

    return {k: v for k, v in metrics.items() if v is not None}


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------


def _tool_success_rate(events: list[dict]) -> float | None:
    """Ratio of successful outcomes to total events with outcomes.

    Events without an outcome field are excluded from the calculation.
    """
    with_outcome = [e for e in events if e.get("outcome")]
    if not with_outcome:
        return None
    successes = sum(1 for e in with_outcome if e["outcome"] == "success")
    return round(successes / len(with_outcome), 4)


def _memory_read_count(events: list[dict]) -> float:
    """Count of memory_read events in the session."""
    return float(sum(1 for e in events if e.get("event_type") == "memory_read"))


def _memory_write_count(events: list[dict]) -> float:
    """Count of memory_write events in the session."""
    return float(sum(1 for e in events if e.get("event_type") == "memory_write"))


def _recall_used_rate(events: list[dict]) -> float:
    """Fraction of memory_read events that were followed by an action/write.

    v1 proxy for "did recall actually feed action?": a memory_read is "used" if
    any agent.action.predict or memory_write event follows it in the session.
    Returns 0.0 when there are no reads — so sessions that never recall pull the
    aggregate (surfaced as recall_hit_rate in the briefing) down, by design.
    """
    reads = [i for i, e in enumerate(events) if e.get("event_type") == "memory_read"]
    if not reads:
        return 0.0

    def _used(after: int) -> bool:
        return any(e.get("event_type") in ("agent.action.predict", "memory_write")
                   for e in events[after + 1:])

    return sum(1 for i in reads if _used(i)) / len(reads)


def _memory_freshness_at_recall(events: list[dict]) -> float | None:
    """Average top_score of memory_read events.

    Higher scores suggest the recalled memories were more relevant.
    This is a proxy for memory freshness — stale memories tend to score lower.
    """
    read_events = [e for e in events if e.get("event_type") == "memory_read"]
    if not read_events:
        return None

    scores = []
    for e in read_events:
        payload = e.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}
        top_score = payload.get("top_score")
        if top_score is not None:
            try:
                scores.append(float(top_score))
            except (ValueError, TypeError):
                pass

    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)


def _claim_contention_rate(events: list[dict]) -> float | None:
    """Rate of claim attempts that resulted in contention (not acquired).

    Claim events with outcome != "success" indicate the resource was
    already held by another agent.
    """
    claim_events = [e for e in events if e.get("event_type") == "claim"]
    if not claim_events:
        return None
    # Claims without explicit outcome are assumed successful
    contended = sum(1 for e in claim_events if e.get("outcome") == "failure")
    return round(contended / len(claim_events), 4)


def _failure_rate(events: list[dict]) -> float:
    """Rate of events with outcome=failure."""
    with_outcome = [e for e in events if e.get("outcome")]
    if not with_outcome:
        return 0.0
    failures = sum(1 for e in with_outcome if e["outcome"] == "failure")
    return round(failures / len(with_outcome), 4)


def _session_duration_ms(events: list[dict]) -> float | None:
    """Duration from first to last event in milliseconds."""
    timestamps = []
    for e in events:
        ts = e.get("timestamp", "")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except (ValueError, TypeError):
                pass
    if len(timestamps) < 2:
        return None
    timestamps.sort()
    delta = (timestamps[-1] - timestamps[0]).total_seconds() * 1000
    return round(delta, 1)


def _unique_event_types(events: list[dict]) -> float:
    """Count of distinct event types in the session.

    More diverse event types suggest richer agent behavior (memory reads,
    writes, coordination, etc.) vs. simple linear execution.
    """
    types = {e.get("event_type") for e in events if e.get("event_type")}
    return float(len(types))


def _context_snapshot_count(events: list[dict]) -> float:
    """Count of events that have a context snapshot attached.

    More snapshots = better debuggability for the session.
    """
    return float(sum(1 for e in events if e.get("context_ref")))


def brier_score(actions: list[dict]) -> float | None:
    """Compute Brier score over actions with a prediction.

    Args:
        actions: list of dicts each containing 'prediction_confidence' (float in [0,1])
                 and 'prediction_match_score' (float in [0,1] or None).
                 Actions with None score or None confidence are excluded.

    Returns:
        Mean squared error between confidence and match score, or None if no scorable
        actions.
    """
    scored = [
        (a["prediction_confidence"], a["prediction_match_score"])
        for a in actions
        if a.get("prediction_match_score") is not None
        and a.get("prediction_confidence") is not None
    ]
    if not scored:
        return None
    return round(sum((c - s) ** 2 for c, s in scored) / len(scored), 4)
