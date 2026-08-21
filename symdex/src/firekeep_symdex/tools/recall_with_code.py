"""Recall memories from FirekeepCortex cross-referenced with code context."""

import re
import time
from collections import Counter
from typing import Optional

from ..cortex import get_cortex_client
from .get_context import get_context

# Words shorter than this are ignored when extracting keywords from memories.
_MIN_KEYWORD_LEN = 4
# Maximum number of keywords to extract from memory content.
_MAX_KEYWORDS = 12


def _extract_keywords(text: str, max_keywords: int = _MAX_KEYWORDS) -> list[str]:
    """Extract the most frequent meaningful words from *text*.

    Filters out short words, common markdown artifacts, and stopwords.
    Returns up to *max_keywords* terms ordered by frequency.
    """
    stopwords = {
        "this", "that", "with", "from", "have", "will", "been", "were",
        "they", "their", "about", "would", "could", "should", "which",
        "when", "what", "there", "into", "also", "than", "some", "more",
        "other", "after", "before", "between", "each", "most", "such",
        "only", "over", "does", "then", "them", "very", "just", "because",
        "through", "using", "used",
    }
    # Tokenise on non-alphanumeric boundaries
    words = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", text.lower())
    counts: Counter[str] = Counter()
    for w in words:
        if len(w) >= _MIN_KEYWORD_LEN and w not in stopwords:
            counts[w] += 1
    return [word for word, _ in counts.most_common(max_keywords)]


def _build_cross_references(
    symbols: list[dict],
    context_block: str,
) -> list[dict]:
    """Find symbols whose names appear in the cortex *context_block*."""
    if not context_block:
        return []

    text_lower = context_block.lower()
    refs = []
    seen: set[str] = set()
    for sym in symbols:
        name = sym.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        # Only count non-trivial names (>= 3 chars) to avoid false positives
        if len(name) >= 3 and name.lower() in text_lower:
            refs.append({
                "symbol_name": name,
                "file": sym.get("file", ""),
                "mentioned_in_memories": True,
            })
    return refs


async def recall_with_code(
    task: str,
    repo: str,
    tags: Optional[list[str]] = None,
    top_k: int = 5,
    budget_tokens: int = 8000,
    storage_path: Optional[str] = None,
) -> dict:
    """Recall memories from FirekeepCortex and cross-reference with the code index.

    When FirekeepCortex is available the tool fetches relevant memories for *task*,
    extracts keywords from the recalled content, and uses them together with the
    original *task* string to pull focused code context from the repository index.

    When FirekeepCortex is unavailable or returns an error the tool falls back to
    code-only context using *task* as the focus query.

    Args:
        task: Natural-language description of the current task.
        repo: Repository identifier (``owner/repo`` or just repo name).
        tags: Optional tags passed to FirekeepCortex recall.
        top_k: Maximum number of memories to request.
        budget_tokens: Token budget for code context (default 8000).
            Passed straight to get_context, whose budget counts the whole
            entry rather than source bytes alone (see that tool).
        storage_path: Custom storage path for the code index.

    Returns:
        Dict with ``memories``, ``code_context``, ``cross_references``, and
        ``_meta`` keys.
    """
    start = time.perf_counter()

    # ------------------------------------------------------------------
    # 1. Recall from FirekeepCortex
    # ------------------------------------------------------------------
    cortex = get_cortex_client()
    cortex_available = cortex.is_available
    context_block = ""
    source_count = 0
    memory_score = 0.0
    cortex_error: Optional[str] = None

    if cortex_available:
        recall_result = await cortex.recall(task, tags=tags, top_k=top_k)

        if "error" in recall_result:
            cortex_error = recall_result["error"]
            cortex_available = False
        else:
            context_block = recall_result.get("context_block", "")
            source_count = len(recall_result.get("sources", []))
            memory_score = recall_result.get("score", 0.0)

    # ------------------------------------------------------------------
    # 2. Build combined focus from task + memory keywords
    # ------------------------------------------------------------------
    if context_block:
        keywords = _extract_keywords(context_block)
        combined_focus = " ".join([task] + keywords)
    else:
        combined_focus = task

    # ------------------------------------------------------------------
    # 3. Get code context from FirekeepSymdex
    # ------------------------------------------------------------------
    code_result = get_context(
        repo,
        budget_tokens=budget_tokens,
        focus=combined_focus,
        include_deps=True,
        storage_path=storage_path,
    )

    if "error" in code_result:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "error": code_result["error"],
            "_meta": {
                "timing_ms": round(elapsed, 1),
                "cortex_available": cortex_available,
            },
        }

    symbols = code_result.get("symbols", [])

    # ------------------------------------------------------------------
    # 4. Build cross-references
    # ------------------------------------------------------------------
    cross_refs = _build_cross_references(symbols, context_block)

    # ------------------------------------------------------------------
    # 5. Assemble response
    # ------------------------------------------------------------------
    elapsed = (time.perf_counter() - start) * 1000
    tokens_used = code_result.get("_meta", {}).get("tokens_used", 0)

    result: dict = {
        "repo": code_result.get("repo", repo),
        "task": task,
        "memories": {
            "context_block": context_block,
            "source_count": source_count,
            "score": memory_score,
        },
        "code_context": {
            "symbols_included": code_result.get("symbols_included", len(symbols)),
            "symbols": symbols,
        },
        "cross_references": cross_refs,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "cortex_available": cortex_available,
            "tokens_used": tokens_used,
        },
    }

    if cortex_error:
        result["_meta"]["cortex_error"] = cortex_error

    return result


TOOL_DEF = {
    "name": "recall_with_code",
    "description": "Recall memories from FirekeepCortex and cross-reference with current code symbols. Combines historical context with live code intelligence for richer task context.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "task": {
                            "type": "string",
                            "description": "Description of what you're trying to do"
                    },
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "tags": {
                            "type": "array",
                            "items": {
                                    "type": "string"
                            },
                            "description": "Optional filter tags"
                    },
                    "top_k": {
                            "type": "integer",
                            "description": "Max memories to recall (default 5)",
                            "default": 5
                    },
                    "budget_tokens": {
                            "type": "integer",
                            "description": "Token budget for code context (default 8000). Counts the whole entry, not just source bytes",
                            "default": 8000
                    }
            },
            "required": [
                    "task",
                    "repo"
            ]
    },
    "is_async": True,
    "handler": recall_with_code,
}
