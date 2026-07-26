"""SP1b hook core: pre_tool — the blocking contract (every exit-code path)."""
from __future__ import annotations


def _lease(monkeypatch, result):
    from firekeep_client.hooks import _mcp
    monkeypatch.setattr(_mcp, "call_tool", lambda service, tool, args, **k: result)


def _gateway(monkeypatch, resp=None, boom=False):
    from firekeep_client import transport

    def fake_post(url, body, **k):
        if boom:
            raise transport.TransportError("cortex down")
        return resp

    monkeypatch.setattr(transport, "post_json", fake_post)


class TestPreTool:
    def test_allow_returns_0_and_records_prestate(self, client_env, monkeypatch, tmp_path):
        from firekeep_client import state
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("print(1)\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, {"decision": "allow", "action_id": "act-1", "advisories": []})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 0
        assert state.pop_action("s1") == "act-1"
        assert state.read_prestate("act-1")  # a sha was captured

    def test_gateway_block_returns_1(self, client_env, monkeypatch, tmp_path, capsys):
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, {"decision": "block", "action_id": "a",
                               "advisories": [{"message": "risky path"}]})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 1
        assert "block" in capsys.readouterr().err

    def test_gateway_rethink_returns_1(self, client_env, monkeypatch, tmp_path, capsys):
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, {"decision": "rethink", "action_id": "a",
                               "advisories": [{"message": "reconsider approach"}]})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 1
        assert "rethink" in capsys.readouterr().err

    def test_lease_held_by_other_returns_2(self, client_env, monkeypatch, tmp_path, capsys):
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": True, "holder_id": "other"})
        _gateway(monkeypatch, {"decision": "allow", "action_id": "a", "advisories": []})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 2
        assert "BLOCKED" in capsys.readouterr().err

    def test_lease_held_by_self_not_blocked(self, client_env, monkeypatch, tmp_path):
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": True, "holder_id": "tester"})  # us
        _gateway(monkeypatch, {"decision": "allow", "action_id": "a", "advisories": []})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 0

    def test_gateway_warn_returns_0_and_records_prestate(self, client_env, monkeypatch,
                                                          tmp_path, capsys):
        from firekeep_client import state
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, {"decision": "warn", "action_id": "act-warn",
                               "advisories": [{"message": "unusual hour"}]})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 0
        assert "warn" in capsys.readouterr().err
        assert state.pop_action("s1") == "act-warn"
        assert state.read_prestate("act-warn")  # recorded on warn too, not just allow

    def test_server_unreachable_returns_0(self, client_env, monkeypatch, tmp_path):
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, boom=True)
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 0

    def test_edit_without_path_returns_0(self, client_env, monkeypatch):
        from firekeep_client.hooks import pre_tool
        rc = pre_tool.run({"tool_name": "Edit", "tool_input": {}, "session_id": "s1"})
        assert rc == 0


class TestRealServerContract:
    """Pins the outgoing request-body shape against the REAL cortex gateway
    schema — the mocked-transport tests above are structurally blind to it
    (Task 15 review caught adapter='firekeep-hook' 422ing on the live server)."""

    def test_adapter_literal_is_shell_hook(self, client_env, monkeypatch, tmp_path):
        from firekeep_client import transport
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        seen = {}

        def capture(url, body, **k):
            seen["body"] = body
            return {"decision": "allow", "action_id": "a", "advisories": []}

        monkeypatch.setattr(transport, "post_json", capture)
        pre_tool.run({"tool_name": "Edit",
                      "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        # cortex/app/agent_gateway/models.py: Adapter = Literal["shell-hook","mcp","rest"]
        assert seen["body"]["adapter"] == "shell-hook"

    def test_arguments_payload_shape_still_guarded(self, client_env, monkeypatch,
                                                   tmp_path, capsys):
        """MCP/JSON-RPC-framed callers use 'arguments' — the 3-key fallback must
        keep the lease+gateway checks live for them (bash parity)."""
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": True, "holder_id": "someone-else"})
        rc = pre_tool.run({"tool_name": "Edit",
                           "arguments": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 2  # the lease check FIRED for the arguments-framed payload
        assert "BLOCKED" in capsys.readouterr().err

    def test_advisories_surfaced_on_allow(self, client_env, monkeypatch,
                                          tmp_path, capsys):
        """The live gateway remaps warn->allow and carries advisories in the
        payload; they must reach stderr, not be dropped by a warn-only gate."""
        from firekeep_client.hooks import pre_tool
        f = tmp_path / "x.py"
        f.write_text("x\n")
        _lease(monkeypatch, {"held": False})
        _gateway(monkeypatch, {"decision": "allow", "action_id": "a",
                               "advisories": [{"message": "hotspot: elevated failure rate"}]})
        rc = pre_tool.run({"tool_name": "Edit",
                           "tool_input": {"file_path": str(f)}, "session_id": "s1"})
        assert rc == 0
        assert "hotspot" in capsys.readouterr().err
