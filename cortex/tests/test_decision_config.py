"""SP4 Decision Board: config settings."""
from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

_REPO = Path(__file__).resolve().parents[2]


def test_decision_defaults():
    s = Settings()
    assert s.DECISION_ENABLED is True
    # 20.0 -> 30.0 (LLM endpoint phase 2). Pinned because the number is a
    # CONTRACT with a second process, not a preference: the client's
    # firekeep-decision server defaults its own synth timeout to 30.0 and gives
    # this endpoint synth + 15s = 45s before hanging up.
    assert s.DECISION_SYNTH_TIMEOUT_SECONDS == 30.0
    assert s.DECISION_MAX_QUESTIONS == 8


def test_compose_default_does_not_override_the_code_default():
    """A stale `${VAR:-20}` in compose silently wins over the code default.

    Compose interpolation supplies the fallback when the operator's `.env` does
    not set the variable, so the container receives compose's number and
    Settings never sees its own. That is not hypothetical: it is how the phase-1
    change nearly shipped a reduced timeout that the code no longer declared.
    Both cortex services must therefore carry the same default this file pins.
    """
    text = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    found = re.findall(
        r"DECISION_SYNTH_TIMEOUT_SECONDS:\s*\$\{DECISION_SYNTH_TIMEOUT_SECONDS:-([0-9.]+)\}",
        text,
    )
    assert found, "docker-compose.yml no longer sets DECISION_SYNTH_TIMEOUT_SECONDS"
    assert {float(v) for v in found} == {Settings().DECISION_SYNTH_TIMEOUT_SECONDS}


def test_env_example_default_matches_the_code_default():
    text = (_REPO / ".env.example").read_text(encoding="utf-8")
    found = re.findall(r"^DECISION_SYNTH_TIMEOUT_SECONDS=([0-9.]+)$", text, re.MULTILINE)
    assert found, ".env.example no longer sets DECISION_SYNTH_TIMEOUT_SECONDS"
    assert {float(v) for v in found} == {Settings().DECISION_SYNTH_TIMEOUT_SECONDS}
