"""The tool-outcome signal (2026-11-09): events are filed under the KEEP
session, failed commands are reported (PostToolUseFailure), ungated commands
are reported at all (/agent/action/observed), and PowerShell is a shell."""
from __future__ import annotations

import pytest


def _agent():
    from firekeep_client import resolver
    return resolver.agent_id(resolver.load_config())


def _start_keep_session(sid="keep-1"):
    from firekeep_client import state
    state.write_session_stash(_agent(), session_id=sid)


def _capture(monkeypatch):
    from firekeep_client import transport
    calls: list[dict] = []

    def fake_post(url, body, **k):
        calls.append({"url": url, "body": body, **k})
        return {"decision": "allow", "action_id": "act-1", "advisories": []}

    monkeypatch.setattr(transport, "post_json", fake_post)
    return calls


class TestKeepSessionId:
    def test_none_without_a_stash(self, client_env):
        from firekeep_client import state
        assert state.keep_session_id(_agent()) is None

    def test_reads_the_stashed_keep_session(self, client_env):
        from firekeep_client import state
        _start_keep_session("632e8617-283")
        assert state.keep_session_id(_agent()) == "632e8617-283"

    def test_refuses_an_id_that_is_not_header_safe(self, client_env):
        from firekeep_client import state
        state.write_session_stash(_agent(), session_id="bad id\nX-Evil: 1")
        assert state.keep_session_id(_agent()) is None


class TestWireSession:
    def test_edit_gate_files_under_the_keep_session(self, client_env, monkeypatch, tmp_path):
        from firekeep_client import state
        from firekeep_client.hooks import _mcp, pre_tool

        _start_keep_session("keep-1")
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {"held": False})
        calls = _capture(monkeypatch)
        f = tmp_path / "x.py"
        f.write_text("old\n")

        assert pre_tool.run({"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                             "session_id": "harness-uuid"}) == 0

        assert calls[0]["body"]["session_id"] == "keep-1"
        # Local pre/post pairing still keys on the runtime's own id.
        assert state.pop_action("harness-uuid") == "act-1"

    def test_falls_back_to_the_runtime_id_without_a_keep_session(
            self, client_env, monkeypatch, tmp_path):
        from firekeep_client.hooks import _mcp, pre_tool

        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {"held": False})
        calls = _capture(monkeypatch)
        f = tmp_path / "x.py"
        f.write_text("old\n")

        pre_tool.run({"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                      "session_id": "harness-uuid"})
        assert calls[0]["body"]["session_id"] == "harness-uuid"


class TestPowerShellIsAShell:
    def test_action_type(self):
        from firekeep_client.hooks import pre_tool
        assert pre_tool._action_type("PowerShell") == "run_command"
        assert pre_tool._action_type("Bash") == "run_command"
        assert pre_tool._action_type("Read") == "other"

    def test_unmatched_command_stays_off_the_network_before_it_runs(
            self, client_env, monkeypatch):
        from firekeep_client.hooks import pre_tool
        calls = _capture(monkeypatch)
        assert pre_tool.run({"tool_name": "PowerShell",
                             "tool_input": {"command": "Get-ChildItem"},
                             "session_id": "s"}) == 0
        assert calls == []


class TestObservedCommands:
    def test_successful_ungated_command_is_reported(self, client_env, monkeypatch):
        from firekeep_client.hooks import post_tool
        _start_keep_session("keep-1")
        calls = _capture(monkeypatch)

        assert post_tool.run({
            "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": "pytest -q tests/ --token=s3cr3t"},
            "tool_response": {"stdout": "ok", "stderr": "", "interrupted": False},
            "session_id": "harness-uuid"}) == 0

        (call,) = calls
        assert call["url"].endswith("/agent/action/observed")
        assert call["timeout"] == post_tool.OBSERVED_TIMEOUT
        assert call["body"] == {
            "session_id": "keep-1", "agent_id": _agent(), "adapter": "shell-hook",
            "action": {"type": "run_command", "target": "pytest"},
            "outcome": {"success": True}, "exit_status": None,
        }
        # The command line never leaves the machine on this path.
        assert "s3cr3t" not in repr(call)

    def test_failed_command_is_reported_with_its_exit_status(self, client_env, monkeypatch):
        from firekeep_client.hooks import post_tool
        _start_keep_session("keep-1")
        calls = _capture(monkeypatch)

        post_tool.run({
            "hook_event_name": "PostToolUseFailure", "tool_name": "PowerShell",
            "tool_input": {"command": "& 'C:\\tools\\git.exe' status"},
            "error": "Exit code 1\ngit: not recognized", "is_interrupt": False,
            "session_id": "harness-uuid"})

        (call,) = calls
        assert call["body"]["outcome"] == {"success": False}
        assert call["body"]["exit_status"] == 1
        assert call["body"]["action"]["target"] == "git.exe"

    def test_not_reported_without_a_keep_session(self, client_env, monkeypatch):
        from firekeep_client.hooks import post_tool
        calls = _capture(monkeypatch)
        post_tool.run({"tool_name": "Bash", "tool_input": {"command": "ls"},
                       "tool_response": {}, "session_id": "harness-uuid"})
        assert calls == []

    def test_interrupted_command_is_not_reported(self, client_env, monkeypatch):
        from firekeep_client.hooks import post_tool
        _start_keep_session()
        calls = _capture(monkeypatch)
        post_tool.run({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                       "tool_input": {"command": "sleep 100"},
                       "error": "aborted", "is_interrupt": True, "session_id": "s"})
        post_tool.run({"tool_name": "Bash", "tool_input": {"command": "sleep 100"},
                       "tool_response": {"interrupted": True}, "session_id": "s"})
        assert calls == []

    def test_ungated_edit_is_not_reported(self, client_env, monkeypatch):
        from firekeep_client.hooks import post_tool
        _start_keep_session()
        calls = _capture(monkeypatch)
        post_tool.run({"tool_name": "Edit", "tool_input": {"file_path": "x.py"},
                       "tool_response": {"success": True}, "session_id": "s"})
        assert calls == []

    @pytest.mark.parametrize("status, paused", [(404, True), (405, True), (500, False)])
    def test_an_old_server_pauses_reporting(self, client_env, monkeypatch, status, paused):
        from firekeep_client import transport
        from firekeep_client.hooks import post_tool
        _start_keep_session()
        attempts = {"n": 0}

        def failing_post(url, body, **k):
            attempts["n"] += 1
            raise transport.TransportError("nope", status=status)

        monkeypatch.setattr(transport, "post_json", failing_post)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                   "tool_response": {}, "session_id": "s"}
        assert post_tool.run(payload) == 0
        assert post_tool.run(payload) == 0
        assert attempts["n"] == (1 if paused else 2)

    def test_an_unreachable_server_pauses_reporting(self, client_env, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import post_tool
        _start_keep_session()
        attempts = {"n": 0}

        def timing_out(url, body, **k):
            attempts["n"] += 1
            raise transport.TransportError("timed out", category="timeout")

        monkeypatch.setattr(transport, "post_json", timing_out)
        payload = {"tool_name": "Bash", "tool_input": {"command": "ls"},
                   "tool_response": {}, "session_id": "s"}
        post_tool.run(payload)
        post_tool.run(payload)
        assert attempts["n"] == 1


class TestFailureReconcile:
    def test_failed_gated_command_reconciles_as_failure(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import post_tool, runbooks
        state.push_action("s", "act-b", command_hash=runbooks.local_command_hash("make deploy"))
        calls = _capture(monkeypatch)

        post_tool.run({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                       "tool_input": {"command": "make deploy"},
                       "error": "Exit code 2\nmake: *** [deploy] Error 1",
                       "session_id": "s"})

        (call,) = calls
        assert call["url"].endswith("/agent/action/after")
        assert call["body"]["action_id"] == "act-b"
        assert call["body"]["outcome"]["success"] is False
        assert call["body"]["exit_status"] == 2
        assert call["body"]["outcome"]["deviation_notes"].startswith("Exit code 2")

    def test_failed_gated_edit_reconciles_as_failure(self, client_env, monkeypatch, tmp_path):
        from firekeep_client import state
        from firekeep_client.hooks import post_tool
        f = tmp_path / "x.py"
        f.write_text("unchanged\n")
        state.push_action("s", "act-e")
        calls = _capture(monkeypatch)

        post_tool.run({"hook_event_name": "PostToolUseFailure", "tool_name": "Edit",
                       "tool_input": {"file_path": str(f)},
                       "error": "String to replace not found in file", "session_id": "s"})

        (call,) = calls
        assert call["body"]["outcome"] == {"success": False, "actual_changes": []}
        assert "exit_status" not in call["body"]


class TestParsers:
    @pytest.mark.parametrize("error, expected", [
        ("Exit code 1\nboom", 1),
        ("Exit code 127", 127),
        ("  Exit code 2: nope", 2),
        ("Exit code 0", None),          # a failure event cannot have succeeded
        ("Command timed out after 120s", None),
        ("something Exit code 3", None),  # only a LEADING status counts
        (None, None),
        (17, None),
    ])
    def test_exit_status_from_error(self, error, expected):
        from firekeep_client.hooks import post_tool
        assert post_tool._exit_status_from_error(error) == expected

    @pytest.mark.parametrize("command, expected", [
        ("pytest -q tests/", "pytest"),
        ("FOO=bar BAZ=1 npm test", "npm"),
        ("sudo /usr/local/bin/deploy.sh --prod", "deploy.sh"),
        ("& \"C:\\Program Files\\Git\\cmd\\git.exe\" status", "git.exe"),
        ("", ""),
        (None, ""),
        ("x" * 500, "x" * 60),
    ])
    def test_program(self, command, expected):
        from firekeep_client.hooks import post_tool
        assert post_tool._program(command) == expected


class TestAdapterAndDispatcher:
    def test_claude_registers_the_failure_event_and_powershell(self):
        from firekeep_client.adapters.claude import CLAUDE_HOOKS
        rows = {event: (core, matcher) for event, core, matcher, _t in CLAUDE_HOOKS}
        assert rows["PostToolUseFailure"][0] == "post_tool"
        for event in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            assert "PowerShell" in rows[event][1]
            assert "Bash" in rows[event][1]

    def test_a_bom_prefixed_payload_is_parsed(self, client_env, monkeypatch):
        import io
        import sys
        from firekeep_client.hooks import __main__ as dispatcher
        monkeypatch.setattr(sys, "stdin", io.StringIO('\ufeff{"tool_name": "Bash"}'))
        assert dispatcher._read_payload("pre_tool") == {"tool_name": "Bash"}
