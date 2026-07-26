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

from tests.test_import_boundary import CLIENT_PKG, find_violations


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform —
    mirrors the win32 `.exe` handling in firekeep_client.adapters.base.console_script_path."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


# --------------------------------------------------------------------------- #
# shim_servers / FIREKEEP_MCP_KEYS                                               #
# --------------------------------------------------------------------------- #


def test_firekeep_decision_in_mcp_keys():
    assert "firekeep-decision" in FIREKEEP_MCP_KEYS


def test_shim_servers_includes_decision_and_symdex_always_on(tmp_path):
    """Both stdio-local servers are unconditional now: firekeep-decision AND
    firekeep-symdex are always registered (symdex is no longer opt-in)."""
    venv_bin = tmp_path / "Scripts"
    servers = shim_servers(venv_bin)
    assert "firekeep-decision" in servers
    assert "firekeep-symdex" in servers  # always-on alongside firekeep-decision
    cmd, args = servers["firekeep-decision"]
    assert cmd == _exe(venv_bin / "firekeep-decision")  # its OWN console-script path
    assert args == []


# --------------------------------------------------------------------------- #
# Adapter render -> unrender round trip                                       #
# --------------------------------------------------------------------------- #


def test_claude_adapter_round_trip_removes_firekeep_decision(tmp_path, monkeypatch):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    venv_bin = tmp_path / "venv" / "Scripts"
    adapter = get_adapter("claude")

    adapter.render(venv_bin=venv_bin)
    cfg = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert cfg["mcpServers"]["firekeep-decision"] == {
        "type": "stdio", "command": _exe(venv_bin / "firekeep-decision"), "args": []}

    adapter.unrender()
    cfg2 = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "firekeep-decision" not in cfg2["mcpServers"]


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
