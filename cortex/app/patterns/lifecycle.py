"""Pattern promotion, decay, and retirement logic."""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import get_settings
from app.patterns.models import PatternCard

CONFIDENCE_DECAY_PER_WEEK = 0.02
STALE_THRESHOLD_DAYS = 30
RETIRE_CONFIDENCE = 0.2
MAX_ACTIVE_PATTERNS = 50

# Promotion thresholds
PROMOTION_RULES = {
    "candidate": {"min_evidence": 10, "min_confidence": 0.3, "next": "observed"},
    "observed": {"min_evidence": 15, "min_confidence": 0.5, "next": "trial"},
    "trial": {"min_evidence": 25, "min_confidence": 0.65, "min_tip_lift": 0.05, "next": "validated"},
}


def evaluate_promotion(pattern: PatternCard, now: datetime | None = None) -> PatternCard:
    """Evaluate a pattern and promote/demote based on evidence.

    Rules:
    - Quarantined patterns stay quarantined
    - Retired patterns stay retired
    - Behavioral patterns cap at 'observed' (never shown in briefings)
    - Confidence decays if no recent matches
    - Low confidence -> retired
    - Promotion requires meeting threshold for next stage
    """
    now = now or datetime.now(timezone.utc)
    p = pattern.model_copy()

    # Quarantined and retired are terminal (quarantine must be manually lifted)
    if p.stage in ("quarantined", "retired"):
        return p

    # Validation frozen at small N: skip the whole scale-dependent ladder
    # (promote/decay/retire/stale). Detectors still run and feed the N=1
    # "observed" briefing surface — only promotion is gated, not the math.
    if not get_settings().PATTERN_VALIDATION_ENABLED:
        return p

    # Confidence decay: -0.02 per week since last match
    if p.last_matched_at:
        weeks_since = (now - p.last_matched_at).days / 7
        if weeks_since > 1:
            decay = CONFIDENCE_DECAY_PER_WEEK * weeks_since
            p.confidence = max(0.05, p.confidence - decay)

    # Retire if confidence too low
    if p.confidence < RETIRE_CONFIDENCE:
        p.stage = "retired"
        return p

    # Stale check: no matches in STALE_THRESHOLD_DAYS
    if p.last_matched_at and (now - p.last_matched_at).days > STALE_THRESHOLD_DAYS:
        if p.stage not in ("stale", "retired"):
            p.stage = "stale"
        return p

    # Behavioral patterns cap at observed
    if p.category == "behavioral":
        if p.stage == "candidate":
            rule = PROMOTION_RULES["candidate"]
            if p.evidence_count >= rule["min_evidence"] and p.confidence >= rule["min_confidence"]:
                p.stage = "observed"
                p.promoted_at = now
        return p  # Never goes past observed

    # Try promotion
    rule = PROMOTION_RULES.get(p.stage)
    if rule:
        meets_criteria = (
            p.evidence_count >= rule["min_evidence"]
            and p.confidence >= rule["min_confidence"]
        )
        # Trial -> validated also requires positive tip_lift
        if rule.get("min_tip_lift") is not None:
            if p.tip_lift is not None:
                meets_criteria = meets_criteria and p.tip_lift >= rule["min_tip_lift"]
            else:
                meets_criteria = False  # Can't promote without tip data

        if meets_criteria:
            p.stage = rule["next"]
            p.promoted_at = now

    return p


def apply_lifecycle(patterns: list[PatternCard]) -> list[PatternCard]:
    """Evaluate all patterns and enforce hard limits.

    - Promote/demote each pattern
    - Enforce MAX_ACTIVE_PATTERNS (keep highest confidence non-candidates)
    - Return all surviving patterns (retired removed)
    """
    now = datetime.now(timezone.utc)

    # Evaluate each pattern
    evaluated = [evaluate_promotion(p, now) for p in patterns]

    # Remove retired
    active = [p for p in evaluated if p.stage != "retired"]

    # Enforce max active (keep top by confidence, exclude candidates)
    non_candidates = sorted(
        [p for p in active if p.stage != "candidate"],
        key=lambda p: p.confidence, reverse=True,
    )
    candidates = [p for p in active if p.stage == "candidate"]

    if len(non_candidates) > MAX_ACTIVE_PATTERNS:
        # Retire lowest confidence patterns beyond the limit
        for p in non_candidates[MAX_ACTIVE_PATTERNS:]:
            p.stage = "retired"
        non_candidates = non_candidates[:MAX_ACTIVE_PATTERNS]

    return non_candidates + candidates


def quarantine_pattern(pattern: PatternCard, reason: str) -> PatternCard:
    """Immediately quarantine a pattern -- removes from all briefings."""
    p = pattern.model_copy()
    p.stage = "quarantined"
    p.quarantine_reason = reason
    p.quarantined_at = datetime.now(timezone.utc)
    return p


def unquarantine_pattern(pattern: PatternCard) -> PatternCard:
    """Lift quarantine -- pattern returns to 'candidate' for re-evaluation."""
    p = pattern.model_copy()
    p.stage = "candidate"
    p.quarantine_reason = ""
    p.quarantined_at = None
    return p
