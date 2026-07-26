"""Find recurring structural patterns in the codebase."""

import os
import time
from collections import defaultdict
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def _extract_param_count(signature: str) -> int:
    """Extract parameter count from a signature string."""
    open_idx = signature.find("(")
    close_idx = signature.rfind(")")
    if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
        return 0
    params_str = signature[open_idx + 1:close_idx].strip()
    if not params_str:
        return 0
    params = [p.strip() for p in params_str.split(",") if p.strip()]
    # Exclude self/this/cls
    return len([p for p in params if p.split(":")[0].split("=")[0].strip().split()[-1].strip("*&") not in ("self", "this", "cls")])


def _has_return_type(signature: str) -> bool:
    """Check if a signature declares a return type."""
    # Python: -> Type
    if "->" in signature:
        return True
    # TS/Java: ): Type
    close_paren = signature.rfind(")")
    if close_paren != -1:
        after = signature[close_paren + 1:].strip()
        if after.startswith(":") and len(after) > 1:
            return True
    return False


def _decorator_fingerprint(decorators: list[str]) -> tuple:
    """Create a hashable fingerprint from decorators."""
    return tuple(sorted(decorators))


def _file_directory(file_path: str) -> str:
    """Extract directory from file path."""
    return os.path.dirname(file_path.replace("\\", "/"))


def _describe_pattern(kind: str, param_count: int, has_return: bool, decorators: tuple, directory: str) -> str:
    """Generate a human-readable description of a structural pattern."""
    parts = []
    parts.append(f"{kind}s")
    parts.append(f"with {param_count} parameter(s)")
    if has_return:
        parts.append("with return type")
    else:
        parts.append("without return type")
    if decorators:
        parts.append(f"decorated with {', '.join(decorators)}")
    if directory:
        parts.append(f"in {directory}/")
    return " ".join(parts).capitalize()


def detect_patterns(
    repo: str,
    kind: Optional[str] = None,
    min_group_size: int = 3,
    max_results: int = 10,
    storage_path: Optional[str] = None,
) -> dict:
    """Find recurring structural patterns - groups of symbols following the same template.

    Groups symbols by kind, creates structural fingerprints, and clusters
    symbols with matching fingerprints.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        kind: Filter by symbol kind (e.g., "function", "method", "class").
        min_group_size: Minimum symbols to form a pattern (default 3).
        max_results: Maximum pattern groups to return (default 10).
        storage_path: Custom storage path.

    Returns:
        Dict with pattern groups and _meta envelope.
    """
    start = time.perf_counter()
    min_group_size = max(2, min_group_size)
    max_results = max(1, min(max_results, 50))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    symbols = index.symbols
    if not symbols:
        elapsed = (time.perf_counter() - start) * 1000
        return {"patterns": [], "_meta": {"timing_ms": round(elapsed, 1)}}

    # Group symbols by kind
    by_kind: dict[str, list[dict]] = defaultdict(list)
    for sym in symbols:
        sym_kind = sym.get("kind", "")
        if kind and sym_kind != kind:
            continue
        if sym_kind:
            by_kind[sym_kind].append(sym)

    # Create fingerprints and cluster
    # fingerprint: (kind, param_count, has_return, decorator_tuple, directory)
    clusters: dict[tuple, list[dict]] = defaultdict(list)

    for sym_kind, sym_list in by_kind.items():
        for sym in sym_list:
            sig = sym.get("signature", "")
            decorators = sym.get("decorators", [])
            file_path = sym.get("file", "")

            fp = (
                sym_kind,
                _extract_param_count(sig),
                _has_return_type(sig),
                _decorator_fingerprint(decorators),
                _file_directory(file_path),
            )
            clusters[fp].append(sym)

    # Filter by min_group_size and sort by group size descending
    valid_clusters = [
        (fp, syms) for fp, syms in clusters.items()
        if len(syms) >= min_group_size
    ]
    valid_clusters.sort(key=lambda x: len(x[1]), reverse=True)

    # Build results
    patterns = []
    for fp, syms in valid_clusters[:max_results]:
        sym_kind, param_count, has_return, decorators, directory = fp

        examples = []
        for sym in syms[:5]:
            examples.append({
                "symbol_id": sym["id"],
                "name": sym.get("name", ""),
                "file": sym.get("file", ""),
                "signature": sym.get("signature", ""),
            })

        patterns.append({
            "pattern_name": _describe_pattern(sym_kind, param_count, has_return, decorators, directory),
            "description": _describe_pattern(sym_kind, param_count, has_return, decorators, directory),
            "symbol_count": len(syms),
            "common_traits": {
                "kind": sym_kind,
                "param_count": param_count,
                "has_return_type": has_return,
                "decorators": list(decorators),
                "directory": directory,
            },
            "examples": examples,
        })

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "patterns": patterns,
        "total_patterns": len(patterns),
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "detect_patterns",
    "description": "Find recurring structural patterns in the codebase - groups of symbols that follow the same template (e.g., all API endpoints follow the same structure).",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "kind": {
                            "type": "string",
                            "description": "Filter by symbol kind (e.g. function, method, class)"
                    },
                    "min_group_size": {
                            "type": "integer",
                            "description": "Minimum symbols to form a pattern (default 3)",
                            "default": 3
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Max pattern groups (default 10)",
                            "default": 10
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": detect_patterns,
}
