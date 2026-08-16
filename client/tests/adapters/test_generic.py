"""The generic adapter: any MCP client the kit ships no bespoke adapter for.

Mold: test_codex.py, the existing no-hooks runtime. What is different here is
that generic owns NO native config file — it prints a paste-in snippet instead —
and its instruction block carries its OWN content hash, because the text it
renders (GENERIC_INSTRUCTIONS) drops the clause claiming a pre-edit hook gate
exists. A generic client has no such gate.
"""
from __future__ import annotations

import json
import sys

import pytest

from firekeep_client.adapters import base, get_adapter
from firekeep_client.adapters.generic import GenericAdapter


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)  # opencode: default ~/.config
    return tmp_path


# --- the printed snippet (the load-bearing half) ------------------------------


def test_generic_render_prints_mcp_snippet(tmp_path, capsys):
    GenericAdapter().render(venv_bin=tmp_path)
    out = capsys.readouterr().out
    blob = json.loads(out[out.index("{"): out.rindex("}") + 1])
    srv = blob["mcpServers"]["firekeep"]
    assert srv["command"] == _exe(tmp_path / "firekeep")
    assert srv["args"] == ["gateway", "--runtime", "generic"]


def test_generic_win32_appends_exe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "win32")
    GenericAdapter().render(venv_bin=tmp_path / "Scripts")
    out = capsys.readouterr().out
    blob = json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert blob["mcpServers"]["firekeep"]["command"].endswith("firekeep.exe")


def test_generic_posix_no_exe(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    GenericAdapter().render(venv_bin=tmp_path / "bin")
    out = capsys.readouterr().out
    blob = json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert blob["mcpServers"]["firekeep"]["command"].endswith("firekeep")


def test_generic_output_states_no_lifecycle_automation(tmp_path, capsys):
    """Honest degradation is the point of the tier: the note must say what a
    generic client does NOT get, not only what it does."""
    GenericAdapter().render(venv_bin=tmp_path)
    out = capsys.readouterr().out.lower()
    assert "no hooks" in out or "does not" in out
    assert "auto-briefing" in out or "briefing" in out


def test_generic_render_writes_nothing_without_agents_md(tmp_path, capsys):
    before = set(tmp_path.rglob("*"))
    GenericAdapter().render(venv_bin=tmp_path)
    assert set(tmp_path.rglob("*")) == before


def test_generic_is_registered_in_get_adapter(fake_home, tmp_path):
    adapter = get_adapter("generic")
    assert adapter.name == "generic"
    assert isinstance(adapter, GenericAdapter)


# --- the optional instruction block -------------------------------------------


def test_generic_agents_md_upserts_hookfree_block_and_keeps_user_text(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My rules\nkeep me\n", encoding="utf-8")
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    text = target.read_text(encoding="utf-8")
    assert "keep me" in text
    assert base.GENERIC_INSTRUCTIONS.splitlines()[0] in text
    assert "gated by hooks" not in text


def test_generic_block_is_stamped_with_the_generic_hash(tmp_path):
    """Guards the doctor false-positive: a block stamped with the FOUR's hash
    would read 'edited' forever on a correctly-rendered generic file."""
    target = tmp_path / "AGENTS.md"
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    text = target.read_text(encoding="utf-8")
    assert base.rendered_block_stamp(text) == base.RENDERED_GENERIC_INSTRUCTIONS_HASH
    assert base.rendered_block_stamp(text) != base.RENDERED_INSTRUCTIONS_HASH
    assert text.splitlines()[0].startswith(base.INSTRUCTIONS_BEGIN_PREFIX)


def test_generic_agents_md_writes_only_that_file(tmp_path):
    """No native config, no hooks, no settings.json — exactly one file appears."""
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    assert [p for p in home.rglob("*")] == [target]
    assert base.HOOK_MARKER not in target.read_text(encoding="utf-8")


def test_generic_agents_md_creates_a_missing_file(tmp_path):
    target = tmp_path / "nested" / "AGENTS.md"
    GenericAdapter(agents_md=target).render(venv_bin=tmp_path / "venv")
    assert base.has_marked_begin(target.read_text(encoding="utf-8"))


def test_generic_rerender_is_byte_identical(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("x\n", encoding="utf-8")
    a = GenericAdapter(agents_md=target)
    a.render(venv_bin=tmp_path / "venv")
    first = target.read_bytes()
    a.render(venv_bin=tmp_path / "venv")
    assert target.read_bytes() == first


def test_generic_unrender_strips_only_our_block(tmp_path):
    target = tmp_path / "AGENTS.md"
    target.write_text("# My rules\nkeep me\n", encoding="utf-8")
    a = GenericAdapter(agents_md=target)
    a.render(venv_bin=tmp_path / "venv")
    a.unrender()
    text = target.read_text(encoding="utf-8")
    assert "keep me" in text
    assert not base.has_marked_begin(text)


def test_generic_unrender_is_noop_when_never_opted_in(tmp_path):
    GenericAdapter().unrender()  # must not raise


def test_generic_unrender_is_noop_when_target_deleted(tmp_path):
    GenericAdapter(agents_md=tmp_path / "gone.md").unrender()  # must not raise


def test_generic_instruction_write_failure_warns_but_keeps_mcp_snippet(tmp_path, capsys, monkeypatch):
    """Codex's best-effort discipline: the printed snippet is the load-bearing
    half, so an unwritable rules file warns to stderr and does not abort."""
    def boom(path, body):
        raise OSError("read-only filesystem")

    monkeypatch.setattr("firekeep_client.adapters.generic.write_text_if_changed", boom)
    GenericAdapter(agents_md=tmp_path / "AGENTS.md").render(venv_bin=tmp_path / "venv")
    captured = capsys.readouterr()
    assert "mcpServers" in captured.out
    assert "WARNING" in captured.err


# --- collision with a file another adapter already owns -----------------------


@pytest.mark.parametrize("runtime", ["claude", "codex", "kiro", "opencode"])
def test_generic_refuses_a_target_managed_by_another_adapter(fake_home, tmp_path, runtime):
    """Two adapters writing one file would share the begin PREFIX, so each
    render would replace the other's block. Refuse it up front."""
    managed = base.rendered_instructions_path(runtime)
    assert managed is not None
    with pytest.raises(ValueError, match="already managed"):
        GenericAdapter(agents_md=managed).render(venv_bin=tmp_path / "venv")


def test_generic_collision_is_rechecked_on_every_render(fake_home, tmp_path, monkeypatch):
    """XDG_CONFIG_HOME can change between installs, moving opencode's AGENTS.md
    onto a path that was free at the previous render — so the check cannot be
    cached at construction time."""
    elsewhere = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(elsewhere))
    target = elsewhere / "opencode" / "AGENTS.md"
    adapter = GenericAdapter(agents_md=target)
    with pytest.raises(ValueError, match="already managed"):
        adapter.render(venv_bin=tmp_path / "venv")
