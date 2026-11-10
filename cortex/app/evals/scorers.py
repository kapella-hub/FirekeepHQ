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
    metrics["command_count"] = _command_count(events)
    metrics["command_failure_rate"] = _command_failure_rate(events)
    metrics["final_command_failed"] = _final_command_failed(events)

    return {k: v for k, v in metrics.items() if v is not None}


# ---------------------------------------------------------------------------
# Observed commands (agent.action.observed, 2026-11-09)
# ---------------------------------------------------------------------------
#
# Shell commands reported by the client's post-tool hooks with their real exit
# status. They are the first outcome events that are neither a self-report nor
# a pre-declared action, and they arrive in volume — tens to hundreds per
# session, a healthy share of them failing, because a red test run before a
# green one is how work normally goes.
#
# That is why they get THEIR OWN metrics and are EXCLUDED from the
# outcome-bearing population the older ones read (`_has_legacy_outcome`):
#
#   * `tool_success_rate`, `failure_rate` and `outcome_event_count` are read by
#     frozen consumers — compliance.py's optimism-skew counts any
#     `tool_success_rate < 1.0` at `outcome_event_count >= 2` as an independent
#     contradiction of a self-reported success. Folding commands in would turn
#     that into "the session ran at least one failing command", true of nearly
#     every session, and saturate the series instead of fixing it.
#   * a failure RATE over commands is not a quality verdict either. What
#     contradicts "task succeeded" is the session ENDING red, which is what
#     `final_command_failed` reports.

OBSERVED_EVENT_TYPE = "agent.action.observed"


def _is_observed(event: dict) -> bool:
    return event.get("event_type") == OBSERVED_EVENT_TYPE


def _has_legacy_outcome(event: dict) -> bool:
    """An outcome-bearing event of the pre-2026-11-09 population."""
    return bool(event.get("outcome")) and not _is_observed(event)


def _observed_outcomes(events: list[dict]) -> list[str]:
    return [e["outcome"] for e in events if _is_observed(e) and e.get("outcome")]


def _command_count(events: list[dict]) -> float:
    """How many observed commands carried an outcome. Read it beside the two
    metrics below, for the reason `outcome_event_count` exists."""
    return float(len(_observed_outcomes(events)))


def _command_failure_rate(events: list[dict]) -> float | None:
    """Share of observed commands that failed; None when none were observed."""
    outcomes = _observed_outcomes(events)
    if not outcomes:
        return None
    return round(sum(1 for o in outcomes if o == "failure") / len(outcomes), 4)


def _final_command_failed(events: list[dict]) -> float | None:
    """1.0 when the LAST observed command of the session failed, else 0.0;
    None when none were observed.

    Events arrive in timeline order (compute.py hydrates them by session
    index), so the last one is the state the session was left in. A coarse
    signal on purpose: it does not know which commands were verification and
    which were exploration, only that the final one was red.
    """
    outcomes = _observed_outcomes(events)
    if not outcomes:
        return None
    return 1.0 if outcomes[-1] == "failure" else 0.0


# ---------------------------------------------------------------------------
# Individual scorers
# ---------------------------------------------------------------------------


def _tool_success_rate(events: list[dict]) -> float | None:
    """Ratio of successful outcomes to total events with outcomes.

    Events without an outcome field are excluded from the calculation, and so
    are observed commands (see "Observed commands" above).
    """
    with_outcome = [e for e in events if _has_legacy_outcome(e)]
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
    to 0.0 (allow), the correct no-signal posture. Observed commands are not
    part of this population (see "Observed commands" above).
    """
    with_outcome = [e for e in events if _has_legacy_outcome(e)]
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
    difference. Observed commands are counted by `command_count` instead.
    """
    return float(sum(1 for e in events if _has_legacy_outcome(e)))


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


def outcome_brier_score(actions: list[dict]) -> float | None:
    """Brier score of stated confidence against whether the action SUCCEEDED.

    Args:
        actions: dicts with 'prediction_confidence' (float in [0,1]) and
                 'success' (bool). Entries missing either are excluded.

    ``brier_score`` above scores confidence against `prediction_match_score`,
    which is the share of the agent's free-text success criteria that the
    reconcile echoed back VERBATIM in `observed_criteria_met`
    (agent_gateway/reconciler.py). No client populates that field with the
    agent's own wording, so the criteria half scores 0 whenever criteria were
    stated, the match lands near 0.5 for an action that went exactly as
    predicted, and a well-calibrated 0.9 scores 0.16. Measured 2026-11-09:
    mean 0.32 over 50 sessions — worse than the 0.25 a constant 0.5 earns —
    which says the instrument is off, not the agents.

    This one is the textbook definition: outcome is 1 if the action succeeded,
    else 0. It is reported beside `brier_score`, which is left unchanged
    because the Trust Ledger's formulas are frozen against it.
    """
    scored = [
        (float(a["prediction_confidence"]), 1.0 if a["success"] else 0.0)
        for a in actions
        if a.get("prediction_confidence") is not None
        and isinstance(a.get("success"), bool)
    ]
    if not scored:
        return None
    return round(sum((c - s) ** 2 for c, s in scored) / len(scored), 4)
