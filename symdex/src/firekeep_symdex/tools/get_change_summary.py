"""Compare current file contents against stored index to show what symbols changed."""

import os
import time
from pathlib import Path
from typing import Optional

from ..parser import parse_file, LANGUAGE_EXTENSIONS
from ..storage import IndexStore
from ._utils import resolve_repo


def get_change_summary(
    repo: str,
    path: Optional[str] = None,
    storage_path: Optional[str] = None,
) -> dict:
    """Compare current files on disk against the stored index to find symbol changes.

    For repos indexed from local folders, reads current files from the original
    path (or a user-supplied path) and compares against stored symbols.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        path: Optional path to local folder with current files.
        storage_path: Custom storage path.

    Returns:
        Dict with changed/added/removed symbols and file-level diff counts.
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

    # Determine local folder path
    local_path = _resolve_local_path(owner, name, path)
    if local_path is None:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "repo": f"{owner}/{name}",
            "message": "No local path available. Provide 'path' parameter pointing to the local folder.",
            "_meta": {"timing_ms": round(elapsed, 1)},
        }

    if not local_path.is_dir():
        return {"error": f"Path is not a directory: {local_path}"}

    # Read current files from disk
    current_files = _read_current_files(local_path, index.source_files)

    # Detect file-level changes
    changed_files, new_files, deleted_files = store.detect_changes(
        owner, name, current_files
    )

    # Build stored symbols index by file
    stored_by_file: dict[str, list[dict]] = {}
    for sym in index.symbols:
        file_path = sym.get("file", "")
        stored_by_file.setdefault(file_path, []).append(sym)

    symbols_added = []
    symbols_removed = []
    symbols_modified = []

    # Process changed files: compare stored vs current symbols
    for fp in changed_files:
        content = current_files.get(fp, "")
        current_syms = _parse_file_symbols(fp, content)
        stored_syms = stored_by_file.get(fp, [])
        added, removed, modified = _diff_symbols(stored_syms, current_syms, fp)
        symbols_added.extend(added)
        symbols_removed.extend(removed)
        symbols_modified.extend(modified)

    # Process new files: all symbols are added
    for fp in new_files:
        content = current_files.get(fp, "")
        current_syms = _parse_file_symbols(fp, content)
        for sym in current_syms:
            symbols_added.append(_symbol_summary(sym, fp))

    # Process deleted files: all stored symbols are removed
    for fp in deleted_files:
        for sym in stored_by_file.get(fp, []):
            symbols_removed.append(_symbol_summary(sym, fp))

    elapsed = (time.perf_counter() - start) * 1000

    # Build summary text
    parts = []
    if changed_files:
        parts.append(f"{len(changed_files)} file(s) changed")
    if new_files:
        parts.append(f"{len(new_files)} file(s) added")
    if deleted_files:
        parts.append(f"{len(deleted_files)} file(s) deleted")
    if symbols_added:
        parts.append(f"{len(symbols_added)} symbol(s) added")
    if symbols_removed:
        parts.append(f"{len(symbols_removed)} symbol(s) removed")
    if symbols_modified:
        parts.append(f"{len(symbols_modified)} symbol(s) modified")
    summary = ". ".join(parts) + "." if parts else "No changes detected."

    return {
        "repo": f"{owner}/{name}",
        "files_changed": len(changed_files),
        "files_added": len(new_files),
        "files_deleted": len(deleted_files),
        "symbols_added": symbols_added,
        "symbols_removed": symbols_removed,
        "symbols_modified": symbols_modified,
        "summary": summary,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


def _resolve_local_path(
    owner: str, name: str, user_path: Optional[str]
) -> Optional[Path]:
    """Determine the local folder path for a repo.

    If the user provides a path, use it. Otherwise, for local repos (owner == "local"),
    there is no stored path in the index, so we cannot infer it automatically.
    """
    if user_path:
        return Path(user_path).expanduser().resolve()

    # For non-local repos (GitHub), there is no local folder
    if owner != "local":
        return None

    # Local repos don't store their original path in the index.
    # The user must provide it explicitly.
    return None


def _read_current_files(
    folder_path: Path, indexed_files: list[str]
) -> dict[str, str]:
    """Read current file contents from disk.

    Reads files that were previously indexed plus any new files with
    supported extensions found in the folder.
    """
    current_files: dict[str, str] = {}

    # Walk the folder for all supported source files
    for file_path in folder_path.rglob("*"):
        if not file_path.is_file():
            continue

        ext = file_path.suffix
        if ext not in LANGUAGE_EXTENSIONS:
            continue

        try:
            rel_path = file_path.relative_to(folder_path).as_posix()
        except ValueError:
            continue

        # Skip common non-source directories
        normalized = rel_path.replace("\\", "/")
        if any(
            skip in normalized
            for skip in (
                "node_modules/", "vendor/", "venv/", ".venv/",
                "__pycache__/", "dist/", "build/", ".git/",
            )
        ):
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            current_files[rel_path] = content
        except Exception:
            continue

    return current_files


def _parse_file_symbols(file_path: str, content: str) -> list[dict]:
    """Parse a file and return symbol dicts."""
    ext = os.path.splitext(file_path)[1]
    language = LANGUAGE_EXTENSIONS.get(ext)
    if not language:
        return []

    try:
        symbols = parse_file(content, file_path, language)
        return [
            {
                "name": s.name,
                "qualified_name": s.qualified_name,
                "kind": s.kind,
                "line": s.line,
                "content_hash": s.content_hash,
            }
            for s in symbols
        ]
    except Exception:
        return []


def _diff_symbols(
    stored: list[dict], current: list[dict], file_path: str
) -> tuple[list[dict], list[dict], list[dict]]:
    """Compare stored and current symbols for a single file.

    Returns (added, removed, modified) symbol summary lists.
    """
    stored_by_qname = {
        s.get("qualified_name", s.get("name", "")): s for s in stored
    }
    current_by_qname = {
        s.get("qualified_name", s.get("name", "")): s for s in current
    }

    stored_names = set(stored_by_qname.keys())
    current_names = set(current_by_qname.keys())

    added = [
        _symbol_summary(current_by_qname[qn], file_path)
        for qn in (current_names - stored_names)
    ]
    removed = [
        _symbol_summary(stored_by_qname[qn], file_path)
        for qn in (stored_names - current_names)
    ]
    modified = []
    for qn in stored_names & current_names:
        old_hash = stored_by_qname[qn].get("content_hash", "")
        new_hash = current_by_qname[qn].get("content_hash", "")
        if old_hash != new_hash:
            modified.append(_symbol_summary(current_by_qname[qn], file_path))

    return added, removed, modified


def _symbol_summary(sym: dict, file_path: str) -> dict:
    """Create a compact symbol summary for the output."""
    return {
        "name": sym.get("qualified_name") or sym.get("name", ""),
        "kind": sym.get("kind", ""),
        "file": file_path,
        "line": sym.get("line", 0),
    }


TOOL_DEF = {
    "name": "get_change_summary",
    "description": "Compare current file contents against the stored index to show what symbols changed, were added, or were removed since the last index. Requires a local path for the repo.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "path": {
                            "type": "string",
                            "description": "Optional path to local folder with current files (required for local repos if not auto-detectable)"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_change_summary,
}
