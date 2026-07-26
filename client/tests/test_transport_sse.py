"""One-shot SSE-framed response handling (the MCP tools/call reality fix).

FastMCP's streamable-HTTP endpoint requires `Accept: application/json,
text/event-stream` and may answer a single tools/call POST with an SSE-framed
body. transport must (a) let callers override the Accept header and (b) parse
the complete SSE body per Content-Type. This is NOT iterative streaming —
the body is fully buffered; streaming MCP stays the shim's job.
"""
from __future__ import annotations

import json

import pytest

from firekeep_client import transport


class _FakeResp:
    def __init__(self, body: bytes, content_type: str):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, body: bytes, content_type: str, captured: dict):
    def fake_urlopen(req, **kw):
        captured["accept"] = req.headers.get("Accept")
        return _FakeResp(body, content_type)

    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)


def test_sse_body_single_frame_parsed(monkeypatch):
    captured: dict = {}
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": "{}"}]}}
    body = f"event: message\ndata: {json.dumps(rpc)}\n\n".encode()
    _serve(monkeypatch, body, "text/event-stream", captured)

    out = transport.post_json("http://h/mcp", {}, headers={})
    assert out == rpc


def test_sse_body_prefers_last_jsonrpc_response_over_notifications(monkeypatch):
    captured: dict = {}
    notif = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    body = (
        f"data: {json.dumps(notif)}\n\n" f"data: {json.dumps(rpc)}\n\n"
    ).encode()
    _serve(monkeypatch, body, "text/event-stream", captured)

    out = transport.post_json("http://h/mcp", {}, headers={})
    assert out == rpc  # the response frame, not the notification


def test_sse_body_with_no_json_frame_raises(monkeypatch):
    captured: dict = {}
    _serve(monkeypatch, b": keepalive comment\n\n", "text/event-stream", captured)

    with pytest.raises(transport.TransportError):
        transport.post_json("http://h/mcp", {}, headers={})


def test_caller_accept_header_wins_over_default(monkeypatch):
    captured: dict = {}
    _serve(monkeypatch, b"{}", "application/json", captured)

    transport.post_json(
        "http://h/mcp", {}, headers={"Accept": "application/json, text/event-stream"}
    )
    assert captured["accept"] == "application/json, text/event-stream"


def test_plain_json_body_unaffected(monkeypatch):
    captured: dict = {}
    _serve(monkeypatch, b'{"a": 1}', "application/json", captured)

    assert transport.get_json("http://h/x", headers={}) == {"a": 1}
    assert captured["accept"] == "application/json"


def test_non_utf8_body_raises_transport_error(monkeypatch):
    captured: dict = {}
    _serve(monkeypatch, b"\xff\xfe\x00bad", "application/json", captured)

    with pytest.raises(transport.TransportError):
        transport.get_json("http://h/x", headers={})


def test_mcp_call_tool_sends_dual_accept(monkeypatch, tmp_path):
    """_mcp.call_tool must request both accept types (FastMCP 406s otherwise)."""
    from firekeep_client.hooks import _mcp

    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "[active]\nprofile = personal\n"
        "[personal]\nkind = ports\nscheme = http\nhost = 127.0.0.1\n"
        "verify_tls = false\nagent_id = tester\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg_path))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)  # profile value must win here

    seen: dict = {}

    def fake_post(url, body, *, headers, verify=True, timeout=None):
        seen["headers"] = headers
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"text": "{\"ok\": true}"}]}}

    monkeypatch.setattr(_mcp.transport, "post_json", fake_post)
    out = _mcp.call_tool("relay", "relay_register", {"agent_id": "tester"})
    assert out == {"ok": True}
    assert seen["headers"]["Accept"] == "application/json, text/event-stream"
    assert seen["headers"]["X-Agent-Id"] == "tester"
