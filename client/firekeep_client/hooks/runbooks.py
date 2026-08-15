"""Enforced Runbooks — the client half of the wire contract (Phase B).

Spec: docs/superpowers/specs/2026-08-15-enforced-runbooks-design.md (normative
for every name, key shape, TTL, verdict and posture used here).

The command's journey through this module (called from pre_tool's run_command
branch, AFTER the destructive snapshot-then-allow guard, which is unchanged):

  local match against the session's runbook BUNDLE (state.read_runbook_bundle)
    no match -> exit 0, ZERO network — exactly today's behaviour
    match    -> POST /agent/action/before (adapter "shell-hook", cwd for audit,
                EXPLICIT 5s timeout — the transport default is 10s)
                verdict: allow -> 0 (advisory on stderr when present)
                         rethink -> 1   block -> 2

Failure postures (spec "Failure postures"): advise / require_ack matches fail
OPEN on network failure (hooklog + one stderr line). block matches fail CLOSED:
`_gate_closed` initialises its exit code to 2 BEFORE any network I/O, only an
authenticated allow (which is also how a consumed require_ack permit arrives)
lowers it to 0, and the whole branch is exception-tight so the outer
@never_raise(0) wrapper can never observe a failure from it and wave the
command through (review finding 3).

MATCH NORMALIZATION PARITY: `normalize_command` collapses every run of
whitespace to a single space and strips the ends (`" ".join(cmd.split())`),
then patterns are matched with `fnmatch.fnmatchcase` — case-SENSITIVE on every
platform (`fnmatch.fnmatch` would be case-insensitive on Windows while the
Linux server is not). These rules MUST stay byte-identical to the server-side
command matcher (cortex/app/procedures/match.py): the client decides WHETHER to
escalate with this matcher, the server decides WHAT the verdict is with its
own; if they disagree, a command silently skips its runbook. Pinned by
client/tests/test_runbook_match_parity.py.

Command matching is mistake-catching, not adversary-proof (spec: stated on
every surface that shows it) — the owner can always bypass by reconfiguring
their own client.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import sys

from firekeep_client import hooklog, resolver, state, transport

_HOOK = "pre_tool"          # gate failures surface under the hook that runs them
_SYNC_HOOK = "session_start"

# The transport default timeout is 10s; the escalation call pins its OWN
# (spec tightening: "the transport default timeout is 10s so the escalation
# call pins its own (5s)") — it sits on the hottest tool in the kit.
ESCALATION_TIMEOUT = 5.0


def _quiet_log(hook: str, message: str) -> None:
    """hooklog.log_failure never raises by contract, but the fail-closed branch
    must not depend on any collaborator honouring its contract."""
    try:
        hooklog.log_failure(hook, message)
    except Exception:  # noqa: BLE001
        pass


# --- local matching (pure; never raises) -------------------------------------


def normalize_command(command: object) -> str:
    """Whitespace-normalize a command string: collapse every run of whitespace
    (space, tab, newline, CR, vertical tab, form feed) to one space and strip
    the ends. THE parity rule with the server matcher — byte-identical to
    cortex normalize_command by review (2026-08-15): a non-string is REFUSED,
    not repr'd (str(None) is the matchable four-character command "None"),
    and the normalized string is bounded at 4096 chars exactly as the server
    bounds it — an unbounded client would escalate a suffix-anchored match on
    a >4096-char command that the truncating server then cannot see."""
    try:
        if not isinstance(command, str):
            return ""
        return " ".join(command.split()).strip()[:4096]
    except Exception:  # noqa: BLE001
        return ""


def match_entries(entries: list, command: str) -> list[dict]:
    """Bundle entries whose glob `pattern` matches the normalized command.

    Patterns are normalized identically to the command before matching, and
    matched with fnmatchcase (case-sensitive on every platform — Linux-server
    parity). A hostile or malformed entry is skipped, never raised on. Entries
    carrying a non-command `kind` are skipped (the bundle is command-kind by
    construction; tolerate a future field rather than mismatching on it)."""
    cmd = normalize_command(command)
    if not cmd:
        return []
    out: list[dict] = []
    for e in entries if isinstance(entries, list) else []:
        try:
            if not isinstance(e, dict):
                continue
            if e.get("kind") not in (None, "command"):
                continue
            pattern = normalize_command(e.get("pattern") or "")
            if pattern and fnmatch.fnmatchcase(cmd, pattern):
                out.append(e)
        except Exception:  # noqa: BLE001
            continue
    return out


def _fail_closed(entry: dict) -> bool:
    """Does this matched entry demand the fail-CLOSED posture? Mode `block` is
    the normative trigger; an explicit `fail_posture: "closed"` from the server
    is honoured too (conservative: either signal closes). An unreadable entry
    counts as closed — in doubt, enforce."""
    try:
        return entry.get("mode") == "block" or entry.get("fail_posture") == "closed"
    except Exception:  # noqa: BLE001
        return True


# --- bundle fetch / ack ------------------------------------------------------


def fetch_and_store(cfg=None, *, session_id: str | None = None,
                    timeout: float | None = None, hook: str = _SYNC_HOOK) -> dict | None:
    """GET /procedures/bundle and persist it as last-known-good.

    Returns the STORED record (with its fetched_at stamp) on success, None on
    ANY failure — and a failure leaves the previously stored bundle untouched
    (last-known-good, spec "Bundle"). Never raises."""
    try:
        cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
        kwargs: dict = {"headers": cep.headers, "verify": cep.verify}
        if timeout is not None:
            kwargs["timeout"] = timeout
        data = transport.get_json(f"{cep.rest_base}/procedures/bundle", **kwargs)
        if not state.write_runbook_bundle(data):
            _quiet_log(hook, "runbook bundle payload invalid; keeping last-known-good")
            return None
        return state.read_runbook_bundle(data["workspace_id"])
    except Exception as e:  # noqa: BLE001
        _quiet_log(hook, f"runbook bundle fetch failed; keeping last-known-good: {e}")
        return None


def sync_bundle(cfg=None, *, session_id: str | None = None) -> dict | None:
    """Session-start bundle handshake: fetch + store, then POST
    /procedures/bundle/ack {version} so the server can report which sessions
    hold which version (dashboard "NOT ACTIVELY ENFORCED" coverage honesty —
    the ack is reporting, not enforcement, so its failure is logged and
    swallowed). Deliberately INDEPENDENT of the briefing call: neither fetch
    may be coupled to the other's failure. Never raises."""
    stored = fetch_and_store(cfg, session_id=session_id)
    if stored is None:
        return None
    try:
        version = stored.get("version") or ""
        if version:
            cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
            transport.post_json(
                f"{cep.rest_base}/procedures/bundle/ack",
                {"version": version},
                headers=cep.headers, verify=cep.verify,
            )
    except Exception as e:  # noqa: BLE001
        _quiet_log(_SYNC_HOOK, f"runbook bundle ack failed: {e}")
    return stored


# --- the pre_tool gate -------------------------------------------------------


def gate(command: str, payload: dict, cfg, agent: str) -> int:
    """Runbook escalation for one shell command. Returns the pre_tool exit
    code: 0 allow (advisory on stderr when present), 1 rethink, 2 block.

    No stored bundle, or no matching entry -> 0 with ZERO added network —
    exactly today's behaviour for every command outside a runbook. The posture
    split happens BEFORE any network I/O, on the last-known-good bundle: once a
    block-mode entry has matched, everything network-bound (including the
    staleness refetch) runs inside the exception-tight fail-closed branch."""
    bundle = state.read_runbook_bundle()
    if not bundle:
        return 0
    matched = match_entries(bundle.get("entries") or [], command)
    if not matched:
        return 0
    stale = state.runbook_bundle_is_stale(bundle)
    closed = [e for e in matched if _fail_closed(e)]
    if closed:
        return _gate_closed(command, payload, cfg, agent, closed, bundle_stale=stale)
    if stale:
        # Stale bundle + an advisory match: one refetch attempt so a retired
        # pattern or a changed mode is honoured; failure keeps last-known-good.
        fresh = fetch_and_store(cfg, timeout=ESCALATION_TIMEOUT, hook=_HOOK)
        if fresh is not None:
            matched = match_entries(fresh.get("entries") or [], command)
            if not matched:
                return 0
            closed = [e for e in matched if _fail_closed(e)]
            if closed:
                # Promoted to block while we were stale — enforce the fresh mode.
                return _gate_closed(command, payload, cfg, agent, closed,
                                    bundle_stale=False)
    return _gate_open(command, payload, cfg, agent)


def _escalate(command: str, payload: dict, cfg, agent: str, session_id: str):
    """POST /agent/action/before for a runbook-matched command. cwd rides in
    the action for audit (spec: "cwd sent for audit"); old servers ignore the
    extra field (pydantic default), Phase A records it."""
    cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
    try:
        cwd = payload.get("cwd") or os.getcwd()
    except Exception:  # noqa: BLE001
        cwd = ""
    return transport.post_json(
        f"{cep.rest_base}/agent/action/before",
        {"session_id": session_id, "agent_id": agent, "adapter": "shell-hook",
         "action": {"type": "run_command", "target": command, "cwd": cwd}},
        headers=cep.headers, verify=cep.verify,
        # EXPLICIT — the transport default is 10s; the spec pins 5s here.
        timeout=ESCALATION_TIMEOUT,
    )


def _reasons(resp: dict) -> str:
    """Advisory messages flattened the way pre_tool always has ('; '-joined
    `message` fields) — anything a human needs must be in `message`."""
    try:
        advisories = resp.get("advisories") or []
        return "; ".join(a.get("message", "") for a in advisories
                         if isinstance(a, dict) and a.get("message"))
    except Exception:  # noqa: BLE001
        return ""


def _evaluated(resp: object) -> bool:
    """Whether the verdict carries the server's positive-evaluation receipt
    (advisory code `runbook_evaluated`, empty message so it never reaches the
    human line). Block mode trusts an allow only with this receipt — a bare
    allow from a degraded server is indistinguishable from 'nothing ran'."""
    try:
        if not isinstance(resp, dict):
            return False
        return any(isinstance(a, dict) and a.get("code") == "runbook_evaluated"
                   for a in (resp.get("advisories") or []))
    except Exception:  # noqa: BLE001
        return False


def local_command_hash(command: str) -> str:
    """Local pairing id for the pre→post action stack: sha256[:16] of the
    normalized command. Pairing exit statuses to action_ids by command (review
    2026-08-15): the stack was LIFO-only, and two PARALLEL Bash calls in one
    session could cross-attribute — command A's exit 0 reconciling (and
    committing evidence for) command B's pending record. Identical duplicate
    commands remain ambiguous, which is inside the mistake-catching model."""
    try:
        return hashlib.sha256(
            normalize_command(command).encode("utf-8", "replace")
        ).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return ""


def _gate_open(command: str, payload: dict, cfg, agent: str) -> int:
    """advise / require_ack posture: a live server verdict is honoured in full
    (a real `block` decision still exits 2 — fail-open is a posture towards
    FAILURE, not towards the server's answer), but any network failure allows
    the command (hooklog + one stderr line)."""
    try:
        session_id = state.resolve_session_id(payload, cfg)
        resp = _escalate(command, payload, cfg, agent, session_id)
        decision = (resp.get("decision") if isinstance(resp, dict) else "allow") or "allow"
        reasons = _reasons(resp) if isinstance(resp, dict) else ""
        if decision == "block":
            print(f"[firekeep pre_tool] block: {reasons}", file=sys.stderr)
            return 2
        if decision == "rethink":
            print(f"[firekeep pre_tool] rethink: {reasons}", file=sys.stderr)
            return 1
        if reasons:
            print(f"[firekeep pre_tool] warn: {reasons}", file=sys.stderr)
        action_id = resp.get("action_id", "") if isinstance(resp, dict) else ""
        if action_id:
            state.push_action(session_id, action_id,
                             command_hash=local_command_hash(command))
        return 0
    except Exception as e:  # noqa: BLE001 — advise/require_ack fail OPEN
        _quiet_log(_HOOK, f"runbook escalation unavailable (advisory runbooks fail open): {e}")
        try:
            print("firekeep: runbook check unavailable — advisory runbooks fail "
                  "open; proceeding.", file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        return 0


def _gate_closed(command: str, payload: dict, cfg, agent: str,
                 closed: list[dict], *, bundle_stale: bool) -> int:
    """THE fail-closed branch (review finding 3).

    rc starts at 2 BEFORE any network I/O; only an authenticated allow from the
    server — which is also how a consumed require_ack permit arrives — lowers
    it to 0 (rethink lowers to 1, which the adapter seam still blocks). A
    malformed verdict is NOT an allow. ANY exception maps to 2, with stderr
    naming the runbook and stating the posture, so the outer @never_raise(0)
    wrapper never observes a failure from this branch. The staleness refetch
    happens INSIDE the branch: it is network I/O, and once a block entry has
    matched, network I/O does not get to run ahead of rc=2."""
    rc = 2
    names = _names(closed)
    try:
        if bundle_stale:
            fresh = fetch_and_store(cfg, timeout=ESCALATION_TIMEOUT, hook=_HOOK)
            if fresh is not None:
                matched = match_entries(fresh.get("entries") or [], command)
                if not matched:
                    # The refreshed, authenticated bundle says no runbook
                    # governs this command any more — nothing left to enforce.
                    return 0
                still_closed = [e for e in matched if _fail_closed(e)]
                if not still_closed:
                    # Demoted to advise/require_ack while we were stale.
                    return _gate_open(command, payload, cfg, agent)
                closed = still_closed
                names = _names(closed)
        session_id = state.resolve_session_id(payload, cfg)
        resp = _escalate(command, payload, cfg, agent, session_id)
        decision = resp.get("decision") if isinstance(resp, dict) else None
        if decision == "allow" and not _evaluated(resp):
            # An allow WITHOUT the runbook_evaluated marker is a degraded
            # server (unreadable index, internal failure), not a verdict —
            # review 2026-08-15: a reachable-but-broken server must not
            # convert block mode into an authenticated allow. Fail closed.
            rc = 2
            _say_fail_closed(names, "the server answered without evaluating "
                                    "the runbook")
        elif decision == "allow":
            rc = 0
            reasons = _reasons(resp)
            if reasons:
                print(f"[firekeep pre_tool] warn: {reasons}", file=sys.stderr)
            action_id = resp.get("action_id", "")
            if action_id:
                state.push_action(session_id, action_id,
                                 command_hash=local_command_hash(command))
        elif decision == "rethink":
            rc = 1
            print(f"[firekeep pre_tool] rethink: {_reasons(resp)}", file=sys.stderr)
        elif decision == "block":
            rc = 2
            print(f"[firekeep pre_tool] block: {_reasons(resp)}", file=sys.stderr)
        else:
            rc = 2  # malformed verdict is not an allow
            _say_fail_closed(names, "the server returned a malformed verdict")
    except Exception as e:  # noqa: BLE001 — ANY failure shape maps to 2
        rc = 2
        _quiet_log(_HOOK, f"block-mode runbook escalation failed CLOSED: {e}")
        _say_fail_closed(names, "the Firekeep server could not be reached")
    return rc


def _names(entries: list[dict]) -> str:
    try:
        return ", ".join(sorted({str(e.get("skill_id") or "unknown-runbook")
                                 for e in entries})) or "unknown-runbook"
    except Exception:  # noqa: BLE001
        return "unknown-runbook"


def _say_fail_closed(names: str, why: str) -> None:
    try:
        print(f'[firekeep pre_tool] BLOCKED (fail-closed): runbook "{names}" is '
              f"in block mode and {why} — load-bearing enforcement is "
              f"fail-closed while the server is unreachable. Retry when the "
              f"Firekeep server is back, or ask the owner to lower the "
              f"runbook's mode.", file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass
