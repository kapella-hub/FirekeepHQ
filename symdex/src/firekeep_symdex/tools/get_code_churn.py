"""Analyze code churn (change frequency) using git log --numstat."""

import subprocess
import time
from pathlib import Path
from typing import Optional

from ..storage import IndexStore
from .get_evolution_timeline import _resolve_repo_with_path


def _run_git(args: list[str], cwd: Path) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _risk_level(churn_score: float) -> str:
    """Classify risk based on churn score."""
    if churn_score >= 5000:
        return "high"
    elif churn_score >= 1000:
        return "medium"
    return "low"


def get_code_churn(
    repo: str,
    since: Optional[str] = None,
    max_results: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Identify files with the highest change frequency (churn).

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        since: Date filter (e.g. "2025-01-01" or "3 months ago").
        max_results: Maximum results (default 20).
        storage_path: Custom storage path.

    Returns:
        Dict with churn-ranked files and risk levels.
    """
    start = time.perf_counter()
    max_results = max(1, min(max_results, 100))

    try:
        owner, name, local_root = _resolve_repo_with_path(repo, storage_path)
    except ValueError as e:
        return {"error": str(e)}

    store = IndexStore(base_path=storage_path)
    index = store.load_index(owner, name)

    if not index:
        return {"error": f"Repository not indexed: {owner}/{name}"}

    if not local_root or not (local_root / ".git").exists():
        return {"error": "Repository is not a local git repo or could not find local root"}

    # Build git log --numstat command
    git_args = ["log", "--numstat", "--format=%H"]
    if since:
        git_args.append(f"--since={since}")

    output = _run_git(git_args, local_root)
    if output is None:
        return {"error": "Failed to run git log"}

    # Parse numstat output
    # Format: lines of commit hashes interspersed with "added\tremoved\tfile" lines
    file_stats: dict[str, dict] = {}
    indexed_files = set(index.source_files)
    current_commit = None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Commit hash lines are 40 hex chars (git may output upper or lowercase)
        if len(line) == 40 and all(c in "0123456789abcdefABCDEF" for c in line):
            current_commit = line
            continue

        # Numstat line: "added\tremoved\tfile"
        parts = line.split("\t")
        if len(parts) != 3:
            continue

        added_str, removed_str, file_name = parts

        # Binary files show "-" for added/removed
        if added_str == "-" or removed_str == "-":
            continue

        try:
            added = int(added_str)
            removed = int(removed_str)
        except ValueError:
            continue

        if file_name not in file_stats:
            file_stats[file_name] = {
                "file": file_name,
                "commits": set(),
                "lines_added": 0,
                "lines_removed": 0,
            }

        file_stats[file_name]["commits"].add(current_commit)
        file_stats[file_name]["lines_added"] += added
        file_stats[file_name]["lines_removed"] += removed

    # Build results, preferring indexed files but including all
    results = []
    for fname, stats in file_stats.items():
        commit_count = len(stats["commits"])
        lines_added = stats["lines_added"]
        lines_removed = stats["lines_removed"]
        churn_score = commit_count * (lines_added + lines_removed)

        results.append({
            "file": fname,
            "commits": commit_count,
            "lines_added": lines_added,
            "lines_removed": lines_removed,
            "churn_score": churn_score,
            "indexed": fname in indexed_files,
            "risk_level": _risk_level(churn_score),
        })

    # Sort by churn_score descending
    results.sort(key=lambda r: -r["churn_score"])
    results = results[:max_results]

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "results": results,
        "result_count": len(results),
        "period": {
            "since": since or "all time",
            "until": "now",
        },
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_code_churn",
    "description": "Identify files with the highest change frequency (churn). High churn combined with high complexity indicates technical debt hotspots.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "since": {
                            "type": "string",
                            "description": "Date filter (e.g. '2025-01-01' or '3 months ago')"
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Max results (default 20)",
                            "default": 20
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_code_churn,
}
