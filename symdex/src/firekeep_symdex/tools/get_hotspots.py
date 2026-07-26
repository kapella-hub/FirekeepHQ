"""Rank symbols by how many callers/references they have."""

import time
from collections import Counter
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def get_hotspots(
    repo: str,
    kind: Optional[str] = None,
    min_callers: int = 2,
    max_results: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Find the most-depended-on symbols in a repository.

    Counts how many call references point to each symbol and returns them
    sorted by caller count descending.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        kind: Optional filter by symbol kind (e.g. "function", "method").
        min_callers: Minimum caller count to include (default 2).
        max_results: Maximum results to return (clamped to 1-100).
        storage_path: Custom storage path.

    Returns:
        Dict with ranked hotspots and _meta envelope.
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 100))
    min_callers = max(0, min_callers)

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Count call references by bare name and by qualified name
    # A reference "obj.method" should count toward symbol named "method"
    caller_counts: Counter[str] = Counter()
    for ref in index.references:
        if ref.get("type") != "call":
            continue
        ref_name = ref.get("name", "")
        # Count for both the full name and the bare name
        caller_counts[ref_name] += 1
        if "." in ref_name:
            bare = ref_name.rsplit(".", 1)[-1]
            caller_counts[bare] += 1

    # Score each symbol
    hotspots = []
    for sym in index.symbols:
        if kind and sym.get("kind") != kind:
            continue

        sym_name = sym.get("name", "")
        sym_qualified = sym.get("qualified_name", sym_name)

        # Take the max of bare name and qualified name counts
        count = max(
            caller_counts.get(sym_name, 0),
            caller_counts.get(sym_qualified, 0),
        )

        if count < min_callers:
            continue

        hotspots.append({
            "symbol_id": sym["id"],
            "name": sym_name,
            "file": sym["file"],
            "caller_count": count,
            "kind": sym.get("kind", ""),
            "signature": sym.get("signature", ""),
        })

    # Sort by caller count descending, then by name for stability
    hotspots.sort(key=lambda h: (-h["caller_count"], h["name"]))
    hotspots = hotspots[:max_results]

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": f"{owner}/{name}",
        "result_count": len(hotspots),
        "results": hotspots,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "total_symbols": len(index.symbols),
        },
    }


TOOL_DEF = {
    "name": "get_hotspots",
    "description": "Rank symbols by how many callers reference them. Identifies the most-depended-on code \u2014 high-risk areas for changes.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "kind": {
                            "type": "string",
                            "description": "Optional filter by symbol kind",
                            "enum": [
                                    "function",
                                    "class",
                                    "method",
                                    "constant",
                                    "type"
                            ]
                    },
                    "min_callers": {
                            "type": "integer",
                            "description": "Minimum caller count to include (default 2)",
                            "default": 2
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Maximum results (default 20)",
                            "default": 20
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_hotspots,
}
