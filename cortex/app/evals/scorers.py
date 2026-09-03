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
    metrics["outcome_event_count"] = _outcome_event_count(events)
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


def _is_briefing_receipt(event: dict) -> bool:
    """True for the briefing's own skills-section `memory_read` receipt
    (`trigger="briefing"`, emitted by `app/briefing/sections.py`).

    IT IS NOT A RECALL. The receipt exists so the skill ladder can tell that a
    skill was *shown*; it fires automatically at session start, with the agent
    having asked for nothing. Counting it makes `memory_read_count` gain a
    floor of 1 in every session and pulls `recall_used_rate` toward 1.0 (an
    action almost always follows a session-start event), which would silently
    inflate a series the compliance rows are frozen against — the same reason
    `app/owm.py` excludes it from both of its tallies.

    A non-dict payload (some stored events carry it as a JSON string) reads as
    "not a briefing receipt" rather than raising, matching
    `_memory_freshness_at_recall`'s own defensiveness.
    """
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("trigger") == "briefing"


def _memory_read_count(events: list[dict]) -> float:
    """Count of memory_read events in the session, excluding briefing receipts."""
    return float(sum(1 for e in events
                     if e.get("event_type") == "memory_read"
                     and not _is_briefing_receipt(e)))


def _memory_write_count(events: list[dict]) -> float:
    """Count of memory_write events in the session."""
    return float(sum(1 for e in events if e.get("event_type") == "memory_write"))


def _recall_used_rate(events: list[dict]) -> float:
    """Fraction of memory_read events that were followed by an action/write.

    v1 proxy for "did recall actually feed action?": a memory_read is "used" if
    any agent.action.predict or memory_write event follows it in the session.
    Returns 0.0 when there are no reads — so sessions that never recall pull the
    aggregate (surfaced as recall_hit_rate in the briefing) down, by design.

    Briefing receipts are excluded (`_is_briefing_receipt`): the receipt fires
    at session start, so something follows it in nearly every session and it
    would score as "used" while the agent recalled nothing.
    """
    reads = [i for i, e in enumerate(events)
             if e.get("event_type") == "memory_read" and not _is_briefing_receipt(e)]
    if not reads:
        return 0.0

    def _used(after: int) -> bool:
        return any(e.get("event_type") in ("agent.action.predict", "memory_write")
                   for e in events[after + 1:])

    return sum(1 for i in reads if _used(i)) / len(reads)


def _memory_freshness_at_recall(events: list[dict]) -> float | None:
    """Average best-relevance of memory_read events.

    Higher scores suggest the recalled memories were more relevant. This is a
    proxy for memory freshness — stale memories tend to score lower.

    READS `raw_top_score`, NOT `top_score`, and the difference is the whole
    metric. `top_score` is `RecallResponse.score`, which is a max over values
    that `_min_max_normalize` has already rescaled so the best entry is exactly
    1.0 — so it is 1.0 whenever any result survives, however poor the match.
    Measured live 2026-08-06: three unrelated queries, including deliberate
    nonsense about knitting patterns, all returned 1.0, and this metric read
    1.0 across all 19 evaluated sessions. A freshness proxy pinned at "perfectly
    fresh" cannot report staleness, which is the only thing it is for.

    `raw_top_score` (emitted by `main.py::_raw_top_score`) is the
    pre-normalization value. Events predating it carry only `top_score`; those
    fall back rather than being dropped, so history stays readable — but such
    events are the pinned-1.0 kind, so a mixed window reads high. Prefer windows
    after 2026-08-06 when judging a trend.
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
        top_score = payload.get("raw_top_score")
        if top_score is None:
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


def _failure_rate(events: list[dict]) -> float | None:
    """Rate of events with outcome=failure; None when nothing carries one.

    SUPERSEDES the 2026-08-06 decision that pinned this at 0.0 on no-outcome
    input "because owm.session_success and the Living Procedures Tier B gate
    both key off it": since 2026-08-23 (outcome truth) both grade from the
    EvalResult task-grade pair, nothing load-bearing reads this metric, and
    the asymmetry with _tool_success_rate is resolved — an empty population
    answers "cannot tell", not "no failures". Read `outcome_event_count`
    beside this number; policy's SessionHealthRule defaults an absent metric
    to 0.0 (allow), the correct no-signal posture.
    """
    with_outcome = [e for e in events if e.get("outcome")]
    if not with_outcome:
        return None
    failures = sum(1 for e in with_outcome if e["outcome"] == "failure")
    return round(failures / len(with_outcome), 4)


def _outcome_event_count(events: list[dict]) -> float:
    """How many events actually carried an outcome.

    Exists because `failure_rate` and `tool_success_rate` are unreadable without
    it: both are ratios over this population, and the population is ~1 in
    production. A rate computed over a single self-reported event is not a
    quality measurement, and nothing downstream could previously tell the
    difference.
    """
    return float(sum(1 for e in events if e.get("outcome")))


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
