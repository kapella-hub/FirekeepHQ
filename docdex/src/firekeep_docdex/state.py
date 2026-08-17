"""Per-source sync state — `~/.firekeep/docdex/state/<source_id>.json`.

What docdex believes the server holds for one folder, and the only thing that
makes an incremental sync possible.

**`seen_hash` and `ingested_hash` are separate fields, and that is the whole
design (review #6).** One hash would force a choice between two wrong
behaviours:

* a file whose extraction honestly yields nothing (a scanned PDF — there is no
  OCR, I5) would never match "ingested" and would be re-extracted every six
  hours, forever, for the same zero;
* or it would be marked done, and a transient 503 during ingest would look
  identical — the file silently never reaching the server.

Split, both are exact: `seen_hash` says "these bytes were processed",
`ingested_hash` says "these bytes are on the server", and `error` distinguishes
"nothing to send" from "sending failed".
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import docdex_dir, read_json, write_atomic

STATE_VERSION = 1


@dataclass
class FileState:
    seen_hash: str | None = None
    ingested_hash: str | None = None
    ingested_at: str | None = None
    truncated: bool = False
    error: str | None = None
    pending_delete: bool = False


@dataclass
class SourceState:
    files: dict[str, FileState] = field(default_factory=dict)
    last_sync_at: str | None = None
    # Recorded so `list` can say WHY a source shows no deletions: the last walk
    # never completed, so none could be inferred (I4a).
    last_walk_completed: bool = False

    def counts(self) -> dict[str, int]:
        """The numbers the `list` output and the doctor row read."""
        return {
            "files": len(self.files),
            "failures": sum(1 for f in self.files.values() if f.error),
            "pending_deletes": sum(1 for f in self.files.values() if f.pending_delete),
            "truncated": sum(1 for f in self.files.values() if f.truncated),
        }


def state_dir() -> Path:
    # Deliberately does NOT create the directory: asking where a file WOULD be
    # must not bring it into existence, or "has this source ever synced?"
    # answers itself wrongly. `write_atomic` creates the parent when there is
    # finally something to write.
    return docdex_dir() / "state"


def state_path(source_id: str) -> Path:
    return state_dir() / f"{source_id}.json"


def read_state(source_id: str) -> SourceState:
    """State for a source. An empty state for missing/corrupt — which means a
    corrupt file costs a full re-ingest, never a phantom set of deletions."""
    raw = read_json(state_path(source_id), what=f"docdex state {source_id}.json", default={})
    files = {}
    for rel, entry in (raw.get("files") or {}).items():
        if isinstance(entry, dict):
            files[rel] = FileState(**{
                k: v for k, v in entry.items() if k in FileState.__dataclass_fields__
            })
    return SourceState(
        files=files,
        last_sync_at=raw.get("last_sync_at"),
        last_walk_completed=bool(raw.get("last_walk_completed")),
    )


def write_state(source_id: str, state: SourceState) -> None:
    write_atomic(state_path(source_id), {
        "version": STATE_VERSION,
        "files": {rel: asdict(fs) for rel, fs in sorted(state.files.items())},
        "last_sync_at": state.last_sync_at,
        "last_walk_completed": state.last_walk_completed,
    })


def delete_state(source_id: str) -> None:
    try:
        state_path(source_id).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def needs_sync(fs: FileState | None, digest: str) -> bool:
    """Does this file belong in the work set?

    Never seen, changed bytes, a recorded failure, or a tombstone whose file
    came back. Deliberately NOT `ingested_hash != digest`: that is the clause
    that would retry every honest zero forever.
    """
    if fs is None:
        return True
    if fs.seen_hash != digest:
        return True
    if fs.error:
        return True
    return bool(fs.pending_delete)


def record_ingested(state: SourceState, relpath: str, digest: str, *,
                    truncated: bool = False) -> None:
    state.files[relpath] = FileState(
        seen_hash=digest, ingested_hash=digest, ingested_at=now(),
        truncated=truncated, error=None, pending_delete=False,
    )


def record_seen_only(state: SourceState, relpath: str, digest: str) -> None:
    """These bytes were processed and produced nothing to send.

    `ingested_hash` stays empty because it is a factual claim about the server,
    and the server holds nothing for this file. `error` is cleared: a previous
    failure over older bytes says nothing about these.
    """
    state.files[relpath] = FileState(
        seen_hash=digest, ingested_hash=None, ingested_at=None,
        truncated=False, error=None, pending_delete=False,
    )


def record_failure(state: SourceState, relpath: str, digest: str, error: str) -> None:
    """Processing these bytes failed. `ingested_hash` keeps whatever the server
    genuinely still holds — usually the previous generation — so the retry can
    tell "never landed" from "landed, then changed"."""
    previous = state.files.get(relpath)
    state.files[relpath] = FileState(
        seen_hash=digest,
        ingested_hash=previous.ingested_hash if previous else None,
        ingested_at=previous.ingested_at if previous else None,
        truncated=previous.truncated if previous else False,
        error=str(error)[:500],
        pending_delete=False,
    )


def mark_pending_delete(state: SourceState, relpath: str) -> None:
    """Tombstone a file whose bytes are gone locally. The entry SURVIVES until
    the server confirms the replica is deleted — dropping it here would lose
    the only record that a replica still needs removing."""
    fs = state.files.get(relpath)
    if fs is None:
        return
    fs.pending_delete = True


def clear(state: SourceState, relpath: str) -> None:
    """Forget a file entirely — only after its replica is confirmed gone."""
    state.files.pop(relpath, None)
