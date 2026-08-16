"""`firekeep install --runtime generic --agents-md <path>`.

The ORDERING is the load-bearing property: `[generic] agents_md` must be
persisted INSIDE _configure, before the render loop, because the loop builds
each adapter from `get_adapter(name)` and offers no argument channel. Persist
late and the first run renders print-only and silently drops the flag.

Fixture shape mirrors tests/test_cli_install.py (same _RecordingAdapter +
install_env), so the four's invariants and these share one arrangement.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from firekeep_client import cli, resolver


class _RecordingAdapter:
    """Records render() per runtime name, exactly like test_cli_install.py's."""

    def __init__(self, name, log):
        self.name = name
        self.log = log

    def render(self, *, venv_bin):
        self.log.append(self.name)

    def unrender(self):
        pass


@pytest.fixture
def install_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    monkeypatch.setenv("FIREKEEP_CONFIG", str(home / "config"))
    monkeypatch.setattr("firekeep_client.state._private", lambda p: None)
    monkeypatch.setattr(cli, "_kit_version", lambda kit: "1.2.3")

    def fake_run(cmd, **kw):
        if "-m" in cmd and "venv" in cmd:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "_run", fake_run)
    rendered: list[str] = []
    monkeypatch.setattr(cli, "get_adapter", lambda name: _RecordingAdapter(name, rendered))
    monkeypatch.setattr(cli.pathenv, "ensure_on_path", lambda home, venv_bin, **kw: [])
    return home, rendered


def test_install_generic_persists_agents_md_and_renders_generic(install_env, tmp_path):
    home, rendered = install_env
    target = tmp_path / "cursor" / "AGENTS.md"
    rc = cli.main(["install", "--runtime", "generic", "--agents-md", str(target)])
    assert rc == 0
    assert resolver.generic_agents_md() == target.resolve()
    assert rendered == ["generic"]


def test_the_persist_happens_before_the_render_loop(install_env, tmp_path, monkeypatch):
    """The ordering itself, not just the end state: get_adapter must already be
    able to read the persisted path when the loop builds the adapter."""
    home, rendered = install_env
    target = tmp_path / "AGENTS.md"
    seen: list = []

    def recording_get_adapter(name):
        seen.append(resolver.generic_agents_md())
        return _RecordingAdapter(name, rendered)

    monkeypatch.setattr(cli, "get_adapter", recording_get_adapter)
    assert cli.main(["install", "--runtime", "generic", "--agents-md", str(target)]) == 0
    assert seen == [target.resolve()]


def test_generic_config_survives_the_install_that_wrote_it(install_env, tmp_path):
    """The section is written through the same ConfigParser round trip the rest
    of _configure uses — [identity] and [server] must still be there."""
    home, rendered = install_env
    target = tmp_path / "AGENTS.md"
    cli.main(["install", "--runtime", "generic", "--agents-md", str(target),
              "--agent-id", "tester", "--host", "198.51.100.7"])
    cfg = configparser.ConfigParser()
    cfg.read(home / "config")
    assert cfg["generic"]["agents_md"] == str(target.resolve())
    assert cfg["identity"]["agent_id"] == "tester"
    assert cfg["server"]["host"] == "198.51.100.7"


def test_install_agents_md_without_generic_runtime_errors(install_env, tmp_path, capsys):
    """argparse cannot express "only with --runtime generic" — a manual check
    must, and it must refuse rather than silently ignore the flag."""
    home, rendered = install_env
    rc = cli.main(["install", "--runtime", "claude", "--agents-md", str(tmp_path / "A.md")])
    assert rc != 0
    assert "--agents-md" in capsys.readouterr().err
    assert rendered == []  # refused before any render


def test_install_all_without_generic_config_still_renders_exactly_four(install_env):
    home, rendered = install_env
    assert cli.main(["install", "--runtime", "all"]) == 0
    assert rendered == ["claude", "codex", "kiro", "opencode"]


def test_install_all_includes_generic_once_configured(install_env, tmp_path):
    """The update path: a later `firekeep install` (runtime defaulted to all)
    re-renders the generic block, keeping it current."""
    home, rendered = install_env
    target = tmp_path / "AGENTS.md"
    assert cli.main(["install", "--runtime", "generic", "--agents-md", str(target)]) == 0
    rendered.clear()
    assert cli.main(["install", "--runtime", "all"]) == 0
    assert rendered == ["claude", "codex", "kiro", "opencode", "generic"]


def test_generic_runtime_alone_does_not_persist_anything(install_env):
    """--runtime generic with no --agents-md is a one-shot print: no [generic]
    section, so generic does NOT join the "all" fan-out afterwards."""
    home, rendered = install_env
    assert cli.main(["install", "--runtime", "generic"]) == 0
    assert resolver.generic_agents_md() is None
    assert rendered == ["generic"]


def test_runtime_choices_accept_generic():
    from firekeep_client.cli import _build_parser

    assert _build_parser().parse_args(["install", "--runtime", "generic"]).runtime == "generic"


def test_gateway_runtime_help_mentions_generic():
    """The rendered snippet tells users to run `gateway --runtime generic`; the
    help text listing only four would contradict the thing we just printed."""
    from firekeep_client.cli import _build_parser

    parser = _build_parser()
    action = next(
        a for a in parser._subparsers._group_actions[0].choices["gateway"]._actions
        if "--runtime" in a.option_strings
    )
    assert "generic" in action.help
