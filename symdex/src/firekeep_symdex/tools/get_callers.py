"""Find all call sites that reference a given symbol."""

import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def get_callers(
    repo: str,
    symbol_id: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Find all places that call/reference a given symbol.

    Searches the stored references for calls matching the symbol's name.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol ID to find callers for.
        storage_path: Custom storage path.

    Returns:
        Dict with callers list and metadata.
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

    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return {"error": f"Symbol not found: {symbol_id}"}

    target_name = symbol["name"]
    target_qualified = symbol.get("qualified_name", target_name)
    target_file = symbol["file"]

    callers = []
    # Use per-file get_refs() instead of scanning all references
    for source_file in index.source_files:
        for ref in index.get_refs(source_file, "call"):
            ref_name = ref.get("name", "")
            # Match by simple name, qualified name, or attribute access ending
            if (
                ref_name == target_name
                or ref_name == target_qualified
                or ref_name.endswith(f".{target_name}")
            ):
                # Skip self-references within the same symbol's line range
                ref_file = ref.get("file", source_file)
                ref_line = ref.get("line", 0)
                if ref_file == target_file and symbol["line"] <= ref_line <= symbol["end_line"]:
                    continue

                # Find which symbol contains this call
                containing_symbol = index.find_containing_symbol(ref_file, ref_line)

                callers.append({
                    "file": ref_file,
                    "line": ref_line,
                    "call_expression": ref_name,
                    "from_symbol": containing_symbol,
                })

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "symbol_id": symbol_id,
        "symbol_name": target_name,
        "caller_count": len(callers),
        "callers": callers,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_callers",
    "description": "Find all call sites that reference a given symbol. Shows where a function/method is called from across the indexed codebase.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID to find callers for"
                    }
            },
            "required": [
                    "repo",
                    "symbol_id"
            ]
    },
    "handler": get_callers,
}
