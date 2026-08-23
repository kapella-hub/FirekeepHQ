#!/usr/bin/env python3
"""VPS field-failure ingest puller.

Spec: docs/superpowers/specs/2026-08-22-field-failure-reporting-design.md
("VPS ingest" steps 3-5). Runs on the Firekeep VPS host directly with the
system python3 (no venv, stdlib only): reads sealed segments a shell wrapper
(pull-failures.sh) has fetched into a durable inbox, INDEPENDENTLY re-validates
every line (the Hostinger log is untrusted input — this box does not trust
firekeep-site's failure-report.php to have gotten it right, and does not
import from firekeep_client, which isn't installed here), aggregates by
signature, and POSTs summarized events to Sentinel.

Vocabulary tables below are copied verbatim from
client/firekeep_client/report.py (source of truth for kind/stage/error/os/
arch/py/runtime/dex/backend) as of the field-failure-reporting spec.
PRE_VERSION_STAGES and the client-version regex have no report.py equivalent
(report.py never emits "unknown-bootstrap" -- that value only exists on the
server side) and are copied instead from firekeep-site's failure-report.php,
which is the other independent implementation of this same validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SOURCE = "firekeep.ai/failure-report"
PER_PULL_CEILING = 500

# --- Vocabulary tables (source of truth: client/firekeep_client/report.py) ---
KINDS = ("install", "connectivity", "runtime")
INSTALL_STAGES = (
    "bootstrap-home", "configure-config", "create-venv", "pip-install-client",
    "pip-install-dex", "lock-config-perms", "select-version", "render-adapters",
    "render-adapter", "add-to-path", "join-server",
)
BOOTSTRAP_STAGES = (
    "detect-platform", "fetch-manifest", "verify-checksum", "provision-python",
    "fetch-wheels", "create-venv", "install-wheels", "runnable-check",
    "flip-current", "handoff",
)
CONNECTIVITY_STAGES = ("cortex", "bridge", "sentinel", "relay", "server",
                       "embeddings", "backup")
RUNTIME_STAGES = (
    "session-start", "prompt", "pre-tool", "post-tool", "stop", "session-end",
    "precompact", "gateway-call", "gateway-dispatch",
)
ERRORS = (
    "permission-denied", "disk-full", "not-found", "dns-failure",
    "connection-refused", "network-unreachable", "tls-verify-failed", "timeout",
    "http-401", "http-403", "http-404", "http-429", "http-5xx",
    "unsupported-platform", "other",
)
OS_FAMILIES = ("darwin", "linux-gnu", "linux-musl", "windows")
ARCHES = ("x86_64", "arm64", "other")
PY_BUCKETS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "other")
RUNTIMES = ("claude", "codex", "kiro", "opencode", "claude-desktop", "generic")
DEX_NAMES = ("symdex", "docdex", "maildex")
BACKENDS = ("cortex", "bridge", "sentinel", "relay")

_STAGES_BY_KIND = {
    "install": INSTALL_STAGES + BOOTSTRAP_STAGES,
    "connectivity": CONNECTIVITY_STAGES,
    "runtime": RUNTIME_STAGES,
}

# --- Server-only vocabulary (source of truth: firekeep-site/failure-report.php) ---
# unknown-bootstrap is legal ONLY for install + these two pre-version stages.
PRE_VERSION_STAGES = ("detect-platform", "fetch-manifest")
# All three regexes below are matched with .fullmatch(), never .match(): a
# bare $ (no /D-style flag in Python's re) matches just before ONE trailing
# newline even under .match(), so "a"*32 + "\n" would otherwise pass the id
# check -- the exact anchor gotcha failure-report.php's /D exists to
# prevent, reachable here via a crafted log line. fullmatch is the actual
# guarantee, so the patterns below carry no ^/$ of their own.
_CLIENT_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")

_ID_RE = re.compile(r"[0-9a-f]{32}")
_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

# Outer sealed-segment line shape written by failure-report.php:
# {"ts": ..., "first": ..., "id": ..., "e": {...validated event...}}
_OUTER_KEYS = {"ts", "first", "id", "e"}
# "e" is the collector's own validated event dict. Its REQUIRED base keys
# never include "id" in the ingest brief's line shape, but the live collector
# in fact writes the full validated event (which does carry "id") into "e" --
# so "id" is accepted as an OPTIONAL key here, validated if present, rather
# than required. Everything else follows failure-report.php's validate_event
# tagged-union rules, reimplemented independently.
_E_REQUIRED_KEYS = {"kind", "stage", "error", "os", "arch", "client", "py"}
_E_OPTIONAL_KEYS = {"id", "exit", "runtime", "dex", "backend"}

_MISSING = object()


def _validate_e(e, outer_id: str) -> bool:
    if not isinstance(e, dict):
        return False
    keys = set(e.keys())
    if not _E_REQUIRED_KEYS.issubset(keys):
        return False
    if not keys.issubset(_E_REQUIRED_KEYS | _E_OPTIONAL_KEYS):
        return False

    kind, stage, error = e["kind"], e["stage"], e["error"]
    if kind not in KINDS:
        return False
    if not isinstance(stage, str) or stage not in _STAGES_BY_KIND.get(kind, ()):
        return False
    if error not in ERRORS:
        return False
    if e["os"] not in OS_FAMILIES:
        return False
    if e["arch"] not in ARCHES:
        return False
    if e["py"] not in PY_BUCKETS:
        return False

    # Shape only -- deliberately NOT checked against allowed-versions.txt.
    # That file is server-side runtime state on the collector host (mutated
    # out-of-band, not an embeddable closed vocabulary), so it's out of
    # scope for this box's independent re-validation. A client version this
    # box accepts as well-formed may still have been rejected by the
    # collector itself; the boundary here is shape plus downstream context
    # (Sentinel, not this script, is where that distinction would matter).
    client = e["client"]
    if not isinstance(client, str):
        return False
    if client == "unknown-bootstrap":
        if kind != "install" or stage not in PRE_VERSION_STAGES:
            return False
    elif not _CLIENT_VERSION_RE.fullmatch(client):
        return False

    if "id" in e:
        e_id = e["id"]
        if not isinstance(e_id, str) or not _ID_RE.fullmatch(e_id) or e_id != outer_id:
            return False

    if "exit" in e:
        exit_v = e["exit"]
        if (kind != "install" or isinstance(exit_v, bool)
                or not isinstance(exit_v, int) or not (0 <= exit_v <= 255)):
            return False
    if "runtime" in e:
        if stage != "render-adapter" or e["runtime"] not in RUNTIMES:
            return False
    if "dex" in e:
        if stage != "pip-install-dex" or e["dex"] not in DEX_NAMES:
            return False
    if "backend" in e:
        if kind != "runtime" or stage != "gateway-call" or e["backend"] not in BACKENDS:
            return False
    return True


def validate_line(raw: str) -> dict | None:
    """Untrusted input (spec step 4): exact key sets, every value against the
    embedded tables, id shape, unknown-bootstrap only on its two stages, full
    tagged union -- the same rules as failure-report.php, enforced AGAIN
    here. Returns the parsed dict on success, None on ANY violation."""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or set(obj.keys()) != _OUTER_KEYS:
        return None

    ts, first, line_id, e = obj["ts"], obj["first"], obj["id"], obj["e"]
    if not isinstance(ts, str) or not _TS_RE.fullmatch(ts):
        return None
    if not isinstance(first, bool):
        return None
    if not isinstance(line_id, str) or not _ID_RE.fullmatch(line_id):
        return None
    if not _validate_e(e, line_id):
        return None
    return obj


def _signature(e: dict) -> tuple[str, str, str, str, str]:
    return (e["kind"], e["stage"], e["error"], e["os"], e["client"])


def _batch_key(segment: str, sig: tuple[str, ...]) -> str:
    sig_hash = hashlib.sha256("|".join(sig).encode("utf-8")).hexdigest()
    return f"{segment}|{sig_hash}"


def _group_event(sig: tuple[str, str, str, str, str], group: list[dict],
                 segment: str) -> dict:
    kind, stage, error, os_family, client = sig
    rep_e = group[0]["e"]
    has_first = any(line["first"] for line in group)
    count = len(group)

    details = {
        "kind": kind, "stage": stage, "error": error, "os": os_family,
        "arch": rep_e["arch"], "client": client, "py": rep_e["py"],
        "first": has_first, "count": count,
        "batch": _batch_key(segment, sig), "integrity": "unverified",
    }
    # Tagged-union extras: only when every line in the group agrees, so a
    # summary event never asserts a value some of its constituent lines
    # didn't actually report.
    for key in ("exit", "runtime", "dex", "backend"):
        values = [line["e"].get(key, _MISSING) for line in group]
        first_v = values[0]
        if first_v is not _MISSING and all(v == first_v for v in values):
            details[key] = first_v

    summary = f"{kind} failure: {stage} {error} {os_family} {client} (n={count})"
    return {
        "source": SOURCE,
        "event_type": f"{kind}-failure",
        "summary": summary,
        "severity": "warning" if has_first else "info",
        "details": details,
    }


def _folded_event(folded_groups: list[tuple[tuple, list[dict]]], segment: str) -> dict:
    sig_count = len(folded_groups)
    line_count = sum(len(group) for _, group in folded_groups)
    has_first = any(line["first"] for _, group in folded_groups for line in group)
    return {
        "source": SOURCE,
        "event_type": "ingest-summary",
        "summary": (f"{sig_count} further signatures folded "
                    f"({line_count} events, segment {segment})"),
        "severity": "warning" if has_first else "info",
        "details": {
            "count": line_count, "signatures_folded": sig_count,
            "batch": f"{segment}|folded", "integrity": "unverified",
        },
    }


def aggregate(lines: list[dict], *, segment: str) -> list[dict]:
    """One Sentinel event per (kind|stage|error|os|client) signature, count in
    details, severity warning iff any line in the group has first=true, else
    info. details carries every dimension + first + count +
    batch=f"{segment}|{sig_hash}" + integrity="unverified". summary is built
    ONLY from the validated enum values:
    f"{kind} failure: {stage} {error} {os} {client} (n={count})".
    Over PER_PULL_CEILING signatures: emit the first 500 plus ONE summary
    event ("... {n} further signatures folded") -- never silent truncation."""
    groups: dict[tuple[str, str, str, str, str], list[dict]] = {}
    for line in lines:
        sig = _signature(line["e"])
        groups.setdefault(sig, []).append(line)

    items = list(groups.items())  # insertion order == first-occurrence order
    kept, folded = items[:PER_PULL_CEILING], items[PER_PULL_CEILING:]

    events = [_group_event(sig, group, segment) for sig, group in kept]
    if folded:
        events.append(_folded_event(folded, segment))
    return events


def post_events(events, sentinel_url, api_key, timeout=10):
    """urllib POST to {sentinel_url}/events with X-API-Key when set; raises on
    non-202 so the caller does NOT move the segment to done/."""
    if not events:
        return
    body = json.dumps(events).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    req = urllib.request.Request(
        f"{sentinel_url.rstrip('/')}/events", data=body, headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 202:
            raise RuntimeError(f"POST /events returned {resp.status}, expected 202")


def _read_valid_lines(segment_path: Path) -> list[dict]:
    raw_lines = segment_path.read_text(encoding="utf-8").splitlines()
    valid = []
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        parsed = validate_line(raw)
        if parsed is not None:
            valid.append(parsed)
    return valid


def process_inbox(inbox: Path, done: Path, sentinel_url: str, api_key: str) -> int:
    """For each failures.*.log in inbox (sorted): validate lines, aggregate,
    post, then move to done/ -- move ONLY after a 202 (VPS->Sentinel hop is
    at-least-once; the deterministic details.batch key lets consumers collapse
    replays, spec step 3). A segment whose POST fails is left in inbox for the
    next pull to retry; other segments in the same run are unaffected."""
    done.mkdir(parents=True, exist_ok=True)
    processed = 0
    for segment_path in sorted(inbox.glob("failures.*.log")):
        try:
            valid_lines = _read_valid_lines(segment_path)
            events = aggregate(valid_lines, segment=segment_path.name)
            if events:
                post_events(events, sentinel_url, api_key)
        except Exception as exc:  # noqa: BLE001 -- leave the segment for retry
            print(f"failure-ingest: {segment_path.name}: {exc}", file=sys.stderr)
            continue
        segment_path.rename(done / segment_path.name)
        processed += 1
    return processed


def _watchdog_check(inbox: Path) -> float | None:
    """When the inbox has been empty and the last successful collector ping
    marker (a timestamp file pull-failures.sh touches on a 200 from the
    maintenance ping) is older than 7 days, post ONE warning event. Runs
    every invocation while the condition holds -- this script keeps no state
    of its own to debounce repeats, so a stuck collector will alert on every
    cron tick until connectivity is restored (documented in README.md)."""
    if any(inbox.glob("failures.*.log")):
        return  # segments still pending: not "empty", nothing to watchdog
    marker = inbox.parent / "last-ping-ok"
    if not marker.exists():
        return
    age = time.time() - marker.stat().st_mtime
    if age <= 7 * 86400:
        return
    return age


def main():
    """argparse: --inbox --done --sentinel --api-key-env NAME [--dry-run].
    Also: when the inbox has been empty and the last successful collector ping
    marker (a timestamp file the shell wrapper touches) is older than 7 days,
    post ONE warning event: source firekeep.ai/failure-report, event_type
    "collector-watchdog", summary "no successful collector ping for 7 days"."""
    parser = argparse.ArgumentParser(description="VPS field-failure ingest puller")
    parser.add_argument("--inbox", required=True, type=Path)
    parser.add_argument("--done", required=True, type=Path)
    parser.add_argument("--sentinel", required=True)
    parser.add_argument("--api-key-env", default="FIREKEEP_INTERNAL_KEY")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "")

    if args.dry_run:
        segments = sorted(args.inbox.glob("failures.*.log"))
        print(f"dry-run: {len(segments)} segment(s) in inbox")
        for seg in segments:
            valid_lines = _read_valid_lines(seg)
            events = aggregate(valid_lines, segment=seg.name)
            print(f"  {seg.name}: {len(valid_lines)} valid line(s), "
                  f"{len(events)} event(s) would post")
        return

    processed = process_inbox(args.inbox, args.done, args.sentinel, api_key)
    print(f"processed {processed} segment(s)")

    age = _watchdog_check(args.inbox)
    if age:
        watchdog_event = {
            "source": SOURCE,
            "event_type": "collector-watchdog",
            "summary": "no successful collector ping for 7 days",
            "severity": "warning",
            "details": {"age_seconds": int(age), "integrity": "unverified"},
        }
        try:
            post_events([watchdog_event], args.sentinel, api_key)
        except Exception as exc:  # noqa: BLE001 -- best-effort alert, never fatal
            print(f"failure-ingest: watchdog post failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
