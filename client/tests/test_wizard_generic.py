"""The wizard's "other MCP client" question — the primary discovery path.

Nothing else in the product tells a Cursor/Windsurf/Zed user that a runtime for
them exists, so the installer asks once, skippably, at the end.

The wizard contract holds: it asks and returns a Plan, it does NOT touch the
filesystem. cli.py owns persisting the answer — and must do so BEFORE the render
loop, since the loop builds the generic adapter from the config.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest
from firekeep_client import cli, resolver, wizard


def _cfg(text=""):
    cfg = configparser.ConfigParser(interpolation=None)
    if text:
        cfg.read_string(text)
    return cfg


def _scripted(answers):
    queue = list(answers)
    seen = []

    def ask(prompt, default=""):
        seen.append((prompt, default))
        answer = queue.pop(0) if queue else ""
        return answer or default

    ask.seen = seen
    return ask


SKELETON = """\
[identity]
agent_id = CHANGEME

[server]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
"""


def test_wizard_returns_the_pasted_rules_path(tmp_path):
    cfg = _cfg(SKELETON)
    target = tmp_path / "cursor-rules.md"
    plan = wizard.prompt_config(
        cfg, ask=_scripted(["Alex", "4", str(target)]), docker=True)
    assert plan.generic_agents_md == str(target)


def test_wizard_skipped_answer_is_none():
    cfg = _cfg(SKELETON)
    plan = wizard.prompt_config(cfg, ask=_scripted(["Alex", "4"]), docker=True)
    assert plan.generic_agents_md is None


def test_wizard_does_not_touch_the_filesystem(tmp_path):
    """The wizard contract: questions only. The named file must not appear."""
    cfg = _cfg(SKELETON)
    target = tmp_path / "AGENTS.md"
    wizard.prompt_config(cfg, ask=_scripted(["Alex", "4", str(target)]), docker=True)
    assert not target.exists()


def test_the_question_is_asked_last_and_names_real_clients():
    cfg = _cfg(SKELETON)
    ask = _scripted(["Alex", "4"])
    wizard.prompt_config(cfg, ask=ask, docker=True)
    prompt = ask.seen[-1][0]
    assert "Cursor" in prompt
    assert ask.seen[-1][1] == ""  # skippable: no default to accept blindly


def test_a_connected_machine_is_asked_too(tmp_path):
    """The already-connected path returns early from the routing question — the
    generic question must still be reached, or a re-run can never opt in.

    Answers by PROMPT rather than by position: this path runs the edit-in-place
    prompts, so a positional script would silently feed the path to one of them."""
    cfg = _cfg(SKELETON.replace("127.0.0.1", "10.0.0.4"))
    target = tmp_path / "AGENTS.md"

    def ask(prompt, default=""):
        return str(target) if "rules/AGENTS.md" in prompt else default

    plan = wizard.prompt_config(cfg, ask=ask, docker=True)
    assert plan.generic_agents_md == str(target)


def test_the_host_flag_path_is_asked_too(tmp_path):
    cfg = _cfg(SKELETON)
    target = tmp_path / "AGENTS.md"
    plan = wizard.prompt_config(
        cfg, ask=_scripted(["Alex", "", str(target)]), host="10.0.0.9")
    assert plan.generic_agents_md == str(target)


# --- cli consumes the answer --------------------------------------------------


class _RecordingAdapter:
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
    monkeypatch.setattr("firekeep_client.wizard.is_interactive", lambda *a, **k: True)
    return home, rendered


def test_install_persists_the_wizard_answer_and_renders_generic(install_env, tmp_path,
                                                                monkeypatch):
    home, rendered = install_env
    target = tmp_path / "AGENTS.md"
    monkeypatch.setattr(
        "firekeep_client.wizard.prompt_config",
        lambda cfg, **k: wizard.Plan(cfg, wizard.EXISTING_SERVER,
                                     generic_agents_md=str(target)),
    )
    assert cli.main(["install"]) == 0
    assert resolver.generic_agents_md() == target.resolve()
    assert rendered == ["claude", "codex", "kiro", "opencode", "generic"]


def test_skipping_the_question_leaves_the_four_untouched(install_env, monkeypatch):
    home, rendered = install_env
    monkeypatch.setattr(
        "firekeep_client.wizard.prompt_config",
        lambda cfg, **k: wizard.Plan(cfg, wizard.EXISTING_SERVER),
    )
    assert cli.main(["install"]) == 0
    assert resolver.generic_agents_md() is None
    assert rendered == ["claude", "codex", "kiro", "opencode"]
