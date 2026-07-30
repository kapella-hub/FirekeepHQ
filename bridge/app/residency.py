"""The residency contract — pure functions, no I/O.

`ctx_get_shadow()` with no argument is a FULL restore, byte-identical to what it
has always returned. That is the default and it is always correct. A caller may
opt into a delta by passing back the opaque cursor from an earlier full response,
which asserts one thing only: *the earlier shadow is still visible in my context*.

Every doubtful path here returns the full document. The design has no path that
omits content the agent lost; where it can be wrong, it is wrong in the direction
of sending too much.

Why timestamps and not counts: decisions/progress are stored LPUSH + LTRIM
(bridge/app/session.py), so the OLDEST entries are evicted. "I have seen the
first 47" stops meaning anything once that window shifts, and would silently
SKIP entries. Timestamps are stable under eviction because eviction only ever
removes entries the agent already received.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
from datetime import datetime
from typing import Any

_CURSOR_VERSION = 1
# Sections whose entries carry a per-entry timestamp and can therefore be filtered.
_LIST_SECTIONS = ("decisions", "progress")


def plan_sha_of(data: dict[str, Any]) -> str:
    return hashlib.sha256(str(data.get("plan") or "").encode("utf-8")).hexdigest()[:16]


def high_water_of(data: dict[str, Any]) -> str:
    """The newest timestamp anywhere in the session, as an ISO string. Empty when
    the session has no timestamped entry yet — which decodes to a full restore."""
    stamps: list[str] = []
    for section in _LIST_SECTIONS:
        for entry in data.get(section) or []:
            if isinstance(entry, dict) and (entry.get("timestamp") or ""):
                stamps.append(entry["timestamp"])
    for entry in (data.get("files") or {}).values():
        if isinstance(entry, dict) and (entry.get("last_action") or ""):
            stamps.append(entry["last_action"])
    return max(stamps) if stamps else ""


def encode_cursor(session_id: str, epoch: str, high_water: str, plan_sha: str) -> str:
    raw = json.dumps({"v": _CURSOR_VERSION, "sid": session_id, "epoch": str(epoch),
                      "hw": high_water, "plan_sha": plan_sha},
                     separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any] | None:
    """Parse a cursor, or None if it is anything other than one of ours. None is
    the safe answer: the caller turns it into a full restore."""
    if not cursor:
        return None
    try:
        pad = "=" * (-len(cursor) % 4)
        obj = json.loads(base64.urlsafe_b64decode(cursor + pad).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict) or obj.get("v") != _CURSOR_VERSION:
        return None
    if not isinstance(obj.get("sid"), str) or not isinstance(obj.get("hw"), str):
        return None
    return obj


def _keep_entry(stamp: object, hw: str) -> bool:
    """True if this entry must be KEPT.

    Unknown or unparseable age means we cannot PROVE the agent already has this entry, so
    it is kept: duplication beats omission. Parsing rather than comparing strings also
    removes a class of silent drop that raw comparison invites - a naive stamp
    ('2026-07-30T10:00:00') is a PREFIX of the same instant with an offset, so it sorts
    lexicographically LESS and would be dropped despite being newer-or-equal.
    """
    if not isinstance(stamp, str) or not stamp:
        return True                      # unknown -> keep
    try:
        a, b = datetime.fromisoformat(stamp), datetime.fromisoformat(hw)
    except (TypeError, ValueError):
        return True                      # unparseable -> keep
    try:
        return a >= b                    # INCLUSIVE: the boundary entry is re-sent
    except TypeError:
        return True                      # naive vs aware -> keep


def filter_since(
    data: dict[str, Any],
    cursor: str | None,
    *,
    session_id: str,
    epoch: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Return (data_to_render, omission_report).

    `omission_report is None` means no filtering happened — the caller must treat
    the result as a full restore and mint a fresh cursor. Any of these yields a
    full restore: no cursor, unparsable cursor, cursor minted for a different
    session, cursor carrying a stale epoch (precompact bumped it), or a cursor
    with no high-water mark.
    """
    parsed = decode_cursor(cursor) if cursor else None
    if parsed is None:
        return data, None
    if parsed.get("sid") != session_id:
        return data, None
    if str(parsed.get("epoch") or "") != str(epoch or ""):
        return data, None
    hw = parsed.get("hw") or ""
    if not hw.strip():
        return data, None

    out = dict(data)
    omitted: dict[str, Any] = {}

    # INCLUSIVE (>=): a boundary entry may be re-sent. Duplication beats omission.
    for section in _LIST_SECTIONS:
        entries = data.get(section) or []
        kept = [e for e in entries
                if not isinstance(e, dict) or _keep_entry(e.get("timestamp"), hw)]
        out[section] = kept
        omitted[section] = len(entries) - len(kept)

    files = data.get("files") or {}
    kept_files = {k: v for k, v in files.items()
                  if not isinstance(v, dict) or _keep_entry(v.get("last_action"), hw)}
    out["files"] = kept_files
    omitted["files"] = len(files) - len(kept_files)

    # scratch has NO per-entry timestamp — always sent in full, never counted as
    # omitted, because nothing was omitted.
    out["scratch"] = data.get("scratch") or {}

    plan_unchanged = plan_sha_of(data) == parsed.get("plan_sha")
    out["plan"] = "" if plan_unchanged else (data.get("plan") or "")
    omitted["plan"] = plan_unchanged

    return out, omitted


def omission_notice(omitted: dict[str, Any]) -> str:
    """The sentence that makes a delta safe to read.

    An agent reading a delta must never be able to conclude the omitted content
    DOES NOT EXIST — that inference is the degradation, not the omission. So the
    delta names what it withheld and how to get it.
    """
    parts: list[str] = []
    for label, key in (("decisions", "decisions"), ("progress entries", "progress"),
                       ("files", "files")):
        n = omitted.get(key) or 0
        if n:
            parts.append(f"{n} {label}")
    if omitted.get("plan"):
        parts.append("your unchanged plan")
    if not parts:
        return ""
    return (
        "DELTA RESTORE — omitted " + ", ".join(parts) + " that were delivered to you "
        "earlier in this conversation. They still exist. If they are no longer "
        "visible to you, call ctx_get_shadow() with no arguments for the full document."
    )
