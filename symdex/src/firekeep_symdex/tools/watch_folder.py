"""File watching for auto-reindex on changes."""

import os
import threading
from pathlib import Path
from typing import Optional

from ..parser import LANGUAGE_EXTENSIONS
from ..security import is_secret_file
from ..storage import IndexStore
from .index_folder import _load_gitignore, index_folder, should_skip_file

# Directories pruned during traversal rather than filtered afterwards. Every
# entry here is already covered by index_folder.SKIP_PATTERNS -- this list only
# stops us DESCENDING into them, which is where the time goes on a real repo.
_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "vendor", "venv", ".venv",
    "__pycache__", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "target", ".gradle", ".idea", ".next", ".cache",
})


# Active watcher threads keyed by resolved folder path
_active_watches: dict[str, threading.Thread] = {}
# Signals to stop watcher threads
_stop_events: dict[str, threading.Event] = {}
# Guard concurrent access to _active_watches and _stop_events
_watch_lock = threading.Lock()

POLL_INTERVAL = 5  # seconds


def _scan_source_mtimes(folder_path: Path) -> dict[str, float]:
    """Filesystem scan of indexable paths -> ``{relpath: mtime}``.

    THIS REPLACES an index-derived snapshot, and the difference is the bug.
    The previous version built its map by iterating the symbols ALREADY in the
    index, so a newly added file -- which by definition contributes no symbols
    yet -- could never appear in it. `rel not in last_mtimes` was therefore
    unreachable for additions, and only modifications and deletions of
    already-indexed files were ever detected. A watcher that cannot see a new
    file is the one case an intra-session watcher exists for.

    Deliberately NOT `discover_local_files`, which is the obvious reuse and the
    wrong one: it opens every candidate to sniff for binary content and stats it
    for size, MEASURED at ~4.0s on the Firekeep root against this module's 5s
    poll -- a watcher that spends 80% of its life walking the disk. The walk
    itself is not the cost (a full `rglob` of 24,936 entries is 142ms; a pruned
    walk is 38ms), the per-file opens are. So this applies only the PATH-based
    half of the same filter chain -- directory pruning, ``should_skip_file``,
    ``.gitignore``, ``is_secret_file``, the extension whitelist -- and takes
    mtime from the directory entry `scandir` already populated.

    Being a strict SUPERSET of what gets indexed is the correct error direction.
    A new file that passes these filters but would be rejected by indexing
    (binary, oversized) costs one reindex that then declines to index it, and
    cannot retrigger, because this snapshot comes from the filesystem and
    records its mtime either way. A missed addition is silent forever.

    No ``max_files`` cap is applied, unlike indexing: the cap bounds the SIZE of
    an index, it does not say which changes matter, and on a repo over the cap a
    change outside the current 1500 can still change which 1500 are chosen.
    Over-triggering is recoverable; under-triggering is the bug above.
    """
    mtimes: dict[str, float] = {}
    root = folder_path.resolve()
    gitignore_spec = _load_gitignore(root)

    def _walk(directory: str) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return  # unreadable dir is not a reason to kill the watcher
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue  # mirrors discover_local_files' follow_symlinks=False
                if entry.is_dir():
                    if entry.name not in _PRUNE_DIRS:
                        _walk(entry.path)
                    continue
                if not entry.is_file():
                    continue
                if os.path.splitext(entry.name)[1] not in LANGUAGE_EXTENSIONS:
                    continue
                rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                if should_skip_file(rel):
                    continue
                if gitignore_spec and gitignore_spec.match_file(rel):
                    continue
                if is_secret_file(rel):
                    continue
                mtimes[rel] = entry.stat().st_mtime
            except OSError:
                continue

    _walk(str(root))
    return mtimes


def _changed(last: dict[str, float], current: dict[str, float]) -> bool:
    """Whether a reindex is warranted: any addition, modification or deletion.

    Kept separate from the loop so the three cases are testable without
    spawning a thread and waiting out a poll interval.
    """
    if len(last) != len(current):
        return True
    for rel, mtime in current.items():
        if rel not in last or last[rel] != mtime:
            return True
    return False


def _watcher_loop(folder_path: Path, storage_path: Optional[str], stop_event: threading.Event):
    """Poll loop that detects file changes and triggers incremental reindex."""
    # Snapshot the FILESYSTEM, not the index -- see _scan_source_mtimes.
    last_mtimes = _scan_source_mtimes(folder_path)

    while not stop_event.is_set():
        stop_event.wait(POLL_INTERVAL)
        if stop_event.is_set():
            break

        try:
            current_mtimes = _scan_source_mtimes(folder_path)
        except Exception as exc:  # a scan failure must not kill the watcher
            import logging
            logging.getLogger("firekeep_symdex.watch").warning(
                "Scan failed for %s: %s", folder_path, exc
            )
            continue

        if _changed(last_mtimes, current_mtimes):
            try:
                index_folder(
                    path=str(folder_path),
                    use_ai_summaries=False,
                    storage_path=storage_path,
                    incremental=True,
                )
            except Exception as exc:
                import logging
                logging.getLogger("firekeep_symdex.watch").warning(
                    "Auto-reindex failed for %s: %s", folder_path, exc
                )
        # Advance to the snapshot we just COMPARED, in both branches, and
        # deliberately not to a fresh post-reindex scan. A reindex takes real
        # time, and a file edited while it ran may or may not have made it in;
        # re-scanning afterwards would record that file's NEW mtime as already
        # seen and lose the edit permanently. Carrying `current_mtimes` forward
        # means such an edit simply reappears as a change on the next poll --
        # at worst one redundant reindex, never a dropped one.
        last_mtimes = current_mtimes


def watch_folder(path: str, storage_path: Optional[str] = None) -> dict:
    """Start watching a local folder for changes and auto-reindex.

    Args:
        path: Path to the local folder to watch.
        storage_path: Custom storage path.

    Returns:
        Status dict.
    """
    folder_path = Path(path).expanduser().resolve()

    if not folder_path.exists():
        return {"error": f"Folder not found: {path}"}
    if not folder_path.is_dir():
        return {"error": f"Path is not a directory: {path}"}

    key = str(folder_path)

    with _watch_lock:
        if key in _active_watches and _active_watches[key].is_alive():
            return {"status": "already_watching", "path": key}

        # Verify the folder is indexed
        store = IndexStore(base_path=storage_path)
        index = store.load_index("local", folder_path.name)
        if not index:
            return {"error": f"Folder not indexed. Run index_folder first: {path}"}

        stop_event = threading.Event()
        _stop_events[key] = stop_event

        thread = threading.Thread(
            target=_watcher_loop,
            args=(folder_path, storage_path, stop_event),
            daemon=True,
            name=f"watch-{folder_path.name}",
        )
        thread.start()
        _active_watches[key] = thread

    return {
        "status": "watching",
        "path": key,
        "poll_interval_seconds": POLL_INTERVAL,
    }


def unwatch_folder(path: str, storage_path: Optional[str] = None) -> dict:
    """Stop watching a folder.

    Args:
        path: Path to the folder to stop watching.
        storage_path: Unused, kept for API consistency.

    Returns:
        Status dict.
    """
    folder_path = Path(path).expanduser().resolve()
    key = str(folder_path)

    with _watch_lock:
        if key not in _active_watches:
            return {"error": f"Not watching: {path}"}

        stop_event = _stop_events.pop(key, None)
        if stop_event:
            stop_event.set()

        thread = _active_watches.pop(key)

    thread.join(timeout=POLL_INTERVAL + 2)

    return {"status": "stopped", "path": key}


def list_watches(storage_path: Optional[str] = None) -> dict:
    """List actively watched folders.

    Args:
        storage_path: Unused, kept for API consistency.

    Returns:
        Dict with list of watched paths.
    """
    with _watch_lock:
        active = []
        dead_keys = []

        for key, thread in _active_watches.items():
            if thread.is_alive():
                active.append(key)
            else:
                dead_keys.append(key)

        # Clean up dead threads
        for key in dead_keys:
            _active_watches.pop(key, None)
            _stop_events.pop(key, None)

    return {
        "watches": active,
        "count": len(active),
    }


TOOL_DEFS = [
    {
        "name": "watch_folder",
        "description": "Start watching a local folder for file changes and automatically trigger incremental reindex. Requires the folder to be indexed first.",
        "inputSchema": {
                    "type": "object",
                    "properties": {
                                "path": {
                                            "type": "string",
                                            "description": "Path to the local folder to watch"
                                }
                    },
                    "required": [
                                "path"
                    ]
        },
        "handler": watch_folder,
    },
    {
        "name": "unwatch_folder",
        "description": "Stop watching a folder for changes.",
        "inputSchema": {
                    "type": "object",
                    "properties": {
                                "path": {
                                            "type": "string",
                                            "description": "Path to the folder to stop watching"
                                }
                    },
                    "required": [
                                "path"
                    ]
        },
        "handler": unwatch_folder,
    },
    {
        "name": "list_watches",
        "description": "List all actively watched folders.",
        "inputSchema": {
                    "type": "object",
                    "properties": {}
        },
        "handler": list_watches,
    },
]
