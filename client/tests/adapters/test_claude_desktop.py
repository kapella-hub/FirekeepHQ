"""claude-desktop adapter: the firekeep entry in claude_desktop_config.json.

The invariants under test:
  - render sets ONLY mcpServers.firekeep; every other key (foreign servers,
    app settings) survives at the value level.
  - a file that does not parse is refused loudly and left byte-identical —
    the render loop has no per-runtime catch, so the refusal must not raise.
  - unrender removes only our entry and never deletes the file (it belongs
    to Claude Desktop, not to us).
  - app_present is the auto-render gate: config DIR existence, nothing else.
"""
import json
import sys

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.claude_desktop import (
    app_present,
    config_path,
    mcp_entry_is_current,
)


@pytest.fixture
def desktop_home(tmp_path, monkeypatch):
    """Steer _config_dir to tmp on every platform: APPDATA (win32),
    XDG_CONFIG_HOME (linux), HOME (darwin)."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _exe(path):
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def _render(tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude-desktop").render(venv_bin=venv_bin)
    return venv_bin


def test_render_creates_config_with_gateway(desktop_home, tmp_path):
    venv_bin = _render(tmp_path)
    data = json.loads(config_path().read_text(encoding="utf-8"))
    entry = data["mcpServers"]["firekeep"]
    assert entry["command"] == _exe(venv_bin / "firekeep")
    assert entry["args"] == ["gateway", "--runtime", "claude-desktop"]


def test_render_preserves_foreign_keys(desktop_home, tmp_path):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "mcpServers": {"other": {"command": "npx", "args": ["-y", "other"]}},
        "globalShortcut": "Ctrl+Space",
    }), encoding="utf-8")
    _render(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == {"command": "npx", "args": ["-y", "other"]}
    assert data["globalShortcut"] == "Ctrl+Space"
    assert "firekeep" in data["mcpServers"]


def test_render_is_idempotent_and_nags_once(desktop_home, tmp_path, capsys):
    _render(tmp_path)
    first = config_path().read_text(encoding="utf-8")
    assert "restart the app" in capsys.readouterr().out
    _render(tmp_path)
    # Byte-identical second render: no rewrite, and no repeat restart nag —
    # this re-runs on every `firekeep update`.
    assert config_path().read_text(encoding="utf-8") == first
    assert "restart the app" not in capsys.readouterr().out


def test_render_refuses_corrupt_json(desktop_home, tmp_path, capsys):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    _render(tmp_path)  # must not raise: the install loop has no per-runtime catch
    assert path.read_text(encoding="utf-8") == "{not json"
    assert "not valid JSON" in capsys.readouterr().err


def test_render_refuses_non_object_config(desktop_home, tmp_path, capsys):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("[1, 2]", encoding="utf-8")
    _render(tmp_path)
    assert path.read_text(encoding="utf-8") == "[1, 2]"
    assert capsys.readouterr().err  # warned, not silent


def test_render_refuses_non_object_mcpservers(desktop_home, tmp_path, capsys):
    path = config_path()
    path.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": "oops"})
    path.write_text(original, encoding="utf-8")
    _render(tmp_path)
    assert path.read_text(encoding="utf-8") == original
    assert "mcpServers" in capsys.readouterr().err


def test_render_treats_empty_file_as_fresh(desktop_home, tmp_path):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("", encoding="utf-8")
    _render(tmp_path)
    assert "firekeep" in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_unrender_removes_only_our_entry(desktop_home, tmp_path):
    _render(tmp_path)
    path = config_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["other"] = {"command": "npx", "args": []}
    data["theme"] = "dark"
    path.write_text(json.dumps(data), encoding="utf-8")

    get_adapter("claude-desktop").unrender()
    after = json.loads(path.read_text(encoding="utf-8"))
    assert "firekeep" not in after["mcpServers"]
    assert after["mcpServers"]["other"] == {"command": "npx", "args": []}
    assert after["theme"] == "dark"
    assert path.exists()  # the file is Claude Desktop's, never deleted


def test_unrender_missing_file_is_noop(desktop_home):
    get_adapter("claude-desktop").unrender()  # nothing to do, nothing raised
    assert not config_path().exists()


def test_unrender_corrupt_file_warns_and_preserves(desktop_home, capsys):
    path = config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")
    get_adapter("claude-desktop").unrender()
    assert path.read_text(encoding="utf-8") == "{broken"
    assert "could not parse" in capsys.readouterr().err


def test_unrender_without_our_entry_leaves_file_untouched(desktop_home):
    path = config_path()
    path.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {"other": {"command": "x", "args": []}}})
    path.write_text(original, encoding="utf-8")
    get_adapter("claude-desktop").unrender()
    assert path.read_text(encoding="utf-8") == original


def test_app_present_follows_config_dir(desktop_home):
    assert app_present() is False
    config_path().parent.mkdir(parents=True)
    assert app_present() is True


def test_mcp_entry_is_current(desktop_home, tmp_path):
    venv_bin = _render(tmp_path)
    text = config_path().read_text(encoding="utf-8")
    assert mcp_entry_is_current(text, venv_bin)
    assert not mcp_entry_is_current(text, tmp_path / "other" / "bin")
    assert not mcp_entry_is_current("{broken", venv_bin)
    assert not mcp_entry_is_current(json.dumps({"mcpServers": {}}), venv_bin)


def test_selected_runtimes_gate():
    from firekeep_client import cli
    assert cli._selected_runtimes("all") == ["claude", "codex", "kiro", "opencode"]
    assert cli._selected_runtimes("all", include_claude_desktop=True) == [
        "claude", "codex", "kiro", "opencode", "claude-desktop"]
    assert cli._selected_runtimes("all", include_claude_desktop=True, include_generic=True) == [
        "claude", "codex", "kiro", "opencode", "claude-desktop", "generic"]
    assert cli._selected_runtimes("claude-desktop") == ["claude-desktop"]
