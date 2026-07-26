"""Find symbols with similar signatures or structure."""

import os
import re
import time
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


# Split camelCase and snake_case into tokens
_TOKEN_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]+|[a-z]+|\d+")


def _tokenize_name(name: str) -> set[str]:
    """Split a symbol name into lowercase tokens.

    Handles camelCase, PascalCase, snake_case, and UPPER_CASE.
    """
    return {t.lower() for t in _TOKEN_RE.findall(name)}


def _extract_params(signature: str) -> list[str]:
    """Extract parameter names from a function/method signature.

    Looks for text inside the first set of parentheses, then splits
    on commas and extracts the parameter name (first identifier).
    """
    # Find content within first parens
    open_idx = signature.find("(")
    close_idx = signature.rfind(")")
    if open_idx == -1 or close_idx == -1 or close_idx <= open_idx:
        return []

    params_str = signature[open_idx + 1:close_idx].strip()
    if not params_str:
        return []

    params = []
    for part in params_str.split(","):
        part = part.strip()
        if not part:
            continue
        # Skip self/this/cls
        token = part.split(":")[0].split("=")[0].strip()
        # Handle "type name" patterns (e.g., "int x" in Java/C)
        tokens = token.split()
        if tokens:
            name = tokens[-1].strip("*&")  # strip pointer/ref markers
            if name and name not in ("self", "this", "cls"):
                params.append(name.lower())
    return params


def _extract_return_type(signature: str) -> str:
    """Extract a return type hint from a signature, if present.

    Handles Python (-> Type) and typed languages (: Type after closing paren).
    """
    # Python-style: def foo(...) -> ReturnType
    arrow_idx = signature.find("->")
    if arrow_idx != -1:
        ret = signature[arrow_idx + 2:].strip().rstrip(":{")
        return ret.strip().lower()

    # TypeScript-style: function foo(...): ReturnType
    close_paren = signature.rfind(")")
    if close_paren != -1:
        after = signature[close_paren + 1:].strip()
        if after.startswith(":"):
            ret = after[1:].strip().rstrip("{").strip()
            return ret.lower()

    return ""


def get_similar_symbols(
    repo: str,
    symbol_id: str,
    max_results: int = 10,
    storage_path: Optional[str] = None,
) -> dict:
    """Find symbols with similar signatures or structure.

    Scores similarity based on:
    - Same kind (required filter)
    - Parameter count similarity
    - Parameter name overlap
    - Name token overlap (camelCase/snake_case split)
    - Same file directory bonus
    - Return type similarity

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Symbol ID to find similar symbols for.
        max_results: Maximum results to return (default 10).
        storage_path: Custom storage path.

    Returns:
        Dict with ranked similar symbols, similarity scores, and metadata.
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

    symbol = index.get_symbol(symbol_id)
    if not symbol:
        return {"error": f"Symbol not found: {symbol_id}"}

    target_kind = symbol["kind"]
    target_sig = symbol.get("signature", "")
    target_name_tokens = _tokenize_name(symbol["name"])
    target_params = _extract_params(target_sig)
    target_param_count = len(target_params)
    target_param_set = set(target_params)
    target_return = _extract_return_type(target_sig)
    target_dir = os.path.dirname(symbol.get("file", ""))

    scored = []

    for sym in index.symbols:
        # Required: same kind
        if sym.get("kind") != target_kind:
            continue
        # Exclude self
        if sym["id"] == symbol_id:
            continue

        score = 0.0
        reasons = []

        sig = sym.get("signature", "")

        # 1. Parameter count similarity (max 25 points)
        params = _extract_params(sig)
        param_count = len(params)
        if target_param_count == 0 and param_count == 0:
            score += 25
            reasons.append("same_param_count")
        elif target_param_count > 0 or param_count > 0:
            max_count = max(target_param_count, param_count)
            diff = abs(target_param_count - param_count)
            param_score = max(0, 25 - (diff / max_count) * 25)
            if param_score > 0:
                score += param_score
                if diff == 0:
                    reasons.append("same_param_count")
                else:
                    reasons.append("similar_param_count")

        # 2. Parameter name overlap (max 25 points)
        param_set = set(params)
        if target_param_set and param_set:
            overlap = len(target_param_set & param_set)
            total = len(target_param_set | param_set)
            if total > 0:
                param_name_score = (overlap / total) * 25
                if param_name_score > 0:
                    score += param_name_score
                    reasons.append("param_name_overlap")

        # 3. Name token overlap (max 25 points)
        name_tokens = _tokenize_name(sym.get("name", ""))
        if target_name_tokens and name_tokens:
            overlap = len(target_name_tokens & name_tokens)
            total = len(target_name_tokens | name_tokens)
            if total > 0:
                name_score = (overlap / total) * 25
                if name_score > 0:
                    score += name_score
                    reasons.append("name_token_overlap")

        # 4. Same directory bonus (5 points)
        sym_dir = os.path.dirname(sym.get("file", ""))
        if target_dir and sym_dir == target_dir:
            score += 5
            reasons.append("same_directory")

        # 5. Return type similarity (max 20 points)
        sym_return = _extract_return_type(sig)
        if target_return and sym_return:
            if target_return == sym_return:
                score += 20
                reasons.append("same_return_type")
            else:
                # Partial match on return type tokens
                ret_target_tokens = _tokenize_name(target_return)
                ret_sym_tokens = _tokenize_name(sym_return)
                if ret_target_tokens and ret_sym_tokens:
                    overlap = len(ret_target_tokens & ret_sym_tokens)
                    total = len(ret_target_tokens | ret_sym_tokens)
                    if total > 0 and overlap > 0:
                        ret_score = (overlap / total) * 10
                        score += ret_score
                        reasons.append("similar_return_type")

        if score > 0:
            scored.append((score, sym, reasons))

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)

    # Take top results
    results = []
    for score_val, sym, reasons in scored[:max_results]:
        results.append({
            "symbol_id": sym["id"],
            "name": sym["name"],
            "qualified_name": sym.get("qualified_name", sym["name"]),
            "kind": sym["kind"],
            "file": sym.get("file", ""),
            "line": sym.get("line", 0),
            "signature": sym.get("signature", ""),
            "similarity_score": round(score_val, 1),
            "match_reasons": reasons,
        })

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "symbol_id": symbol_id,
        "symbol_name": symbol["name"],
        "similar_count": len(results),
        "similar_symbols": results,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_similar_symbols",
    "description": "Find symbols with similar signatures or structure. Useful for detecting near-duplicates, finding related implementations, or identifying refactoring candidates.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID to find similar symbols for"
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Maximum results (default 10)",
                            "default": 10
                    }
            },
            "required": [
                    "repo",
                    "symbol_id"
            ]
    },
    "handler": get_similar_symbols,
}
