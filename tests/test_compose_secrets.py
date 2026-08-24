"""Static compose parsing (D8f): FIREKEEP_BRIDGE_KEY must reach only bridge.

env_file imports the WHOLE .env into every service that declares it, so a key
minted for one container is available to all of them unless each OTHER
service explicitly blanks it in its own `environment` block — the cortex-mcp
confused-deputy pin (FIREKEEP_API_KEY: "") is the precedent this follows.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bridge_key_reaches_only_the_bridge_container():
    """env_file imports the WHOLE .env: every non-bridge service that uses it
    must explicitly blank FIREKEEP_BRIDGE_KEY (the cortex-mcp confused-deputy
    pin is the precedent)."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        env_files = svc.get("env_file") or []
        env = svc.get("environment") or {}
        if not env_files or name == "bridge":
            continue
        assert isinstance(env, dict), f"service {name} environment must be a mapping"
        assert env.get("FIREKEEP_BRIDGE_KEY") == "", (
            f"service {name} imports .env but does not blank FIREKEEP_BRIDGE_KEY")
    bridge_env = compose["services"]["bridge"]["environment"]
    assert bridge_env["NB_FIREKEEP_API_KEY"] == "${FIREKEEP_BRIDGE_KEY:-}"
