"""Firekeep's MCP tool catalog over stdio, for directory introspection.

Glama (and any directory that scores a server by its tool definitions) builds
the repository and asks it for ``tools/list``. Firekeep's tools only exist
behind a running Keep, so the real gateway would report an empty inventory.
This server reports the REAL definitions instead: each service's FastMCP app is
imported in its own subprocess (every service names its package ``app``, so
one interpreter cannot hold two of them) and its tool list is passed through
unchanged. Nothing is restated here, so the catalog cannot drift from the code.

Every ``tools/call`` fails with an explanation of how to run Firekeep. This is
a catalog, not a Keep: it stores nothing and connects to nothing.

Needs only ``deploy/glama/requirements.txt``; run from the repository root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVICES = ("cortex", "bridge", "relay", "sentinel")
PROTOCOL_VERSION = "2025-06-18"

INSTRUCTIONS = (
    "Firekeep is a self-hosted memory and coordination layer for coding agents "
    "(Claude Code, Codex, Kiro, OpenCode, Pi and any MCP client). Agents record "
    "what worked and why, recall it in later sessions and other tools, keep "
    "working state across context compaction, and coordinate through leases, "
    "tasks and messages. These tools run against your own Keep: install with "
    "`curl -fsSL https://firekeep.ai/latest/install | sh`, then connect through "
    "`firekeep gateway`. Docs: https://firekeep.ai/docs.html"
)

CALL_ERROR = (
    "This is Firekeep's tool catalog for MCP directories; it is not connected "
    "to a Keep. Firekeep's tools run against a self-hosted Keep: install with "
    "`curl -fsSL https://firekeep.ai/latest/install | sh` (Windows: "
    "`irm https://firekeep.ai/latest/install.ps1 | iex`), then point your MCP "
    "client at `firekeep gateway`. Docs: https://firekeep.ai/docs.html"
)

_DUMP = """
import asyncio, json
from app.mcp_server import mcp

async def main():
    out = []
    for t in await mcp.list_tools():
        tool = t.to_mcp_tool()
        out.append(tool.model_dump(mode="json", by_alias=True, exclude_none=True))
    print(json.dumps(out))

asyncio.run(main())
"""


def _service_tools(service: str) -> list[dict]:
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(REPO / service), str(REPO)]),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP],
        cwd=REPO / service,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{service}: tool enumeration failed:\n{proc.stderr}")
    # Import-time log lines go to stderr; stdout carries exactly one JSON line.
    return json.loads(proc.stdout.strip().splitlines()[-1])


def load_catalog() -> list[dict]:
    tools: list[dict] = []
    seen: set[str] = set()
    for service in SERVICES:
        for tool in _service_tools(service):
            if tool["name"] in seen:
                raise RuntimeError(f"duplicate tool name across services: {tool['name']}")
            seen.add(tool["name"])
            tools.append(tool)
    return tools


def _version() -> str:
    """The registry version from server.json, the same number directories show."""
    try:
        return json.loads((REPO / "server.json").read_text(encoding="utf-8"))["version"]
    except Exception:  # noqa: BLE001 — a missing version must not break the handshake
        return "0.0.0"


def _reply(request_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve(tools: list[dict]) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        method = msg.get("method")
        request_id = msg.get("id")
        if request_id is None:
            continue  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            client_version = (msg.get("params") or {}).get("protocolVersion")
            _reply(request_id, {
                "protocolVersion": client_version or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "firekeep", "title": "Firekeep", "version": _version()},
                "instructions": INSTRUCTIONS,
            })
        elif method == "tools/list":
            _reply(request_id, {"tools": tools})
        elif method == "tools/call":
            _reply(request_id, {
                "content": [{"type": "text", "text": CALL_ERROR}],
                "isError": True,
            })
        elif method == "ping":
            _reply(request_id, {})
        else:
            _reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> int:
    try:
        tools = load_catalog()
    except Exception as exc:  # noqa: BLE001 — say why, loudly, then exit nonzero
        print(f"firekeep catalog: {exc}", file=sys.stderr)
        return 1
    serve(tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
