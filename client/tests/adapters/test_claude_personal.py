"""The /personal Claude slash command: rendered by the claude adapter, marker-guarded
so unrender removes only our copy (a user's own personal.md survives)."""
from __future__ import annotations

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.claude import COMMAND_MARKER


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _cmd_path(home):
    return home / ".claude" / "commands" / "personal.md"


def test_render_writes_personal_command(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    get_adapter("claude").render(venv_bin=venv_bin)

    cmd = _cmd_path(fake_home)
    assert cmd.exists()
    text = cmd.read_text(encoding="utf-8")
    assert "personal toggle" in text            # the !-exec toggles via the CLI
    assert "!`" in text                          # bash-exec form (runs at expansion)
    assert "allowed-tools:" in text
    assert COMMAND_MARKER in text                # unrender guard
    # path is forward-slashed for bash (no lone backslashes in the command line)
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("!`"))
    assert "\\" not in exec_line


def test_unrender_removes_our_command(fake_home, tmp_path):
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")
    adapter.render(venv_bin=venv_bin)
    assert _cmd_path(fake_home).exists()

    adapter.unrender()
    assert not _cmd_path(fake_home).exists()


def test_unrender_preserves_a_users_own_personal_command(fake_home):
    # A hand-written personal.md WITHOUT our marker must survive unrender.
    cmd = _cmd_path(fake_home)
    cmd.parent.mkdir(parents=True)
    cmd.write_text("my own /personal command — no firekeep marker here", encoding="utf-8")

    get_adapter("claude").unrender()

    assert cmd.exists()
    assert "my own" in cmd.read_text(encoding="utf-8")
