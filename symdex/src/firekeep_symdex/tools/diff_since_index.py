"""Show what changed since last indexing by comparing stored hashes against disk."""

import hashlib
import os
import time
from pathlib import Path
from typing import Optional

from ..parser.languages import LANGUAGE_EXTENSIONS
from ..storage import IndexStore
from ._utils import resolve_repo


def _hash_file(file_path: Path) -> Optional[str]:
    """SHA-256 hash of file content, or None on read error."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    except Exception:
        return None


# Directories to skip when walking (same as index_folder.py)
_SKIP_DIRS = {
    "node_modules", "vendor", "venv", ".venv", "__pycache__",
    "dist", "build", ".git", ".tox", ".mypy_cache",
    "target", ".gradle", "test_data", "testdata",
    "fixtures", "snapshots", "migrations", "generated", "proto",
}


def diff_since_index(
    repo: str,
    storage_path: Optional[str] = None,
) -> dict:
    """Show what changed on disk since the last indexing.

    Compares stored file hashes in the index against the current state of
    source files on disk. Only works for locally-indexed repos (owner="local").

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        storage_path: Custom storage path.

    Returns:
        Dict with new_files, modified_files, deleted_files, unchanged_count, and _meta.
    """
    start = time.perf_counter()

    # Check if repo is a local path first
    repo_path = Path(repo).expanduser().resolve()
    if repo_path.is_dir():
        owner = "local"
        name = repo_path.name
        repo_root = repo_path
    else:
        try:
            owner, name = resolve_repo(repo, storage_path)
        except ValueError as e:
            return {"error": str(e)}
        repo_root = None  # Will be resolved after loading the index

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Resolve repo_root now that index is loaded (for non-path repos)
    if repo_root is None:
        repo_root = _find_repo_root(repo, owner, name, index)

    stored_hashes = index.file_hashes
    if not stored_hashes:
        return {"error": "Index has no file hashes (indexed with older version?)"}

    if repo_root and repo_root.is_dir():
        # Walk the local directory and compare
        return _diff_local(index, repo_root, stored_hashes, start)
    else:
        # Non-local repo or can't find root -- compare against stored files only
        return _diff_stored_only(index, stored_hashes, start)


def _find_repo_root(repo: str, owner: str, name: str, index=None) -> Optional[Path]:
    """Try to find the local folder root for this repo."""
    # Prefer repo_root from the index if available
    if index and getattr(index, "repo_root", ""):
        candidate = Path(index.repo_root)
        if candidate.is_dir():
            return candidate

    # If repo looks like a path, use it directly
    candidate = Path(repo).expanduser().resolve()
    if candidate.is_dir():
        return candidate

    # For local repos, try common locations
    if owner == "local":
        # The folder name is the repo name; check cwd and parent
        cwd = Path.cwd()
        if cwd.name == name:
            return cwd
        child = cwd / name
        if child.is_dir():
            return child

    return None


def _diff_local(
    index,
    repo_root: Path,
    stored_hashes: dict[str, str],
    start: float,
) -> dict:
    """Diff by walking the local directory tree."""
    supported_exts = set(LANGUAGE_EXTENSIONS.keys())
    disk_files: dict[str, str] = {}  # rel_path -> hash

    for dirpath, dirnames, filenames in os.walk(repo_root):
        # Prune skip directories in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]

        for filename in filenames:
            ext = os.path.splitext(filename)[1]
            if ext not in supported_exts:
                continue

            full_path = Path(dirpath) / filename
            try:
                rel_path = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                continue

            file_hash = _hash_file(full_path)
            if file_hash is not None:
                disk_files[rel_path] = file_hash

    stored_set = set(stored_hashes.keys())
    disk_set = set(disk_files.keys())

    new_files = sorted(disk_set - stored_set)
    deleted_files = sorted(stored_set - disk_set)
    common = stored_set & disk_set

    modified_files = sorted(
        f for f in common if stored_hashes[f] != disk_files[f]
    )
    unchanged_count = len(common) - len(modified_files)

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "new_files": new_files,
        "modified_files": modified_files,
        "deleted_files": deleted_files,
        "unchanged_count": unchanged_count,
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "repo": index.repo,
            "indexed_at": index.indexed_at,
            "total_disk_files": len(disk_files),
            "total_stored_files": len(stored_hashes),
        },
    }


def _diff_stored_only(
    index,
    stored_hashes: dict[str, str],
    start: float,
) -> dict:
    """Fallback when we cannot walk the local directory.

    Reports stored file counts but cannot detect new files on disk.
    """
    elapsed = (time.perf_counter() - start) * 1000

    return {
        "new_files": [],
        "modified_files": [],
        "deleted_files": [],
        "unchanged_count": len(stored_hashes),
        "note": "Cannot walk local directory; showing stored state only. "
                "Provide the repo as a local path for full diff.",
        "_meta": {
            "timing_ms": round(elapsed, 1),
            "repo": index.repo,
            "indexed_at": index.indexed_at,
            "total_stored_files": len(stored_hashes),
        },
    }


TOOL_DEF = {
    "name": "diff_since_index",
    "description": "Show what changed on disk since the last indexing. Compares stored file hashes against current files to detect new, modified, and deleted files.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": diff_since_index,
}
