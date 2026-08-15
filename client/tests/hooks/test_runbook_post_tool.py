"""Enforced Runbooks Phase B — post_tool carries the REAL Bash exit status.

Spec ("Wire contract"): ActionAfterRequest gains optional `exit_status`; the
client sends the real exit code when the harness provides it, and an absent or
unparseable status is None — NEVER coerced to 0, because on the server
`exit_status == 0` is the ONLY thing that commits pending command evidence
("Allow is not success": a permitted-but-failed backup must not unlock the
deploy).
"""
from __future__ import annotations

import pytest


def _run_bash(monkeypatch, tool_response, posted):
    from firekeep_client import state, transport
    from firekeep_client.hooks import post_tool, runbooks

    # Pushed WITH the command hash (review 2026-08-15): the stack pairs exit
    # statuses to the command that ran, so parallel Bash calls cannot
    # cross-attribute enforcement evidence.
    state.push_action("s", "act-cmd",
                      command_hash=runbooks.local_command_hash("x"))
    monkeypatch.setattr(transport, "post_json",
                        lambda url, body, **k: posted.update(body) or {"ok": True})
    return post_tool.run({"tool_name": "Bash", "tool_input": {"command": "x"},
                          "tool_response": tool_response, "session_id": "s"})


class TestExitStatusExtraction:
    def test_real_zero_exit_code_commits_success(self, client_env, monkeypatch):
        posted = {}
        assert _run_bash(monkeypatch, {"exitCode": 0, "stdout": "ok"}, posted) == 0
        assert posted["exit_status"] == 0
        assert posted["outcome"]["success"] is True

    def test_nonzero_exit_code_is_not_success(self, client_env, monkeypatch):
        """interrupted=False said 'success' before; the real code outranks it."""
        posted = {}
        assert _run_bash(monkeypatch,
                         {"exitCode": 3, "interrupted": False}, posted) == 0
        assert posted["exit_status"] == 3
        assert posted["outcome"]["success"] is False

    def test_snake_case_exit_code_key_accepted(self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exit_code": 1}, posted)
        assert posted["exit_status"] == 1

    def test_absent_exit_code_is_explicit_null_never_zero(self, client_env,
                                                          monkeypatch):
        """THE coercion pin: no code from the harness -> exit_status is None in
        the body (json null), even though success fell back to True — the
        server must see 'unknown', not a fabricated 0."""
        posted = {}
        assert _run_bash(monkeypatch,
                         {"stdout": "ok", "interrupted": False}, posted) == 0
        assert "exit_status" in posted
        assert posted["exit_status"] is None
        assert posted["outcome"]["success"] is True  # old heuristic, unchanged

    @pytest.mark.parametrize("value", [
        "weird", "", "12abc", True, False, [0], {"code": 0}, None, 1.5,
    ])
    def test_unparseable_exit_code_is_none(self, client_env, monkeypatch, value):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": value, "interrupted": False}, posted)
        assert posted["exit_status"] is None

    def test_string_digits_are_parsed(self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": " 2 "}, posted)
        assert posted["exit_status"] == 2
        assert posted["outcome"]["success"] is False

    def test_integral_float_is_parsed(self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": 0.0}, posted)
        assert posted["exit_status"] == 0
        assert posted["outcome"]["success"] is True

    def test_first_present_key_decides_no_fallthrough(self, client_env, monkeypatch):
        """An unparseable value under the first present key answers None — it
        does not fall through to a later key's guess."""
        posted = {}
        _run_bash(monkeypatch, {"exit_code": "junk", "exitCode": 7}, posted)
        assert posted["exit_status"] is None

    def test_interrupted_with_real_code_keeps_code(self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": 130, "interrupted": True,
                                "stderr": "^C"}, posted)
        assert posted["exit_status"] == 130
        assert posted["outcome"]["success"] is False


class TestBodyShape:
    def test_edit_reconcile_body_has_no_exit_status(self, client_env, monkeypatch,
                                                    tmp_path):
        """The edit path keeps its exact old wire shape (round-1 invariant:
        existing endpoint shapes untouched)."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import post_tool

        f = tmp_path / "x.py"
        f.write_text("new\n", encoding="utf-8")
        state.push_action("s", "act-edit")
        state.write_prestate("act-edit", "oldsha")
        posted = {}
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: posted.update(body) or {"ok": True})
        assert post_tool.run({"tool_name": "Edit",
                              "tool_input": {"file_path": str(f)},
                              "tool_response": {"success": True},
                              "session_id": "s"}) == 0
        assert "exit_status" not in posted
        assert posted["outcome"]["actual_changes"] == [str(f)]

    def test_bash_body_carries_action_id_outcome_and_exit_status(
            self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": 0}, posted)
        assert set(posted.keys()) == {"action_id", "outcome", "exit_status"}
        assert posted["action_id"] == "act-cmd"

    def test_stderr_still_recorded_as_deviation_notes(self, client_env, monkeypatch):
        posted = {}
        _run_bash(monkeypatch, {"exitCode": 1, "stderr": "boom"}, posted)
        assert posted["outcome"]["deviation_notes"] == "boom"
