"""Failing-first tests for SP4 Task 6: registering `firekeep-decision` across the
adapter/pyproject/import-boundary surfaces.

Task 5 already built the frozen `client/firekeep_client/decision/server.py`
(console-script entrypoint `firekeep_client.decision.server:main`). This task
only wires it up:
  - `firekeep-decision` becomes an ALWAYS-ON `shim_servers()`/`FIREKEEP_MCP_KEYS`
    entry (its own console-script, args `[]` — stdio-local like symdex, but
    unconditional rather than opt-in: it is registered regardless of the
    `symdex` flag).
  - render -> unrender round-trips cleanly remove it (same as every other
    firekeep-owned MCP key), since it rides `FIREKEEP_MCP_KEYS`/`drop_owned`.
  - the import-boundary RULE 2 (mcp/httpx confinement, see
    test_import_boundary.py) is widened so decision/server.py may `import
    mcp` (it does, lazily inside main()) while remaining banned from `httpx`
    and server packages — verified here against the REAL tree.
"""
import json
import sys

from firekeep_client.adapters import get_adapter
from firekeep_client.adapters.base import FIREKEEP_MCP_KEYS, shim_servers
from firekeep_client.gateway import LOCAL_SERVERS

from tests.test_import_boundary import CLIENT_PKG, find_violations


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


# --------------------------------------------------------------------------- #
# shim_servers / FIREKEEP_MCP_KEYS                                               #
# --------------------------------------------------------------------------- #


def test_decision_is_behind_the_one_gateway_key():
    assert FIREKEEP_MCP_KEYS == ("firekeep",)
    assert "decision" in LOCAL_SERVERS


def test_adapters_register_one_gateway_for_decision_and_symdex(tmp_path):
    venv_bin = tmp_path / "Scripts"
    servers = shim_servers(venv_bin)
    assert LOCAL_SERVERS == ("symdex", "decision")
    assert servers == {"firekeep": (_exe(venv_bin / "firekeep"), ["gateway"])}


# --------------------------------------------------------------------------- #
# Adapter render -> unrender round trip                                       #
# --------------------------------------------------------------------------- #


def test_claude_adapter_round_trip_removes_firekeep_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")

    adapter.render(venv_bin=venv_bin)
    cfg = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["firekeep"] == {
        "type": "stdio", "command": _exe(venv_bin / "firekeep"),
        "args": ["gateway", "--runtime", "claude"]}

    adapter.unrender()
    cfg2 = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "firekeep" not in cfg2["mcpServers"]


# --------------------------------------------------------------------------- #
# Import boundary: decision/server.py may `import mcp` (Task 6 widening)      #
# --------------------------------------------------------------------------- #


def test_decision_server_file_exists_under_client_pkg():
    # Sanity: the exemption target from Task 5 is really there before we
    # assert anything about how it's scanned.
    assert (CLIENT_PKG / "decision" / "server.py").is_file()


def test_real_tree_scan_has_no_mcp_violation_for_decision_server():
    """Integration check against the REAL tree: decision/server.py's lazy
    `import mcp` inside main() must NOT be flagged now that Task 6 widens the
    RULE 2 exemption to cover it. (The wider "no violations at all" guarantee
    is already pinned by test_import_boundary.py's own full-tree test; this
    narrows to the specific case this task adds.)"""
    violations = find_violations()
    assert not any(
        "decision" in v and "server.py" in v and "mcp" in v for v in violations
    )
