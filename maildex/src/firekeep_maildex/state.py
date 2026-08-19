"""Per-account sync state — `~/.firekeep/maildex/state/<account_id>.json`.

What maildex believes the server holds for one mailbox, and the only thing
that makes an incremental sync possible.

**The watermark is per-(folder, UIDVALIDITY), and that pairing is M7.** An IMAP
UID means nothing on its own — it is unique only within one generation of one
mailbox, and a provider-side rebuild bumps UIDVALIDITY and starts UIDs over
from 1. A watermark that remembered `last_uid = 4000` across such a rebuild
would skip the mailbox's entire new contents in silence, which is the worst
failure this dex could have: no error, no gap in the output, just mail that is
never indexed. So the folder's generation is stored beside its watermark, a
mismatch re-baselines the folder from scratch, and the two are never mixed.

Per-message entries carry the same distinction docdex's `seen_hash` /
`ingested_hash` split carries. A message that extracts to nothing (an image
with no text) is recorded with no `ingested_at` and no `error`: nothing to
send, nothing to retry. A message whose ingest FAILED keeps `error`, and
`retry_uids` puts it back in the next run's work set even though the watermark
has moved past it.
"""
from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import maildex_dir, read_json, write_atomic

STATE_VERSION = 1


@dataclass
class FolderState:
    uidvalidity: int = 0
    last_uid: int = 0
    # Recorded so `list` can say WHY a folder suddenly re-fetched 90 days of
    # mail: the provider rebuilt it, and that is not a maildex bug.
    rebaselined_at: str | None = None


@dataclass
class MessageState:
    ingested_at: str | None = None
    truncated: bool = False
    # Retryable. Set only for failures a later run could plausibly succeed at:
    # a dropped fetch, a 503 from the corpus. `retry_uids` reads this field.
    error: str | None = None
    # Terminal, and reported rather than retried. A message whose MIME cannot
    # be decoded will not decode any better in six hours; putting it in the
    # retry set would re-fetch it forever for the same nothing, which is
    # exactly the trap docdex's seen/ingested split exists to avoid.
    note: str | None = None


@dataclass
class AccountState:
    folders: dict[str, FolderState] = field(default_factory=dict)
    messages: dict[str, MessageState] = field(default_factory=dict)
    last_sync_at: str | None = None

    def counts(self) -> dict[str, int]:
        """The numbers `list` and the doctor row read."""
        return {
            "messages": sum(1 for m in self.messages.values() if m.ingested_at),
            "failures": sum(1 for m in self.messages.values() if m.error),
            "truncated": sum(1 for m in self.messages.values() if m.truncated),
            "unparsed": sum(1 for m in self.messages.values() if m.note),
            "folders": len(self.folders),
        }


def message_key(folder: str, uidvalidity: int, uid: int) -> str:
    """The state key for one message.

    UIDVALIDITY is IN the key, not just alongside it: after a rebuild, UID 7 of
    generation 900 and UID 7 of generation 901 are different messages, and one
    key for both would have the new one inherit the old one's recorded failure.
    """
    return f"{folder}|{uidvalidity}|{uid}"


def state_dir() -> Path:
    # Deliberately does NOT create the directory: asking where a file WOULD be
    # must not bring it into existence, or "has this mailbox ever synced?"
    # answers itself wrongly. `write_atomic` creates the parent when there is
    # finally something to write.
    return maildex_dir() / "state"


def state_path(account_id: str) -> Path:
    return state_dir() / f"{account_id}.json"


def read_state(account_id: str) -> AccountState:
    """State for an account. An empty state for missing/corrupt — which costs a
    re-fetch of the backfill window, never a phantom set of deletions."""
    raw = read_json(state_path(account_id), what=f"maildex state {account_id}.json", default={})
    folders = {}
    for name, entry in (raw.get("folders") or {}).items():
        if isinstance(entry, dict):
            folders[name] = FolderState(**{
                k: v for k, v in entry.items() if k in FolderState.__dataclass_fields__
            })
    messages = {}
    for key, entry in (raw.get("messages") or {}).items():
        if isinstance(entry, dict):
            messages[key] = MessageState(**{
                k: v for k, v in entry.items() if k in MessageState.__dataclass_fields__
            })
    return AccountState(
        folders=folders, messages=messages, last_sync_at=raw.get("last_sync_at")
    )


def write_state(account_id: str, state: AccountState) -> None:
    write_atomic(state_path(account_id), {
        "version": STATE_VERSION,
        "folders": {name: asdict(fs) for name, fs in sorted(state.folders.items())},
        "messages": {key: asdict(ms) for key, ms in sorted(state.messages.items())},
        "last_sync_at": state.last_sync_at,
    })


def delete_state(account_id: str) -> None:
    try:
        state_path(account_id).unlink()
    except (FileNotFoundError, OSError):
        pass


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def folder_state(state: AccountState, folder: str) -> FolderState:
    fs = state.folders.get(folder)
    if fs is None:
        fs = FolderState()
        state.folders[folder] = fs
    return fs


def reconcile(state: AccountState, folder: str, uidvalidity: int) -> bool:
    """Line the folder's stored generation up with the server's. Returns True
    when a re-baseline happened (M7).

    A change means every UID we remember belongs to a mailbox that no longer
    exists. The watermark goes back to zero so the next search is a full
    backfill, and the old generation's message entries are dropped — keeping
    them would let a stale `error` from generation 900 put a UID from
    generation 901 in the retry set.

    The FIRST sight of a folder is not a re-baseline: there is nothing to
    invalidate, so it records the generation and reports False.
    """
    fs = folder_state(state, folder)
    if fs.uidvalidity == uidvalidity:
        return False
    first_sight = fs.uidvalidity == 0
    previous = fs.uidvalidity
    fs.uidvalidity = uidvalidity
    fs.last_uid = 0
    if first_sight:
        return False
    fs.rebaselined_at = now()
    stale = f"{folder}|{previous}|"
    for key in [k for k in state.messages if k.startswith(stale)]:
        del state.messages[key]
    return True


def retry_uids(state: AccountState, folder: str, uidvalidity: int) -> list[int]:
    """UIDs below the watermark whose last attempt FAILED.

    Without this, a single 503 during ingest would lose a message permanently:
    the watermark moves past it and no later search would ever name it again.
    """
    prefix = f"{folder}|{uidvalidity}|"
    out = []
    for key, ms in state.messages.items():
        if ms.error and key.startswith(prefix):
            try:
                out.append(int(key[len(prefix):]))
            except ValueError:  # pragma: no cover - keys are built, not parsed
                continue
    return sorted(out)


def advance(state: AccountState, folder: str, uid: int) -> None:
    """Move the watermark, never backwards.

    Called only after a message's outcome is RECORDED. A run that aborts on an
    unreachable server leaves the watermark where the last recorded message put
    it, so the next run resumes at exactly the message that did not land.
    """
    fs = folder_state(state, folder)
    if uid > fs.last_uid:
        fs.last_uid = uid


def record_ingested(state: AccountState, folder: str, uidvalidity: int, uid: int, *,
                    truncated: bool = False, note: str | None = None) -> None:
    """This message reached the server. `note` carries a partial-parse warning
    for a message that was ingested despite a broken part — the content is
    real, and so is the caveat."""
    state.messages[message_key(folder, uidvalidity, uid)] = MessageState(
        ingested_at=now(), truncated=truncated, error=None, note=_short(note)
    )
    advance(state, folder, uid)


def record_zero(state: AccountState, folder: str, uidvalidity: int, uid: int, *,
                note: str | None = None) -> None:
    """This message was fetched and had nothing to index.

    `ingested_at` stays empty because it is a factual claim about the server,
    and the server holds nothing for this message. `error` stays empty because
    there is nothing a retry would do differently — an image-only message is
    empty on every future fetch too. `note` says why, when there is a why.
    """
    state.messages[message_key(folder, uidvalidity, uid)] = MessageState(
        ingested_at=None, truncated=False, error=None, note=_short(note)
    )
    advance(state, folder, uid)


def _short(note: str | None) -> str | None:
    return str(note)[:500] if note else None


def record_failure(state: AccountState, folder: str, uidvalidity: int, uid: int,
                   error: str) -> None:
    """This message could not be fetched, parsed or ingested. It keeps whatever
    the server genuinely still holds, so a retry can tell "never landed" from
    "landed, then failed on a later attempt"."""
    key = message_key(folder, uidvalidity, uid)
    previous = state.messages.get(key)
    state.messages[key] = MessageState(
        ingested_at=previous.ingested_at if previous else None,
        truncated=previous.truncated if previous else False,
        error=str(error)[:500],
        note=previous.note if previous else None,
    )
    advance(state, folder, uid)
