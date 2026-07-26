"""Find symbols that are never referenced from anywhere else in the codebase."""

import re
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo

# Entry-point names that should not be flagged as dead code
_ENTRY_POINT_NAMES = frozenset({
    "main", "__init__", "__main__", "setup", "teardown",
})

# Decorator patterns that indicate a symbol is an endpoint/hook
_LIVE_DECORATOR_PATTERNS = re.compile(
    r"@(app\.route|router\.|pytest\.|override)"
)

# File path patterns that indicate test files
_TEST_FILE_PATTERNS = re.compile(
    r"(^|/)tests?/|(^|/)specs?/|/test_[^/]+$|_test\.[^/]+$|\.spec\.[^/]+$",
    re.IGNORECASE,
)


def find_dead_code(
    repo: str,
    include_tests: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Find symbols that are never referenced (called/imported) elsewhere.

    Scans all symbols in the index and checks whether each one appears
    in the references list.  Excludes common entry points (main, __init__,
    test functions, decorated endpoints) to reduce false positives.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        include_tests: When True, include symbols from test files in results.
        storage_path: Custom storage path.

    Returns:
        Dict with dead_symbols list and metadata.
    """
    start = time.perf_counter()

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Build set of all referenced symbol names (call AND import references)
    referenced_names: set[str] = set()
    for ref in index.references:
        ref_type = ref.get("type")
        if ref_type not in ("call", "import"):
            continue
        ref_name = ref.get("name", "")
        referenced_names.add(ref_name)
        # Also add the bare name for dotted references like "obj.method"
        # or "module.ClassName"
        if "." in ref_name:
            referenced_names.add(ref_name.rsplit(".", 1)[-1])

    dead_symbols = []
    for sym in index.symbols:
        sym_name = sym.get("name", "")
        sym_file = sym.get("file", "")
        sym_kind = sym.get("kind", "")

        # Skip test-file symbols unless include_tests is set
        if not include_tests and _TEST_FILE_PATTERNS.search(sym_file):
            continue

        # Skip if the symbol is referenced
        if sym_name in referenced_names:
            continue

        # Skip well-known entry points
        if sym_name in _ENTRY_POINT_NAMES:
            continue

        # Skip test functions/classes
        if sym_name.startswith("test_") or sym_name.startswith("Test"):
            continue

        # Skip symbols with live-indicating decorators
        decorators = sym.get("decorators", [])
        if any(_LIVE_DECORATOR_PATTERNS.search(d) for d in decorators):
            continue

        # Skip module preambles and route registrations (inherently entry points)
        if sym_kind in ("module", "route"):
            continue

        # Skip exported symbols
        qualified = sym.get("qualified_name", "")
        if qualified.startswith("exports.") or qualified.startswith("module.exports"):
            continue

        # Skip all dunder methods — they are invoked implicitly by
        # the runtime (e.g. __str__, __repr__, __eq__, __hash__,
        # __len__, __iter__, __enter__, __exit__, etc.) and will
        # never appear in explicit call references.
        if sym_name.startswith("__") and sym_name.endswith("__"):
            continue

        dead_symbols.append({
            "name": sym_name,
            "file": sym_file,
            "line": sym.get("line", 0),
            "kind": sym_kind,
            "qualified_name": qualified,
        })

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": f"{owner}/{name}",
        "dead_count": len(dead_symbols),
        "dead_symbols": dead_symbols,
        "total_symbols": len(index.symbols),
        "include_tests": include_tests,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "find_dead_code",
    "description": "Find symbols that are never referenced (called/imported) from anywhere else in the codebase. Detects potential dead code. Excludes common entry points (main, test functions, decorated endpoints) to reduce false positives.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "include_tests": {
                            "type": "boolean",
                            "description": "When true, include symbols from test files in results",
                            "default": False
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": find_dead_code,
}
