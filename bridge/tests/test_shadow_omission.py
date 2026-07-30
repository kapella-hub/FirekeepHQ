"""Tests for `assemble_shadow`'s `omitted` parameter (Task 5 fix round 1, C1).

A delta must never let a reader conclude the omitted content does not exist. These
tests assert the opposite of the C1 defect: a section with withheld entries renders
a line SAYING SO, never the "none recorded" denial that collides with genuine
emptiness. Kept in a separate file from test_shadow.py so that file's pinned,
exact-text assertions are never at risk of being edited alongside this fix.
"""
from __future__ import annotations

from app.shadow import assemble_shadow


def _data(**over):
    d = {
        "goal": "g", "status": "active",
        "created_at": "2026-07-30T00:00:00Z", "updated_at": "2026-07-30T00:00:00Z",
        "plan": "", "decisions": [], "files": {}, "progress": [], "scratch": {},
    }
    d.update(over)
    return d


def test_omitted_decisions_never_render_as_no_decisions_recorded():
    out = assemble_shadow(_data(), omitted={"decisions": 2, "progress": 0, "files": 0, "plan": False})
    assert "No decisions recorded" not in out
    assert "2 earlier decision(s) omitted" in out
    assert "ctx_get_shadow()" in out


def test_omitted_progress_never_renders_as_no_progress_logged():
    out = assemble_shadow(_data(), omitted={"decisions": 0, "progress": 1, "files": 0, "plan": False})
    assert "No progress logged" not in out
    assert "1 earlier progress entry(s) omitted" in out
    assert "ctx_get_shadow()" in out


def test_omitted_files_never_render_as_no_files_tracked():
    out = assemble_shadow(_data(), omitted={"decisions": 0, "progress": 0, "files": 3, "plan": False})
    assert "No files tracked" not in out
    assert "3 earlier file(s) omitted" in out
    assert "ctx_get_shadow()" in out


def test_omitted_unchanged_plan_never_renders_as_no_plan_set():
    out = assemble_shadow(_data(), omitted={"decisions": 0, "progress": 0, "files": 0, "plan": True})
    assert "No plan set" not in out
    assert "Plan unchanged" in out
    assert "ctx_get_shadow()" in out


def test_a_delta_with_every_section_omitted_denies_nothing():
    out = assemble_shadow(
        _data(),
        omitted={"decisions": 1, "progress": 1, "files": 1, "plan": True},
    )
    assert "No decisions recorded" not in out
    assert "No progress logged" not in out
    assert "No files tracked" not in out
    assert "No plan set" not in out


def test_full_restore_of_a_genuinely_empty_session_still_shows_the_original_placeholders():
    """Regression guard: the omitted=None default must leave a real full restore of
    an empty session exactly as it always rendered -- this is the same shape as
    test_shadow.py::test_empty_components, kept here so the fix's default-path
    behavior is asserted alongside the new omitted-path behavior."""
    out = assemble_shadow(_data())
    assert "No plan set" in out
    assert "No decisions recorded" in out
    assert "No files tracked" in out
    assert "No progress logged" in out


def test_non_empty_sections_are_unaffected_by_an_omitted_report():
    """omitted only ever substitutes for the EMPTY-section placeholder branch; a
    section that has real content to show must keep showing it regardless of what
    omitted says about some other, empty section."""
    out = assemble_shadow(
        _data(decisions=[{"timestamp": "2026-07-30T10:00:00Z", "content": "chose A"}]),
        omitted={"decisions": 0, "progress": 1, "files": 0, "plan": False},
    )
    assert "chose A" in out
    assert "No decisions recorded" not in out
    assert "earlier decision(s) omitted" not in out


# --- fix round 2, Important #1: PARTIAL omission (kept entries + withheld entries) --
#
# The round-1 fix used `elif`, which only fires when a section is entirely empty. A
# delta that KEPT some entries and withheld others rendered the kept ones and said
# NOTHING about what was withheld -- and this is the *common* shape: a fully-empty
# section only happens when nothing changed at all since the cursor. Four states,
# four renderings (entries x omitted): the dominant case (both yes) must show both.

def test_partially_omitted_decisions_show_both_the_kept_entries_and_the_omission_line():
    out = assemble_shadow(
        _data(decisions=[{"timestamp": "2026-07-30T12:00:00Z", "content": "chose B"}]),
        omitted={"decisions": 3, "progress": 0, "files": 0, "plan": False},
    )
    assert "chose B" in out
    assert "3 earlier decision(s) omitted" in out
    assert "No decisions recorded" not in out


def test_partially_omitted_progress_shows_both_the_kept_entries_and_the_omission_line():
    out = assemble_shadow(
        _data(progress=[{"timestamp": "2026-07-30T12:00:00Z", "content": "did X"}]),
        omitted={"decisions": 0, "progress": 2, "files": 0, "plan": False},
    )
    assert "did X" in out
    assert "2 earlier progress entry(s) omitted" in out
    assert "No progress logged" not in out


def test_partially_omitted_files_show_both_the_kept_entries_and_the_omission_line():
    out = assemble_shadow(
        _data(files={"b.py": {"summary": "new", "last_action": "2026-07-30T12:00:00Z"}}),
        omitted={"decisions": 0, "progress": 0, "files": 1, "plan": False},
    )
    assert "b.py" in out
    assert "1 earlier file(s) omitted" in out
    assert "No files tracked" not in out
