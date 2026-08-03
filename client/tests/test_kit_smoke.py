"""Kit smoke test (Task 36): adapters + resolver + doctor hang together, one tmp env.

Mocked-transport, end-to-end pass over the whole installed-kit topology:
  1. Write a tmp ~/.firekeep OFFICE profile via the existing tests/conftest.py fixtures.
  2. Render each runtime adapter (claude/codex/kiro) into tmp native-config dirs, with
     Path.home() monkeypatched the same way each adapter's own test module does --
     ALL THREE share one tmp home (mirrors a real teammate's `~`), not three isolated
     tmp_paths, since the point here is "does the whole kit cohere", not per-adapter
     isolation (already covered by tests/adapters/test_*.py).
  3. Assert every rendered MCP entry points at the ABSOLUTE venv firekeep-shim path per
     service, and every hook command invokes the stdlib dispatcher.
  4. Run `firekeep doctor` (run_doctor) with ONLY the network transport (get_json) and the
     CA-cert decode mocked -- every other check (agent-id, api-key, venv-scripts,
     config-perms, ca-expiry) runs for real against the tmp topology built above -- and
     assert no FAIL status.

Unlike the adapters' own render tests (one adapter at a time, isolated tmp_path) and
test_cli_doctor.py (doctor checks in isolation with everything but the transport
mocked), this is the ONE flow that exercises all three adapters, the resolver-backed
~/.firekeep config, and doctor's real (non-network) checks together against a single
shared tmp topology -- the "whole kit hangs together" pin the SP1b design spec (§10/§12)
calls for.
"""
from __future__ import annotations

import json
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from firekeep_client import cli
from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import console_script_path
from firekeep_client.resolver import SERVICES

from tests.conftest import DEFAULT_OFFICE


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# (rendered-hook-event, hook-core) pairs, per runtime -- mirrors CLAUDE_HOOKS /
# KIRO_HOOKS in the adapters themselves; kept local so this test pins the OBSERVED
# rendered shape, not an import of the adapters' own tables.
CLAUDE_EVENTS = (
    ("SessionStart", "session_start"), ("Stop", "stop"),
    ("UserPromptSubmit", "prompt"), ("PreToolUse", "pre_tool"),
    ("PostToolUse", "post_tool"),
)
KIRO_EVENTS = (
    ("agentSpawn", "session_start"), ("userPromptSubmit", "prompt"),
    ("preToolUse", "pre_tool"), ("postToolUse", "post_tool"), ("stop", "stop"),
)


def test_kit_hangs_together(firekeep_env, write_config, monkeypatch):
    # -- 1. tmp ~/.firekeep OFFICE profile, via the existing conftest fixtures ---------
    home_root = firekeep_env["home"].parent  # firekeep_env["home"] IS ~/.firekeep already
    monkeypatch.setenv("USERPROFILE", str(home_root))  # adapters' fake_home trick
    monkeypatch.setenv("HOME", str(home_root))          # (Path.home() on both OSes)
    write_config(active="office", office=DEFAULT_OFFICE)

    # A believable installed venv at the SAME location `firekeep install` uses
    # (firekeep_env["home"] / "venv") -- serves double duty below: the path baked into
    # every adapter's rendered MCP command, and what doctor's venv-scripts check
    # inspects, so this one fixture stands in for a real `firekeep install`.
    ext = ".exe" if sys.platform == "win32" else ""
    bindir_name = "Scripts" if sys.platform == "win32" else "bin"
    venv_bin = firekeep_env["home"] / "venv" / bindir_name
    venv_bin.mkdir(parents=True, exist_ok=True)
    for name in ("firekeep", "firekeep-shim", "firekeep-sidecar"):
        (venv_bin / f"{name}{ext}").write_text("x", encoding="utf-8")

    expected_gateway = console_script_path(venv_bin / "firekeep")
    expected_python = console_script_path(venv_bin / "python")
    assert Path(expected_gateway).is_absolute()

    # -- 2 & 3. render each adapter; assert ABSOLUTE shim path + dispatcher hooks ---
    get_adapter("claude").render(venv_bin=venv_bin)
    claude_cfg = _read_json(home_root / ".claude.json")
    claude_settings = _read_json(home_root / ".claude" / "settings.json")
    assert set(claude_cfg["mcpServers"]) == {"firekeep"}
    entry = claude_cfg["mcpServers"]["firekeep"]
    assert entry["command"] == expected_gateway
    assert entry["args"] == ["gateway"]
    for event, core in CLAUDE_EVENTS:
        cmd = claude_settings["hooks"][event][0]["hooks"][0]["command"]
        # hooks are bash-executed -> forward-slash interpreter path (bash eats backslashes)
        assert expected_python.replace("\\", "/") in cmd
        assert f"-m firekeep_client.hooks {core}" in cmd

    get_adapter("kiro").render(venv_bin=venv_bin)
    kiro_data = _read_json(home_root / ".kiro" / "agents" / "firekeep.json")
    assert set(kiro_data["mcpServers"]) == {"firekeep"}
    entry = kiro_data["mcpServers"]["firekeep"]
    assert entry["command"] == expected_gateway
    assert entry["args"] == ["gateway"]
    for event, core in KIRO_EVENTS:
        cmd = kiro_data["hooks"][event][0]["command"]
        assert expected_python.replace("\\", "/") in cmd
        assert f"-m firekeep_client.hooks {core}" in cmd

    get_adapter("codex").render(venv_bin=venv_bin)
    codex_text = (home_root / ".codex" / "config.toml").read_text(encoding="utf-8")
    codex_parsed = tomllib.loads(codex_text)
    assert set(codex_parsed["mcp_servers"]) == {"firekeep"}
    entry = codex_parsed["mcp_servers"]["firekeep"]
    assert entry["command"] == expected_gateway
    assert entry["args"] == ["gateway"]
    # codex renders no hooks (spec §7.1: MCP servers only) -- nothing to assert there.

    # -- 4. `firekeep doctor` -- mock ONLY the network transport + CA-cert decode ------
    monkeypatch.setattr(
        cli, "get_json", lambda url, **kw: {"status": "ok", "version": cli.__version__}
    )
    ca_path = firekeep_env["home"] / "firekeep-root-ca.crt"  # DEFAULT_OFFICE's ca_path
    ca_path.write_text("dummy PEM bytes -- decode is mocked below", encoding="utf-8")
    monkeypatch.setattr(
        cli, "_cert_not_after",
        lambda p: datetime.now(timezone.utc) + timedelta(days=400),
    )

    results = cli.run_doctor()
    failures = [r for r in results if r[1] == "fail"]
    assert not failures, f"doctor reported FAIL: {failures}"

    names = {name for name, _, _ in results}
    assert set(SERVICES) <= names  # per-service health rows present
    assert {"versions", "agent-id", "api-key", "venv-scripts",
            "config-perms", "ca-expiry"} <= names
