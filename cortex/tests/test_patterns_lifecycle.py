"""N=1 learning loop, Task 1: promotion is frozen behind PATTERN_VALIDATION_ENABLED.

The promotion-ladder MATH is unchanged (see TestPromotionLifecycle in
test_patterns.py) — this file only proves the flag gates whether it RUNS.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.patterns import lifecycle
from app.patterns.models import PatternCard


def _pattern(stage: str = "candidate", evidence: int = 50, confidence: float = 0.9) -> PatternCard:
    return PatternCard(
        id="pat-test", description="test", pattern_type="memory_first",
        confidence=confidence, evidence_count=evidence, baseline_rate=0.6,
        pattern_rate=0.8, lift=1.33, recommendation="test",
        stage=stage, category="procedural",
    )


def test_promotion_frozen_when_validation_disabled(monkeypatch):
    """Flag off: a pattern that would normally promote stays put (validation frozen)."""
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: SimpleNamespace(PATTERN_VALIDATION_ENABLED=False),
        raising=False,
    )
    p = _pattern(stage="candidate", evidence=50, confidence=0.9)  # would normally promote
    assert lifecycle.evaluate_promotion(p).stage == "candidate"   # frozen


def test_promotion_runs_when_validation_enabled(monkeypatch):
    """Flag on: the ladder math is untouched — candidate still promotes to observed."""
    monkeypatch.setattr(
        lifecycle, "get_settings",
        lambda: SimpleNamespace(PATTERN_VALIDATION_ENABLED=True),
        raising=False,
    )
    p = _pattern(stage="candidate", evidence=50, confidence=0.9)
    assert lifecycle.evaluate_promotion(p).stage == "observed"
