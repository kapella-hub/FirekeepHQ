"""Confidence scoring for versioned memories.

Confidence is an intrinsic quality signal — how trustworthy is this memory?
It is SEPARATE from freshness (time-based decay), which is handled by the
existing memory_type decay system. Both feed recall scoring as distinct
factors.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Base confidence by source
# ---------------------------------------------------------------------------

_BASE_CONFIDENCE = {
    "user": 0.9,       # Explicitly provided by user
    "agent": 0.7,      # Created by an agent during a session
    "sleep_cycle": 0.5, # Inferred by the sleep cycle LLM extraction
    "system": 0.6,     # System-generated (e.g., distillation)
}

_DEFAULT_BASE = 0.7

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

_CONFIRMATION_BONUS_PER = 0.05   # Per confirmation
_CONFIRMATION_BONUS_CAP = 0.3    # Max bonus from confirmations
_CONTRADICTION_PENALTY_PER = 0.15  # Per contradiction
_CONTRADICTION_PENALTY_CAP = 0.5   # Max penalty from contradictions
_MIN_CONFIDENCE = 0.1
_MAX_CONFIDENCE = 1.0


def compute_confidence(
    created_by: str = "agent",
    confirmed_count: int = 0,
    contradicted_count: int = 0,
) -> float:
    """Compute confidence score using a weighted sum model.

    confidence = clamp(base + confirmation_bonus - contradiction_penalty, 0.1, 1.0)

    This is a pure function with no side effects — call it whenever you need
    a confidence score from the component signals.
    """
    base = _BASE_CONFIDENCE.get(created_by, _DEFAULT_BASE)
    bonus = min(_CONFIRMATION_BONUS_CAP, _CONFIRMATION_BONUS_PER * confirmed_count)
    penalty = min(_CONTRADICTION_PENALTY_CAP, _CONTRADICTION_PENALTY_PER * contradicted_count)

    raw = base + bonus - penalty
    return max(_MIN_CONFIDENCE, min(_MAX_CONFIDENCE, round(raw, 3)))


def confidence_for_recall_boost(confidence: float) -> float:
    """Convert a confidence score into a recall score multiplier.

    Used in the RAG engine to boost high-confidence memories and penalize
    low-confidence ones. Returns a multiplier in [0.5, 1.2] range.

    This is deliberately conservative — we don't want confidence to
    dominate over semantic relevance.
    """
    # Linear mapping: confidence 0.1 → 0.5x, confidence 1.0 → 1.2x
    return 0.5 + 0.7 * confidence
