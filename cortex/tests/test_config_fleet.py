"""Fleet-as-GPU settings exist, default as documented, and are wired in compose."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings
from tests.test_procedure_config import _has_env_entry, _service_block

REPO = Path(__file__).resolve().parents[2]


def test_defaults():
    s = Settings(_env_file=None)
    assert s.FLEET_ENQUEUE_ENABLED is True
    assert s.FLEET_ENQUEUE_MAX_PER_RUN == 20


def test_env_override(monkeypatch):
    monkeypatch.setenv("FLEET_ENQUEUE_ENABLED", "false")
    monkeypatch.setenv("FLEET_ENQUEUE_MAX_PER_RUN", "5")
    s = Settings(_env_file=None)
    assert s.FLEET_ENQUEUE_ENABLED is False and s.FLEET_ENQUEUE_MAX_PER_RUN == 5


COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
FLAGS = [("FLEET_ENQUEUE_ENABLED", "true"), ("FLEET_ENQUEUE_MAX_PER_RUN", "20")]


@pytest.mark.parametrize("service", ["cortex-api", "cortex-mcp", "cortex-worker", "cortex-beat"])
@pytest.mark.parametrize("name,default", FLAGS)
def test_every_cortex_service_carries_the_flag_with_the_code_default(service, name, default):
    """One Settings class, four processes: the enqueue pass runs in the worker,
    the digest's `enabled` flag is read by the API — a var plumbed into only some
    of them makes one deployment answer differently per container."""
    assert _has_env_entry(_service_block(service), name), f"{name} missing from {service}"
    hits = re.findall(rf"{name}:\s*\$\{{{name}:-([^}}]*)\}}", COMPOSE)
    assert hits, f"{name} is not plumbed in docker-compose.yml"
    assert all(h.strip().lower() == default for h in hits), hits


def test_env_example_and_guide_carry_the_flags():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    assert "FLEET_ENQUEUE_ENABLED" in env and "FLEET_ENQUEUE_MAX_PER_RUN" in env
    guide = (REPO / "docs/guides/cortex-configuration.md").read_text(encoding="utf-8")
    assert "`FLEET_ENQUEUE_ENABLED` (default `true`)" in guide
    assert "`FLEET_ENQUEUE_MAX_PER_RUN` (default `20`)" in guide
