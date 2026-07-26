"""Search symbols across repository."""

import os
import time
from typing import Optional

from ..storage import IndexStore, record_savings, estimate_savings, cost_avoided, score_symbol
from ._utils import resolve_repo


def search_symbols(
    repo: str,
    query: str,
    kind: Optional[str] = None,
    file_pattern: Optional[str] = None,
    language: Optional[str] = None,
    max_results: int = 10,
    storage_path: Optional[str] = None
) -> dict:
    """Search for symbols matching a query.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        query: Search query.
        kind: Optional filter by symbol kind.
        file_pattern: Optional glob pattern to filter files.
        language: Optional filter by language (e.g., "python", "javascript").
        max_results: Maximum results to return.
        storage_path: Custom storage path.

    Returns:
        Dict with search results and _meta envelope.

    Note:
        Results are based on the last full index and may be stale if files
        have been modified since indexing. Use get_file_outline or get_symbol
        for auto-refreshed results on specific files.
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 100))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    # Load index
    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Search
    results = index.search(query, kind=kind, file_pattern=file_pattern)

    # Apply language filter (post-search since CodeIndex.search doesn't support it)
    if language:
        results = [s for s in results if s.get("language") == language]

    # Score and sort (search already does this, but we need to add score to output)
    query_lower = query.lower()
    query_words = set(query_lower.split())

    scored_results = []
    for sym in results[:max_results]:
        score = score_symbol(sym, query_lower, query_words)
        scored_results.append({
            "id": sym["id"],
            "kind": sym["kind"],
            "name": sym["name"],
            "file": sym["file"],
            "line": sym["line"],
            "signature": sym["signature"],
            "summary": sym.get("summary", ""),
            "score": score
        })

    # Token savings: files containing matches vs symbol byte_lengths of results
    raw_bytes = 0
    seen_files: set = set()
    response_bytes = 0
    content_dir = store._content_dir(owner, name)
    for sym in results[:max_results]:
        f = sym["file"]
        if f not in seen_files:
            seen_files.add(f)
            try:
                raw_bytes += os.path.getsize(content_dir / f)
            except OSError:
                pass
        response_bytes += sym.get("byte_length", 0)
    tokens_saved = estimate_savings(raw_bytes, response_bytes)
    total_saved = record_savings(tokens_saved)

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": f"{owner}/{name}",
        "query": query,
        "result_count": len(scored_results),
        "results": scored_results,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "total_symbols": len(index.symbols),
            "truncated": len(results) > max_results,
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }


TOOL_DEF = {
    "name": "search_symbols",
    "description": "Search for symbols matching a query across the entire indexed repository. Returns matches with signatures and summaries.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "query": {
                            "type": "string",
                            "description": "Search query (matches symbol names, signatures, summaries, docstrings)"
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
                    "file_pattern": {
                            "type": "string",
                            "description": "Optional glob pattern to filter files (e.g., 'src/**/*.py')"
                    },
                    "language": {
                            "type": "string",
                            "description": "Optional filter by language",
                            "enum": [
                                    "python",
                                    "javascript",
                                    "typescript",
                                    "go",
                                    "rust",
                                    "java",
                                    "php"
                            ]
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return",
                            "default": 10
                    }
            },
            "required": [
                    "repo",
                    "query"
            ]
    },
    "handler": search_symbols,
}
