"""deploy/glama/catalog_server.py: the tool catalog Glama introspects.

Glama scores a listing from the definitions this server returns, and builds it
with only deploy/glama/requirements.txt installed. The failure mode worth
guarding is silent: a service gains an import-time dependency that file does
not carry, the catalog exits on Glama's builder, and the listing's score goes
blank with nothing red here. So this drives the real server over stdio, exactly
as mcp-proxy does, and the repo-scripts CI job installs the same requirements
file Glama does — never the services' full locks.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "deploy" / "glama" / "catalog_server.py"


def _session(*requests: dict) -> list[dict]:
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input=stdin, capture_output=True, text=True, timeout=300, cwd=REPO,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_catalog_serves_every_services_real_tools():
    init, listing, call = _session(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "memory_recall", "arguments": {"task": "x"}}},
    )

    version = json.loads((REPO / "server.json").read_text(encoding="utf-8"))["version"]
    assert init["result"]["serverInfo"]["version"] == version

    tools = {t["name"]: t for t in listing["result"]["tools"]}
    # One anchor tool per service: an empty slice means that service's import broke.
    for name in ("memory_recall", "ctx_start_session", "relay_lease", "sentinel_get_events"):
        assert name in tools, f"{name} missing from the catalog"
    for tool in tools.values():
        assert tool.get("description"), f"{tool['name']} has no description"
        assert tool["inputSchema"]["type"] == "object"

    # A catalog never pretends to be a Keep.
    assert call["result"]["isError"] is True
    assert "firekeep.ai" in call["result"]["content"][0]["text"]
