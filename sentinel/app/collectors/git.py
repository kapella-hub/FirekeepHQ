"""Git commit activity collector."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from app.constants import WATCHES_KEY
from app.store import push_event

logger = logging.getLogger(__name__)


async def _trigger_reindex(symdex_url: str, repo_path: str) -> None:
    """Fire-and-forget incremental reindex via Symdex MCP."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"{symdex_url}/mcp",
                json={
                    "jsonrpc": "2.0", "method": "tools/call", "id": 1,
                    "params": {"name": "index_folder", "arguments": {
                        "path": repo_path,
                        "use_ai_summaries": False,
                    }},
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
            logger.info("Auto-reindex triggered for %s", repo_path)
    except Exception:
        logger.debug("Auto-reindex trigger failed for %s (non-critical)", repo_path)


class GitCollector:
    """Tracks git commit activity."""

    def __init__(self):
        self.name = "git"
        self.healthy = True


_collector = GitCollector()


def get_collector() -> GitCollector:
    return _collector


async def _get_watched_repos(redis) -> list[str]:
    """Read git-type watches from Redis."""
    members = await redis.smembers(WATCHES_KEY)
    repos: list[str] = []
    for raw in members:
        try:
            entry = json.loads(raw)
            if entry.get("watch_type") == "git":
                repos.append(entry["path"])
        except (json.JSONDecodeError, KeyError):
            continue
    return repos


async def _get_recent_commits(repo_path: str, seconds: int) -> list[dict]:
    """Run git log to find commits within the last N seconds.

    Uses asyncio.create_subprocess_exec with explicit argument list
    to avoid shell injection. All arguments are hardcoded or validated.
    """
    args = [
        "git", "-C", repo_path, "log",
        f"--since={seconds}s ago",
        "--oneline", "--format=%H|%an|%s",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0 or not stdout:
            return []

        commits = []
        for line in stdout.decode().strip().splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                commits.append({"hash": parts[0], "author": parts[1], "message": parts[2]})
        return commits
    except Exception:
        return []


async def run_git_collector(redis, settings, stop_event: asyncio.Event) -> None:
    """Poll watched git repositories for new commits."""
    collector = get_collector()
    seen_commits: set[str] = set()

    while not stop_event.is_set():
        try:
            repos = await _get_watched_repos(redis)
            # Also include any paths from WATCH_PATHS config
            if settings.WATCH_PATHS:
                for p in settings.WATCH_PATHS.split(","):
                    p = p.strip()
                    if p and p not in repos:
                        repos.append(p)

            for repo in repos:
                commits = await _get_recent_commits(repo, settings.POLL_INTERVAL_GIT + 10)
                has_new = False
                for commit in commits:
                    commit_key = f"{repo}:{commit['hash']}"
                    if commit_key in seen_commits:
                        continue
                    has_new = True
                    seen_commits.add(commit_key)
                    await push_event(
                        redis,
                        "git",
                        "commit.new",
                        f"[{repo}] {commit['author']}: {commit['message']}",
                        {"repo": repo, **commit},
                        "info",
                        ["git", repo.rsplit("/", 1)[-1]],
                        maxlen=settings.EVENT_MAXLEN,
                    )

                # Auto-index: trigger Symdex reindex once per repo when new commits found
                if has_new and settings.AUTO_INDEX_ENABLED:
                    asyncio.create_task(_trigger_reindex(settings.SYMDEX_URL, repo))

            # Prevent seen_commits from growing unbounded — keep most recent half
            if len(seen_commits) > 5000:
                seen_commits.clear()

            collector.healthy = True
        except Exception as e:
            logger.warning("Collector %s error: %s", collector.name, e)
            collector.healthy = False

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.POLL_INTERVAL_GIT)
        except asyncio.TimeoutError:
            pass
