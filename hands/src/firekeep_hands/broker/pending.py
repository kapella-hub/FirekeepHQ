"""`~/.firekeep/hands/pending.json` — what is waiting for a human, on disk.

The notification in `notify.py` is a moment; this is the record. A human who
missed the toast, or who wants to look before they press, runs `firekeep
hands status` and sees the same thing from the broker's own state rather
than from the runtime being gated.

It is a file rather than an HTTP route on purpose: `status` must work from a
process that holds no bearer token, and reading a file cannot be made to
hang the way a request to a wedged broker can.

Written atomically and `0600`, through the same helper as `config.json` and
`broker.json` — a half-written file must never be read as "nothing pending",
and the titles name what the human is about to do on their own machine.

Kept deliberately small: challenge, title, classes, and the seconds left.
Not a log — the previous contents are replaced on every change, and the
evidence ledger is what remembers.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import paths
from ..config import _write_json_atomic

log = logging.getLogger(__name__)


def pending_path() -> Path:
    """Beside `broker.json`, in the directory `paths` already owns. Defined
    here rather than in `paths.py` so the file's writer, its reader and its
    location stay in one module."""
    return paths.hands_home() / "pending.json"


def write_pending(store, *, chord: str = "", deny_chord: str = "") -> None:
    """Replace the file with the store's current pending set.

    Best-effort by contract: a disk that is full or read-only must not stop a
    permit being granted, so a failure here is a DEBUG line. The chords ride
    along so `status` can tell the human what to press without asking the
    broker."""
    try:
        rows = [
            {
                "challenge": permit.challenge,
                "title": permit.title,
                "classes": list(permit.classes),
                "expires_in_s": max(0.0, round(permit.expires_at - store.now(), 1)),
            }
            for permit in store.pending()
        ]
        _write_json_atomic(
            pending_path(),
            {"chord": chord, "deny_chord": deny_chord, "permits": rows},
        )
    except Exception as exc:  # noqa: BLE001 - never worth a failed approval
        log.debug("could not write pending.json: %s", exc)


def read_pending() -> dict:
    """`{"chord": …, "deny_chord": …, "permits": [...]}`, or empty defaults.

    Every failure — no file, unreadable, invalid JSON, JSON of the wrong
    shape — is "nothing pending", because this feeds a status line and a
    status command that raises is worse than one that under-reports. The
    caller should only trust it when the broker is answering: nothing removes
    this file when a broker is killed, so a stale one outlives its writer."""
    try:
        data = json.loads(pending_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"chord": "", "deny_chord": "", "permits": []}
    if not isinstance(data, dict):
        return {"chord": "", "deny_chord": "", "permits": []}
    permits = data.get("permits")
    return {
        "chord": data.get("chord") if isinstance(data.get("chord"), str) else "",
        "deny_chord": data.get("deny_chord") if isinstance(data.get("deny_chord"), str) else "",
        "permits": [row for row in permits if isinstance(row, dict)] if isinstance(permits, list) else [],
    }


def clear_pending() -> None:
    """Remove the file. Called when the broker stops, so a dead broker does
    not leave `status` describing approvals nobody is waiting on."""
    try:
        pending_path().unlink(missing_ok=True)
    except OSError as exc:
        log.debug("could not remove pending.json: %s", exc)
