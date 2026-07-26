"""Suggest symbols relevant to a natural language task description."""

import os
import re
import time
from collections import Counter
from typing import Optional

from ..storage import IndexStore, score_symbol, record_savings, estimate_savings, cost_avoided
from ._utils import resolve_repo


# Words to strip from task descriptions before keyword extraction
_STOP_WORDS = frozenset({
    "a", "an", "the", "to", "in", "on", "of", "for", "is", "it", "and", "or",
    "with", "that", "this", "from", "by", "at", "be", "as", "do", "if", "so",
    "all", "but", "not", "are", "was", "were", "been", "have", "has", "had",
    "will", "would", "should", "could", "can", "may", "might", "into", "some",
    "its", "they", "them", "we", "our", "you", "your", "i", "my", "me", "he",
    "she", "his", "her",
})

# Task words that hint at architecture-level work (classes matter more)
_ARCHITECTURE_WORDS = frozenset({
    "architecture", "refactor", "restructure", "redesign", "organize",
    "abstract", "interface", "inheritance", "hierarchy", "pattern",
})


def _tokenize_task(task: str) -> list[str]:
    """Extract meaningful keywords from a task description."""
    # Split on non-alphanumeric, lowercase
    raw = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", task.lower())
    # Also split camelCase/snake_case tokens into sub-words
    words = []
    for token in raw:
        # Split snake_case
        parts = token.split("_")
        for part in parts:
            # Split camelCase
            sub = re.findall(r"[a-z]+|[A-Z][a-z]*", part)
            words.extend(sub)
        words.append(token)  # Keep the original token too
    return [w for w in words if w not in _STOP_WORDS and len(w) > 1]


def _is_architecture_task(keywords: list[str]) -> bool:
    """Check if the task description implies architecture-level work."""
    return bool(set(keywords) & _ARCHITECTURE_WORDS)


def suggest_symbols(
    repo: str,
    task: str,
    max_results: int = 15,
    storage_path: Optional[str] = None,
) -> dict:
    """Suggest symbols relevant to a natural language task.

    Tokenizes the task into keywords and scores each symbol using multiple
    signals: name/signature match, file path relevance, symbol kind weighting,
    and caller count as an importance signal.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        task: Natural language description (e.g. "add rate limiting to the API").
        max_results: Maximum results to return (clamped to 1-100).
        storage_path: Custom storage path.

    Returns:
        Dict with ranked symbols and _meta envelope.
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

    keywords = _tokenize_task(task)
    if not keywords:
        return {"error": "Could not extract keywords from task description"}

    keyword_set = set(keywords)
    is_arch = _is_architecture_task(keywords)

    # Pre-compute caller counts for all symbol names (avoid O(n*m) per symbol)
    caller_counts: Counter[str] = Counter()
    for ref in index.references:
        if ref.get("type") == "call":
            ref_name = ref.get("name", "")
            # Index by bare name (after last dot)
            bare = ref_name.rsplit(".", 1)[-1] if "." in ref_name else ref_name
            caller_counts[bare] += 1

    scored: list[tuple[float, dict, str]] = []

    for sym in index.symbols:
        total_score = 0.0
        reasons = []

        sym_name = sym.get("name", "")
        sym_kind = sym.get("kind", "")
        sym_file = sym.get("file", "")

        # --- Signal 1: Name/signature match via score_symbol ---
        query_str = " ".join(keywords)
        base_score = score_symbol(sym, query_str, keyword_set)
        if base_score > 0:
            total_score += base_score
            reasons.append(f"name/signature match (score {base_score})")

        # --- Signal 2: File path relevance ---
        file_lower = sym_file.lower()
        path_hits = sum(1 for kw in keyword_set if kw in file_lower)
        if path_hits:
            path_bonus = path_hits * 3
            total_score += path_bonus
            reasons.append(f"file path contains {path_hits} keyword(s)")

        # --- Signal 3: Symbol kind weighting ---
        if is_arch:
            kind_weights = {"class": 4, "type": 3, "function": 2, "method": 1}
        else:
            kind_weights = {"function": 3, "method": 3, "class": 1, "type": 1}
        kind_bonus = kind_weights.get(sym_kind, 0)
        if kind_bonus:
            total_score += kind_bonus

        # --- Signal 4: Caller count as importance signal ---
        callers = caller_counts.get(sym_name, 0)
        if callers > 0:
            # Logarithmic scaling so heavily-called symbols don't dominate
            import math
            caller_bonus = round(math.log2(callers + 1) * 2, 1)
            total_score += caller_bonus
            reasons.append(f"{callers} caller(s)")

        if total_score > 0:
            reason_str = "; ".join(reasons) if reasons else "kind weight"
            scored.append((total_score, sym, reason_str))

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_results]

    results = []
    for score_val, sym, reason in top:
        results.append({
            "id": sym["id"],
            "kind": sym["kind"],
            "name": sym["name"],
            "file": sym["file"],
            "line": sym["line"],
            "signature": sym["signature"],
            "score": round(score_val, 1),
            "relevance_reason": reason,
        })

    # Token savings
    raw_bytes = 0
    seen_files: set[str] = set()
    response_bytes = 0
    content_dir = store._content_dir(owner, name)
    for _, sym, _ in top:
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
        "task": task,
        "keywords": keywords,
        "result_count": len(results),
        "results": results,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "total_symbols": len(index.symbols),
            "truncated": len(scored) > max_results,
            "tokens_saved": tokens_saved,
            "total_tokens_saved": total_saved,
            **cost_avoided(tokens_saved, total_saved),
        },
    }


TOOL_DEF = {
    "name": "suggest_symbols",
    "description": "Given a natural language task description, return the most relevant symbols to read or modify. Combines search relevance, file path heuristics, symbol kind weighting, and caller-count importance.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "task": {
                            "type": "string",
                            "description": "Natural language task description (e.g., 'add rate limiting to the API')"
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Maximum symbols to suggest (default 15)",
                            "default": 15
                    }
            },
            "required": [
                    "repo",
                    "task"
            ]
    },
    "handler": suggest_symbols,
}
