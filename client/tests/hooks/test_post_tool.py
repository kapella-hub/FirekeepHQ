"""SP1b hook core: post_tool — end-to-end pre->post correlation (same session)."""
from __future__ import annotations


class TestPostTool:
    def test_reconciles_action_pushed_by_pre_tool(self, client_env, monkeypatch, tmp_path):
        from firekeep_client import transport
        from firekeep_client.hooks import _mcp, post_tool, pre_tool

        f = tmp_path / "x.py"
        f.write_text("old\n")

        # pre_tool: lease free, gateway allows with action_id act-9.
        monkeypatch.setattr(_mcp, "call_tool", lambda service, tool, args, **k: {"held": False})
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "act-9", "advisories": []})
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                   "session_id": "s"}
        assert pre_tool.run(payload) == 0

        # the edit happens
        f.write_text("new content\n")

        # post_tool: capture the /after POST.
        posted = {}

        def fake_post(url, body, **k):
            posted["url"] = url
            posted["body"] = body
            return {"ok": True}

        monkeypatch.setattr(transport, "post_json", fake_post)
        post_payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                        "tool_response": {"success": True}, "session_id": "s"}
        assert post_tool.run(post_payload) == 0
        assert posted["url"].endswith("/agent/action/after")
        assert posted["body"]["action_id"] == "act-9"
        assert posted["body"]["outcome"]["success"] is True
        assert posted["body"]["outcome"]["actual_changes"] == [str(f)]

        # second post: nothing left to reconcile.
        posted.clear()
        assert post_tool.run(post_payload) == 0
        assert posted == {}

    def test_bash_failure_recorded_from_interrupted(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import post_tool

        from firekeep_client.hooks import runbooks
        # Hash-paired push (review 2026-08-15): Bash pops match on the command.
        state.push_action("s", "act-b",
                          command_hash=runbooks.local_command_hash("x"))
        posted = {}
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: posted.update(body) or {"ok": True})
        rc = post_tool.run({"tool_name": "Bash", "tool_input": {"command": "x"},
                            "tool_response": {"interrupted": True, "stderr": "boom"},
                            "session_id": "s"})
        assert rc == 0
        assert posted["outcome"]["success"] is False
        assert posted["outcome"]["deviation_notes"] == "boom"
        assert posted["outcome"]["actual_changes"] == []

    def test_no_action_short_circuits(self, client_env, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import post_tool

        called = {"n": 0}
        monkeypatch.setattr(transport, "post_json",
                            lambda *a, **k: called.update(n=called["n"] + 1) or {})
        rc = post_tool.run({"tool_name": "Edit", "tool_input": {},
                            "tool_response": {}, "session_id": "s-none"})
        assert rc == 0
        assert called["n"] == 0  # pop_action -> None, no /after POST


def test_reconcile_deletes_prestate(client_env, monkeypatch, tmp_path):
    """Bash parity: the snapshot is unlinked after reconciliation (no leak)."""
    from firekeep_client import state, transport
    from firekeep_client.hooks import post_tool

    f = tmp_path / "x.py"
    f.write_text("after\n")
    state.write_prestate("act-9", "oldsha")
    state.push_action("s1", "act-9")
    monkeypatch.setattr(transport, "post_json", lambda *a, **k: {})

    rc = post_tool.run({"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                        "tool_response": {"success": True}, "session_id": "s1"})

    assert rc == 0
    assert state.read_prestate("act-9") is None
