import json
import sys

import pytest

from firekeep_client.adapters import get_adapter


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # The adapter honors XDG_CONFIG_HOME (opencode follows XDG); a developer's real
    # value must never leak a test render outside tmp_path.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


def _read(p):
    return json.loads(p.read_text())


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path
    (see test_claude.py's identical helper)."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def _config(fake_home):
    return fake_home / ".config" / "opencode" / "opencode.json"


def _plugin(fake_home):
    return fake_home / ".config" / "opencode" / "plugins" / "firekeep-hooks.js"


def test_opencode_render_writes_local_mcp_servers(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("opencode").render(venv_bin=venv_bin)
    data = _read(_config(fake_home))

    # opencode's `mcp` shape: type=local, command is ONE array (cmd + args), enabled flag.
    assert data["mcp"]["firekeep"] == {
        "type": "local",
        "command": [_exe(venv_bin / "firekeep"), "gateway", "--runtime", "opencode"],
        "enabled": True,
    }
    assert list(data["mcp"]) == ["firekeep"]


def test_opencode_render_writes_plugin_bridge(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("opencode").render(venv_bin=venv_bin)
    text = _plugin(fake_home).read_text(encoding="utf-8")

    assert "firekeep-owned: opencode hook bridge" in text
    # Bridge invokes the hook DISPATCHER (module __main__), never a core module import.
    assert "firekeep_client.hooks" in text
    # The venv python is baked in, forward-slashed (JS string, and bash-safe on Windows).
    assert _exe(venv_bin / "python").replace("\\", "/") in text
    # pre_tool carries the Claude-style block remap; a gateway block (rc=1) must
    # surface as the blocking exit code, and exit 2 is what the bridge throws on.
    assert "--block-exit" in text
    # opencode tool names map to the Claude-shaped names the hook cores expect.
    assert '"Edit"' in text and '"Write"' in text and '"Bash"' in text
    assert "file_path" in text
    # All five cores are wired.
    for core in ("session_start", "prompt", "stop", "pre_tool", "post_tool"):
        assert core in text
    # Validated live (opencode 1.14.22, 2026-07-18): in `opencode run` mode the
    # session.created bus event publishes BEFORE plugins subscribe, so the bridge
    # must also fire session_start from the first event/tool hook it ever sees.
    assert "ensureStarted" in text


_PINNED_CFG = """
[active]
profile = personal
[personal]
agent_id = tester
[office]
agent_id = tester
[pins]
opencode = office
"""


def _write_cfg(tmp_path, monkeypatch, text):
    cfg = tmp_path / "config"
    cfg.write_text(text, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    return cfg


def test_legacy_pinned_opencode_renders_no_profile_artifacts(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG)
    get_adapter("opencode").render(venv_bin=tmp_path / "vbin")

    data = _read(_config(fake_home))
    assert "environment" not in data["mcp"]["firekeep"]
    text = _plugin(fake_home).read_text(encoding="utf-8")
    assert "--profile" not in text


def test_unpinned_opencode_render_has_no_environment_or_profile(tmp_path, monkeypatch, fake_home):
    _write_cfg(tmp_path, monkeypatch, _PINNED_CFG.replace("[pins]\nopencode = office\n", ""))
    get_adapter("opencode").render(venv_bin=tmp_path / "vbin")

    data = _read(_config(fake_home))
    assert "environment" not in data["mcp"]["firekeep"]
    assert "--profile" not in _plugin(fake_home).read_text(encoding="utf-8")


def test_opencode_non_clobbering(fake_home, tmp_path):
    cfg = _config(fake_home)
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({
        "model": "anthropic/claude-sonnet-5",
        "mcp": {"custom": {"type": "local", "command": ["x"]}},
        "plugin": ["some-npm-plugin"],
    }))
    adapter = get_adapter("opencode")

    adapter.render(venv_bin=tmp_path / "vbin")
    data = _read(cfg)
    assert data["model"] == "anthropic/claude-sonnet-5"          # foreign top-level key
    assert data["mcp"]["custom"] == {"type": "local", "command": ["x"]}
    assert data["plugin"] == ["some-npm-plugin"]
    assert "firekeep" in data["mcp"]

    adapter.unrender()
    data2 = _read(cfg)
    assert data2["mcp"] == {"custom": {"type": "local", "command": ["x"]}}
    assert data2["model"] == "anthropic/claude-sonnet-5"


def test_opencode_render_respects_xdg_config_home(fake_home, tmp_path, monkeypatch):
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    get_adapter("opencode").render(venv_bin=tmp_path / "vbin")
    assert (xdg / "opencode" / "opencode.json").exists()
    assert (xdg / "opencode" / "plugins" / "firekeep-hooks.js").exists()
    assert not _config(fake_home).exists()


def test_opencode_foreign_plugin_file_survives_render_and_unrender(fake_home, tmp_path):
    """kiro clobber lesson (2026-07-13): assume a pre-kit machine may already have a
    hand-authored file at our chosen path. No marker -> not ours -> never overwrite,
    never delete."""
    plugin = _plugin(fake_home)
    plugin.parent.mkdir(parents=True)
    plugin.write_text("export const Mine = async () => ({})\n", encoding="utf-8")
    adapter = get_adapter("opencode")

    adapter.render(venv_bin=tmp_path / "vbin")
    assert plugin.read_text(encoding="utf-8") == "export const Mine = async () => ({})\n"

    adapter.unrender()
    assert plugin.exists()


def test_opencode_unrender_removes_owned_plugin_and_only_firekeep_mcp(fake_home, tmp_path):
    adapter = get_adapter("opencode")
    adapter.render(venv_bin=tmp_path / "vbin")
    assert _plugin(fake_home).exists()

    adapter.unrender()
    assert not _plugin(fake_home).exists()
    data = _read(_config(fake_home))
    assert data.get("mcp", {}) == {}


def test_opencode_rerender_is_idempotent(fake_home, tmp_path):
    adapter = get_adapter("opencode")
    adapter.render(venv_bin=tmp_path / "vbin")
    first = _config(fake_home).read_text()
    adapter.render(venv_bin=tmp_path / "vbin")
    assert _config(fake_home).read_text() == first
    # exactly one owned plugin file, still marker-bearing
    assert "firekeep-owned: opencode hook bridge" in _plugin(fake_home).read_text(encoding="utf-8")


def test_opencode_session_deleted_runs_both_stop_and_session_end(fake_home, tmp_path):
    """session.deleted is opencode's REAL session end (unlike claude's per-turn
    Stop), so it must drive both cores: `stop` for the snapshot + distill enqueue,
    `session_end` for the presence deregister.

    Regression guard with teeth: OPENCODE-VALIDATION.md row 7 confirmed opencode's
    deregister empirically, but it did so via the deregister that used to live in
    the `stop` core. That code moved to `session_end`; if this dispatch is dropped,
    opencode silently stops deregistering and row 7's evidence no longer describes
    the shipped behaviour.
    """
    adapter = get_adapter("opencode")
    adapter.render(venv_bin=tmp_path / "vbin")
    js = _plugin(fake_home).read_text(encoding="utf-8")

    # Slice to the next handler, not to the next "}," — that would cut at the `{}`
    # ARGUMENT inside runCore("stop", {}, 5000) and hide the session_end call.
    branch = js[js.index("session.deleted"):js.index('"tool.execute.before"')]
    assert 'runCore("stop"' in branch
    assert 'runCore("session_end"' in branch


def test_opencode_plugin_dispatches_every_hook_core(fake_home, tmp_path):
    """All six cores reachable from the bridge. Catches a core added to the
    dispatcher but never wired into opencode (kiro/claude have their own tables;
    opencode's wiring is hand-written JS and is the easiest to forget)."""
    adapter = get_adapter("opencode")
    adapter.render(venv_bin=tmp_path / "vbin")
    js = _plugin(fake_home).read_text(encoding="utf-8")

    for core in ("session_start", "prompt", "stop", "session_end", "pre_tool", "post_tool"):
        assert f'runCore("{core}"' in js, f"opencode plugin never dispatches {core}"
