"""Learn from code changes — combines change detection with FirekeepCortex memory."""

import time
from typing import Optional

from ..cortex import get_cortex_client
from .get_change_summary import get_change_summary


async def learn_from_changes(
    repo: str,
    path: Optional[str] = None,
    message: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Detect symbol-level changes and persist them as learnings in FirekeepCortex.

    Calls ``get_change_summary`` to find what changed on disk compared to the
    stored index, then sends structured learning data to FirekeepCortex so the
    Sleep Cycle can build long-term memory from code evolution.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        path: Optional path to local folder with current files.
        message: Optional human-readable description to prepend to the action.
        storage_path: Custom storage path.

    Returns:
        Dict with change summary plus learn/stream status from FirekeepCortex.
    """
    start = time.perf_counter()

    try:
        changes = get_change_summary(repo, path, storage_path)
    except Exception as exc:
        return {"error": f"Failed to get change summary: {exc}"}

    if "error" in changes:
        return changes

    # No changes detected — nothing to learn.
    has_changes = (
        changes.get("files_changed", 0)
        + changes.get("files_added", 0)
        + changes.get("files_deleted", 0)
    )
    if not has_changes:
        return {"status": "no_changes"}

    # Build action string from changed symbols
    action = _build_action(changes)
    if message:
        action = f"{message} — {action}"

    outcome = changes.get("summary", "")
    domain = changes.get("repo", repo)
    tags = _build_tags(changes)

    # Fire learn + stream in sequence (both are cheap HTTP calls)
    cortex = get_cortex_client()
    learn_result = await cortex.learn(
        action=action,
        outcome=outcome,
        tags=tags,
        domain=domain,
    )

    stream_result = await cortex.stream(
        source="firekeep-symdex/learn_from_changes",
        payload={
            "repo": domain,
            "files_changed": changes.get("files_changed", 0),
            "files_added": changes.get("files_added", 0),
            "files_deleted": changes.get("files_deleted", 0),
            "symbols_added": changes.get("symbols_added", []),
            "symbols_removed": changes.get("symbols_removed", []),
            "symbols_modified": changes.get("symbols_modified", []),
            "summary": outcome,
        },
        tags=tags,
    )

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "repo": domain,
        "summary": outcome,
        "learn": learn_result,
        "stream": stream_result,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


def _build_action(changes: dict) -> str:
    """Construct a human-readable action string from the change summary."""
    parts: list[str] = []

    for category, label in [
        ("symbols_modified", "Modified"),
        ("symbols_added", "Added"),
        ("symbols_removed", "Removed"),
    ]:
        symbols = changes.get(category, [])
        if not symbols:
            continue

        # Group by file for a compact description
        by_file: dict[str, list[str]] = {}
        for sym in symbols:
            f = sym.get("file", "unknown")
            by_file.setdefault(f, []).append(sym.get("name", "?"))

        for file_path, names in by_file.items():
            name_list = ", ".join(names[:5])
            if len(names) > 5:
                name_list += f" (+{len(names) - 5} more)"
            parts.append(f"{label} {len(names)} symbol(s) in {file_path}: {name_list}")

    return ". ".join(parts) if parts else "Code changes detected"


def _build_tags(changes: dict) -> list[str]:
    """Extract tags from the change summary (file names + symbol kinds)."""
    tags: set[str] = set()

    for category in ("symbols_added", "symbols_removed", "symbols_modified"):
        for sym in changes.get(category, []):
            # Add the file name (without extension) as a tag
            file_path = sym.get("file", "")
            if file_path:
                basename = file_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                if basename:
                    tags.add(basename)
            # Add the symbol kind as a tag
            kind = sym.get("kind", "")
            if kind:
                tags.add(kind)

    return sorted(tags)


TOOL_DEF = {
    "name": "learn_from_changes",
    "description": "Record code changes to FirekeepCortex memory. Detects current changes vs stored index and learns the action/outcome for future recall. Requires FirekeepCortex to be running.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "path": {
                            "type": "string",
                            "description": "Local folder path for change detection"
                    },
                    "message": {
                            "type": "string",
                            "description": "Optional description of what changed and why"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "is_async": True,
    "handler": learn_from_changes,
}
