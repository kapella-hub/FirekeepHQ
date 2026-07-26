"""File watching for auto-reindex on changes."""

import threading
from pathlib import Path
from typing import Optional

from ..storage import IndexStore
from .index_folder import index_folder


# Active watcher threads keyed by resolved folder path
_active_watches: dict[str, threading.Thread] = {}
# Signals to stop watcher threads
_stop_events: dict[str, threading.Event] = {}
# Guard concurrent access to _active_watches and _stop_events
_watch_lock = threading.Lock()

POLL_INTERVAL = 5  # seconds


def _get_indexed_mtimes(store: IndexStore, owner: str, name: str, folder_path: Path) -> dict[str, float]:
    """Get current mtimes for all files in the index."""
    index = store.load_index(owner, name)
    if not index:
        return {}

    mtimes: dict[str, float] = {}
    for sym in index.symbols:
        rel = sym["file"]
        if rel in mtimes:
            continue
        full = folder_path / rel
        try:
            mtimes[rel] = full.stat().st_mtime
        except OSError:
            pass
    return mtimes


def _watcher_loop(folder_path: Path, storage_path: Optional[str], stop_event: threading.Event):
    """Poll loop that detects file changes and triggers incremental reindex."""
    owner = "local"
    name = folder_path.name
    store = IndexStore(base_path=storage_path)

    # Snapshot current mtimes
    last_mtimes = _get_indexed_mtimes(store, owner, name, folder_path)

    while not stop_event.is_set():
        stop_event.wait(POLL_INTERVAL)
        if stop_event.is_set():
            break

        current_mtimes = _get_indexed_mtimes(store, owner, name, folder_path)

        changed = False
        for rel, mtime in current_mtimes.items():
            if rel not in last_mtimes or last_mtimes[rel] != mtime:
                changed = True
                break

        if not changed:
            # Check for deleted files
            for rel in last_mtimes:
                if rel not in current_mtimes:
                    changed = True
                    break

        if changed:
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
            # Refresh snapshot after reindex
            last_mtimes = _get_indexed_mtimes(store, owner, name, folder_path)
        else:
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
