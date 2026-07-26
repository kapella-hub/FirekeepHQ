"""PR review context enriched with historical memory from FirekeepCortex."""

import asyncio
import logging
import os
import re
import time
from typing import Optional

from ..cortex import get_cortex_client
from .get_review_context import get_review_context
from .get_architecture_map import get_architecture_map

logger = logging.getLogger(__name__)

# Total timeout (seconds) for all parallel per-file cortex recalls.
_RECALL_TIMEOUT = 10.0

_WARNING_PATTERN = re.compile(
    r"\b(regression|bug|broke|failed|revert)\b",
    re.IGNORECASE,
)


async def review_with_history(
    repo: str,
    changed_files: list[str],
    budget_tokens: int = 8000,
    storage_path: Optional[str] = None,
) -> dict:
    """Assemble PR review context and enrich it with historical memory.

    Combines FirekeepSymdex's structural review context (changed symbols,
    callers, dependencies, tests) with FirekeepCortex's per-file historical
    insights.  When Cortex is unavailable, the standard review context is
    still returned — history sections are simply empty.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        changed_files: List of file paths that changed.
        budget_tokens: Max tokens for the structural review context.
        storage_path: Custom storage path for the index store.

    Returns:
        Dict with review sections, per-file history, warnings, and metadata.
    """
    start = time.perf_counter()

    # 1. Standard review context (synchronous call)
    review = get_review_context(repo, changed_files, budget_tokens, storage_path)
    if "error" in review:
        return review

    cortex = get_cortex_client()
    cortex_available = cortex.is_available

    # 2. Recall historical context per file (parallel, with timeout)
    history: dict[str, dict] = {}
    if cortex_available:
        repo_name = review.get("repo", repo).split("/")[-1]

        async def _recall_file(filepath: str) -> tuple[str, dict]:
            basename = os.path.basename(filepath)
            task = f"Previous changes to {basename} in {repo_name}"
            result = await cortex.recall(
                task=task,
                tags=[basename, repo_name],
                top_k=3,
            )
            context_block = result.get("context_block", "")
            has_history = bool(context_block) and "error" not in result
            return filepath, {
                "context_block": context_block if has_history else "",
                "has_history": has_history,
            }

        try:
            recall_results = await asyncio.wait_for(
                asyncio.gather(
                    *[_recall_file(f) for f in changed_files],
                    return_exceptions=True,
                ),
                timeout=_RECALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Cortex parallel recalls timed out after %.1fs", _RECALL_TIMEOUT)
            recall_results = []

        for item in recall_results:
            if isinstance(item, Exception):
                logger.warning("Cortex recall failed for a file: %s", item)
                continue
            filepath, file_history = item
            history[filepath] = file_history
    else:
        for f in changed_files:
            history[f] = {"context_block": "", "has_history": False}

    # 3. Stream architecture snapshot to Cortex
    if cortex_available:
        try:
            arch_map = get_architecture_map(repo, storage_path)
            if "error" not in arch_map:
                repo_tag = review.get("repo", repo)
                await cortex.stream(
                    source="firekeep-symdex:architecture",
                    payload=arch_map,
                    tags=[repo_tag],
                )
        except Exception as exc:
            logger.warning("Failed to stream architecture to Cortex: %s", exc)

    # 4. Generate warnings from historical context
    warnings = _extract_warnings(history)

    # 5. Assemble response
    elapsed = (time.perf_counter() - start) * 1000
    files_with_history = sum(1 for h in history.values() if h.get("has_history"))

    return {
        "repo": review["repo"],
        "changed_files": changed_files,
        "review": review.get("sections", {}),
        "history": history,
        "warnings": warnings,
        "related_test_files": review.get("related_test_files", []),
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "tokens_used": review.get("_meta", {}).get("tokens_used", 0),
            "cortex_available": cortex_available,
            "files_with_history": files_with_history,
        },
    }


def _extract_warnings(history: dict[str, dict]) -> list[str]:
    """Scan historical context blocks for warning-worthy keywords."""
    warnings = []
    for filepath, info in history.items():
        context = info.get("context_block", "")
        if not context:
            continue
        match = _WARNING_PATTERN.search(context)
        if match:
            keyword = match.group(0).lower()
            basename = os.path.basename(filepath)
            warnings.append(
                f"{filepath}: Previous changes mention '{keyword}' (see history for {basename})"
            )
    return warnings


TOOL_DEF = {
    "name": "review_with_history",
    "description": "PR review context enriched with historical memory. Combines changed symbols, callers, dependencies, and tests with FirekeepCortex memories about past changes to the same files.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "changed_files": {
                            "type": "array",
                            "items": {
                                    "type": "string"
                            },
                            "description": "List of file paths that changed"
                    },
                    "budget_tokens": {
                            "type": "integer",
                            "description": "Token budget (default 8000)",
                            "default": 8000
                    }
            },
            "required": [
                    "repo",
                    "changed_files"
            ]
    },
    "is_async": True,
    "handler": review_with_history,
}
