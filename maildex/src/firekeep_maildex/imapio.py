"""The read-only IMAP session — the only module in the wheel that speaks IMAP.

**M2 is the whole point of this file.** Every mailbox is opened with
`select(..., readonly=True)`, which is IMAP `EXAMINE`: the SERVER then refuses
any state-changing command on that mailbox for the life of the connection. That
is the difference between "we are careful" and "we cannot" — a bug in `sync`,
a malicious folder name, a confused retry loop, none of them can flag, move or
delete a message, because the permission does not exist on the wire.

Every fetch is `BODY.PEEK[]` for the same reason one layer down: a plain
`BODY[]` fetch sets `\\Seen`. Reading the Keep's copy of a mailbox must not mark
a person's mail as read; that is their inbox, not ours.

`Session` deliberately exposes four reads — examine, two searches, one fetch —
and no escape hatch to the raw connection's other methods. `tests/test_no_mutation.py` asserts, structurally,
that no other module imports `imaplib`, that this module touches only the
allowlisted connection methods, and that no mutating IMAP verb appears as a
string literal anywhere in the package.
"""
from __future__ import annotations

import datetime
import re
import ssl
from contextlib import contextmanager
from typing import Iterator

DEFAULT_PORT = 993

# IMAP dates are locale-independent: `01-Sep-2026`, always English, always the
# same three letters. `strftime("%b")` would produce whatever the machine's
# locale says, and a German workstation would send `01-Okt-2026` and get a
# parse error from the server rather than a smaller result set.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# An IMAP atom needs no quoting; anything else does. `[Gmail]/Sent Mail` is the
# common case that breaks a naive implementation — imaplib passes the mailbox
# name through verbatim, so an unquoted space becomes two arguments.
_ATOM = re.compile(r"^[A-Za-z0-9_./-]+$")

_UIDVALIDITY = "UIDVALIDITY"


class ImapError(RuntimeError):
    """Anything the mail server did that stops us reading it."""


class AuthError(ImapError):
    """The credentials were rejected.

    Split from ImapError because the human action is completely different: an
    expired app password needs re-adding the account, not retrying the sync.
    """


def imap_date(day: datetime.date) -> str:
    """`SINCE`'s date literal — `05-Aug-2026`."""
    return f"{day.day:02d}-{_MONTHS[day.month - 1]}-{day.year}"


def _mailbox(folder: str) -> str:
    if _ATOM.match(folder):
        return folder
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _ok(typ, data, what: str):
    if typ != "OK":
        detail = _first_text(data) or typ
        raise ImapError(f"{what} failed: {detail}")
    return data


def _first_text(data) -> str:
    for item in data or ():
        if isinstance(item, bytes):
            return item.decode("utf-8", "replace")
        if isinstance(item, str):
            return item
    return ""


class Session:
    """One authenticated, read-only connection.

    Holds the connection privately: every method below is a read, and there is
    no accessor that would let a caller reach past them.
    """

    def __init__(self, conn):
        self._conn = conn

    def examine(self, folder: str) -> int:
        """Open `folder` read-only and return its UIDVALIDITY (M7).

        `readonly=True` is not a default anyone can change — it is written at
        the single call site, and the structural test fails the build if a
        `select(` anywhere in the package omits it.
        """
        typ, data = self._conn.select(_mailbox(folder), readonly=True)
        _ok(typ, data, f"opening {folder}")
        return self._uidvalidity(folder)

    def _uidvalidity(self, folder: str) -> int:
        """The value the server reported for the mailbox just opened.

        Mandatory in every SELECT/EXAMINE response, so a server that omits it
        is one we cannot safely keep watermarks for — refuse rather than guess
        a value and mix two generations of UIDs (M7).
        """
        try:
            _, data = self._conn.response(_UIDVALIDITY)
        except Exception as e:  # noqa: BLE001 - a client library, not our contract
            raise ImapError(f"{folder}: could not read UIDVALIDITY: {e}") from e
        text = _first_text(data).strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            raise ImapError(
                f"{folder}: the server reported no usable UIDVALIDITY ({text!r}) — "
                f"maildex will not keep watermarks it cannot trust"
            ) from None

    def search_since(self, day: datetime.date) -> list[int]:
        """UIDs of messages received on or after `day` — the backfill query."""
        typ, data = self._conn.uid("SEARCH", None, "SINCE", imap_date(day))
        return _uids(_ok(typ, data, "searching by date"))

    def search_after(self, last_uid: int) -> list[int]:
        """UIDs above the watermark — the incremental query.

        Filtered client-side as well as server-side, because `UID n:*` is
        specified to return the highest UID in the mailbox even when it is
        BELOW `n`. Without the filter, every sync of a quiet mailbox would
        re-fetch and re-ingest its newest message forever.
        """
        typ, data = self._conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        return [uid for uid in _uids(_ok(typ, data, "searching by uid")) if uid > last_uid]

    def fetch(self, uid: int) -> bytes:
        """One message's raw bytes, WITHOUT setting `\\Seen` (M2)."""
        typ, data = self._conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        _ok(typ, data, f"fetching uid {uid}")
        for item in data or ():
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        # A UID that vanished between SEARCH and FETCH answers OK with no
        # literal. That is a message the human deleted a moment ago, not a
        # failure of the run.
        raise ImapError(f"uid {uid} returned no message body")


def _uids(data) -> list[int]:
    out: list[int] = []
    for item in data or ():
        if isinstance(item, (bytes, bytearray)):
            item = item.decode("ascii", "ignore")
        if not isinstance(item, str):
            continue
        for token in item.split():
            try:
                out.append(int(token))
            except ValueError:
                continue
    return sorted(set(out))


def _default_connector(host: str, port: int, timeout: float):
    import imaplib

    # `ssl.create_default_context()` is passed EXPLICITLY, and that is not
    # belt-and-braces. imaplib's own fallback context verifies NEITHER the
    # certificate chain NOR the hostname — "the stdlib default" for IMAP is an
    # unauthenticated TLS tunnel. Spec §5 rules out self-signed endpoints
    # precisely because verification is on; taking the library at its word
    # would have shipped the exact opposite of what the docs promise.
    return imaplib.IMAP4_SSL(
        host, port, ssl_context=ssl.create_default_context(), timeout=timeout
    )


@contextmanager
def session(host: str, port: int, username: str, password: str, *,
            connector=None, timeout: float = 60.0) -> Iterator[Session]:
    """Connect, authenticate, yield a read-only session, always log out.

    `password` is a parameter and never an attribute: nothing in this module
    stores it, so nothing in this module can leak it into a repr, a log line
    or a pickled traceback.
    """
    connect = connector or _default_connector
    try:
        conn = connect(host, port, timeout)
    except Exception as e:  # noqa: BLE001 - socket, DNS, TLS all land here
        raise ImapError(f"cannot connect to {host}:{port}: {e}") from e
    try:
        try:
            typ, data = conn.login(username, password)
        except Exception as e:  # noqa: BLE001 - imaplib raises on a NO response
            raise AuthError(f"{username} was rejected by {host}: {e}") from e
        if typ != "OK":
            raise AuthError(f"{username} was rejected by {host}: {_first_text(data)}")
        yield Session(conn)
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
