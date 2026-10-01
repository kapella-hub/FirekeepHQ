"""PostToolUseFailure core — the event a FAILED tool call actually arrives on.

Why this file exists at all: Claude Code dispatches a failed tool call to
`PostToolUseFailure`, NOT `PostToolUse`. Measured 2026-09-13 with a sentinel — a
marker written into the escalation scratch file by a shell that is not a Claude
Code tool, then `exit 1` through the real Bash tool, then read back intact: the
PostToolUse hook never ran. So every failure was invisible to the client, which
had no hook on this event at all (grep for PostToolUseFailure returned nothing).

That mattered twice over. `escalation`'s failure-streak trigger could never fire,
and post_tool's own Bash reconciliation (exit_status, stderr deviation_notes) had
only ever executed for SUCCESSFUL commands.

The payload differs from PostToolUse in the way that matters here: there is no
`tool_response`, and the failure carries `error`, plus `is_interrupt` so a user
cancellation stays distinguishable from work that genuinely failed.
"""
from __future__ import annotations

from firekeep_client import escalation, state
from firekeep_client.hooks import post_tool_failure

SESSION = "s-fail"


def _cfg():
    from firekeep_client import resolver
    return resolver.load_config()


def _state():
    return escalation.read_state(SESSION)


def _failure(tool="Bash", tool_input=None, **extra):
    """A payload shaped like Claude Code's PostToolUseFailure event."""
    payload = {
        "hook_event_name": "PostToolUseFailure",
        "session_id": SESSION,
        "tool_name": tool,
        "tool_input": tool_input if tool_input is not None else {"command": "false"},
        "tool_use_id": "toolu_1",
        "error": "Exit code 1",
    }
    payload.update(extra)
    return payload


class TestCounting:
    """The core's whole job: turn the failure event into counted evidence."""

    def test_a_failed_command_increments_the_streak(self, client_env):
        assert post_tool_failure.run(_failure()) == 0
        assert _state()["fail_streak"] == 1

    def test_two_failures_trip_the_nudge(self, client_env, monkeypatch):
        # Pinned explicitly: the nudge is gated on the main model benefiting from
        # escalation (escalation.should_escalate). Without this the test would read
        # whatever model and endpoint the machine running it happens to have.
        monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-flash[1m]")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:47812")
        post_tool_failure.run(_failure())
        post_tool_failure.run(_failure())
        assert escalation.nudge(_cfg(), {"session_id": SESSION}) != ""

    def test_a_failed_edit_counts_toward_thrash(self, client_env):
        payload = _failure(tool="Edit", tool_input={"file_path": "src/foo.py"})
        for _ in range(escalation.EDIT_THRASH_THRESHOLD):
            post_tool_failure.run(payload)
        assert _state()["edits"] == {"src/foo.py": 3}

    def test_an_interrupt_is_not_a_failure(self, client_env):
        """A cancelled tool call is the user changing their mind, not the work
        going wrong. Counting it would manufacture evidence of being stuck."""
        post_tool_failure.run(_failure(is_interrupt=True))
        assert _state()["fail_streak"] == 0

    def test_the_failure_event_and_the_success_event_share_one_counter(self, client_env):
        """The two hooks must write the SAME session key or the streak resets to
        nothing on every success and the trigger can never reach two."""
        from firekeep_client.hooks import post_tool
        post_tool_failure.run(_failure())
        assert _state()["fail_streak"] == 1
        post_tool.run({"session_id": SESSION, "tool_name": "Bash",
                       "tool_input": {"command": "true"},
                       "tool_response": {"exitCode": 0}})
        assert _state()["fail_streak"] == 0


class TestRobustness:
    def test_missing_tool_input_is_tolerated(self, client_env):
        assert post_tool_failure.run(_failure(tool_input=None)) == 0
        assert _state()["fail_streak"] == 1

    def test_an_empty_payload_still_returns_zero(self, client_env):
        assert post_tool_failure.run({}) == 0

    def test_garbage_never_raises(self, client_env):
        """Every hook core's contract: a malformed payload costs nothing."""
        assert post_tool_failure.run({"tool_input": "not a dict"}) == 0
        assert post_tool_failure.run({"tool_name": None, "tool_input": 7}) == 0

    def test_an_unwritable_counter_still_returns_zero(self, client_env, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(state, "write_scratch", boom)
        assert post_tool_failure.run(_failure()) == 0


class TestRegistration:
    """Implemented-but-never-wired is the failure this whole change was about, so
    the wiring is pinned as hard as the behaviour."""

    def test_the_claude_adapter_registers_the_event(self):
        from firekeep_client.adapters import claude
        events = {row[0] for row in claude.CLAUDE_HOOKS}
        assert "PostToolUseFailure" in events, (
            "the core exists but nothing installs a hook for it — every failed "
            "tool call would stay invisible again")

    def test_it_matches_the_same_tools_the_success_hook_does(self):
        from firekeep_client.adapters import claude
        by_event = {row[0]: row for row in claude.CLAUDE_HOOKS}
        assert by_event["PostToolUseFailure"][2] == by_event["PostToolUse"][2], (
            "the failure hook must watch the same tools as the success hook, or "
            "a tool's successes count while its failures vanish")

    def test_it_points_at_the_post_tool_failure_core(self):
        from firekeep_client.adapters import claude
        by_event = {row[0]: row for row in claude.CLAUDE_HOOKS}
        assert by_event["PostToolUseFailure"][1] == "post_tool_failure"
