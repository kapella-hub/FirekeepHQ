"""Compute complexity metrics for symbols from source code."""

import re
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo
from .get_similar_symbols import _extract_params


# Patterns that contribute to cyclomatic complexity
_BRANCH_PATTERN = re.compile(
    r"\b(?:if|else|elif|for|while|try|catch|except|case|match|switch)\b"
    r"|&&|\|\|"
)


def _compute_nesting_depth(source: str) -> int:
    """Compute maximum nesting depth from source code.

    Uses indentation for Python-style languages and brace counting
    for C-style languages.
    """
    max_depth = 0

    # Detect style: if source has significant braces, use brace counting
    brace_count = source.count("{")
    if brace_count > 2:
        # Brace-based nesting
        depth = 0
        for ch in source:
            if ch == "{":
                depth += 1
                max_depth = max(max_depth, depth)
            elif ch == "}":
                depth = max(0, depth - 1)
    else:
        # Indentation-based nesting
        base_indent = None
        for line in source.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or stripped.startswith("//"):
                continue
            indent = len(line) - len(stripped)
            if base_indent is None:
                base_indent = indent
            relative = indent - base_indent
            # Assume 4-space or 2-space indentation
            depth = relative // 4 if relative >= 4 else relative // 2
            max_depth = max(max_depth, depth)

    return max_depth


def _compute_cyclomatic(source: str) -> int:
    """Approximate cyclomatic complexity from source text.

    Counts branching keywords and logical operators, adds 1 for the base path.
    """
    return len(_BRANCH_PATTERN.findall(source)) + 1


def _risk_level(complexity: int) -> str:
    """Classify risk based on complexity score."""
    if complexity >= 20:
        return "high"
    elif complexity >= 10:
        return "medium"
    return "low"


def get_complexity_metrics(
    repo: str,
    symbol_id: Optional[str] = None,
    file_path: Optional[str] = None,
    kind: Optional[str] = None,
    sort_by: str = "complexity",
    max_results: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Compute complexity metrics for symbols.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Specific symbol to analyze.
        file_path: Analyze all symbols in this file.
        kind: Filter by symbol kind (e.g. "function", "method").
        sort_by: Sort field - "complexity", "lines", "nesting" (default "complexity").
        max_results: Maximum results (default 20).
        storage_path: Custom storage path.

    Returns:
        Dict with complexity metrics per symbol.
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 100))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Determine which symbols to analyze
    if symbol_id:
        symbol = index.get_symbol(symbol_id)
        if not symbol:
            return {"error": f"Symbol not found: {symbol_id}"}
        symbols = [symbol]
    else:
        symbols = index.symbols
        if file_path:
            symbols = [s for s in symbols if s.get("file") == file_path]
        if kind:
            symbols = [s for s in symbols if s.get("kind") == kind]

    # Compute metrics for each symbol
    results = []
    for sym in symbols:
        source = store.get_symbol_content(owner, name, sym["id"])
        if not source:
            continue

        lines = source.count("\n") + 1
        nesting = _compute_nesting_depth(source)
        sig = sym.get("signature", "")
        param_count = len(_extract_params(sig))
        complexity = _compute_cyclomatic(source)
        byte_length = sym.get("byte_length", len(source.encode("utf-8")))

        results.append({
            "symbol_id": sym["id"],
            "name": sym.get("name", ""),
            "file": sym.get("file", ""),
            "kind": sym.get("kind", ""),
            "lines": lines,
            "nesting_depth": nesting,
            "param_count": param_count,
            "complexity_score": complexity,
            "byte_length": byte_length,
            "risk_level": _risk_level(complexity),
        })

    # Sort
    sort_keys = {
        "complexity": lambda r: -r["complexity_score"],
        "lines": lambda r: -r["lines"],
        "nesting": lambda r: -r["nesting_depth"],
    }
    sort_fn = sort_keys.get(sort_by, sort_keys["complexity"])
    results.sort(key=sort_fn)
    results = results[:max_results]

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "result_count": len(results),
        "results": results,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_complexity_metrics",
    "description": "Compute complexity metrics for symbols: line count, nesting depth, parameter count, and cyclomatic complexity approximation. Identifies high-risk complex code.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Specific symbol to analyze"
                    },
                    "file_path": {
                            "type": "string",
                            "description": "Analyze all symbols in this file"
                    },
                    "kind": {
                            "type": "string",
                            "description": "Filter by symbol kind (e.g. function, method)"
                    },
                    "sort_by": {
                            "type": "string",
                            "description": "Sort field",
                            "enum": [
                                    "complexity",
                                    "lines",
                                    "nesting"
                            ],
                            "default": "complexity"
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Max results (default 20)",
                            "default": 20
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_complexity_metrics,
}
