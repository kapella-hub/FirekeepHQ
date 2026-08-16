"""`firekeep uninstall` — the exact inverse of the install render/PATH/home wiring.

Two properties matter most and both are tested here: it never removes anything
before confirming (a mistyped uninstall must be recoverable), and server teardown
(`docker compose down -v`, which DELETES ALL DATA) is opt-in behind its OWN
confirmation, distinct from the client-removal confirmation.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from firekeep_client import cli


class _RecordingAdapter:
    """Every runtime adapter exposes unrender(); record that it was called."""

    def __init__(self, log):
        self.log = log

    def render(self, *, venv_bin):  # unused by uninstall
        pass

    def unrender(self):
        self.log.append("unrender")


@pytest.fixture
def uninstall_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    (home / "venvs" / "1.2.3").mkdir(parents=True)
    (home / "config").write_text("[identity]\nagent_id = x\n", encoding="utf-8")
    (home / "logs").mkdir()
    monkeypatch.setattr(cli, "_firekeep_home", lambda: home)

    unrendered: list[str] = []
    monkeypatch.setattr(cli, "get_adapter", lambda name: _RecordingAdapter(unrendered))

    path_calls: list[Path] = []

    def fake_remove(h, **kw):
        path_calls.append(h)
        return [f"stripped PATH entry ({h}/shims)"]

    monkeypatch.setattr(cli.pathenv, "remove_from_path", fake_remove)
    # Default to non-interactive (a dev box with a TTY must behave like CI here).
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: False)
    return home, unrendered, path_calls


@pytest.fixture
def with_server(uninstall_env):
    home, unrendered, path_calls = uninstall_env
    server = home / "server"
    server.mkdir(parents=True)
    (server / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return home, unrendered, path_calls, server


# --- confirm-first --------------------------------------------------------------

def test_yes_removes_adapters_path_and_home(uninstall_env, capsys):
    home, unrendered, path_calls = uninstall_env
    assert cli.main(["uninstall", "--yes"]) == 0
    assert len(unrendered) == 4          # claude, codex, kiro, opencode
    assert path_calls == [home]          # PATH stripped exactly once
    assert not home.exists()             # ~/.firekeep deleted
    assert "uninstalled" in capsys.readouterr().out.lower()


def test_aborts_when_not_confirmed(uninstall_env, capsys):
    """No --yes and no TTY -> decline. Nothing may be touched."""
    home, unrendered, path_calls = uninstall_env
    assert cli.main(["uninstall"]) == 0
    assert unrendered == []
    assert path_calls == []
    assert home.exists()
    assert "aborted" in capsys.readouterr().out.lower()


def test_interactive_decline_removes_nothing(uninstall_env, monkeypatch):
    home, unrendered, path_calls = uninstall_env
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    assert cli.main(["uninstall"]) == 0
    assert unrendered == []
    assert home.exists()


def test_interactive_accept_removes(uninstall_env, monkeypatch):
    home, unrendered, path_calls = uninstall_env
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: True)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert cli.main(["uninstall"]) == 0
    assert len(unrendered) == 4
    assert not home.exists()


def test_current_junction_is_removed_node_first(uninstall_env):
    """The `current` alias (a real junction on Windows / symlink on POSIX) must be
    removed as a NODE before the recursive delete, so the tree walk never follows
    the reparse point into the target venv. End-to-end proof: home is fully gone."""
    home, unrendered, path_calls = uninstall_env
    versioned = home / "venvs" / "1.2.3"
    cli._point_current(home, versioned)
    assert (home / "current").exists()
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not home.exists()


# --- server teardown: opt-in and its own confirm --------------------------------

def _docker_ok(monkeypatch):
    runs: list[list[str]] = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda cmd, **kw: runs.append([str(c) for c in cmd])
        or type("R", (), {"returncode": 0})(),
    )
    return runs


def test_server_torn_down_only_with_flag_and_confirm(with_server, monkeypatch):
    home, unrendered, path_calls, server = with_server
    runs = _docker_ok(monkeypatch)
    assert cli.main(["uninstall", "--yes", "--server"]) == 0
    down = [c for c in runs if "down" in c and "-v" in c]
    assert down, f"docker compose down -v must run: {runs}"
    assert str(server / "docker-compose.yml") in down[0]


def test_server_skipped_without_flag(with_server, monkeypatch):
    """`--yes` alone removes the client but must NEVER opt into data loss."""
    home, unrendered, path_calls, server = with_server
    runs = _docker_ok(monkeypatch)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert [c for c in runs if "down" in c] == [], "down -v must not run without --server"
    assert not home.exists()  # client still removed


def test_server_flag_still_requires_its_own_data_loss_confirm(with_server, monkeypatch):
    """Even with --server, `down -v` waits for a confirm distinct from the client
    confirm: proceed=yes, data-loss=no -> the stack survives, the client does not."""
    home, unrendered, path_calls, server = with_server
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: True)
    answers = iter(["y", "n"])  # proceed with client removal, decline data loss
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    runs = _docker_ok(monkeypatch)
    assert cli.main(["uninstall", "--server"]) == 0
    assert [c for c in runs if "down" in c] == [], "down -v needs its own confirm"
    assert len(unrendered) == 4
    assert not home.exists()


def test_server_torn_down_with_flag_and_interactive_confirm(with_server, monkeypatch):
    home, unrendered, path_calls, server = with_server
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: True)
    answers = iter(["y", "y"])  # proceed, then confirm data loss
    monkeypatch.setattr("builtins.input", lambda _p: next(answers))
    runs = _docker_ok(monkeypatch)
    assert cli.main(["uninstall", "--server"]) == 0
    assert [c for c in runs if "down" in c and "-v" in c], runs


def test_docker_absent_names_manual_command_and_continues(with_server, monkeypatch, capsys):
    """No docker -> the server can't be torn down here; name the manual command,
    still remove the client, and report the server as NOT removed (exit 1)."""
    home, unrendered, path_calls, server = with_server
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    rc = cli.main(["uninstall", "--yes", "--server"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "docker not found" in err
    assert "docker compose down -v" in err
    assert len(unrendered) == 4          # client removal still happened
    assert not home.exists()


def test_partial_failure_is_reported_not_raised(uninstall_env, monkeypatch, capsys):
    """One adapter blowing up must not abort the rest, must not raise, and must
    surface as a NOT-removed line with a non-zero exit."""
    home, unrendered, path_calls = uninstall_env

    def half_broken(name):
        if name == "codex":
            raise RuntimeError("boom")
        return _RecordingAdapter(unrendered)

    monkeypatch.setattr(cli, "get_adapter", half_broken)
    rc = cli.main(["uninstall", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "codex adapter unrender" in err
    assert len(unrendered) == 3          # the other three still ran
    assert not home.exists()             # home still deleted
