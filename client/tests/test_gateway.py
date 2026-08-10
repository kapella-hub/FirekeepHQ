from __future__ import annotations

import json
import sys

from firekeep_client.gateway import Backend, Gateway, STATUS_TOOL


class FakeBackend:
    def __init__(self, name, tools=None, error=None):
        self.name = name
        self.tools = tools or []
        self.error = error
        self.state = "not checked"
        self.calls = []

    def discover(self, protocol_version):
        if self.error:
            self.tools = []
            self.state = f"unavailable: {self.error}"
        else:
            self.state = f"ready ({len(self.tools)} tools)"

    def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        return {
            "jsonrpc": "2.0",
            "id": "upstream",
            "result": {"content": [{"type": "text", "text": self.name}]},
        }

    def close(self):
        pass


def _gateway():
    gateway = Gateway()
    gateway.backends = [
        FakeBackend("cortex", [{"name": "memory_recall", "inputSchema": {}}]),
        FakeBackend("relay", error="connection refused"),
    ]
    return gateway


def test_gateway_initialize_carries_memory_and_decision_instructions():
    reply = _gateway().handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    })
    instructions = reply["result"]["instructions"]
    assert "memory_recall" in instructions
    assert "decision_board" in instructions
    assert "decision_board_check" in instructions


def test_gateway_advertises_reachable_tools_and_diagnostic_status_only():
    gateway = _gateway()
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [tool["name"] for tool in reply["result"]["tools"]]
    assert names == [STATUS_TOOL["name"], "memory_recall"]
    assert gateway.status() == {
        "backends": {
            "cortex": "ready (1 tools)",
            "relay": "unavailable: connection refused",
        },
        "tool_count": 1,
        "plan_filtering": False,
    }


def test_gateway_routes_calls_without_rewriting_tool_result_or_plan_filtering():
    gateway = _gateway()
    gateway.discover()
    response = gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": "client-7",
            "method": "tools/call",
            "params": {"name": "memory_recall", "arguments": {"task": "x"}},
        }
    )
    assert response["id"] == "client-7"
    assert response["result"]["content"][0]["text"] == "cortex"
    assert gateway.backends[0].calls[0][0] == "tools/call"


def test_gateway_status_tool_explains_degradation_in_model_context():
    gateway = _gateway()
    gateway.discover()
    response = gateway.handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": STATUS_TOOL["name"], "arguments": {}},
        }
    )
    data = json.loads(response["result"]["content"][0]["text"])
    assert "connection refused" in data["backends"]["relay"]
    assert data["plan_filtering"] is False


def test_real_backend_process_initialize_discover_and_call_round_trip():
    code = r'''
import json, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        result = {"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"fake","version":"1"}}
    elif m["method"] == "tools/list":
        result = {"tools":[{"name":"fake_tool","inputSchema":{"type":"object"}}]}
    else:
        result = {"content":[{"type":"text","text":"called"}]}
    print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":result}), flush=True)
'''
    backend = Backend("fake", [sys.executable, "-u", "-c", code])
    try:
        backend.discover("2025-03-26")
        assert backend.state == "ready (1 tools)"
        assert backend.tools[0]["name"] == "fake_tool"
        called = backend.request("tools/call", {"name": "fake_tool", "arguments": {}})
        assert called["result"]["content"][0]["text"] == "called"
    finally:
        backend.close()


class TestConsoleScriptResolution:
    """client 0.1.37 shipped a Linux gateway whose six backends all reported
    executable-not-found: _console_script resolved the venv python SYMLINK into
    the standalone CPython directory and looked for console scripts there.
    Windows never hit it (Scripts\python.exe is a real file), which is exactly
    why these tests build the POSIX layout explicitly instead of trusting the
    host the suite happens to run on."""

    def test_prefers_the_unresolved_venv_bin_dir(self, tmp_path, monkeypatch):
        """The venv bin dir must win even when python is a symlink pointing
        elsewhere — the console scripts live beside the symlink, not beside
        its target."""
        from firekeep_client import gateway as gw

        standalone = tmp_path / "cpython" / "bin"
        standalone.mkdir(parents=True)
        (standalone / "python3").write_text("")

        venv_bin = tmp_path / "venvs" / "1.0.0" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            (venv_bin / "python").symlink_to(standalone / "python3")
        except OSError:
            import pytest
            pytest.skip("symlinks unavailable (Windows without dev mode)")
        shim = venv_bin / "firekeep-shim"
        shim.write_text("")

        monkeypatch.setattr(gw.sys, "executable", str(venv_bin / "python"))
        monkeypatch.setattr(gw.os, "name", "posix")
        assert gw._console_script("firekeep-shim") == str(shim)

    def test_resolved_dir_still_found_when_venv_bin_lacks_the_script(
        self, tmp_path, monkeypatch
    ):
        """The resolved location stays a fallback — a layout that really does
        keep scripts beside the interpreter target must keep working."""
        from firekeep_client import gateway as gw

        standalone = tmp_path / "cpython" / "bin"
        standalone.mkdir(parents=True)
        (standalone / "python3").write_text("")
        beside_target = standalone / "firekeep-shim"
        beside_target.write_text("")

        venv_bin = tmp_path / "venvs" / "1.0.0" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            (venv_bin / "python").symlink_to(standalone / "python3")
        except OSError:
            import pytest
            pytest.skip("symlinks unavailable (Windows without dev mode)")

        monkeypatch.setattr(gw.sys, "executable", str(venv_bin / "python"))
        monkeypatch.setattr(gw.os, "name", "posix")
        assert gw._console_script("firekeep-shim") == str(beside_target)
