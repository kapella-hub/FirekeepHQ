"""Doctor's generic instruction row.

Two failures this guards, both of which would ship as permanent noise:

  P2 — a correctly-rendered generic block compared against the FOUR's hash reads
       "edited/stale" forever. The expected hash must be per-runtime.
  P8 — the four's presence gate ("no trace on disk -> stay silent") is right for
       a runtime the user never installed, and WRONG for generic: `[generic]` in
       the config IS the user saying they installed it, so a missing target is a
       broken state to report, not an absence to hide.

And the four-runtime user, who has no [generic] section, must get no generic row
at all.
"""
from __future__ import annotations

import pytest
from firekeep_client import cli, resolver
from firekeep_client.adapters import base
from firekeep_client.adapters.generic import GenericAdapter


@pytest.fixture
def generic_home(tmp_path, monkeypatch):
    """An isolated config with [generic] pointed at a real file."""
    cfg = tmp_path / "config"
    cfg.write_text("[identity]\nagent_id = a\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    return tmp_path


def _row(runtime="generic"):
    return cli._check_runtime_instructions(runtime)


def test_doctor_generic_block_reports_ok_not_edited(generic_home, tmp_path):
    target = generic_home / "AGENTS.md"
    resolver.set_generic_agents_md(target)
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")

    name, status, detail = _row()
    assert (name, status) == ("generic-instructions", "ok"), detail
    assert base.RENDERED_GENERIC_INSTRUCTIONS_HASH in detail
    assert str(target) in detail


def test_doctor_generic_configured_but_missing_target_reports_broken(generic_home):
    target = generic_home / "gone" / "AGENTS.md"
    resolver.set_generic_agents_md(target)

    row = _row()
    assert row is not None, "a configured-but-broken generic target must not be silent"
    name, status, detail = row
    assert name == "generic-instructions"
    assert status in ("warn", "fail")
    assert str(target) in detail
    assert "--runtime generic" in detail


def test_doctor_generic_target_present_but_block_absent_reports(generic_home):
    target = generic_home / "AGENTS.md"
    target.write_text("# just my own rules\n", encoding="utf-8")
    resolver.set_generic_agents_md(target)

    name, status, detail = _row()
    assert status == "warn"
    assert "absent" in detail


def test_doctor_generic_hand_edited_block_is_named_edited(generic_home, tmp_path):
    target = generic_home / "AGENTS.md"
    resolver.set_generic_agents_md(target)
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    target.write_text(
        target.read_text(encoding="utf-8").replace("memory_recall", "memory_recal", 1),
        encoding="utf-8",
    )
    name, status, detail = _row()
    assert status == "warn"
    assert "edited" in detail


def test_doctor_four_runtime_user_gets_no_generic_row(generic_home):
    """No [generic] section -> nothing to check, no row, no noise."""
    assert resolver.generic_agents_md() is None
    assert _row() is None
    assert not [r for r in cli._check_instructions() if r[0].startswith("generic")]


def test_generic_is_in_the_instruction_runtimes(generic_home, tmp_path):
    """The row has to be reachable from _check_instructions, not just callable."""
    target = generic_home / "AGENTS.md"
    resolver.set_generic_agents_md(target)
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    rows = {r[0]: r for r in cli._check_instructions()}
    assert "generic-instructions" in rows
    assert rows["generic-instructions"][1] == "ok"


def test_the_four_still_compare_against_the_four_hash(tmp_path, monkeypatch):
    """The per-runtime map must not have moved the four onto another hash."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    from firekeep_client.adapters import get_adapter

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "Scripts")
    name, status, detail = _row("claude")
    assert (name, status) == ("claude-instructions", "ok")
    assert base.RENDERED_INSTRUCTIONS_HASH in detail


def test_doctor_hints_at_generic_when_unconfigured(generic_home, capsys):
    """Discovery: a user on Cursor has no way to learn the tier exists unless we
    say so. One dim line, and only when [generic] is absent."""
    assert cli._generic_hint() is not None
    assert "--runtime generic" in cli._generic_hint()


def test_no_generic_hint_once_configured(generic_home):
    resolver.set_generic_agents_md(generic_home / "AGENTS.md")
    assert cli._generic_hint() is None
