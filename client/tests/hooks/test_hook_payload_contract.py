"""The payload shapes Claude Code ACTUALLY sends, per hook event.

Every other test in this tree composes its own payload. That is a real limit, and
it is precisely how the PostToolUse/PostToolUseFailure split stayed invisible: a
fixture can only ever confirm what its author believed the harness sends, so
feeding `post_tool` a hand-written `{"exitCode": 1}` "proved" it saw failures when
the live harness was dispatching them to a different event entirely. It took a
sentinel — a marker written by a non-Claude-Code shell, a real `exit 1`, and a
read-back — to see it.

The shapes below are transcribed from the harness's own validators, read out of
the Claude Code 2.1.270 bundle: the zod schemas that build each hook payload.

    PostToolUse:        {hook_event_name, tool_name, tool_input,
                         tool_response, tool_use_id, duration_ms?}
    PostToolUseFailure: {hook_event_name, tool_name, tool_input,
                         tool_use_id, error, is_interrupt?, duration_ms?}

The difference that matters: a failure has NO `tool_response` and carries `error`
plus `is_interrupt`. The cross-tolerance tests at the bottom are the real guard —
if a future harness version routes an event differently, neither core may explode
on the other's shape.
"""
from __future__ import annotations

from firekeep_client.hooks import post_tool, post_tool_failure

# --- the two shapes, verbatim from the harness's schemas ---------------------

POST_TOOL_USE_BASH = {
    "hook_event_name": "PostToolUse",
    "session_id": "s-contract",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q"},
    "tool_response": {"stdout": "3 passed", "stderr": "", "interrupted": False,
                      "exitCode": 0},
    "tool_use_id": "toolu_01A",
    "duration_ms": 1200,
}

POST_TOOL_FAILURE_BASH = {
    "hook_event_name": "PostToolUseFailure",
    "session_id": "s-contract",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q"},
    "tool_use_id": "toolu_01B",
    "error": "Exit code 1",
    "is_interrupt": False,
    "duration_ms": 900,
}

POST_TOOL_FAILURE_EDIT = {
    "hook_event_name": "PostToolUseFailure",
    "session_id": "s-contract",
    "tool_name": "Edit",
    "tool_input": {"file_path": "src/foo.py", "old_string": "a", "new_string": "b"},
    "tool_use_id": "toolu_01C",
    "error": "String to replace not found in file",
}

POST_TOOL_FAILURE_INTERRUPTED = {
    "hook_event_name": "PostToolUseFailure",
    "session_id": "s-contract",
    "tool_name": "Bash",
    "tool_input": {"command": "sleep 100"},
    "tool_use_id": "toolu_01D",
    "error": "Interrupted by user",
    "is_interrupt": True,
}


def _quiet(monkeypatch):
    """Silence the network and the action bookkeeping — this file is about payload
    shape, not reconciliation."""
    from firekeep_client import state, transport
    monkeypatch.setattr(transport, "post_json", lambda *a, **k: {})
    monkeypatch.setattr(state, "pop_action", lambda *a, **k: None)


class TestTheShapesDifferInTheWayThatMatters:
    def test_a_success_payload_carries_tool_response(self):
        assert "tool_response" in POST_TOOL_USE_BASH

    def test_a_failure_payload_carries_error_and_no_tool_response(self):
        assert "error" in POST_TOOL_FAILURE_BASH
        assert "tool_response" not in POST_TOOL_FAILURE_BASH, (
            "if a failure ever carried tool_response, the two events would be "
            "interchangeable and this whole split would not matter")

    def test_a_failure_marks_interrupts_distinguishably(self):
        assert POST_TOOL_FAILURE_INTERRUPTED["is_interrupt"] is True


class TestEachCoreAcceptsItsOwnEvent:
    def test_post_tool_handles_a_real_success_payload(self, client_env, monkeypatch):
        _quiet(monkeypatch)
        assert post_tool.run(POST_TOOL_USE_BASH) == 0

    def test_post_tool_failure_handles_a_real_failure_payload(self, client_env):
        assert post_tool_failure.run(POST_TOOL_FAILURE_BASH) == 0

    def test_the_failure_core_counts_it(self, client_env):
        from firekeep_client import escalation
        post_tool_failure.run(POST_TOOL_FAILURE_BASH)
        assert escalation.read_state("s-contract")["fail_streak"] == 1

    def test_the_failure_core_names_the_edited_file(self, client_env):
        from firekeep_client import escalation
        post_tool_failure.run(POST_TOOL_FAILURE_EDIT)
        assert escalation.read_state("s-contract")["edits"] == {"src/foo.py": 1}

    def test_an_interrupted_payload_is_not_counted(self, client_env):
        from firekeep_client import escalation
        post_tool_failure.run(POST_TOOL_FAILURE_INTERRUPTED)
        assert escalation.read_state("s-contract")["fail_streak"] == 0


class TestCrossTolerance:
    """If the harness ever routes an event differently — or a runtime packs extra
    fields — neither core may explode. Both are on the user's critical path."""

    def test_post_tool_survives_a_failure_payload(self, client_env, monkeypatch):
        _quiet(monkeypatch)
        assert post_tool.run(POST_TOOL_FAILURE_BASH) == 0

    def test_post_tool_failure_survives_a_success_payload(self, client_env):
        assert post_tool_failure.run(POST_TOOL_USE_BASH) == 0

    def test_both_cores_survive_an_unknown_event_name(self, client_env, monkeypatch):
        _quiet(monkeypatch)
        unknown = dict(POST_TOOL_USE_BASH, hook_event_name="PostToolUseBatch")
        assert post_tool.run(unknown) == 0
        assert post_tool_failure.run(unknown) == 0
