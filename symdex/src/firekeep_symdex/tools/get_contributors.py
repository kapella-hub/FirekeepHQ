"""Map contributors to symbols/files using git blame."""

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
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _parse_blame_porcelain(output: str) -> list[dict]:
    """Parse git blame --porcelain output into line-level author info.

    Returns a list of dicts with author, email, timestamp per blamed line.
    """
    lines_info = []
    current = {}

    for line in output.splitlines():
        if line.startswith("author "):
            current["author"] = line[7:]
        elif line.startswith("author-mail "):
            # Strip angle brackets
            email = line[12:].strip().strip("<>")
            current["email"] = email
        elif line.startswith("author-time "):
            current["timestamp"] = int(line[12:])
        elif line.startswith("committer-time "):
            current["committer_time"] = int(line[15:])
        elif line.startswith("\t"):
            # This is the actual source line; finalize current entry
            if current.get("author"):
                lines_info.append(dict(current))
            current = {}

    return lines_info


def get_contributors(
    repo: str,
    file_path: Optional[str] = None,
    symbol_id: Optional[str] = None,
    max_results: int = 20,
    storage_path: Optional[str] = None,
) -> dict:
    """Map contributors to a file or symbol using git blame.

    Args:
        repo: Repository identifier (owner/repo or just repo name).
        file_path: Specific file to analyze.
        symbol_id: Specific symbol to analyze.
        max_results: Max contributor results (default 20).
        storage_path: Custom storage path.

    Returns:
        Dict with contributor breakdown and ownership percentages.
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

    # Determine target
    target_file = file_path
    target_label = file_path or ""
    line_start = None
    line_end = None

    if symbol_id:
        symbol = index.get_symbol(symbol_id)
        if not symbol:
            return {"error": f"Symbol not found: {symbol_id}"}
        target_file = symbol.get("file", "")
        target_label = f"{symbol.get('name', symbol_id)} ({target_file})"
        line_start = symbol.get("line")
        line_end = symbol.get("end_line") or line_start

    if not target_file:
        return {"error": "Either file_path or symbol_id is required"}

    if not local_root or not (local_root / ".git").exists():
        return {"error": "Repository is not a local git repo or could not find local root"}

    # Build git blame command
    blame_args = ["blame", "--porcelain"]
    if line_start and line_end:
        blame_args.extend(["-L", f"{line_start},{line_end}"])
    blame_args.append(target_file)

    output = _run_git(blame_args, local_root)
    if output is None:
        return {"error": f"Failed to run git blame on {target_file}"}

    # Parse blame output
    lines_info = _parse_blame_porcelain(output)
    total_lines = len(lines_info)

    if total_lines == 0:
        elapsed = (time.perf_counter() - start) * 1000
        return {
            "target": target_label,
            "contributors": [],
            "total_lines": 0,
            "_meta": {"timing_ms": round(elapsed, 1)},
        }

    # Aggregate by author
    author_data: dict[str, dict] = {}
    for info in lines_info:
        author = info.get("author", "Unknown")
        email = info.get("email", "")
        ts = info.get("committer_time", 0)

        if author not in author_data:
            author_data[author] = {
                "author": author,
                "email": email,
                "lines": 0,
                "last_timestamp": 0,
            }

        author_data[author]["lines"] += 1
        if ts > author_data[author]["last_timestamp"]:
            author_data[author]["last_timestamp"] = ts
            author_data[author]["email"] = email  # use email from most recent commit

    # Build results with percentages
    contributors = []
    for data in author_data.values():
        from datetime import datetime, timezone
        last_ts = data["last_timestamp"]
        last_date = ""
        if last_ts:
            last_date = datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()

        contributors.append({
            "author": data["author"],
            "email": data["email"],
            "lines": data["lines"],
            "percentage": round(data["lines"] / total_lines * 100, 1),
            "last_commit": last_date,
        })

    # Sort by lines descending
    contributors.sort(key=lambda c: -c["lines"])
    contributors = contributors[:max_results]

    elapsed = (time.perf_counter() - start) * 1000

    return {
        "target": target_label,
        "contributors": contributors,
        "total_lines": total_lines,
        "_meta": {"timing_ms": round(elapsed, 1)},
    }


TOOL_DEF = {
    "name": "get_contributors",
    "description": "Map contributors to a file or symbol using git blame. Shows who owns each area of the codebase with line counts and ownership percentages.",
    "inputSchema": {
            "type": "object",
            "properties": {
                    "repo": {
                            "type": "string",
                            "description": "Repository identifier (owner/repo or just repo name)"
                    },
                    "file_path": {
                            "type": "string",
                            "description": "File to analyze"
                    },
                    "symbol_id": {
                            "type": "string",
                            "description": "Symbol to analyze"
                    },
                    "max_results": {
                            "type": "integer",
                            "description": "Max contributor results (default 20)",
                            "default": 20
                    }
            },
            "required": [
                    "repo"
            ]
    },
    "handler": get_contributors,
}
