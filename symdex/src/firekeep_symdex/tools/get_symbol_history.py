"""Track symbol changes over time using stored history snapshots."""

import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def get_symbol_history(
    repo: str,
    symbol_id: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Get the change history for a specific symbol.

    Returns timestamps when the symbol's content hash changed and any
    signature differences between versions.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol ID to look up history for.
        storage_path: Custom storage path.

    Returns:
        Dict with history entries and metadata.
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

    # Look up the symbol in the current index
    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return {"error": f"Symbol not found: {symbol_id}"}

    # Load history
    all_history = store.load_history(owner, name)
    entries = all_history.get(symbol_id, [])

    elapsed = (time.perf_counter() - start) * 1000

    if not entries:
        # No history file yet -- return current state with a note
        return {
            "symbol_id": symbol_id,
            "symbol_name": symbol.get("name", ""),
            "current_state": {
                "content_hash": symbol.get("content_hash", ""),
                "signature": symbol.get("signature", ""),
                "file": symbol.get("file", ""),
                "line": symbol.get("line", 0),
            },
            "history": [],
            "change_count": 0,
            "note": "No history recorded yet. History tracking starts from next re-index.",
            "_meta": {"timing_ms": round(elapsed, 1)},
        }

    # Build enriched history with diffs between consecutive entries
    enriched = []
    for i, entry in enumerate(entries):
        enriched_entry = {
            "timestamp": entry["timestamp"],
            "content_hash": entry["content_hash"],
            "signature": entry["signature"],
        }

        if i > 0:
            prev = entries[i - 1]
            if prev["signature"] != entry["signature"]:
                enriched_entry["signature_changed_from"] = prev["signature"]

        enriched.append(enriched_entry)

    return {
        "symbol_id": symbol_id,
        "symbol_name": symbol.get("name", ""),
        "current_state": {
            "content_hash": symbol.get("content_hash", ""),
            "signature": symbol.get("signature", ""),
            "file": symbol.get("file", ""),
            "line": symbol.get("line", 0),
        },
        "history": enriched,
        "change_count": len(enriched),
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_symbol_history",
    "description": "Get the change history for a specific symbol across re-indexes. Shows when the symbol's content hash or signature changed.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID to get history for"
                    }
            },
            "required": [
                    "repo",
                    "symbol_id"
            ]
    },
    "handler": get_symbol_history,
}
