"""`firekeep uninstall` has to reach the generic block too.

The orphan hazard is the reason this file exists. The generic block lives in a
file OUTSIDE ~/.firekeep, and the only record of WHICH file is `[generic]
agents_md` INSIDE ~/.firekeep — which uninstall deletes. So if the config cannot
be read, the block is stranded in the user's rules file with no way left to find
it. Uninstall must read that path before it deletes anything, and say out loud
what it could not clean.

Fixture shape mirrors tests/test_cli_uninstall.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from firekeep_client import cli, resolver
from firekeep_client.adapters import base
from firekeep_client.adapters.generic import GenericAdapter


class _RecordingAdapter:
    def __init__(self, name, log):
        self.name = name
        self.log = log

    def render(self, *, venv_bin):
        pass

    def unrender(self):
        self.log.append(self.name)


@pytest.fixture
def uninstall_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    (home / "venvs" / "1.2.3").mkdir(parents=True)
    (home / "config").write_text("[identity]\nagent_id = x\n", encoding="utf-8")
    (home / "logs").mkdir()
    monkeypatch.setattr(cli, "_firekeep_home", lambda: home)
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))

    unrendered: list[str] = []
    monkeypatch.setattr(cli, "get_adapter", lambda name: _RecordingAdapter(name, unrendered))
    monkeypatch.setattr(cli.pathenv, "remove_from_path", lambda h, **kw: [])
    monkeypatch.setattr(cli.wizard, "is_interactive", lambda *a, **k: False)
    return home, unrendered


def test_uninstall_four_runtime_user_unchanged(uninstall_env):
    """No [generic] -> exactly four unrender() calls, the existing invariant."""
    home, unrendered = uninstall_env
    assert cli.main(["uninstall", "--yes"]) == 0
    assert unrendered == ["claude", "codex", "kiro", "opencode"]


def test_uninstall_includes_generic_when_configured(uninstall_env, tmp_path):
    home, unrendered = uninstall_env
    resolver.set_generic_agents_md(tmp_path / "AGENTS.md")
    assert cli.main(["uninstall", "--yes"]) == 0
    assert unrendered == ["claude", "codex", "kiro", "opencode", "generic"]


def test_uninstall_strips_generic_block_from_the_real_file(uninstall_env, tmp_path, monkeypatch):
    """End-to-end through the REAL adapter: the user's own text survives, the
    Firekeep block does not."""
    home, unrendered = uninstall_env
    target = tmp_path / "AGENTS.md"
    target.write_text("# mine\nkeep me\n", encoding="utf-8")
    resolver.set_generic_agents_md(target)
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    assert base.has_marked_begin(target.read_text(encoding="utf-8"))

    # NB: the real registry, imported directly — cli.get_adapter is already
    # stubbed by the fixture, so capturing it here would just re-stub generic.
    from firekeep_client.adapters import get_adapter as real_get_adapter

    monkeypatch.setattr(
        cli, "get_adapter",
        lambda name: (real_get_adapter(name) if name == "generic"
                      else _RecordingAdapter(name, unrendered)),
    )
    assert cli.main(["uninstall", "--yes"]) == 0
    text = target.read_text(encoding="utf-8")
    assert "keep me" in text
    assert not base.has_marked_begin(text)


def test_uninstall_banner_names_generic_only_when_configured(uninstall_env, tmp_path, capsys):
    home, unrendered = uninstall_env
    cli.main(["uninstall"])  # declines (non-interactive, no --yes): banner only
    assert "generic" not in capsys.readouterr().out

    resolver.set_generic_agents_md(tmp_path / "AGENTS.md")
    cli.main(["uninstall"])
    assert "generic" in capsys.readouterr().out


def test_uninstall_reports_generic_when_config_read_fails(uninstall_env, tmp_path, capsys):
    """The orphan case: a [generic] section exists but the config cannot be
    parsed, so we cannot learn the path — and ~/.firekeep is about to be deleted.
    Say so; do not delete silently."""
    home, unrendered = uninstall_env
    (home / "config").write_text(
        "[generic]\nagents_md = /a/AGENTS.md\n[generic]\nagents_md = /b/AGENTS.md\n",
        encoding="utf-8",
    )
    assert resolver.generic_agents_md() is None  # unreadable -> not configured

    rc = cli.main(["uninstall", "--yes"])
    captured = capsys.readouterr()
    report = captured.out + captured.err
    assert "generic" in report.lower()
    assert "by hand" in report.lower() or "manually" in report.lower()
    assert not home.exists()  # the rest of the uninstall still completed
    assert rc == 0


def test_uninstall_names_the_file_when_generic_unrender_fails(uninstall_env, tmp_path, capsys,
                                                              monkeypatch):
    """A failed generic unrender must name the file the user has to clean —
    after the home is gone, nothing else records it."""
    home, unrendered = uninstall_env
    target = tmp_path / "cursor" / "AGENTS.md"
    resolver.set_generic_agents_md(target)

    def half_broken(name):
        if name == "generic":
            raise RuntimeError("boom")
        return _RecordingAdapter(name, unrendered)

    monkeypatch.setattr(cli, "get_adapter", half_broken)
    rc = cli.main(["uninstall", "--yes"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "generic adapter unrender" in err
    assert str(target) in err
    assert len(unrendered) == 4  # the four still ran
    assert not home.exists()
