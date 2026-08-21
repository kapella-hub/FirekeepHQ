"""Smart context budgeting tool."""

import json
import time
from typing import Optional

from ..storage import IndexStore, score_symbol, record_savings, estimate_savings
from ._utils import resolve_repo, resolve_call_targets


def _find_dependency_ids(index, symbol_id: str) -> list[str]:
    """Find symbol IDs of direct dependencies (callees) for a given symbol.

    Looks at call references within the symbol's line range, then resolves
    each callee name to an actual symbol in the index.
    """
    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return []

    target_file = symbol["file"]
    sym_start = symbol["line"]
    sym_end = symbol["end_line"]

    callee_names = set()
    for ref in index.get_refs(target_file, "call"):
        ref_line = ref.get("line", 0)
        if sym_start <= ref_line <= sym_end:
            callee_names.add(ref.get("name", ""))

    # Resolve each callee name with scope awareness
    dep_ids = []
    seen: set[str] = set()
    for callee_name in callee_names:
        targets = resolve_call_targets(index, callee_name, target_file)
        for tid in targets:
            if tid != symbol_id and tid not in seen:
                seen.add(tid)
                dep_ids.append(tid)
                break  # Take best match only

    return dep_ids


def _find_file_imports(index, file_path: str) -> list[str]:
    """Find import symbol IDs for a given file.

    Looks at import references for the file, resolves imported names
    to symbols in other files.
    """
    import_names = set()
    for ref in index.get_refs(file_path, "import"):
        import_names.add(ref.get("name", ""))

    # Resolve import names to symbols (heuristic: match by name or module)
    dep_ids = []
    for sym in index.symbols:
        if sym.get("file") == file_path:
            continue  # skip same file
        name = sym.get("name", "")
        qname = sym.get("qualified_name", "")
        for imp in import_names:
            if name == imp or imp.endswith(f".{name}") or qname == imp:
                dep_ids.append(sym["id"])
                break

    return dep_ids


def get_context(
    repo: str,
    budget_tokens: int = 8000,
    focus: Optional[str] = None,
    kind: Optional[str] = None,
    include_deps: bool = False,
    storage_path: Optional[str] = None,
) -> dict:
    """Get the most relevant symbols that fit within a token budget.

    The budget counts the WHOLE entry — source plus the id/kind/name/file/line/
    signature/summary envelope — not just source bytes. Before 2026-08-21 it
    counted source only and a default call on a 938-file index returned 74,218
    tokens while reporting "99.9% of 4,000"; the same call now returns 4,128
    tokens at a budget it actually respects.

    The default rose 4000 -> 8000 in the same change, and the two belong
    together: correct accounting makes each entry cost more, so holding the old
    default would have quietly halved what a caller got back. Measured on the
    same index, `focus="memory recall"` returned 17 symbols before the fix and
    returns 17 symbols at 8000 after it — the same breadth, now honestly priced.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        budget_tokens: Max tokens to include (default 8000). Counts the entire
            serialized entry, not just source.
        focus: Optional search query to focus context on.
        kind: Optional symbol kind filter.
        include_deps: When True and focus is set, also include direct
            dependencies (callees and imports) of the focused symbols.
        storage_path: Custom storage path.

    Returns:
        Dict with symbols (including source), plus _meta with budget info.
    """
    start = time.perf_counter()
    budget_tokens = max(100, min(budget_tokens, 100_000))

    try:
        owner, name = resolve_repo(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Get candidate symbols
    if focus:
        candidates = index.search(focus, kind=kind)
        # Score and sort by relevance
        query_lower = focus.lower()
        query_words = set(query_lower.split())
        candidates = sorted(
            candidates,
            key=lambda s: score_symbol(s, query_lower, query_words),
            reverse=True,
        )
    else:
        # No focus: use all symbols, prefer smaller ones (more informative per token)
        candidates = list(index.symbols)
        if kind:
            candidates = [s for s in candidates if s.get("kind") == kind]
        candidates.sort(key=lambda s: s.get("byte_length", 0))

    # Helper to detect overlapping byte ranges (e.g. methods inside classes)
    included_ranges: dict[str, list[tuple[int, int]]] = {}  # file -> [(start, end), ...]

    def _is_contained(file_path: str, start: int, end: int) -> bool:
        """Check if byte range [start, end) is entirely within an already-included symbol."""
        for existing_start, existing_end in included_ranges.get(file_path, []):
            if existing_start <= start and end <= existing_end:
                return True
        return False

    def _contained_indices(file_path: str, start: int, end: int) -> list[int]:
        """Find included symbols whose byte ranges are contained within [start, end)."""
        contained = []
        for i, entry in enumerate(symbols_out):
            if entry["file"] != file_path:
                continue
            sym_data = index.get_symbol(entry["id"])
            if not sym_data:
                continue
            s_offset = sym_data.get("byte_offset", 0)
            s_length = sym_data.get("byte_length", 0)
            if s_length > 0 and start <= s_offset and s_offset + s_length <= end:
                contained.append(i)
        return contained

    def _evict_contained(file_path: str, to_remove: list[int]):
        """Remove already-included symbols identified as contained."""
        nonlocal tokens_used, raw_bytes_total
        # Remove in reverse order to preserve indices
        for i in reversed(to_remove):
            evicted = symbols_out.pop(i)
            included_ids.discard(evicted["id"])
            tokens_used -= evicted["estimated_tokens"]
            # Decrement raw_bytes_total to keep savings calculation accurate
            evicted_sym = index.get_symbol(evicted["id"])
            if evicted_sym:
                raw_bytes_total -= evicted_sym.get("byte_length", 0)
        # Clean up ranges for this file (rebuild from remaining symbols)
        if to_remove:
            included_ranges[file_path] = [
                (entry_sym.get("byte_offset", 0), entry_sym.get("byte_offset", 0) + entry_sym.get("byte_length", 0))
                for entry in symbols_out if entry["file"] == file_path
                for entry_sym in [index.get_symbol(entry["id"])]
                if entry_sym
            ]

    def _envelope_tokens(sym, tag=None) -> int:
        """Cost of everything in an entry EXCEPT its source.

        Charged because it is not free: measured on the live 938-file index
        (2026-08-21), a no-focus call returned 681 symbols whose envelopes were
        93.5% of a 261,510-character payload, against a budget that counted only
        `source` and therefore reported 99.9% utilisation of 4,000 tokens.

        Computed from the symbol dict alone, with no content read, so the budget
        check can still happen BEFORE `get_symbol_content` — charging the real
        entry after fetching source would mean one disk read per REJECTED
        candidate, and the smallest-first sort walks thousands of them.

        `estimated_tokens` is measured with a placeholder: its own digit count
        shifts the envelope by a character or two, which is far inside the
        `byte_length // 4` approximation used for source.
        """
        envelope = {
            "id": sym.get("id", ""),
            "kind": sym.get("kind", ""),
            "name": sym.get("name", ""),
            "file": sym.get("file", ""),
            "line": sym.get("line", 0),
            "signature": sym.get("signature", ""),
            "summary": sym.get("summary", ""),
            "source": "",
            "estimated_tokens": 0,
        }
        if tag:
            envelope["context_type"] = tag
        return len(json.dumps(envelope, separators=(",", ":"), default=str)) // 4

    # Helper to add a symbol to the output
    def _try_add(sym, tag=None, force=False):
        nonlocal tokens_used, raw_bytes_total
        byte_length = sym.get("byte_length", 0)
        byte_offset = sym.get("byte_offset", 0)
        # The WHOLE entry, not just its source. `estimated_tokens` is what the
        # eviction path refunds (see _evict_contained), so charging and
        # refunding stay the same quantity.
        estimated_tokens = max(1, byte_length // 4 + _envelope_tokens(sym, tag))
        file_path = sym.get("file", "")

        # Skip if this symbol is entirely contained within an already-included symbol
        # Guard against degenerate zero-offset/zero-length symbols being suppressed
        if byte_length > 0 and _is_contained(file_path, byte_offset, byte_offset + byte_length):
            return False

        # Check parent replacement transactionally. A containing symbol may fit
        # only after evicting selected children, but those children must remain
        # when the parent itself cannot be admitted.
        to_evict = []
        evictable_tokens = 0
        if byte_length > 0:
            to_evict = _contained_indices(
                file_path, byte_offset, byte_offset + byte_length
            )
            evictable_tokens = sum(
                symbols_out[i]["estimated_tokens"] for i in to_evict
            )

        if not force and tokens_used - evictable_tokens + estimated_tokens > budget_tokens:
            return False

        source = store.get_symbol_content(owner, name, sym["id"])
        if source is None:
            return False

        if to_evict:
            _evict_contained(file_path, to_evict)

        tokens_used += estimated_tokens
        raw_bytes_total += byte_length

        # Track the byte range
        included_ranges.setdefault(file_path, []).append((byte_offset, byte_offset + byte_length))

        entry = {
            "id": sym["id"],
            "kind": sym["kind"],
            "name": sym["name"],
            "file": sym["file"],
            "line": sym["line"],
            "signature": sym["signature"],
            "summary": sym.get("summary", ""),
            "source": source,
            "estimated_tokens": estimated_tokens,
        }
        if tag:
            entry["context_type"] = tag
        symbols_out.append(entry)
        return True

    # Greedily fill budget
    symbols_out = []
    tokens_used = 0
    raw_bytes_total = 0
    included_ids = set()
    deps_included = 0

    for sym in candidates:
        if sym["id"] in included_ids:
            continue
        if _try_add(sym):
            included_ids.add(sym["id"])

            # When include_deps is on, also add direct dependencies
            if include_deps and focus:
                dep_ids = _find_dependency_ids(index, sym["id"])
                dep_ids += _find_file_imports(index, sym["file"])
                for dep_id in dep_ids:
                    if dep_id in included_ids:
                        continue
                    dep_sym = index.get_symbol(dep_id)
                    if dep_sym and _try_add(dep_sym, tag="dependency"):
                        included_ids.add(dep_id)
                        deps_included += 1

    # Floor: never return an EMPTY result merely because the budget was too
    # small for one entry. Charging the envelope made that reachable — a symbol
    # whose source alone used to fit exactly no longer does — and an empty
    # `symbols` list reads to an agent as "this repo has no relevant code",
    # which is a false negative, not a saving. False negatives on "does this
    # already exist" are the one failure mode a code index must never produce.
    #
    # The first candidate is the right one to force: relevance-sorted under
    # `focus`, smallest-first without it. The overshoot is DISCLOSED rather than
    # hidden, so a caller that genuinely cannot afford it can see what happened
    # and narrow instead of guessing.
    budget_floor_applied = False
    if not symbols_out:
        for sym in candidates:
            if _try_add(sym, force=True):
                included_ids.add(sym["id"])
                budget_floor_applied = True
                break

    # Token savings
    tokens_saved = estimate_savings(raw_bytes_total, tokens_used * 4)
    record_savings(tokens_saved)

    elapsed = (time.perf_counter() - start) * 1000

    meta = {
        "timing_ms": round(elapsed, 1),
        "tokens_used": tokens_used,
        "tokens_budget": budget_tokens,
        "budget_utilization": round(tokens_used / budget_tokens * 100, 1) if budget_tokens else 0,
        "total_symbols_available": len(index.symbols),
    }
    if budget_floor_applied:
        # Say so, and say what to do about it.
        meta["budget_floor_applied"] = True
        meta["note"] = (
            "budget_tokens was too small for any single symbol; returned the "
            "best-matching one anyway so this is not mistaken for 'no results'. "
            "Raise budget_tokens or narrow with focus=."
        )
    if include_deps:
        meta["deps_included"] = deps_included

    return {
        "repo": f"{owner}/{name}",
        "symbols_included": len(symbols_out),
        "symbols": symbols_out,
        "_meta": meta,
    }


TOOL_DEF = {
    "name": "get_context",
    "description": "Get the most relevant symbols that fit within a token budget. Returns symbol source code, greedily filling the budget by relevance (if focus query given) or by size (smallest first). With include_deps=true, also includes direct dependencies (callees and imports) of focused symbols.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "budget_tokens": {
                            "type": "integer",
                            "description": "Max tokens to include (default 8000). Counts the whole entry — source plus signature/summary/metadata — not just source bytes",
                            "default": 8000
                    },
                    "focus": {
                            "type": "string",
                            "description": "Optional search query to focus context on"
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
                    "include_deps": {
                            "type": "boolean",
                            "description": "When true and focus is set, also include direct dependencies (callees and imports) of the focused symbols within the budget",
                            "default": False
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_context,
}
