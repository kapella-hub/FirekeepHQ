from __future__ import annotations

import json
import sys

from firekeep_client import dexes
from firekeep_client.gateway import (
    CORE_LOCAL_SERVERS,
    REMOTE_SERVICES,
    Backend,
    Gateway,
    STATUS_TOOL,
)


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
        "toolset": None,
        "tools_filtered": 0,
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


class TestRegistryDrivenBackends:
    """Dex registry milestone 1 (Task A2): the local leg of `Gateway.backends` is
    no longer a hardcoded tuple. Decision stays core (it indexes nothing, so it
    is not a dex); every dex mounts only while registered, and only if its
    manifest says there is an MCP server to mount at all."""

    @staticmethod
    def _names(gateway):
        return [backend.name for backend in gateway.backends]

    def test_empty_registry_mounts_no_dex_but_keeps_decision(self):
        dexes.write_registry({})
        names = self._names(Gateway())
        assert names == [*REMOTE_SERVICES, *CORE_LOCAL_SERVERS]
        assert "symdex" not in names

    def test_registered_symdex_mounts_its_console_script(self):
        dexes.write_registry({"symdex": {"source": "bundled"}})
        gateway = Gateway()
        assert self._names(gateway) == [*REMOTE_SERVICES, *CORE_LOCAL_SERVERS, "symdex"]
        symdex = gateway.backends[-1]
        assert len(symdex.command) == 1
        # Lowercased: _console_script's shutil.which fallback returns the
        # PATHEXT casing on Windows (".EXE"), which says nothing about the
        # manifest — the console_script NAME is what this pins.
        assert symdex.command[0].lower().endswith(
            ("firekeep-symdex", "firekeep-symdex.exe")
        )

    def test_ingest_client_dexes_mount_nothing(self):
        """docdex has no MCP server (spec §2) — `kind` is exactly the field that
        says so. Registering it must drive lifecycle/doctor/sync and leave the
        gateway's inventory untouched."""
        dexes.write_registry({"docdex": {"source": "bundled"}})
        names = self._names(Gateway())
        assert "docdex" not in names
        assert names == [*REMOTE_SERVICES, *CORE_LOCAL_SERVERS]

    def test_unknown_registry_entries_are_ignored(self):
        dexes.write_registry({"webdex": {}})
        assert self._names(Gateway()) == [*REMOTE_SERVICES, *CORE_LOCAL_SERVERS]

    def test_backends_are_never_empty(self):
        """gateway.discover() sizes a ThreadPoolExecutor from len(self.backends),
        which raises on 0. Four remote services plus decision are unconditional,
        so no guard is needed there — this is the assertion that keeps it true."""
        dexes.write_registry({})
        assert len(Gateway().backends) >= 5


class TestConsoleScriptResolution:
    """client 0.1.37 shipped a Linux gateway whose six backends all reported
    executable-not-found: _console_script resolved the venv python SYMLINK into
    the standalone CPython directory and looked for console scripts there.
    Windows never hit it (Scripts\python.exe is a real file), which is exactly
    why these tests build the POSIX layout explicitly instead of trusting the
    host the suite happens to run on."""

    @staticmethod
    def _suffix():
        # Use the REAL platform suffix rather than monkeypatching os.name:
        # os is a shared module, and forcing name="posix" makes pathlib mint
        # PosixPath objects on Windows — which raises NotImplementedError
        # inside pytest itself (found the hard way on CI, Python 3.11).
        import os

        return ".exe" if os.name == "nt" else ""

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
        shim = venv_bin / f"firekeep-shim{self._suffix()}"
        shim.write_text("")

        monkeypatch.setattr(gw.sys, "executable", str(venv_bin / "python"))
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
        beside_target = standalone / f"firekeep-shim{self._suffix()}"
        beside_target.write_text("")

        venv_bin = tmp_path / "venvs" / "1.0.0" / "bin"
        venv_bin.mkdir(parents=True)
        try:
            (venv_bin / "python").symlink_to(standalone / "python3")
        except OSError:
            import pytest
            pytest.skip("symlinks unavailable (Windows without dev mode)")

        monkeypatch.setattr(gw.sys, "executable", str(venv_bin / "python"))
        # Pin the sysconfig candidate to an empty dir VIA THE MODULE REFERENCE
        # (never the shared sysconfig module): a host with firekeep-shim
        # installed in its real scripts dir must not fake this pass.
        from types import SimpleNamespace

        monkeypatch.setattr(
            gw, "sysconfig",
            SimpleNamespace(get_path=lambda kind: str(tmp_path / "empty")),
        )
        assert gw._console_script("firekeep-shim") == str(beside_target)
