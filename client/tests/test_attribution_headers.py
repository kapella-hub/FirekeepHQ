"""Round-2 measurement contract: X-Firekeep-* attribution on the wire.

Five headers, attached wherever the caller knows its runtime — the gateway
(rendered `firekeep gateway --runtime <name>`, exported to the shim children
that make the actual HTTP calls) and the hook cores (the dispatcher's
`--runtime` flag) — via the one seam every server call already flows through:
resolver.resolve()'s Endpoint.headers, exactly where X-Agent-Id lives.

Trust level is X-Agent-Id's: an untrusted observability label, never a gate.
The load-bearing negative: a process with NO runtime identity (old rendered
configs, direct CLI use) attaches NO attribution headers — sessions from
pre-0.1.41 clients read as unattributed, honestly.
"""
from __future__ import annotations

import io
import sys
import textwrap

import pytest

from firekeep_client import __version__, resolver
from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    GATEWAY_INSTRUCTIONS_HASH,
    RENDERED_INSTRUCTIONS_HASH,
    upsert_marked_block,
)

ATTRIBUTION_HEADERS = (
    "X-Firekeep-Runtime",
    "X-Firekeep-Client",
    "X-Firekeep-Instr-Rendered",
    "X-Firekeep-Instr-Expected",
    "X-Firekeep-Instr-Gateway",
)

CONFIG = textwrap.dedent("""\
    [identity]
    agent_id = mogan

    [server]
    kind = ports
    scheme = http
    host = 198.51.100.7
    verify_tls = false
""")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def server_config(tmp_path, monkeypatch):
    cfg = tmp_path / "fk-config"
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return cfg


def _render_claude_block(home):
    md = home / ".claude" / "CLAUDE.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(upsert_marked_block("", FIREKEEP_INSTRUCTIONS), encoding="utf-8")
    return md


# --- resolver: the one seam --------------------------------------------------


def test_no_runtime_means_no_attribution_headers(server_config):
    """Old rendered configs carry no --runtime flag; their calls must look
    exactly as they did before 0.1.41."""
    headers = resolver.resolve("cortex").headers
    assert headers == {"X-Agent-Id": "mogan"}
    for name in ATTRIBUTION_HEADERS:
        assert name not in headers


def test_runtime_attaches_the_five_headers(server_config, fake_home, monkeypatch):
    _render_claude_block(fake_home)
    monkeypatch.setenv("FIREKEEP_RUNTIME", "claude")
    resolver._ATTRIBUTION_CACHE.clear()

    headers = resolver.resolve("cortex").headers
    assert headers["X-Agent-Id"] == "mogan"  # attribution rides BESIDE identity
    assert headers["X-Firekeep-Runtime"] == "claude"
    assert headers["X-Firekeep-Client"] == __version__
    assert headers["X-Firekeep-Instr-Rendered"] == RENDERED_INSTRUCTIONS_HASH
    assert headers["X-Firekeep-Instr-Expected"] == RENDERED_INSTRUCTIONS_HASH
    assert headers["X-Firekeep-Instr-Gateway"] == GATEWAY_INSTRUCTIONS_HASH


def test_missing_block_reports_literal_absent(server_config, fake_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_RUNTIME", "claude")
    resolver._ATTRIBUTION_CACHE.clear()

    headers = resolver.resolve("cortex").headers
    assert headers["X-Firekeep-Instr-Rendered"] == "absent"
    assert headers["X-Firekeep-Instr-Expected"] == RENDERED_INSTRUCTIONS_HASH


def test_hand_edited_block_reports_its_true_hash(server_config, fake_home, monkeypatch):
    md = _render_claude_block(fake_home)
    md.write_text(md.read_text(encoding="utf-8").replace("memory_recall", "x", 1),
                  encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_RUNTIME", "claude")
    resolver._ATTRIBUTION_CACHE.clear()

    rendered = resolver.resolve("cortex").headers["X-Firekeep-Instr-Rendered"]
    assert rendered not in ("absent", RENDERED_INSTRUCTIONS_HASH)


def test_rendered_hash_is_a_process_start_snapshot(server_config, fake_home, monkeypatch):
    """Computed once and cached: a mid-process file change does not move the
    header — the contract says process start, not per-request stat."""
    _render_claude_block(fake_home)
    monkeypatch.setenv("FIREKEEP_RUNTIME", "claude")
    resolver._ATTRIBUTION_CACHE.clear()

    first = resolver.resolve("cortex").headers["X-Firekeep-Instr-Rendered"]
    (fake_home / ".claude" / "CLAUDE.md").unlink()
    second = resolver.resolve("cortex").headers["X-Firekeep-Instr-Rendered"]
    assert first == second == RENDERED_INSTRUCTIONS_HASH


# --- hook cores: the existing header-merge seam (hooks/_mcp.py) ---------------


def test_hook_mcp_call_carries_attribution(server_config, fake_home, monkeypatch):
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp

    _render_claude_block(fake_home)
    monkeypatch.setenv("FIREKEEP_RUNTIME", "kiro")  # kiro has no block rendered here
    resolver._ATTRIBUTION_CACHE.clear()

    seen = {}

    def capture(url, payload, *, headers=None, verify=False, timeout=None):
        seen["url"] = url
        seen["headers"] = dict(headers or {})
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(transport, "post_json", capture)
    _mcp.call_tool("cortex", "memory_recall", {"task": "t"})

    assert seen["headers"]["X-Agent-Id"] == "mogan"
    assert seen["headers"]["X-Firekeep-Runtime"] == "kiro"
    assert seen["headers"]["X-Firekeep-Client"] == __version__
    # kiro's steering file was never rendered in this fake home -> absent.
    assert seen["headers"]["X-Firekeep-Instr-Rendered"] == "absent"
    assert seen["headers"]["X-Firekeep-Instr-Expected"] == RENDERED_INSTRUCTIONS_HASH
    assert seen["headers"]["X-Firekeep-Instr-Gateway"] == GATEWAY_INSTRUCTIONS_HASH


def test_hook_mcp_call_without_runtime_carries_none(server_config, monkeypatch):
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp

    seen = {}

    def capture(url, payload, *, headers=None, verify=False, timeout=None):
        seen["headers"] = dict(headers or {})
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text", "text": "{}"}]}}

    monkeypatch.setattr(transport, "post_json", capture)
    _mcp.call_tool("cortex", "memory_recall", {"task": "t"})

    for name in ATTRIBUTION_HEADERS:
        assert name not in seen["headers"]


# --- the dispatcher's --runtime flag ------------------------------------------


def _run_core_recording_env(monkeypatch, argv):
    import os

    from firekeep_client.hooks import __main__ as dispatcher

    record = {}

    def fake_run(payload):
        record["runtime"] = os.environ.get("FIREKEEP_RUNTIME")
        return {}

    monkeypatch.setattr(dispatcher._CORE_MODULES["prompt"], "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert dispatcher.main(argv) == 0
    return record


def test_dispatcher_runtime_flag_exports_the_env(server_config, monkeypatch):
    record = _run_core_recording_env(monkeypatch, ["prompt", "--runtime", "kiro"])
    assert record["runtime"] == "kiro"


def test_dispatcher_without_flag_exports_nothing(server_config, monkeypatch):
    """Old rendered hook commands (no --runtime) must keep working with no
    runtime identity — the dispatcher defaults to None, not to a guess."""
    monkeypatch.delenv("FIREKEEP_RUNTIME", raising=False)
    record = _run_core_recording_env(monkeypatch, ["prompt"])
    assert record["runtime"] is None


def test_dispatcher_runtime_combines_with_block_exit(server_config, monkeypatch):
    import os

    from firekeep_client.hooks import __main__ as dispatcher

    record = {}

    class Core:
        @staticmethod
        def run(payload):
            record["runtime"] = os.environ.get("FIREKEEP_RUNTIME")
            return 1

    monkeypatch.setitem(dispatcher._CORE_MODULES, "pre_tool", Core)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    rc = dispatcher.main(["pre_tool", "--block-exit", "2", "--runtime", "claude"])
    assert rc == 2  # the remap still fires with the runtime flag present
    assert record["runtime"] == "claude"


# --- the gateway: runtime export + serverInfo hash ----------------------------


def test_gateway_run_exports_runtime_for_its_shim_children(monkeypatch):
    import os

    from firekeep_client import gateway as gw

    monkeypatch.delenv("FIREKEEP_RUNTIME", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # immediate EOF: no serving
    assert gw.run(runtime="claude") == 0
    # Inherited by every Backend Popen — the processes that make the HTTP calls.
    assert os.environ.get("FIREKEEP_RUNTIME") == "claude"


def test_gateway_run_without_runtime_exports_nothing(monkeypatch):
    import os

    from firekeep_client import gateway as gw

    monkeypatch.delenv("FIREKEEP_RUNTIME", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert gw.run() == 0
    assert "FIREKEEP_RUNTIME" not in os.environ


def test_gateway_cli_parses_the_runtime_flag():
    from firekeep_client import cli

    parser = cli._build_parser()
    args = parser.parse_args(["gateway", "--runtime", "codex"])
    assert args.runtime == "codex"
    assert parser.parse_args(["gateway"]).runtime is None  # old configs keep working


def test_gateway_serverinfo_version_is_the_handshake_hash():
    """serverInfo.version names exactly which instruction text this session
    received — the hardcoded "1" it replaces said nothing (round-2 contract)."""
    from firekeep_client.gateway import Gateway

    reply = Gateway().handle({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26"},
    })
    assert reply["result"]["serverInfo"]["version"] == GATEWAY_INSTRUCTIONS_HASH
    assert "action_before" in reply["result"]["instructions"]
