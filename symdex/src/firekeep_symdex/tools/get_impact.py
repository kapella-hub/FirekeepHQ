"""Transitive impact analysis: if a symbol changes, what else might break?"""

import time
from collections import deque
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def get_impact(
    repo: str,
    symbol_id: str,
    max_depth: int = 5,
    storage_path: Optional[str] = None,
) -> dict:
    """Compute the transitive set of symbols impacted by changing a symbol.

    Uses BFS through the caller graph: for the target symbol, find all direct
    callers, then find the containing symbols for those call sites, then find
    *their* callers, and so on up to ``max_depth`` levels.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol ID to analyse impact for.
        max_depth: How many levels of callers to traverse (clamped to 1..10).
        storage_path: Custom storage path.

    Returns:
        Dict with impact tree, impacted file list, and metadata.
    """
    start = time.perf_counter()

    max_depth = max(1, min(10, max_depth))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return {"error": f"Symbol not found: {symbol_id}"}

    # BFS state
    visited: set[str] = {symbol_id}
    queue: deque[tuple[str, int]] = deque()  # (symbol_id, depth)
    queue.append((symbol_id, 0))
    impact_tree: list[dict] = []
    actual_max_depth = 0

    while queue:
        current_id, depth = queue.popleft()

        if depth >= max_depth:
            continue

        # Find direct callers of the current symbol
        caller_symbol_ids = _find_caller_symbol_ids(index, current_id)

        for caller_id in caller_symbol_ids:
            if caller_id in visited:
                continue
            visited.add(caller_id)

            caller_sym = index.get_symbol(caller_id)
            if not caller_sym:
                continue

            next_depth = depth + 1
            if next_depth > actual_max_depth:
                actual_max_depth = next_depth

            impact_tree.append({
                "symbol_id": caller_id,
                "name": caller_sym["name"],
                "file": caller_sym["file"],
                "line": caller_sym["line"],
                "depth": next_depth,
            })

            queue.append((caller_id, next_depth))

    impacted_files = sorted({entry["file"] for entry in impact_tree})

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "symbol_id": symbol_id,
        "symbol_name": symbol["name"],
        "total_impacted": len(impact_tree),
        "max_depth_reached": actual_max_depth,
        "impact_tree": impact_tree,
        "impacted_files": impacted_files,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


def _find_caller_symbol_ids(index, symbol_id: str) -> list[str]:
    """Find the IDs of symbols that directly call the given symbol.

    Uses the same matching logic as get_callers: match references by simple
    name, qualified name, or ``.name`` suffix, skipping self-references.
    """
    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return []

    target_name = symbol["name"]
    target_qualified = symbol.get("qualified_name", target_name)
    target_file = symbol["file"]

    seen: set[str] = set()
    result: list[str] = []

    for ref in index.references:
        if ref.get("type") != "call":
            continue

        ref_name = ref.get("name", "")
        if not (
            ref_name == target_name
            or ref_name == target_qualified
            or ref_name.endswith(f".{target_name}")
        ):
            continue

        ref_file = ref.get("file", "")
        ref_line = ref.get("line", 0)

        # Skip self-references within the same symbol's line range
        if ref_file == target_file and symbol["line"] <= ref_line <= symbol["end_line"]:
            continue

        containing_id = index.find_containing_symbol(ref_file, ref_line)
        if containing_id and containing_id not in seen:
            seen.add(containing_id)
            result.append(containing_id)

    return result


TOOL_DEF = {
    "name": "get_impact",
    "description": "Transitive impact analysis: if you change a symbol, what else might break? BFS through the caller graph up to max_depth levels.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID to analyse impact for"
                    },
                    "max_depth": {
                            "type": "integer",
                            "description": "Maximum depth of caller traversal (1-10, default 5)",
                            "default": 5
                    }
            },
            "required": [
                    "repo",
                    "symbol_id"
            ]
    },
    "handler": get_impact,
}
