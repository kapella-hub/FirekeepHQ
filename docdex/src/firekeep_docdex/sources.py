"""The folder registry — `~/.firekeep/docdex/sources.json`.

The list of folders a human told Firekeep it may understand. Human-only by
construction: no agent tool writes this file (spec I2).

A missing path is REPORTED, never dropped and never interpreted as deletion
(I4a): an unplugged USB drive must not silently unregister a source, let alone
wipe its index.
"""
from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from . import docdex_dir, read_json, write_atomic

ACTIVE = "active"
PENDING_DELETE = "pending_delete"

MEMBER = "member"       # private to me, even on a shared Keep — the default
WORKSPACE = "workspace"  # shared with my workspace


@dataclass(frozen=True)
class Source:
    id: str
    path: str
    visibility: str
    added_at: str
    status: str
    missing: bool = False  # computed at list time, never stored

    @property
    def root(self) -> Path:
        return Path(self.path)


def sources_path() -> Path:
    return docdex_dir() / "sources.json"


def read_sources() -> dict[str, dict]:
    """The raw registry. `{}` for missing/corrupt — corruption is logged and the
    file left in place."""
    return read_json(sources_path(), what="docdex sources.json", default={})


def write_sources(entries: dict[str, dict]) -> None:
    write_atomic(sources_path(), entries)


def _to_source(source_id: str, entry: dict) -> Source:
    path = str(entry.get("path") or "")
    return Source(
        id=source_id,
        path=path,
        visibility=entry.get("visibility") or MEMBER,
        added_at=entry.get("added_at") or "",
        status=entry.get("status") or ACTIVE,
    )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def add(path: str | Path, *, shared: bool = False) -> Source:
    """Register a folder. Private (`member`) unless `shared`.

    Raises ValueError for a path that is not an existing folder, and for a
    folder already registered ACTIVE — two ids over one folder would ingest
    every file twice under two source names, and only a human could tell which
    replica to keep.
    """
    root = Path(path).expanduser()
    try:
        root = root.resolve()
    except OSError as e:  # pragma: no cover - platform-specific resolve failure
        raise ValueError(f"cannot resolve {path}: {e}") from e
    if not root.exists():
        raise ValueError(f"no such folder: {root}")
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")

    entries = read_sources()
    for sid, entry in entries.items():
        if entry.get("path") == str(root) and (entry.get("status") or ACTIVE) == ACTIVE:
            raise ValueError(f"already registered as {sid}: {root}")

    # 128 bits minted here, not derived from the path: review #3's collision
    # point. Two members' `~/Notes` must never produce the same source id, and
    # the id is half of a corpus source name that must not leak the folder.
    source_id = secrets.token_hex(16)
    while source_id in entries:  # pragma: no cover - 2^-128
        source_id = secrets.token_hex(16)
    entries[source_id] = {
        "path": str(root),
        "visibility": WORKSPACE if shared else MEMBER,
        "added_at": _now(),
        "status": ACTIVE,
    }
    write_sources(entries)
    return _to_source(source_id, entries[source_id])


def get(source_id: str) -> Source | None:
    entry = read_sources().get(source_id)
    return _to_source(source_id, entry) if entry is not None else None


def list_sources() -> list[Source]:
    """Every source, in registration order, with `missing` computed live."""
    out = []
    for sid, entry in sorted(read_sources().items(), key=lambda kv: kv[1].get("added_at") or ""):
        src = _to_source(sid, entry)
        out.append(replace(src, missing=not _exists(src.root)))
    return out


def _exists(root: Path) -> bool:
    try:
        return root.is_dir()
    except OSError:
        return False


def remove_mark(source_id: str) -> Source:
    """Mark a source `pending_delete`. Idempotent.

    This is step ONE of removal and happens BEFORE the lock is taken: a sync
    already running re-reads the registry between ingest batches, sees the
    flag, and stops — so a removal cannot lose a race with a sync that would
    otherwise resurrect content the human asked to be gone.
    """
    entries = read_sources()
    if source_id not in entries:
        raise ValueError(f"unknown source: {source_id}")
    entries[source_id]["status"] = PENDING_DELETE
    write_sources(entries)
    return _to_source(source_id, entries[source_id])


def drop(source_id: str) -> None:
    """Forget a source entirely. Only ever called AFTER the server has confirmed
    its corpus replicas are gone — dropping earlier would strand them with
    nothing left on disk that knows their source id."""
    entries = read_sources()
    if entries.pop(source_id, None) is not None:
        write_sources(entries)
