"""Kit smoke test (Task 36): adapters + resolver + doctor hang together, one tmp env.

Mocked-transport, end-to-end pass over the whole installed-kit topology:
  1. Write a tmp ~/.firekeep OFFICE profile via the existing tests/conftest.py fixtures.
  2. Build the SIDE-BY-SIDE venv layout the 0.1.35 installer provisions: a fake venv at
     ~/.firekeep/venvs/X plus a REAL `current` link created via cli._point_current (a
     junction on Windows, a symlink on POSIX — the same primitive the installer flips).
  3. Render each runtime adapter (claude/codex/kiro/opencode) into tmp native-config
     dirs against _venv_bin(home/current), with Path.home() monkeypatched the same way
     each adapter's own test module does -- ALL FOUR share one tmp home (mirrors a real
     teammate's `~`), not isolated tmp_paths, since the point here is "does the whole
     kit cohere", not per-adapter isolation (already covered by tests/adapters/test_*.py).
  4. Assert every rendered MCP entry points at the ABSOLUTE current-based firekeep path,
     every hook command invokes the stdlib dispatcher, and — the cross-consumer guard —
     every rendered file references the `current` alias and NONE references the
     versioned venvs/<X> dir (a versioned path in a config pins that runtime to a dir a
     later update's GC removes: it works until the sweep, then every spawn dies
     file-not-found with no visible cause).
  5. Run `firekeep doctor` (run_doctor) with ONLY the network transport (get_json) and
     the CA-cert decode mocked -- every other check (agent-id, api-key, current-link,
     venv-scripts, config-perms, ca-expiry) runs for real against the tmp topology built
     above -- and assert no FAIL status, plus that doctor inspects the SAME current-based
     bin dir the adapters embedded (doctor agreeing with the rendered configs is the
     whole point of routing every surface through one alias).

Unlike the adapters' own render tests (one adapter at a time, isolated tmp_path) and
test_cli_doctor.py (doctor checks in isolation with everything but the transport
mocked), this is the ONE flow that exercises all the adapters, the resolver-backed
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
    home = firekeep_env["home"]  # IS ~/.firekeep already
    home_root = home.parent
    monkeypatch.setenv("USERPROFILE", str(home_root))  # adapters' fake_home trick
    monkeypatch.setenv("HOME", str(home_root))          # (Path.home() on both OSes)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home_root / ".config"))  # opencode
    write_config(active="office", office=DEFAULT_OFFICE)

    # -- 2. the SIDE-BY-SIDE layout `firekeep install` provisions: the venv lives at
    # its final versioned path (venvs are not relocatable) and every rendered surface
    # routes through the `current` alias — built here with the REAL link primitive
    # (cli._point_current), so on Windows this test renders and resolves through an
    # actual NTFS junction, exactly like a customer machine.
    ext = ".exe" if sys.platform == "win32" else ""
    bindir_name = "Scripts" if sys.platform == "win32" else "bin"
    versioned = home / cli.VENVS_DIR_NAME / "X"
    real_bin = versioned / bindir_name
    real_bin.mkdir(parents=True, exist_ok=True)
    for name in (
        "python", "firekeep", "firekeep-shim", "firekeep-sidecar",
        "firekeep-decision", "firekeep-symdex",
    ):
        (real_bin / f"{name}{ext}").write_text("x", encoding="utf-8")
    cli._point_current(home, versioned)

    # Render against EXACTLY what cmd_install renders against: the alias.
    venv_bin = cli._venv_bin(home / cli.CURRENT_LINK_NAME)
    assert venv_bin == cli._venv_bin(cli._venv_root(home)), (
        "with a current link present, _venv_root must select it — otherwise this "
        "test and cmd_install would render different paths"
    )

    expected_gateway = console_script_path(venv_bin / "firekeep")
    expected_python = console_script_path(venv_bin / "python")
    assert Path(expected_gateway).is_absolute()
    assert (venv_bin / f"firekeep{ext}").exists(), (
        "the alias must resolve through to the real versioned scripts"
    )

    # -- 3 & 4. render each adapter; assert ABSOLUTE current path + dispatcher hooks ---
    get_adapter("claude").render(venv_bin=venv_bin)
    claude_cfg = _read_json(home_root / ".claude.json")
    claude_settings = _read_json(home_root / ".claude" / "settings.json")
    assert set(claude_cfg["mcpServers"]) == {"firekeep"}
    entry = claude_cfg["mcpServers"]["firekeep"]
    assert entry["command"] == expected_gateway
    assert entry["args"] == ["gateway", "--runtime", "claude"]
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
    assert entry["args"] == ["gateway", "--runtime", "kiro"]
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
    assert entry["args"] == ["gateway", "--runtime", "codex"]
    # codex renders no hooks (spec §7.1: MCP servers only) -- nothing to assert there.

    get_adapter("opencode").render(venv_bin=venv_bin)
    opencode_cfg = _read_json(home_root / ".config" / "opencode" / "opencode.json")
    assert set(opencode_cfg["mcp"]) == {"firekeep"}
    assert opencode_cfg["mcp"]["firekeep"]["command"][0] == expected_gateway

    # CROSS-CONSUMER GUARD (side-by-side venvs, 0.1.35): every rendered file
    # must reference the `current` alias and may NEVER reference the versioned
    # venvs/<X> dir. The alias is what makes updates render-free (the embedded
    # strings stay literally identical across flips) and old venvs GC-able; one
    # adapter embedding the versioned path silently re-couples that runtime's
    # lifetime to a directory a later update deletes.
    rendered = {
        "claude-mcp": home_root / ".claude.json",
        "claude-hooks": home_root / ".claude" / "settings.json",
        "kiro": home_root / ".kiro" / "agents" / "firekeep.json",
        "codex": home_root / ".codex" / "config.toml",
        "opencode": home_root / ".config" / "opencode" / "opencode.json",
        "opencode-plugin": home_root / ".config" / "opencode" / "plugins" / "firekeep-hooks.js",
    }
    for label, path in rendered.items():
        assert path.is_file(), f"{label}: expected rendered file {path}"
        text = path.read_text(encoding="utf-8")
        assert cli.CURRENT_LINK_NAME in text, (
            f"{label} must route through the `current` alias, got no mention in {path}"
        )
        assert cli.VENVS_DIR_NAME not in text, (
            f"{label} embeds a versioned venvs/ path — pinned to a dir GC removes"
        )

    # -- 5. `firekeep doctor` -- mock ONLY the network transport + CA-cert decode ------
    monkeypatch.setattr(
        cli, "get_json", lambda url, **kw: {"status": "ok", "version": cli.__version__}
    )
    # serverupdate.check() (the server-version row) reads cortex /version through its
    # OWN imported get_json, not cli's -- same network transport, separate reference,
    # so it needs the same mock or it would make a real HTTPS call to
    # firekeep.office.example. This profile has no [dist] section, so the manifest
    # half never fetches (dist_base raises before any request is made).
    monkeypatch.setattr(
        cli.serverupdate, "get_json", lambda url, **kw: {"status": "ok", "version": cli.__version__}
    )
    ca_path = home / "firekeep-root-ca.crt"  # DEFAULT_OFFICE's ca_path
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
            "config-perms", "ca-expiry", "current-link"} <= names

    rows = {name: (status, detail) for name, status, detail in results}
    # Doctor must inspect the SAME current-based bin dir the adapters embedded —
    # if doctor resolved the venv differently from the render path, a green
    # venv-scripts row would say nothing about the configs actually in use.
    status, detail = rows["venv-scripts"]
    assert status == "ok"
    assert detail == str(venv_bin)
    assert cli.VENVS_DIR_NAME not in detail
    # current -> venvs/X while this client is __version__: the mismatch row is
    # deliberately a WARN (normal mid-update, or right after a rollback), never
    # a fail — asserted here so the integrated flow pins it, not just the unit
    # tests in test_cli_doctor.py.
    status, detail = rows["current-link"]
    assert status == "warn"
    assert "X" in detail
