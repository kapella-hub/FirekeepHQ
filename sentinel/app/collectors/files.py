"""File change detection collector — snapshot-based mtime tracking."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from app.constants import WATCHES_KEY
from app.store import push_event

logger = logging.getLogger(__name__)


class FileCollector:
    """Tracks file system changes."""

    def __init__(self):
        self.name = "files"
        self.healthy = True


_collector = FileCollector()


def get_collector() -> FileCollector:
    return _collector


async def _get_watched_dirs(redis) -> list[str]:
    """Read file-type watches from Redis."""
    members = await redis.smembers(WATCHES_KEY)
    dirs: list[str] = []
    for raw in members:
        try:
            entry = json.loads(raw)
            if entry.get("watch_type") == "files":
                dirs.append(entry["path"])
        except (json.JSONDecodeError, KeyError):
            continue
    return dirs


def _snapshot_dir(path: str, max_depth: int = 3) -> dict[str, float]:
    """Build a dict of {filepath: mtime} for all files under path."""
    snapshot: dict[str, float] = {}
    try:
        for root, dirs, files in os.walk(path):
            # Limit depth
            depth = root.replace(path, "").count(os.sep)
            if depth >= max_depth:
                dirs.clear()
                continue
            # Skip hidden dirs and common noise
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"node_modules", "__pycache__", ".git"}]
            for f in files:
                if f.startswith("."):
                    continue
                fp = os.path.join(root, f)
                try:
                    snapshot[fp] = os.path.getmtime(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return snapshot


async def run_file_collector(redis, settings, stop_event: asyncio.Event) -> None:
    """Watch directories for file create/modify/delete events."""
    collector = get_collector()
    snapshots: dict[str, dict[str, float]] = {}

    while not stop_event.is_set():
        try:
            dirs = await _get_watched_dirs(redis)
            if settings.WATCH_PATHS:
                for p in settings.WATCH_PATHS.split(","):
                    p = p.strip()
                    if p and p not in dirs:
                        dirs.append(p)

            for watched_dir in dirs:
                current = await asyncio.get_event_loop().run_in_executor(
                    None, _snapshot_dir, watched_dir
                )
                prev = snapshots.get(watched_dir)

                if prev is not None:
                    # Detect new files
                    for fp in current:
                        if fp not in prev:
                            await push_event(
                                redis, "files", "file.created",
                                f"File created: {fp}",
                                {"path": fp, "dir": watched_dir},
                                "info", ["files", os.path.basename(fp)],
                                maxlen=settings.EVENT_MAXLEN,
                            )

                    # Detect modified files
                    for fp, mtime in current.items():
                        if fp in prev and prev[fp] != mtime:
                            await push_event(
                                redis, "files", "file.modified",
                                f"File modified: {fp}",
                                {"path": fp, "dir": watched_dir},
                                "info", ["files", os.path.basename(fp)],
                                maxlen=settings.EVENT_MAXLEN,
                            )

                    # Detect deleted files
                    for fp in prev:
                        if fp not in current:
                            await push_event(
                                redis, "files", "file.deleted",
                                f"File deleted: {fp}",
                                {"path": fp, "dir": watched_dir},
                                "warning", ["files", os.path.basename(fp)],
                                maxlen=settings.EVENT_MAXLEN,
                            )

                snapshots[watched_dir] = current

            collector.healthy = True
        except Exception as e:
            logger.warning("Collector %s error: %s", collector.name, e)
            collector.healthy = False

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.POLL_INTERVAL_FILES)
        except asyncio.TimeoutError:
            pass
