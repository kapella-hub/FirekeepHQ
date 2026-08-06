"""Config drift guards for Living Procedures.

Compose's `${VAR:-default}` wins over the code default whenever `.env` is
silent, so the two must agree — that is not hypothetical, it is how the
decision-board phase-1 change nearly shipped a timeout the code no longer
declared (see test_decision_config.py). And the beat service must receive any
schedule var, because `beat_schedule` is built from `get_settings()` at import
time: a var absent from `cortex-beat`'s environment silently uses the code
default there while the API uses the operator's `.env` value.

Spec: docs/superpowers/specs/2026-08-06-living-procedures-design.md §8
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

DEFAULTS = {
    "PROCEDURE_ENABLED": "false",
    "PROCEDURE_WARN_ENABLED": "true",
    "PROCEDURE_MIN_EXECUTIONS": "5",
    "PROCEDURE_PRIOR_N": "5",
    "PROCEDURE_EFFICACY_DELTA": "0.15",
    "PROCEDURE_WINDOW_DAYS": "30",
    "PROCEDURE_EXEC_TTL_DAYS": "90",
    "PROCEDURE_AGENT_CAP": "5",
    "PROCEDURE_INDEX_CACHE_SECONDS": "30",
    "PROCEDURE_MAX_SPECS": "50",
    "PROCEDURE_SCHEDULE_HOURS": "24",
}


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_setting_exists_with_the_documented_default(name, expected):
    s = Settings()
    actual = getattr(s, name)
    assert str(actual).lower() == expected.lower(), f"{name}={actual!r}"


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_compose_default_matches_the_code_default(name, expected):
    hits = re.findall(rf"{name}:\s*\$\{{{name}:-([^}}]*)\}}", COMPOSE)
    assert hits, f"{name} is not plumbed in docker-compose.yml"
    for h in hits:
        assert h.strip().lower() == expected.lower(), f"{name} compose default {h!r}"


@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_documented_in_env_example(name):
    assert name in ENV_EXAMPLE, f"{name} missing from .env.example"


def _service_block(name: str) -> str:
    """The compose block for one service, bounded at the next top-level key.

    The plan's `COMPOSE.split("cortex-beat:")[1].split("\\n  cortex-")[0]` is a
    no-op here: cortex-beat is the LAST cortex service in the file, so that
    second split left the whole remainder (bridge, sentinel, relay, dashboard,
    volumes, networks) inside the "beat block". Bounding on the next 2-space
    service key is what makes this assert about cortex-beat specifically.
    """
    body = COMPOSE.split(f"\n  {name}:", 1)[1]
    nxt = re.search(r"\n  [a-z0-9_-]+:", body)
    return body[: nxt.start()] if nxt else body


def _has_env_entry(block: str, name: str) -> bool:
    """A real `NAME: ${NAME:-default}` entry, not a mention.

    Substring matching is not enough and this is not theoretical: the comment
    introducing the beat block NAMES PROCEDURE_SCHEDULE_HOURS while explaining
    why beat needs it, so `"PROCEDURE_SCHEDULE_HOURS" in block` stayed true with
    the environment entry deleted. Proven by mutation — the substring version
    passed the deletion it exists to catch.
    """
    return re.search(rf"^\s+{name}:\s*\$\{{{name}:-", block, re.MULTILINE) is not None


def test_schedule_var_reaches_the_beat_service():
    """beat_schedule is built from get_settings() at import time, so a schedule
    var absent from cortex-beat's environment silently uses the code default
    there while the API uses the .env value."""
    assert _has_env_entry(_service_block("cortex-beat"), "PROCEDURE_SCHEDULE_HOURS")


@pytest.mark.parametrize("service", ["cortex-api", "cortex-mcp", "cortex-worker", "cortex-beat"])
@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_every_cortex_service_carries_every_var(service, name):
    """One Settings class, four processes. A var plumbed into only some of them
    means the same deployment answers differently depending on which container
    read it — the drift this whole file exists to prevent, one level up."""
    assert _has_env_entry(_service_block(service), name), f"{name} missing from {service}"
