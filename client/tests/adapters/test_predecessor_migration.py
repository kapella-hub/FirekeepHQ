"""Upgrading from the predecessor kit must leave ONE layer, not two.

Why this file exists
--------------------
A machine that ran the predecessor kit carries six `nexus-*` MCP servers and five
`nexus_client.hooks` entries. Firekeep registers its own six and its own hooks. With
no migration the machine ends up with TWELVE servers -- six pointing at a config path
that no longer exists, failing to connect on every session start -- and every
lifecycle event firing twice: doubled presence registration, doubled distill
enqueues.

That hazard was already known and solved once, for the bash->python generation
(base.LEGACY_HOOK_MARKERS, upsert_hook_group's "collapse ALL groups" behaviour).
The predecessor->Firekeep generation was simply never added.

THE ANTI-RENAME GUARD, which is the subtler half:
Legacy tokens name artifacts left by PREVIOUS generations, so they must keep
spelling the OLD thing forever. The predecessor rename mechanically rewrote
LEGACY_ENV_KEYS from NEXUS_*_URL to FIREKEEP_*_URL -- keys no machine has ever
had -- silently disarming that cleanup while every test stayed green. Renaming a
legacy token is not a rename, it is a deletion. These tests fail loudly if it
happens again.
"""
from __future__ import annotations

import json

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import (
    LEGACY_ENV_KEYS,
    LEGACY_HOOK_MARKERS,
    LEGACY_MCP_KEYS,
)


class TestLegacyTokensStillNameTheOldThing:
    def test_env_keys_name_the_predecessor(self):
        assert LEGACY_ENV_KEYS, "the cleanup list must not be empty"
        for key in LEGACY_ENV_KEYS:
            assert key.startswith("NEXUS_"), (
                f"{key!r} names the CURRENT product. Legacy tokens must spell the OLD "
                f"one -- a rename here silently disarms the migration."
            )

    def test_mcp_keys_name_the_predecessor(self):
        assert LEGACY_MCP_KEYS
        for key in LEGACY_MCP_KEYS:
            assert key.startswith("nexus-"), f"{key!r} must name the predecessor"

    def test_hook_markers_cover_the_predecessor_python_kit(self):
        assert any("nexus_client" in m for m in LEGACY_HOOK_MARKERS), (
            "the predecessor kit invoked `-m nexus_client.hooks`; without a marker for it "
            "an upgraded machine keeps both hook layers and fires every event twice"
        )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    # Redirect Path.home() on both Windows (USERPROFILE) and POSIX (HOME) — never real ~.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _machine_with_predecessor_kit(fake_home):
    """A realistic upgraded machine: predecessor servers, hooks and env in place."""
    (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
    (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "nexus-cortex": {"command": "old"}, "nexus-bridge": {"command": "old"},
        "nexus-sentinel": {"command": "old"}, "nexus-relay": {"command": "old"},
        "nexus-symdex": {"command": "old"}, "nexus-decision": {"command": "old"},
        "someone-elses-server": {"command": "keep me"},
    }}), encoding="utf-8")
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"NEXUS_CORTEX_URL": "http://old:8100", "FOO": "bar"},
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "C:/x/.nexus/venv/Scripts/python.exe -m nexus_client.hooks session_start"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "C:/x/.nexus/venv/Scripts/python.exe -m nexus_client.hooks stop"}]}],
        },
    }), encoding="utf-8")


class TestClaudeUpgrade:
    def test_render_leaves_exactly_one_layer(self, fake_home, tmp_path):
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")

        servers = json.loads((fake_home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]
        for k in LEGACY_MCP_KEYS:
            assert k not in servers, f"{k} survived the upgrade -- the machine now has two kits"
        assert "someone-elses-server" in servers, "a foreign server must never be touched"
        assert any(k.startswith("firekeep-") for k in servers)

    def test_no_lifecycle_event_fires_twice(self, fake_home, tmp_path):
        """THE failure this migration exists to prevent."""
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")

        hooks = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        for event in ("SessionStart", "Stop"):
            commands = [h.get("command", "") for g in hooks.get(event, []) for h in g.get("hooks", [])]
            assert not any("nexus_client" in c for c in commands), (
                f"{event} still invokes the predecessor kit -- it will fire twice"
            )
            assert sum("firekeep_client" in c for c in commands) == 1, (
                f"{event} should invoke exactly one firekeep core, got {commands}"
            )

    def test_retired_env_keys_are_removed_and_foreign_ones_survive(self, fake_home, tmp_path):
        _machine_with_predecessor_kit(fake_home)
        get_adapter("claude").render(venv_bin=tmp_path / "vbin")
        env = json.loads((fake_home / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        assert "NEXUS_CORTEX_URL" not in env
        assert env.get("FOO") == "bar"
