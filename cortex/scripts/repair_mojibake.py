#!/usr/bin/env python3
"""Repair text corrupted by the pre-0.1.34 Windows client's cp1252 stdio.

WHAT HAPPENED. Every kit process read stdin and wrote stdout at the platform
default encoding. On Windows that is the ANSI code page (cp1252), not UTF-8, so
a UTF-8 em dash (`—`, bytes e2 80 94) written by an agent was decoded one byte
at a time into `â€"` and stored that way. The same call from the Linux VPS
round-tripped byte-perfect: the server was never involved. Measured on the live
relay queue 2026-08-06, 32 of 97 distill tasks created that day carried it, and
the owner's own presence entry reads `... Firekeep â€" due-diligence ...`.

`force_utf8_stdio()` (client 0.1.34) stops NEW corruption. It cannot undo what
is already stored. This script is that pass.

THE REPAIR IS THE DETECTOR, deliberately -- one operation, not a heuristic that
guesses followed by a fixer that might disagree with it:

    _encode_mojibake(s).decode("utf-8", errors="strict")

`_encode_mojibake` inverts a cp1252 decode per character (see its docstring for
why NEITHER cp1252 nor latin-1 alone is sufficient -- a real sample needs both,
and the first version of this script used latin-1 only and recovered nothing
from the live data). The decode is strict. Together that is what makes it safe
on text nobody corrupted:

  * Pure ASCII round-trips to itself. Unchanged -> not written.
  * Genuine accented text (`café`) re-encodes fine but FAILS the utf-8 decode --
    a lone 0xe9 is not valid UTF-8 -- so it is left alone.
  * Anything above U+00FF that is not a cp1252 special (`Привет`, `→`, emoji,
    and a correct `—`) is refused by the ENCODE. This is the important one:
    correctly-stored non-Latin text is rejected before a decode is attempted.

CONVERGENT, NOT IDEMPOTENT, and the difference is worth stating. A record
written through two corrupting hops is doubly mojibaked (`Ã¢â‚¬â€`) and needs
two rounds. So `repair()` loops to a fixed point rather than applying once, and
reports the round count -- a record needing >1 round is evidence of a second
corrupting hop, which is a finding, not noise. Re-running the script later is
safe: repaired text no longer round-trips, so it is not touched again.

ONE EXTRA GUARD beyond the round trip. Mojibake EXPANDS one character into two
or three from U+0080-U+00FF, so a real repair always REDUCES the count of
characters in that range. A candidate that does not reduce it is refused. This
is what protects the rare string that is genuinely `Ã©` and means it.

DRY RUN BY DEFAULT. `--apply` is required to write anything, and even then each
surface is repaired independently so a failure in one cannot leave another half
done. Every write is preceded by a JSONL backup of the exact prior values.

WHAT THIS DELIBERATELY DOES NOT DO: re-embed. Qdrant payloads are repaired with
`set_payload`, which leaves the VECTOR untouched -- so a repaired memory's
embedding still encodes the corrupted characters it was written from. That is
the right trade and not an oversight. The corruption is confined to punctuation
(em dashes, curly quotes, the odd accent), so the embedding delta is negligible
for retrieval, whereas re-embedding 19 memories would spend generation calls,
move their vectors, and change ranking on a store the dreaming A/B is scored
against. Fixing what a human READS is the point; the vector can be brought into
line by the existing `POST /admin/embeddings/reembed` if that is ever wanted,
as a separate and deliberate act.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Characters mojibake is built from. A real repair strictly reduces these.
_HIGH = range(0x80, 0x100)

#: A record needing more rounds than this is not mojibake, it is pathological.
#: Two is already unusual (it means two corrupting hops); five is a runaway stop.
MAX_ROUNDS = 5


def _high_count(s: str) -> int:
    return sum(1 for ch in s if ord(ch) in _HIGH)


#: Reverse of the cp1252 code page for the 0x80-0x9F range, where it and
#: latin-1 disagree. Built from the codec itself rather than hand-typed: a
#: hand-typed table of 27 lookalike punctuation characters is a transcription
#: bug waiting to happen, and this one decides whether customer data is
#: rewritten correctly.
_CP1252_HIGH: dict[str, int] = {}
for _b in range(0x80, 0xA0):
    try:
        _CP1252_HIGH[bytes([_b]).decode("cp1252")] = _b
    except UnicodeDecodeError:
        pass  # 0x81/0x8D/0x8F/0x90/0x9D are undefined in cp1252


def _encode_mojibake(s: str) -> bytes | None:
    """Re-encode assuming a cp1252 decode, per character, or None.

    NEITHER CODEC ALONE IS SUFFICIENT and a real sample proves it: `â€\\x9d`
    carries `€` (cp1252 0x80, absent from latin-1) beside U+009D (latin-1 0x9D,
    UNDEFINED in cp1252), so `.encode("cp1252")` and `.encode("latin-1")` both
    raise on the same string and a codec-at-a-time loop recovers nothing.

    Mixing is not exotic: the corrupting decode was cp1252, which has five
    undefined bytes, and whatever produced those characters did not map them
    the way cp1252 does. So this resolves per character -- cp1252's meaning
    where it has one, the raw byte otherwise -- which is exactly the mapping
    that inverts what happened.

    Still strict in the way that matters: any character above U+00FF that is
    not a cp1252 special returns None, so correctly-stored Cyrillic, CJK,
    emoji, arrows and real em dashes are refused before any decode is tried.
    """
    out = bytearray()
    for ch in s:
        cp = ord(ch)
        if cp < 0x80 or 0xA0 <= cp <= 0xFF:
            out.append(cp)          # cp1252 and latin-1 agree here
            continue
        b = _CP1252_HIGH.get(ch)
        if b is not None:
            out.append(b)           # a cp1252 printable from the 0x80-0x9F range
            continue
        if 0x80 <= cp <= 0x9F:
            out.append(cp)          # a latin-1 C1 control
            continue
        return None                 # genuinely non-Latin: not mojibake
    return bytes(out)


def repair_once(s: str) -> str | None:
    """One round. Returns the repaired string, or None if `s` is not mojibake.

    None means "leave this alone" for every reason: not a string, not
    encodable by any candidate codec, not valid UTF-8 once encoded, unchanged
    by the round trip, or changed without reducing the high-character count.
    """
    if not isinstance(s, str) or not s:
        return None
    raw = _encode_mojibake(s)
    if raw is None:
        return None
    try:
        candidate = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if candidate == s:
        return None  # round-trips to itself: correct text, not mojibake
    if _high_count(candidate) >= _high_count(s):
        # Mojibake always EXPANDS one character into two or three, so a real
        # recovery contracts. A change that does not is a coincidence.
        return None
    return candidate


def repair(s: str) -> tuple[str, int]:
    """Repair to a fixed point. Returns (text, rounds_applied)."""
    rounds = 0
    cur = s
    while rounds < MAX_ROUNDS:
        nxt = repair_once(cur)
        if nxt is None:
            break
        cur = nxt
        rounds += 1
    return cur, rounds


def repair_value(value):
    """Recursively repair strings inside dicts/lists, leaving other types alone.

    Returns (new_value, n_strings_changed, max_rounds_seen). Dict KEYS are
    repaired too -- a corrupted key is as wrong as a corrupted value, and
    leaving it would strand the value under an unreachable name.
    """
    if isinstance(value, str):
        out, rounds = repair(value)
        return out, (1 if rounds else 0), rounds
    if isinstance(value, list):
        changed = total = 0
        items = []
        for v in value:
            nv, c, r = repair_value(v)
            items.append(nv)
            changed += c
            total = max(total, r)
        return items, changed, total
    if isinstance(value, dict):
        changed = total = 0
        out = {}
        for k, v in value.items():
            nk, ck, rk = repair_value(k) if isinstance(k, str) else (k, 0, 0)
            nv, cv, rv = repair_value(v)
            out[nk] = nv
            changed += ck + cv
            total = max(total, rk, rv)
        return out, changed, total
    return value, 0, 0


@dataclass
class SurfaceReport:
    """What one storage surface would have done."""

    name: str
    scanned: int = 0
    affected: int = 0
    strings_changed: int = 0
    max_rounds: int = 0
    multi_round: int = 0
    written: int = 0
    error: str | None = None
    samples: list[tuple[str, str, str]] = field(default_factory=list)  # (id, before, after)

    def note(self, rec_id: str, before: str, after: str, strings: int, rounds: int) -> None:
        self.affected += 1
        self.strings_changed += strings
        self.max_rounds = max(self.max_rounds, rounds)
        if rounds > 1:
            self.multi_round += 1
        if len(self.samples) < 5:
            self.samples.append((rec_id, before[:160], after[:160]))


def _backup(path: Path, surface: str, rec_id: str, prior) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(
            {"surface": surface, "id": rec_id, "prior": prior}, ensure_ascii=False
        ) + "\n")


# --------------------------------------------------------------------------
# Surfaces. Each is independent: it opens its own client, and an exception is
# recorded against that surface rather than aborting the run.
# --------------------------------------------------------------------------

def scan_redis_hashes(url: str, pattern: str, fields: list[str], surface: str,
                      *, apply: bool, backup: Path | None) -> SurfaceReport:
    """Repair named fields of every Redis hash matching `pattern`."""
    rep = SurfaceReport(surface)
    try:
        import redis as _redis
    except ImportError:
        rep.error = "redis package not installed"
        return rep
    try:
        client = _redis.from_url(url, decode_responses=True)
        for key in client.scan_iter(match=pattern, count=500):
            # TYPE FIRST. A pattern wide enough to catch the records also
            # catches their index: `nr:presence:*` matches the sorted set
            # `nr:presence:__index`, and hgetall on it raises WRONGTYPE --
            # which, before this check, aborted the WHOLE surface at its first
            # key. Measured on the live store: relay:presence reported
            # "scanned 4, affected 0, ERROR" while genuinely holding corrupted
            # goal text. A scan that dies on an index reports zero and looks
            # like a clean surface, which is the worst way to be wrong here.
            try:
                if client.type(key) != "hash":
                    continue
            except Exception:
                continue
            rep.scanned += 1
            data = client.hgetall(key)
            if not data:
                continue
            updates = {}
            worst = 0
            n = 0
            sample_before = sample_after = ""
            for fname in fields:
                raw = data.get(fname)
                if not isinstance(raw, str) or not raw:
                    continue
                fixed, rounds = repair(raw)
                if rounds:
                    updates[fname] = fixed
                    worst = max(worst, rounds)
                    n += 1
                    if not sample_before:
                        sample_before, sample_after = raw, fixed
            if not updates:
                continue
            rep.note(key, sample_before, sample_after, n, worst)
            if apply:
                if backup is not None:
                    _backup(backup, surface, key, {k: data.get(k) for k in updates})
                client.hset(key, mapping=updates)
                rep.written += 1
    except Exception as exc:  # noqa: BLE001 - one surface must not kill the rest
        rep.error = f"{type(exc).__name__}: {exc}"
    return rep


def scan_qdrant(host: str, port: int, collection: str, *,
                apply: bool, backup: Path | None, limit: int = 0) -> SurfaceReport:
    """Repair string payload fields of every point in a Qdrant collection."""
    rep = SurfaceReport(f"qdrant:{collection}")
    try:
        from qdrant_client import QdrantClient
    except ImportError:
        rep.error = "qdrant_client not installed"
        return rep
    try:
        client = QdrantClient(host=host, port=port)
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection, limit=256, offset=offset,
                with_payload=True, with_vectors=False,
            )
            if not points:
                break
            for p in points:
                rep.scanned += 1
                payload = p.payload or {}
                fixed, n, rounds = repair_value(payload)
                if not n:
                    continue
                before = json.dumps(payload, ensure_ascii=False)
                after = json.dumps(fixed, ensure_ascii=False)
                rep.note(str(p.id), before, after, n, rounds)
                if apply:
                    if backup is not None:
                        _backup(backup, rep.name, str(p.id), payload)
                    # set_payload overwrites named keys only; the vector is
                    # untouched, so no re-embed and no ranking change.
                    client.set_payload(collection_name=collection,
                                       payload=fixed, points=[p.id])
                    rep.written += 1
            if offset is None or (limit and rep.scanned >= limit):
                break
    except Exception as exc:  # noqa: BLE001
        rep.error = f"{type(exc).__name__}: {exc}"
    return rep


def render(reports: list[SurfaceReport], *, apply: bool) -> str:
    out = ["", "=" * 78,
           ("APPLIED" if apply else "DRY RUN — nothing was written"),
           "=" * 78, ""]
    tot_a = tot_s = tot_w = 0
    out.append(f"{'surface':34} {'scanned':>8} {'affected':>9} {'strings':>8} {'written':>8}")
    out.append("-" * 78)
    for r in reports:
        tot_a += r.affected
        tot_s += r.strings_changed
        tot_w += r.written
        flag = "  ERROR" if r.error else ""
        out.append(f"{r.name:34} {r.scanned:>8} {r.affected:>9} "
                   f"{r.strings_changed:>8} {r.written:>8}{flag}")
        if r.error:
            out.append(f"    ! {r.error}")
        if r.multi_round:
            out.append(f"    ! {r.multi_round} record(s) needed >1 round — "
                       f"evidence of a SECOND corrupting hop, not noise")
    out.append("-" * 78)
    out.append(f"{'TOTAL':34} {'':>8} {tot_a:>9} {tot_s:>8} {tot_w:>8}")
    out.append("")
    for r in reports:
        if not r.samples:
            continue
        out.append(f"--- {r.name} ---")
        for rid, b, a in r.samples:
            out.append(f"  {rid}")
            out.append(f"    before: {b}")
            out.append(f"    after : {a}")
        out.append("")
    if not apply and tot_a:
        out.append("Re-run with --apply to write these. A JSONL backup of every prior")
        out.append("value is written first (--backup, default ./mojibake-backup.jsonl).")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="actually write repairs (default: dry run)")
    ap.add_argument("--backup", default="mojibake-backup.jsonl",
                    help="JSONL of prior values, written before any change")
    ap.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    ap.add_argument("--relay-url", default=os.getenv("NR_REDIS_URL", "redis://localhost:6379/5"))
    ap.add_argument("--qdrant-host", default=os.getenv("QDRANT_HOST", "localhost"))
    ap.add_argument("--qdrant-port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    ap.add_argument("--collection", default=os.getenv("QDRANT_COLLECTION", "memories"))
    ap.add_argument("--skip-qdrant", action="store_true")
    args = ap.parse_args(argv[1:])

    backup = None
    if args.apply:
        backup = Path(args.backup)
        stamp = datetime.now(timezone.utc).isoformat()
        backup.write_text(f'{{"_run": "{stamp}"}}\n', encoding="utf-8", newline="\n")

    reports = [
        scan_redis_hashes(args.relay_url, "nr:task:*",
                          ["title", "description", "context"], "relay:tasks",
                          apply=args.apply, backup=backup),
        scan_redis_hashes(args.relay_url, "nr:presence:*",
                          ["goal", "status"], "relay:presence",
                          apply=args.apply, backup=backup),
        # Bulletin POSTS are `nr:post:{pid}` hashes; `nr:bulletin` is the index
        # over them. The first version scanned "nr:bulletin*" and reported
        # "scanned 0" -- a wrong pattern and an empty surface are
        # indistinguishable in the output, so this is named against the source
        # (relay/app/mcp_server.py) rather than guessed.
        scan_redis_hashes(args.relay_url, "nr:post:*",
                          ["content", "title", "author"], "relay:bulletin",
                          apply=args.apply, backup=backup),
        scan_redis_hashes(args.relay_url, "nr:dm:*",
                          ["content", "from_id"], "relay:dm",
                          apply=args.apply, backup=backup),
    ]
    if not args.skip_qdrant:
        reports.append(scan_qdrant(args.qdrant_host, args.qdrant_port, args.collection,
                                   apply=args.apply, backup=backup))

    print(render(reports, apply=args.apply))
    return 1 if any(r.error for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
