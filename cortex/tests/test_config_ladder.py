# cortex/tests/test_config_ladder.py
"""Skill-ladder settings exist, default as documented, and are plumbed everywhere."""
import re
from pathlib import Path

import pytest

from app.config import Settings
from tests.test_procedure_config import _has_env_entry, _service_block

REPO = Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
FLAGS = [
    ("SKILL_LADDER_ENABLED", "true"),
    ("SKILL_LADDER_MODE", "shadow"),
    ("SKILL_LADDER_SCHEDULE_HOURS", "24"),
    ("SKILL_LADDER_PROMOTE_MIN_SUCCESSES", "3"),
    ("SKILL_LADDER_PROMOTE_MIN_AGENTS", "2"),
    ("SKILL_LADDER_TRIAL_TTL_DAYS", "60"),
]


def test_defaults():
    s = Settings(_env_file=None)
    assert s.SKILL_LADDER_ENABLED is True
    assert s.SKILL_LADDER_MODE == "shadow"
    assert s.SKILL_LADDER_SCHEDULE_HOURS == 24
    assert s.SKILL_LADDER_PROMOTE_MIN_SUCCESSES == 3
    assert s.SKILL_LADDER_PROMOTE_MIN_AGENTS == 2
    assert s.SKILL_LADDER_TRIAL_TTL_DAYS == 60


def test_env_override(monkeypatch):
    monkeypatch.setenv("SKILL_LADDER_MODE", "enforce")
    monkeypatch.setenv("SKILL_LADDER_PROMOTE_MIN_SUCCESSES", "5")
    s = Settings(_env_file=None)
    assert s.SKILL_LADDER_MODE == "enforce" and s.SKILL_LADDER_PROMOTE_MIN_SUCCESSES == 5


@pytest.mark.parametrize("service", ["cortex-api", "cortex-mcp", "cortex-worker", "cortex-beat"])
@pytest.mark.parametrize("name,default", FLAGS)
def test_every_cortex_service_carries_the_flag_with_the_code_default(service, name, default):
    assert _has_env_entry(_service_block(service), name), f"{name} missing from {service}"
    hits = re.findall(rf"{name}:\s*\$\{{{name}:-([^}}]*)\}}", COMPOSE)
    assert hits and all(h.strip().lower() == default for h in hits), (name, hits)


def test_env_example_and_guide_carry_the_flags():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    guide = (REPO / "docs/guides/cortex-configuration.md").read_text(encoding="utf-8")
    for name, default in FLAGS:
        assert name in env, name
        assert f"`{name}` (default `{default}`)" in guide, name
