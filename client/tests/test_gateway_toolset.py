"""FIREKEEP_TOOLSET / FIREKEEP_TOOLS_ALLOW — the gateway's curated surfaces.

The invariants:
  - filtering happens at the ROUTING layer: an excluded tool is invisible in
    tools/list AND uncallable (-32601), never decoratively hidden;
  - an unknown preset fails CLOSED (refuses to start) — this gateway can sit
    behind a tunnel reachable from a consumer chat host, and a typo must not
    open the full ~90-tool surface;
  - unset env is byte-identical to today (pinned by test_gateway.py's exact
    status assertion plus the unfiltered case here);
  - the narrowing is disclosed via firekeep_gateway_status;
  - a preset that narrows the tools narrows the handshake text with it, and
    the chat instructions may only name tools the chat preset serves.
"""
from __future__ import annotations

import re

import pytest

from firekeep_client.adapters.base import (
    CHAT_INSTRUCTIONS,
    CHAT_INSTRUCTIONS_HASH,
    GATEWAY_INSTRUCTIONS_HASH,
)
from firekeep_client.gateway import STATUS_TOOL, TOOLSET_PRESETS, Gateway, _active_toolset


class FakeBackend:
    def __init__(self, name, tools):
        self.name = name
        self.tools = tools
        self.state = "not checked"

    def discover(self, protocol_version):
        self.state = f"ready ({len(self.tools)} tools)"

    def request(self, method, params, **kwargs):
        return {"jsonrpc": "2.0", "id": "up", "result": {"content": []}}

    def close(self):
        pass


def _tool(name):
    return {"name": name, "inputSchema": {}}


def _gateway(monkeypatch, **env):
    for key in ("FIREKEEP_TOOLSET", "FIREKEEP_TOOLS_ALLOW"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    gateway = Gateway()
    gateway.backends = [
        FakeBackend("cortex", [_tool("memory_recall"), _tool("memory_learn"),
                               _tool("vault_retrieve"), _tool("corpus_ingest")]),
        FakeBackend("bridge", [_tool("ctx_start_session"), _tool("ctx_update")]),
        FakeBackend("relay", [_tool("relay_broadcast")]),
    ]
    return gateway


def test_chat_preset_filters_list_and_routes(monkeypatch):
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLSET="chat")
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {STATUS_TOOL["name"], "memory_recall", "memory_learn",
                     "ctx_start_session", "ctx_update"}
    # Routing-layer enforcement: the excluded tool is UNCALLABLE, and the
    # handler's re-discover retry must not resurrect it.
    call = gateway.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "vault_retrieve", "arguments": {}}})
    assert call["error"]["code"] == -32601


def test_allowlist_overrides_preset(monkeypatch):
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLSET="chat",
                       FIREKEEP_TOOLS_ALLOW="vault_retrieve, relay_broadcast")
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names == {STATUS_TOOL["name"], "vault_retrieve", "relay_broadcast"}


def test_unset_env_serves_everything(monkeypatch):
    gateway = _gateway(monkeypatch)
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert len(reply["result"]["tools"]) == 8  # 7 fakes + status tool
    assert gateway.status()["toolset"] is None
    assert gateway.status()["tools_filtered"] == 0


def test_unknown_preset_fails_closed(monkeypatch):
    monkeypatch.delenv("FIREKEEP_TOOLS_ALLOW", raising=False)
    monkeypatch.setenv("FIREKEEP_TOOLSET", "chta")
    with pytest.raises(SystemExit) as exc:
        _active_toolset()
    assert "chta" in str(exc.value)
    assert "refusing" in str(exc.value)


def test_status_discloses_narrowing(monkeypatch):
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLSET="chat")
    gateway.discover()
    status = gateway.status()
    assert status["toolset"] == "chat"
    assert status["tools_filtered"] == 3  # vault_retrieve, corpus_ingest, relay_broadcast
    assert status["tool_count"] == 4


def test_status_tool_is_never_filtered(monkeypatch):
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLS_ALLOW="memory_recall")
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert STATUS_TOOL["name"] in {tool["name"] for tool in reply["result"]["tools"]}


def test_chat_preset_swaps_handshake_text_and_hash(monkeypatch):
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLSET="chat")
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"}})
    result = reply["result"]
    assert result["instructions"] == CHAT_INSTRUCTIONS
    assert result["serverInfo"]["version"] == CHAT_INSTRUCTIONS_HASH
    assert "decision_board" not in result["instructions"]
    assert "vault" not in result["instructions"]


def test_allowlist_keeps_default_handshake(monkeypatch):
    # The operator overrode the preset; the default text stays and they own
    # any tool/instruction mismatch.
    gateway = _gateway(monkeypatch, FIREKEEP_TOOLS_ALLOW="memory_recall")
    reply = gateway.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-03-26"}})
    assert reply["result"]["serverInfo"]["version"] == GATEWAY_INSTRUCTIONS_HASH


def test_chat_instructions_only_name_preset_tools():
    """Mechanical honesty: every tool-shaped name the chat handshake mentions
    must be a tool the chat preset actually serves — an instruction to call a
    tool that errors is worse than no instruction."""
    mentioned = set(re.findall(r"\b[a-z]+_[a-z_]+\b", CHAT_INSTRUCTIONS))
    non_tools = {"memory_ids"}  # a parameter name, not a tool
    assert mentioned - non_tools <= TOOLSET_PRESETS["chat"]


def test_chat_preset_is_exactly_the_decided_surface():
    assert TOOLSET_PRESETS["chat"] == {
        "memory_recall", "memory_learn", "memory_feedback",
        "skill_recall", "skill_list",
        "ctx_start_session", "ctx_update", "ctx_complete_session",
        "ctx_abandon_session", "ctx_list_sessions", "ctx_resume_session",
        "ctx_get_shadow",
    }
