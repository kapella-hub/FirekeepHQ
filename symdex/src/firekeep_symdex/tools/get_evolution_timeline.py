"""Track symbol/file change history using git log."""

import subprocess
import time
from pathlib import Path
from typing import Optional

from ..storage import IndexStore
from ._utils import resolve_repo


def _find_local_root(repo: str, owner: str, name: str) -> Optional[Path]:
    """Find the local repo root directory.

    Tries the repo string as a direct path first, then common local-repo
    resolution strategies.
    """
    # Try the repo string itself as a path
    candidate = Path(repo).expanduser().resolve()
    if candidate.is_dir() and (candidate / ".git").exists():
        return candidate

    # For local repos, check cwd
    if owner == "local":
        cwd = Path.cwd()
        if cwd.name == name:
            return cwd
        child = cwd / name
        if child.is_dir() and (child / ".git").exists():
            return child

    return None


def _resolve_repo_with_path(repo: str, storage_path: Optional[str] = None):
    """Resolve repo, handling local paths on Windows/Unix.

    Returns (owner, name, local_root_or_None).
    """
    # Check if repo is a local path first
    repo_path = Path(repo).expanduser().resolve()
    if repo_path.is_dir():
        owner = "local"
        name = repo_path.name
        local_root = repo_path if (repo_path / ".git").exists() else None
        return owner, name, local_root

    # Standard resolve
    owner, name = resolve_repo(repo, storage_path)
    local_root = _find_local_root(repo, owner, name)
    return owner, name, local_root


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def get_evolution_timeline(
    repo: str,
    symbol_id: Optional[str] = None,
    file_path: Optional[str] = None,
    max_entries: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Get the change timeline for a symbol or file from git history.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        symbol_id: Specific symbol to track.
        file_path: File to track (used if no symbol_id).
        max_entries: Max timeline entries (default 20).
        storage_path: Custom storage path.

    Returns:
        Dict with timeline entries and metadata.
    """
    start = time.perf_counter()
    max_entries = max(1, min(max_entries, 100))

    if not symbol_id and not file_path:
        return {"error": "Either symbol_id or file_path is required"}

    try:
        owner, name, local_root = _resolve_repo_with_path(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    # Determine target file path
    target_file = file_path
    target_label = file_path or ""

    if symbol_id:
        symbol = index.get_symbol(symbol_id)
        if not symbol:
            return {"error": f"Symbol not found: {symbol_id}"}
        target_file = symbol.get("file", "")
        target_label = f"{symbol.get('name', symbol_id)} ({target_file})"

    if not target_file:
        return {"error": "Could not determine target file"}

    if not local_root or not (local_root / ".git").exists():
        return {"error": "Repository is not a local git repo or could not find local root"}

    # Build git log command
    git_args = [
        "log",
        f"--max-count={max_entries}",
        "--format=%H%n%an%n%ae%n%aI%n%s%n---END---",
        "--follow",
        "--",
        target_file,
    ]

    output = _run_git(git_args, local_root)
    if output is None:
        return {"error": "Failed to run git log"}

    # Parse git log output
    timeline = []
    entries = output.strip().split("---END---")
    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        lines = entry.split("\n", 4)
        if len(lines) < 5:
            continue
        commit_hash, author, email, date, message = lines
        timeline.append({
            "commit": commit_hash.strip(),
            "author": author.strip(),
            "email": email.strip(),
            "date": date.strip(),
            "message": message.strip(),
            "change_type": "modified",
        })

    # Mark the last entry (oldest) as "created"
    if timeline:
        timeline[-1]["change_type"] = "created"

    elapsed = (time.perf_counter() - start) * 1000

    result = {
        "target": target_label,
        "timeline": timeline,
        "total_changes": len(timeline),
        "_meta": {"timing_ms": round(elapsed, 1)},
    }

    if timeline:
        result["first_seen"] = timeline[-1]["date"]
        result["last_modified"] = timeline[0]["date"]

    return result


TOOL_DEF = {
    "name": "get_evolution_timeline",
    "description": "Track how a symbol or file has changed over time using git history. Returns a timeline of commits with authors, dates, and messages.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol ID to track changes for"
                    },
                    "file_path": {
                            "type": "string",
                            "description": "File path to track (used if no symbol_id)"
                    },
                    "max_entries": {
                            "type": "integer",
                            "description": "Max timeline entries (default 20)",
                            "default": 20
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_evolution_timeline,
}
