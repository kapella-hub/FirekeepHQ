"""Stdlib MCP `tools/call` helper for the hook cores.

The four core services speak FastMCP Streamable-HTTP at `/mcp`. A single
`tools/call` is ONE-SHOT request/response (design §6.1: no *iterative* SSE —
`transport.post_json` parses the single `data:` frame and returns the JSON-RPC
envelope). This helper builds the envelope, POSTs it via the shared stdlib
transport, and unwraps the tool's structured result. It is the ONLY MCP path the
stdlib cores use — streaming MCP is the shim's job, not the cores'. It imports
only stdlib + firekeep_client stdlib modules (never mcp/httpx).
"""
from __future__ import annotations

import json
from typing import Any

from firekeep_client import resolver, transport


def call_tool(
    service: str,
    tool: str,
    arguments: dict,
    *,
    cfg=None,
    session_id: str | None = None,
    timeout: float = transport.DEFAULT_TIMEOUT,
) -> Any:
    """Call an MCP tool and return its unwrapped result.

    Raises transport.TransportError on network/non-2xx failure, on an in-band
    JSON-RPC `error` (MCP tool failures return HTTP 200 with an `error` member,
    which post_json does NOT raise on), or on a malformed envelope. Best-effort
    callers catch it and hooklog.
    """
    ep = resolver.resolve(service, cfg=cfg, session_id=session_id)
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    # FastMCP streamable-HTTP requires BOTH accept types (else 406) and may
    # answer with a one-shot SSE-framed body; transport parses that per its
    # Content-Type. Caller headers win over _request's setdefault.
    headers = {**ep.headers, "Accept": "application/json, text/event-stream"}
    resp = transport.post_json(
        ep.mcp_url, envelope, headers=headers, verify=ep.verify, timeout=timeout
    )
    if not isinstance(resp, dict):
        raise transport.TransportError(f"{service}.{tool}: non-object MCP response")
    if resp.get("error"):
        raise transport.TransportError(f"{service}.{tool}: MCP error {resp['error']}")
    try:
        text = resp["result"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise transport.TransportError(f"{service}.{tool}: malformed MCP result") from e
    if not text:
        return {}
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text  # some tools return plain-text content
