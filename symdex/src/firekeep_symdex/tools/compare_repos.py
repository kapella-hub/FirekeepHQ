"""Compare the symbol surface between two indexed repositories."""

import time
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided
from ._utils import resolve_repo


def compare_repos(
    repo_a: str,
    repo_b: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Diff the symbol surface between two indexed repos.

    Compares symbols by (qualified_name, kind) as the identity key.
    Detects symbols only in A, only in B, and symbols present in both
    but with different content_hash (signature/body changed).

    Useful for comparing forks, versions, or related projects.

    Args:
        repo_a: Repository A identifier (owner/repo or just repo name).
        repo_b: Repository B identifier (owner/repo or just repo name).
        storage_path: Custom storage path.

    Returns:
        Dict with only_in_a, only_in_b, modified, unchanged_count, and _meta.
    """
    start = time.perf_counter()

    try:
        owner_a, name_a = resolve_repo(repo_a, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    try:
        owner_b, name_b = resolve_repo(repo_b, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index_a = store.load_index(owner_a, name_a)
    if not index_a:
        return {"error": f"Repository not indexed: {owner_a}/{name_a}"}

    index_b = store.load_index(owner_b, name_b)
    if not index_b:
        return {"error": f"Repository not indexed: {owner_b}/{name_b}"}

    def _symbol_key(sym: dict) -> tuple[str, str]:
        return (sym.get("qualified_name", sym.get("name", "")), sym.get("kind", ""))

    def _symbol_entry(sym: dict) -> dict:
        return {
            "symbol_id": sym.get("id", ""),
            "name": sym.get("name", ""),
            "kind": sym.get("kind", ""),
            "file": sym.get("file", ""),
            "signature": sym.get("signature", ""),
        }

    # Build lookup maps keyed by (qualified_name, kind)
    map_a: dict[tuple[str, str], dict] = {}
    for sym in index_a.symbols:
        key = _symbol_key(sym)
        map_a[key] = sym

    map_b: dict[tuple[str, str], dict] = {}
    for sym in index_b.symbols:
        key = _symbol_key(sym)
        map_b[key] = sym

    keys_a = set(map_a.keys())
    keys_b = set(map_b.keys())

    only_in_a = [_symbol_entry(map_a[k]) for k in sorted(keys_a - keys_b)]
    only_in_b = [_symbol_entry(map_b[k]) for k in sorted(keys_b - keys_a)]

    modified = []
    unchanged_count = 0

    for key in sorted(keys_a & keys_b):
        sym_a = map_a[key]
        sym_b = map_b[key]
        hash_a = sym_a.get("content_hash", "")
        hash_b = sym_b.get("content_hash", "")
        if hash_a and hash_b and hash_a != hash_b:
            modified.append({
                "symbol_a": _symbol_entry(sym_a),
                "symbol_b": _symbol_entry(sym_b),
            })
        else:
            unchanged_count += 1

    # Token savings: comparing two repos at symbol level avoids reading all raw files
    raw_bytes_a = sum(s.get("byte_length", 0) for s in index_a.symbols)
    raw_bytes_b = sum(s.get("byte_length", 0) for s in index_b.symbols)
    raw_bytes = raw_bytes_a + raw_bytes_b
    # Response is compact: just the diff entries
    response_bytes = (
        len(only_in_a) * 100
        + len(only_in_b) * 100
        + len(modified) * 200
    )
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved)

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo_a": f"{owner_a}/{name_a}",
        "repo_b": f"{owner_b}/{name_b}",
        "only_in_a": only_in_a,
        "only_in_b": only_in_b,
        "modified": modified,
        "unchanged_count": unchanged_count,
        "summary": {
            "symbols_a": len(index_a.symbols),
            "symbols_b": len(index_b.symbols),
            "only_in_a": len(only_in_a),
            "only_in_b": len(only_in_b),
            "modified": len(modified),
            "unchanged": unchanged_count,
        },
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }


TOOL_DEF = {
    "name": "compare_repos",
    "description": "Diff the symbol surface between two indexed repositories. Shows symbols only in A, only in B, and modified (same name but different content). Useful for comparing forks, versions, or related projects.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo_a": {
                            "type": "string",
                            "description": "First repository identifier"
                    },
                    "repo_b": {
                            "type": "string",
                            "description": "Second repository identifier"
                    }
            },
            "required": [
                    "repo_a",
                    "repo_b"
            ]
    },
    "handler": compare_repos,
}
