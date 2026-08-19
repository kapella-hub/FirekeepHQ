"""The mailbox registry — `~/.firekeep/maildex/accounts.json`.

The mailboxes a human told Firekeep it may read. Human-only by construction:
maildex ships no MCP server, so no agent tool can write this file.

**M3 lives here as an absence.** There is no password field, no token field and
no "remember this" flag. What this file holds is exactly what a person would
be comfortable reading aloud: a hostname, a port, their own address, the
folders they picked. The secret is in the Keep's vault under
`vault.vault_key(account_id)`, and `test_accounts.py` asserts that no value
handed to `add` ever reaches disk.
"""
from __future__ import annotations

import datetime
import secrets
from dataclasses import dataclass, replace
from pathlib import Path

from . import env_int, maildex_dir, read_json, write_atomic

ACTIVE = "active"
PENDING_DELETE = "pending_delete"

# M1 — mail is member-private, structurally. This is the only visibility that
# exists in the wheel; `wire.py` hardcodes it and takes no parameter that could
# change it. Sharing a mailbox is a different dex, not a flag on this one.
VISIBILITY = "member"

DEFAULT_PORT = 993
DEFAULT_FOLDERS: tuple[str, ...] = ("INBOX", "Sent")
DEFAULT_BACKFILL_DAYS = 90


def default_backfill_days() -> int:
    """`FIREKEEP_MAILDEX_BACKFILL_DAYS`, default 90 (M6).

    Read at `add` time and FROZEN onto the account: a horizon that moved with
    an env var would make "why is that March email missing?" unanswerable, and
    lowering it would silently strand replicas the sync can no longer see.
    """
    return env_int("FIREKEEP_MAILDEX_BACKFILL_DAYS", DEFAULT_BACKFILL_DAYS)


@dataclass(frozen=True)
class Account:
    id: str
    host: str
    port: int
    username: str
    folders: tuple[str, ...]
    backfill_days: int
    added_at: str
    status: str

    @property
    def visibility(self) -> str:
        # Not stored, not settable, not a parameter anywhere — read straight
        # off the module constant so there is nothing to get wrong (M1).
        return VISIBILITY


def accounts_path() -> Path:
    return maildex_dir() / "accounts.json"


def read_accounts() -> dict[str, dict]:
    """The raw registry. `{}` for missing/corrupt — corruption is logged and
    the file left in place."""
    return read_json(accounts_path(), what="maildex accounts.json", default={})


def write_accounts(entries: dict[str, dict]) -> None:
    write_atomic(accounts_path(), entries)


def _to_account(account_id: str, entry: dict) -> Account:
    folders = entry.get("folders")
    if not isinstance(folders, (list, tuple)) or not folders:
        folders = DEFAULT_FOLDERS
    try:
        port = int(entry.get("port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        days = int(entry.get("backfill_days") or DEFAULT_BACKFILL_DAYS)
    except (TypeError, ValueError):
        days = DEFAULT_BACKFILL_DAYS
    return Account(
        id=account_id,
        host=str(entry.get("host") or ""),
        port=port,
        username=str(entry.get("username") or ""),
        folders=tuple(str(f) for f in folders),
        backfill_days=days,
        added_at=str(entry.get("added_at") or ""),
        status=str(entry.get("status") or ACTIVE),
    )


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def normalize_folders(folders) -> tuple[str, ...]:
    """Trim, drop blanks, de-duplicate, preserve the order the human typed.

    Duplicates are not harmless: the same folder listed twice would be
    EXAMINEd twice per sync and burn the per-sync message budget on messages
    already ingested.
    """
    if folders is None:
        return DEFAULT_FOLDERS
    if isinstance(folders, str):
        folders = folders.split(",")
    out: list[str] = []
    for raw in folders:
        name = str(raw).strip()
        if name and name not in out:
            out.append(name)
    return tuple(out) or DEFAULT_FOLDERS


def add(host: str, username: str, *, port: int = DEFAULT_PORT,
        folders=None, backfill_days: int | None = None) -> Account:
    """Register a mailbox. Always member-private (M1).

    Takes NO password parameter, by design: a signature that accepts one is a
    signature somebody eventually passes on a command line, into a shell
    history, into a process list. `cli.add` reads it with `getpass` and hands
    it straight to the vault.

    Raises ValueError for a blank host/username and for a (host, username)
    pair already registered ACTIVE — two ids over one mailbox would ingest
    every message twice under two source names, and only a human could tell
    which replica to keep.
    """
    host = (host or "").strip()
    username = (username or "").strip()
    if not host:
        raise ValueError("a host is required (e.g. imap.gmail.com)")
    if not username:
        raise ValueError("a username is required (usually your email address)")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError(f"port must be a number, got {port!r}") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"port must be between 1 and 65535, got {port}")

    entries = read_accounts()
    for aid, entry in entries.items():
        same = (entry.get("host") == host and entry.get("username") == username)
        if same and (entry.get("status") or ACTIVE) == ACTIVE:
            raise ValueError(f"already registered as {aid}: {username} at {host}")

    # 128 bits minted here, never derived from the address: the id is half of a
    # corpus source name, and a name derived from an email address would leak
    # that address to anyone who can list source names.
    account_id = secrets.token_hex(16)
    while account_id in entries:  # pragma: no cover - 2^-128
        account_id = secrets.token_hex(16)

    days = default_backfill_days() if backfill_days is None else int(backfill_days)
    if days <= 0:
        raise ValueError(f"backfill days must be positive, got {backfill_days}")

    entries[account_id] = {
        "host": host,
        "port": port,
        "username": username,
        "folders": list(normalize_folders(folders)),
        "backfill_days": days,
        "added_at": _now(),
        "status": ACTIVE,
    }
    write_accounts(entries)
    return _to_account(account_id, entries[account_id])


def get(account_id: str) -> Account | None:
    entry = read_accounts().get(account_id)
    return _to_account(account_id, entry) if entry is not None else None


def list_accounts() -> list[Account]:
    """Every account, in registration order."""
    return [
        _to_account(aid, entry)
        for aid, entry in sorted(read_accounts().items(),
                                 key=lambda kv: kv[1].get("added_at") or "")
    ]


def remove_mark(account_id: str) -> Account:
    """Mark an account `pending_delete`. Idempotent.

    Step ONE of removal, and it happens BEFORE the lock is taken: a sync
    already running re-reads the registry between ingest batches, sees the
    flag, and stops — so a removal cannot lose a race with a sync that would
    otherwise re-upload mail the human asked to be gone.
    """
    entries = read_accounts()
    if account_id not in entries:
        raise ValueError(f"unknown account: {account_id}")
    entries[account_id]["status"] = PENDING_DELETE
    write_accounts(entries)
    return _to_account(account_id, entries[account_id])


def drop(account_id: str) -> None:
    """Forget an account entirely. Only ever called AFTER the server has
    confirmed its corpus replicas are gone — dropping earlier would strand them
    with nothing left on disk that knows their account id."""
    entries = read_accounts()
    if entries.pop(account_id, None) is not None:
        write_accounts(entries)


def rollback(account_id: str) -> None:
    """Undo a registration that never completed.

    `cli.add` registers, then stores the password. If the vault refuses, the
    account is un-registered here rather than left behind as a mailbox that
    can never sync and whose failure message is about a missing secret rather
    than about what actually went wrong.
    """
    drop(account_id)


def with_status(account: Account, status: str) -> Account:
    return replace(account, status=status)
