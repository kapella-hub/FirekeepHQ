"""Instruction-layer rendering: the decision-board trigger must actually reach the
runtimes' instruction surfaces — a tool description alone never triggers proactive
use (the tool sat unadvertised everywhere but one project's CLAUDE.md until
2026-07-14). Claude gets a marker-delimited block upserted into the user's global
~/.claude/CLAUDE.md; kiro gets a firekeep-owned steering file."""
from __future__ import annotations

import pytest

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import (
    DECISION_INSTRUCTIONS,
    INSTRUCTIONS_BEGIN,
    INSTRUCTIONS_END,
    strip_marked_block,
    upsert_marked_block,
)
from firekeep_client.adapters.kiro import STEERING_MARKER


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


# --- pure block helpers -------------------------------------------------------

def test_decision_instructions_mandate_waiting_for_the_board():
    """Field report (2026-07-18): agents opened a board, got `pending`, and wandered
    off — the human's answers were never collected, which reads as 'the board does
    not work'. The contract is WAIT: loop decision_board_check until answered."""
    assert "Wait for the board" in DECISION_INSTRUCTIONS
    assert "keep calling `decision_board_check(board_id)`" in DECISION_INSTRUCTIONS
    assert "board_url" in DECISION_INSTRUCTIONS
    # The old wander-off guidance must be gone.
    assert "keep working on whatever isn't blocked" not in DECISION_INSTRUCTIONS


def test_upsert_is_idempotent_and_preserves_user_content():
    user = "# My own notes\n\nprecious user content\n"
    once = upsert_marked_block(user, DECISION_INSTRUCTIONS)
    twice = upsert_marked_block(once, DECISION_INSTRUCTIONS)
    assert once == twice
    assert once.startswith("# My own notes")
    assert "precious user content" in once
    assert "decision_board" in once


def test_upsert_replaces_a_stale_block_in_place():
    stale = f"before\n\n{INSTRUCTIONS_BEGIN}\nOLD CONTENT\n{INSTRUCTIONS_END}\nafter\n"
    result = upsert_marked_block(stale, DECISION_INSTRUCTIONS)
    assert "OLD CONTENT" not in result
    assert "decision_board" in result
    assert result.startswith("before")
    assert result.rstrip().endswith("after")


def test_strip_removes_only_the_block():
    text = upsert_marked_block("mine\n", DECISION_INSTRUCTIONS)
    stripped = strip_marked_block(text)
    assert "decision_board" not in stripped
    assert "mine" in stripped
    assert strip_marked_block("no block") == "no block"


# --- claude: block inside the user's global CLAUDE.md --------------------------

def _claude_md(home):
    return home / ".claude" / "CLAUDE.md"


def test_claude_render_upserts_block_and_keeps_user_text(fake_home, tmp_path):
    md = _claude_md(fake_home)
    md.parent.mkdir(parents=True)
    md.write_text("# my global rules\n\nnever delete me\n", encoding="utf-8")

    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "Scripts")

    text = md.read_text(encoding="utf-8")
    assert "never delete me" in text
    assert "decision_board" in text
    assert INSTRUCTIONS_BEGIN in text and INSTRUCTIONS_END in text


def test_claude_render_creates_the_file_when_absent(fake_home, tmp_path):
    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "Scripts")
    assert "decision_board" in _claude_md(fake_home).read_text(encoding="utf-8")


def test_claude_unrender_strips_block_but_not_user_text(fake_home, tmp_path):
    md = _claude_md(fake_home)
    md.parent.mkdir(parents=True)
    md.write_text("keep me\n", encoding="utf-8")
    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    adapter.unrender()

    text = md.read_text(encoding="utf-8")
    assert "keep me" in text
    assert "decision_board" not in text


# --- kiro: firekeep-owned steering file -------------------------------------------

def _steering(home):
    return home / ".kiro" / "steering" / "firekeep-instructions.md"


def test_kiro_render_writes_marked_steering_doc(fake_home, tmp_path):
    get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
    text = _steering(fake_home).read_text(encoding="utf-8")
    assert STEERING_MARKER in text
    assert "decision_board" in text


def test_kiro_unrender_removes_only_our_steering_doc(fake_home, tmp_path):
    adapter = get_adapter("kiro")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    assert _steering(fake_home).exists()
    adapter.unrender()
    assert not _steering(fake_home).exists()

    # A hand-written steering doc without our marker survives unrender.
    doc = _steering(fake_home)
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("my own steering\n", encoding="utf-8")
    adapter.unrender()
    assert doc.exists() and doc.read_text(encoding="utf-8") == "my own steering\n"


def test_kiro_render_never_touches_a_preexisting_hand_written_firekeep_md(fake_home, tmp_path):
    """Field reality (2026-07-14): pre-kit machines carry a HAND-WRITTEN
    ~/.kiro/steering/firekeep.md. The kit renders under its own distinct
    filename and must leave that file byte-identical."""
    legacy = fake_home / ".kiro" / "steering" / "firekeep.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("---\nname: firekeep-steering\n---\nhand-written rules\n", encoding="utf-8")

    get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")

    assert legacy.read_text(encoding="utf-8") == "---\nname: firekeep-steering\n---\nhand-written rules\n"
    assert _steering(fake_home).exists()  # ours landed beside it


# --- opencode: block inside the user's global AGENTS.md -------------------------

def _agents_md(home):
    return home / ".config" / "opencode" / "AGENTS.md"


def test_opencode_render_upserts_block_into_agents_md(fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    md = _agents_md(fake_home)
    md.parent.mkdir(parents=True)
    md.write_text("# my opencode rules\n\nnever delete me\n", encoding="utf-8")

    get_adapter("opencode").render(venv_bin=tmp_path / "venv" / "Scripts")

    text = md.read_text(encoding="utf-8")
    assert "never delete me" in text
    assert "decision_board" in text
    assert "corpus_ingest" in text and "skill_create" in text
    assert INSTRUCTIONS_BEGIN in text and INSTRUCTIONS_END in text


def test_opencode_render_creates_agents_md_when_absent(fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    get_adapter("opencode").render(venv_bin=tmp_path / "venv" / "Scripts")
    assert "decision_board" in _agents_md(fake_home).read_text(encoding="utf-8")


def test_opencode_unrender_strips_block_but_not_user_text(fake_home, tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    md = _agents_md(fake_home)
    md.parent.mkdir(parents=True)
    md.write_text("keep me\n", encoding="utf-8")
    adapter = get_adapter("opencode")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    adapter.unrender()

    text = md.read_text(encoding="utf-8")
    assert "keep me" in text
    assert "decision_board" not in text


def test_opencode_plugin_render_failure_does_not_skip_instruction_block(
        fake_home, tmp_path, monkeypatch):
    """Same independence contract the claude adapter earned via regression: the
    plugin bridge and the instruction block are separate firekeep-owned artifacts —
    one failing must not skip the other."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    adapter = get_adapter("opencode")

    def boom(venv_bin, pin):
        raise RuntimeError("plugin render blew up")

    monkeypatch.setattr(adapter, "_render_plugin", boom)
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")  # must NOT raise

    assert "decision_board" in _agents_md(fake_home).read_text(encoding="utf-8")


# --- knowledge-ingest instruction (client-side ingest flow) -------------------

def test_claude_render_includes_knowledge_ingest_flow(fake_home, tmp_path):
    """The firekeep-owned block must carry the client-side knowledge-ingest guidance
    (corpus_ingest + skill_create per procedure) so agents ingest docs without
    depending on a server generation model."""
    get_adapter("claude").render(venv_bin=tmp_path / "venv" / "Scripts")
    text = _claude_md(fake_home).read_text(encoding="utf-8")
    assert "corpus_ingest" in text
    assert "skill_create" in text
    assert "decision_board" in text  # both sections present in one block


def test_kiro_render_includes_knowledge_ingest_flow(fake_home, tmp_path):
    get_adapter("kiro").render(venv_bin=tmp_path / "venv" / "Scripts")
    text = _steering(fake_home).read_text(encoding="utf-8")
    assert "corpus_ingest" in text and "skill_create" in text


def test_unrender_strips_the_combined_block(fake_home, tmp_path):
    """unrender must remove BOTH sections cleanly (single marked block)."""
    md = _claude_md(fake_home)
    md.parent.mkdir(parents=True)
    md.write_text("keep\n", encoding="utf-8")
    adapter = get_adapter("claude")
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")
    adapter.unrender()
    text = md.read_text(encoding="utf-8")
    assert "keep" in text
    assert "corpus_ingest" not in text and "decision_board" not in text


def test_command_render_failure_does_not_skip_instruction_block(fake_home, tmp_path, monkeypatch):
    """ROOT CAUSE of decision_board never triggering (2026-07): claude render()
    called the UNGUARDED _render_command right before _render_instructions, so a
    _render_command failure propagated and skipped the instruction block (and the
    install). The instruction block must render regardless of a sibling step's
    failure — they are independent firekeep-owned artifacts."""
    adapter = get_adapter("claude")

    def boom(venv_bin):
        raise RuntimeError("command render blew up")

    monkeypatch.setattr(adapter, "_render_command", boom)
    adapter.render(venv_bin=tmp_path / "venv" / "Scripts")  # must NOT raise

    text = _claude_md(fake_home).read_text(encoding="utf-8")
    assert "decision_board" in text  # instruction block landed despite the failure
