"""SP4 Decision Board: config settings."""
from __future__ import annotations

from app.config import Settings


def test_decision_defaults():
    s = Settings()
    assert s.DECISION_ENABLED is True
    assert s.DECISION_SYNTH_TIMEOUT_SECONDS == 20.0
    assert s.DECISION_MAX_QUESTIONS == 8
