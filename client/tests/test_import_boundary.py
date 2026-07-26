"""CI import-boundary guard — this boundary *is* the client/server contract.

Two independently-scoped rules (spec §3 + the frozen 'Global constraints'):

  RULE 1 (server-package ban) — applies to BOTH client/firekeep_client AND
    symdex/src/firekeep_symdex. No module may import a server package:
    cortex, bridge, sentinel, relay, auth, vault, replay, corpus.

  RULE 2 (mcp/httpx confinement) — applies to client/firekeep_client ONLY and
    exempts shim.py. No client module except shim.py may import `mcp`/`httpx`
    (hook cores / resolver / sidecar / cli / adapters stay stdlib-only).
    symdex is deliberately NOT subject to RULE 2: it legitimately depends on
    mcp (server.py) and httpx (cortex/client.py, tools/*) — applying RULE 2 to
    it would make CI un-greenable. The task prompt's single 'except shim.py'
    phrasing is reconciled to this authoritative scoping here.

    SP4 Task 6 widens this further: decision/server.py may ALSO import `mcp`
    (its FastMCP entrypoint is imported lazily inside main() — see its module
    docstring). Unlike shim.py, decision/server.py is exempted from the `mcp`
    ban ONLY — it is still banned from `httpx` and every RULE 1 server
    package, same as any other client module. Two separate exemption sets
    below (`FULL_DEP_EXEMPT` vs `MCP_ONLY_EXEMPT`) encode that asymmetry;
    a single blanket per-file skip would have silently re-opened the httpx
    door for decision/server.py too.

Walk `symdex/src/firekeep_symdex`, NEVER `symdex/` — `symdex/.venv/**` holds
hundreds of third-party packages (httpx/pydantic/anyio); rooting at `symdex/`
would false-positive immediately.

Deliberate widening (2026-07-13): cli/updater may import truststore — guarded, optional at
runtime; CI (pytest-only env) exercises the ImportError fallback branch.
"""
import ast
from pathlib import Path

SERVER_PACKAGES = frozenset(
    {"cortex", "bridge", "sentinel", "relay", "auth", "vault", "replay", "corpus"}
)
DEP_PACKAGES = frozenset({"mcp", "httpx"})

REPO_ROOT = Path(__file__).resolve().parents[2]  # client/tests/<file> -> repo root
CLIENT_PKG = REPO_ROOT / "client" / "firekeep_client"
SYMDEX_PKG = REPO_ROOT / "symdex" / "src" / "firekeep_symdex"
SHIM_FILE = CLIENT_PKG / "shim.py"
DECISION_SERVER_FILE = CLIENT_PKG / "decision" / "server.py"
# shim.py is exempt from BOTH mcp and httpx (it legitimately needs both).
FULL_DEP_EXEMPT = frozenset({SHIM_FILE})
# decision/server.py (SP4 Task 6) is exempt from `mcp` ONLY — httpx and the
# RULE 1 server-package ban still apply to it.
MCP_ONLY_EXEMPT = frozenset({DECISION_SERVER_FILE})


def _iter_py_files(root: Path):
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _top_level_imports(path: Path) -> set:
    """Top-level module names imported anywhere in `path` (incl. function-local).

    Relative imports (level > 0) are skipped — they can only reach intra-package
    modules, never a server package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def find_violations(
    client_pkg: Path = CLIENT_PKG,
    symdex_pkg: Path = SYMDEX_PKG,
    full_exempt: frozenset = FULL_DEP_EXEMPT,
    mcp_only_exempt: frozenset = MCP_ONLY_EXEMPT,
) -> list:
    """Return a sorted list of human-readable violation strings ([] == clean).

    `full_exempt` files (shim.py) may import both `mcp` and `httpx`.
    `mcp_only_exempt` files (decision/server.py, as of SP4 Task 6) may import
    `mcp` but are still checked for `httpx` (and, via RULE 1 above, server
    packages) like any other client module.
    """
    violations = []

    # RULE 1 — server-package ban across BOTH trees.
    for root in (client_pkg, symdex_pkg):
        for path in _iter_py_files(root):
            for name in sorted(_top_level_imports(path) & SERVER_PACKAGES):
                rel = path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path
                violations.append(f"{rel}: imports server package '{name}'")

    # RULE 2 — mcp/httpx confined to shim.py (full exemption), CLIENT tree only.
    # decision/server.py gets a narrower, mcp-only exemption (SP4 Task 6).
    full_exempt_resolved = {p.resolve() for p in full_exempt}
    mcp_only_exempt_resolved = {p.resolve() for p in mcp_only_exempt}
    for path in _iter_py_files(client_pkg):
        resolved = path.resolve()
        if resolved in full_exempt_resolved:
            continue
        banned = DEP_PACKAGES - ({"mcp"} if resolved in mcp_only_exempt_resolved else set())
        for name in sorted(_top_level_imports(path) & banned):
            rel = path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path
            violations.append(
                f"{rel}: imports '{name}' — only firekeep_client/shim.py may depend on mcp/httpx"
            )

    return sorted(violations)


# --- Guard over the real tree (goes RED when a violation is injected) --------


def test_client_and_symdex_have_no_forbidden_imports():
    # Fail loud if the scan targets ever move/vanish: an empty walk would make
    # this enforceable contract silently pass (a no-op guard), which is exactly
    # the failure mode this test exists to prevent.
    assert CLIENT_PKG.is_dir(), f"scan target missing: {CLIENT_PKG}"
    assert SYMDEX_PKG.is_dir(), f"scan target missing: {SYMDEX_PKG}"
    assert list(_iter_py_files(CLIENT_PKG)), f"no .py files scanned under {CLIENT_PKG}"
    assert list(_iter_py_files(SYMDEX_PKG)), f"no .py files scanned under {SYMDEX_PKG}"

    violations = find_violations()
    assert violations == [], "Import-boundary violations:\n  " + "\n  ".join(violations)


# --- Unit tests of the checker against synthetic tmp_path trees --------------


def _mk(root: Path, rel: str, source: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_checker_flags_server_import_in_client(tmp_path):
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/resolver.py", "import relay\n")
    symdex = tmp_path / "symdex" / "src" / "firekeep_symdex"
    symdex.mkdir(parents=True)
    v = find_violations(
        client_pkg=client, symdex_pkg=symdex, full_exempt={client / "shim.py"}
    )
    assert any("resolver.py" in x and "relay" in x for x in v)


def test_checker_flags_server_import_in_symdex(tmp_path):
    symdex = tmp_path / "symdex" / "src" / "firekeep_symdex"
    _mk(tmp_path, "symdex/src/firekeep_symdex/server.py", "from cortex.app import x\n")
    client = tmp_path / "client" / "firekeep_client"
    client.mkdir(parents=True)
    v = find_violations(
        client_pkg=client, symdex_pkg=symdex, full_exempt={client / "shim.py"}
    )
    assert any("server.py" in x and "cortex" in x for x in v)


def test_checker_flags_mcp_httpx_outside_shim(tmp_path):
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/sidecar.py", "import httpx\n")
    v = find_violations(
        client_pkg=client, symdex_pkg=tmp_path / "sx", full_exempt={client / "shim.py"}
    )
    assert any("sidecar.py" in x and "httpx" in x for x in v)


def test_checker_exempts_shim_and_symdex_for_mcp_httpx(tmp_path):
    client = tmp_path / "client" / "firekeep_client"
    symdex = tmp_path / "symdex" / "src" / "firekeep_symdex"
    _mk(tmp_path, "client/firekeep_client/shim.py", "import mcp\nimport httpx\n")
    _mk(tmp_path, "symdex/src/firekeep_symdex/server.py", "import mcp\nimport httpx\n")
    v = find_violations(
        client_pkg=client, symdex_pkg=symdex, full_exempt={client / "shim.py"}
    )
    assert v == []


def test_checker_ignores_relative_imports(tmp_path):
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/cli.py", "from . import resolver\n")
    v = find_violations(
        client_pkg=client, symdex_pkg=tmp_path / "sx", full_exempt={client / "shim.py"}
    )
    assert v == []


def test_checker_exempts_decision_server_for_mcp_only(tmp_path):
    """SP4 Task 6: decision/server.py may import `mcp` (its FastMCP entrypoint,
    imported lazily inside main()) without tripping RULE 2."""
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/decision/server.py", "import mcp\n")
    v = find_violations(
        client_pkg=client,
        symdex_pkg=tmp_path / "sx",
        full_exempt=frozenset(),
        mcp_only_exempt={client / "decision" / "server.py"},
    )
    assert v == []


def test_checker_still_flags_httpx_in_decision_server(tmp_path):
    """The Task 6 widening relaxes ONLY the `mcp` ban for decision/server.py —
    it must still be flagged for `httpx` (and, by RULE 1, server packages),
    same as any other client module. Proves the exemption isn't a blanket
    per-file skip that would silently also permit httpx there."""
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/decision/server.py", "import mcp\nimport httpx\n")
    v = find_violations(
        client_pkg=client,
        symdex_pkg=tmp_path / "sx",
        full_exempt=frozenset(),
        mcp_only_exempt={client / "decision" / "server.py"},
    )
    assert any("server.py" in x and "'httpx'" in x for x in v)
    assert not any("'mcp'" in x for x in v)


def test_checker_mcp_only_exemption_does_not_cover_unlisted_files(tmp_path):
    """A file NOT in either exemption set is still fully banned from mcp/httpx —
    pins that `mcp_only_exempt` is scoped per-file, not a global mcp allowance."""
    client = tmp_path / "client" / "firekeep_client"
    _mk(tmp_path, "client/firekeep_client/other.py", "import mcp\n")
    v = find_violations(
        client_pkg=client,
        symdex_pkg=tmp_path / "sx",
        full_exempt=frozenset(),
        mcp_only_exempt={client / "decision" / "server.py"},
    )
    assert any("other.py" in x and "'mcp'" in x for x in v)
