"""Platform cache dir + temp-state, shared IDENTICALLY by pre_tool and post_tool
(design SS6.2): both cores must resolve the same cache_dir() and key files by
session_id/action_id identically, or /agent/action reconciliation breaks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from firekeep_client import resolver, transport

_ACTIONS_SUBDIR = "actions"
_PRESTATE_SUBDIR = "prestate"
_SCRATCH_SUBDIR = "scratch"

# Per-key expiry for scratch markers, declared at the WRITE site and stored in a
# sidecar file under scratch/.ttl/ (a subdir, so it can never collide with a
# marker literally named "<key>.ttl").
#
# Opt-in per key, deliberately. Scratch markers have wildly different intended
# lifetimes — the sidecar pid guard lives as long as its process, the
# update-check stamp for a calendar day, the session stash enforces its own 12h
# via an embedded timestamp — so a blanket age sweep of scratch/ would break
# session attribution, the sidecar singleton, and the once-a-day auto-update
# guard. A key that passes no ttl_seconds behaves exactly as it did before this
# existed.
_SCRATCH_TTL_SUBDIR = ".ttl"

# Session stash: the current Bridge session_id + briefing_id for {agent}@{profile},
# written by the session_start hook (briefing_id) and the bridge shim's
# ctx_start_session response tap (session_id), read by the shim's per-request
# X-Session-Id Auth so agent memory calls are attributed WITHOUT the agent
# passing session_id. TTL is self-enforced via an embedded timestamp rather than
# via write_scratch's ttl_seconds: reap_stale only ever removes markers whose own
# declared expiry has lapsed, and the stash declares none, so a crashed session's
# stale id ages out on its own read path.
_SESSION_STASH_TTL_HOURS_DEFAULT = 12.0

# --- presence registration-race guard (SP1b Task 19 seam reconciliation) ----
#
# ONE canonical scratch key, consulted by BOTH the sidecar (firekeep_client.
# sidecar, for MCP-only runtimes) and the hook cores (firekeep_client.hooks.
# session_start / stop, for Claude Code). Before this reconciliation, the two
# consumers used DIFFERENT keys (sidecar: "presence-{agent}-registered";
# hooks: "presence_registered_{agent}") -- a fresh hook-registered presence
# was invisible to the sidecar's guard, so a mixed composition (hooks +
# sidecar both alive for the same agent_id) could have the sidecar clobber a
# session the hooks had just registered. state.py is now the single keying
# authority; sidecar and the hook cores both call these functions instead of
# building the scratch name themselves.

REGISTRATION_RACE_WINDOW = 5  # seconds


def _registered_key(agent_id: str, profile: str = "") -> str:
    # Profile-qualified: two runtimes on two backends (claude -> personal, kiro ->
    # office) must not consume each other's registration guard — presence lives
    # server-side PER BACKEND. Production callers (hooks/session_start.py, hooks/
    # stop.py, sidecar.py) ALWAYS pass the resolved profile, so real keys are always
    # qualified. The empty-profile default exists only for API back-compat and tests;
    # it falls back to the legacy unqualified key, it is not a path production takes.
    return (f"presence_registered_{agent_id}@{profile}" if profile
            else f"presence_registered_{agent_id}")


def mark_registered(agent_id: str, profile: str = "") -> None:
    """Record the registration epoch consulted by should_deregister()."""
    write_scratch(_registered_key(agent_id, profile), str(int(time.time())))


def should_deregister(agent_id: str, window_seconds: float = REGISTRATION_RACE_WINDOW,
                      profile: str = "") -> bool:
    """True if it is safe to deregister presence for agent_id.

    Skip deregister if presence was (re)registered less than window_seconds
    ago: a newer session/process very likely just took over this agent_id's
    presence entry, and clobbering it would drop a live session offline. No
    record -> safe to deregister. Read-only -- does not consume the mark;
    callers that need consume-on-read semantics (stop.py) call
    clear_registered() explicitly.
    """
    raw = read_scratch(_registered_key(agent_id, profile))
    if raw is None:
        return True
    try:
        reg = int(raw)
    except (TypeError, ValueError):
        return True
    return (int(time.time()) - reg) >= window_seconds


def clear_registered(agent_id: str, profile: str = "") -> None:
    """Consume (delete) the registration-epoch mark for agent_id. Idempotent."""
    delete_scratch(_registered_key(agent_id, profile))


def cache_dir() -> Path:
    override = os.environ.get("FIREKEEP_CACHE_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        d = Path(base) / "firekeep"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        d = Path(xdg) / "firekeep" if xdg else Path.home() / ".cache" / "firekeep"
    d.mkdir(parents=True, exist_ok=True)
    _private(d)
    return d


def _private(p: Path) -> None:
    """Best-effort private-file perms. Never raises -- a permission-tightening
    failure must not break the hook flow (fail-open on hardening, not on data).
    """
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
            if not user:
                return
            flag = "(OI)(CI)F" if p.is_dir() else "F"
            subprocess.run(
                ["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:{flag}"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        else:
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
    except Exception:
        pass


def _safe_name(name: str) -> str:
    """Flatten any identifier into a single path component that cannot escape
    the cache dir.

    session_id/action_id will be derived from tool payloads (pre_tool/post_tool
    cores) — untrusted input. Path separators become '_' and a resulting bare
    '.'/'..' is prefixed so it can never resolve to the dir itself or a parent.
    Deterministic: both hook cores flatten the same input to the same file name,
    preserving the §6.2 identical-keying invariant.
    """
    flat = name.replace("/", "_").replace("\\", "_")
    if flat in (".", ".."):
        flat = f"_{flat}"
    return flat or "_empty"


def _actions_file(session_id: str) -> Path:
    d = cache_dir() / _ACTIONS_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe_name(session_id)}.queue"


def _prestate_file(action_id: str) -> Path:
    d = cache_dir() / _PRESTATE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{_safe_name(action_id)}.sha256"


def _scratch_file(name: str) -> Path:
    d = cache_dir() / _SCRATCH_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d / _safe_name(name)


def push_action(session_id: str, action_id: str) -> None:
    f = _actions_file(session_id)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(action_id + "\n")
    _private(f)


def pop_action(session_id: str) -> str | None:
    f = _actions_file(session_id)
    if not f.exists():
        return None
    lines = [ln for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        f.unlink(missing_ok=True)
        return None
    newest = lines.pop()
    if lines:
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _private(f)
    else:
        f.unlink(missing_ok=True)
    return newest


def write_prestate(action_id: str, sha256: str) -> None:
    f = _prestate_file(action_id)
    f.write_text(sha256, encoding="utf-8")
    _private(f)


def read_prestate(action_id: str) -> str | None:
    f = _prestate_file(action_id)
    return f.read_text(encoding="utf-8").strip() if f.exists() else None


def _scratch_ttl_path(name: str) -> Path:
    """Where this marker's expiry lives. Pure — creates nothing, so a marker
    written with no TTL leaves no .ttl/ dir behind (an empty subdir inside
    scratch/ is a side effect for nothing, and it trips the path-traversal
    guard that asserts scratch/ holds only files)."""
    return cache_dir() / _SCRATCH_SUBDIR / _SCRATCH_TTL_SUBDIR / _safe_name(name)


def _scratch_ttl_file(name: str) -> Path:
    """As _scratch_ttl_path, but ensures the directory exists — write path only."""
    p = _scratch_ttl_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _scratch_expired(name: str) -> bool:
    """True if this marker declared an expiry and that expiry has passed.

    No sidecar file means no TTL was ever requested -> never expires (today's
    behaviour, preserved exactly). A sidecar that is present but unreadable or
    non-numeric counts as EXPIRED: failing to read an expiry is not evidence the
    marker is still fresh, and both consumers want that fallback — a lapsed
    suppression digest re-announces the customer's tasks (accuracy-positive) and
    a lapsed cursor forces a full restore (lossless).
    """
    f = _scratch_ttl_path(name)
    try:
        if not f.exists():
            return False
        return time.time() >= float(f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True


def read_scratch(name: str) -> str | None:
    if _scratch_expired(name):
        return None
    f = _scratch_file(name)
    return f.read_text(encoding="utf-8") if f.exists() else None


def write_scratch(name: str, value: str, *, ttl_seconds: float | None = None) -> None:
    """Write a scratch marker. With ttl_seconds, the marker reads as absent once
    that many seconds have passed; without it, the marker never expires.

    Writes the expiry BEFORE the value: a crash between the two can then only
    leave a marker that expires (safe), never one that has silently become
    permanent.
    """
    if ttl_seconds is None:
        # A key that is being made permanent must not stay on an older clock.
        _scratch_ttl_path(name).unlink(missing_ok=True)
    else:
        ttl_file = _scratch_ttl_file(name)
        ttl_file.write_text(str(time.time() + ttl_seconds), encoding="utf-8")
        _private(ttl_file)
    f = _scratch_file(name)
    f.write_text(value, encoding="utf-8")
    _private(f)


def delete_scratch(name: str) -> None:
    _scratch_file(name).unlink(missing_ok=True)
    # Drop the expiry too, or a re-created key inherits the dead key's clock and
    # reads as absent the instant it is written.
    _scratch_ttl_path(name).unlink(missing_ok=True)


def delete_prestate(action_id: str) -> None:
    """Remove a reconciled action's prestate snapshot (bash parity: the
    original unlinked the snapshot file after every reconciliation)."""
    _prestate_file(action_id).unlink(missing_ok=True)


def reap_stale(max_age_seconds: float = 3600) -> None:
    """Best-effort cleanup of orphaned action-queue/prestate files older than
    max_age_seconds (bash parity: precheck reaped /tmp snapshots >60min).
    Orphans accumulate when a pre_tool fires without a matching post_tool
    (crash, killed session). Never raises.

    Scratch markers are swept too, but by their OWN declared expiry — never by
    file age. max_age_seconds does not apply to them: a marker that asked for no
    TTL must survive here, or this sweep would silently break the session stash,
    the sidecar singleton and the daily auto-update guard.
    """
    now = time.time()
    for sub in (_ACTIONS_SUBDIR, _PRESTATE_SUBDIR):
        try:
            d = cache_dir() / sub
            if not d.is_dir():
                continue
            for entry in d.iterdir():
                try:
                    if entry.is_file() and now - entry.stat().st_mtime > max_age_seconds:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue
        except Exception:
            continue
    _reap_expired_scratch()


def _reap_expired_scratch() -> None:
    """Delete scratch markers whose declared expiry has lapsed, plus any orphan
    expiry sidecar. Never raises."""
    try:
        d = cache_dir() / _SCRATCH_SUBDIR
        if not d.is_dir():
            return
        for entry in d.iterdir():                  # .ttl/ is a dir -> skipped
            try:
                if entry.is_file() and _scratch_expired(entry.name):
                    entry.unlink(missing_ok=True)
                    _scratch_ttl_path(entry.name).unlink(missing_ok=True)
            except OSError:
                continue
        ttl_dir = d / _SCRATCH_TTL_SUBDIR
        if ttl_dir.is_dir():
            for entry in ttl_dir.iterdir():
                try:
                    if entry.is_file() and not (d / entry.name).exists():
                        entry.unlink(missing_ok=True)   # orphan: value already gone
                except OSError:
                    continue
    except Exception:
        return


def _session_stash_key(agent_id: str, profile: str) -> str:
    """Profile-qualified like _registered_key: two runtimes on two backends
    must not read each other's session id."""
    return f"session_current_{agent_id}@{profile}"


def _session_stash_ttl_seconds() -> float:
    try:
        hours = float(os.environ.get("FIREKEEP_SESSION_STASH_TTL_HOURS",
                                     _SESSION_STASH_TTL_HOURS_DEFAULT))
    except (TypeError, ValueError):
        hours = _SESSION_STASH_TTL_HOURS_DEFAULT
    return hours * 3600.0


def write_session_stash(agent_id: str, profile: str, *,
                        session_id: str | None = None,
                        briefing_id: str | None = None) -> None:
    """Merge-write the current session stash for {agent}@{profile}. Only the
    provided fields are updated; the timestamp is refreshed on every write.
    Never raises — an attribution stash failure must not break the flow."""
    try:
        current = _read_session_stash_raw(agent_id, profile) or {}
        if session_id is not None:
            current["session_id"] = session_id
        if briefing_id is not None:
            current["briefing_id"] = briefing_id
        current["ts"] = time.time()
        write_scratch(_session_stash_key(agent_id, profile), json.dumps(current))
    except Exception:
        pass


def _read_session_stash_raw(agent_id: str, profile: str) -> dict | None:
    raw = read_scratch(_session_stash_key(agent_id, profile))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_session_stash(agent_id: str, profile: str,
                       max_age_seconds: float | None = None) -> dict | None:
    """Return the stash dict if present AND fresh (embedded-ts TTL), else None.
    Never raises."""
    try:
        data = _read_session_stash_raw(agent_id, profile)
        if data is None:
            return None
        ttl = max_age_seconds if max_age_seconds is not None else _session_stash_ttl_seconds()
        try:
            age = time.time() - float(data.get("ts", 0.0))
        except (TypeError, ValueError):
            return None
        if age >= ttl:
            return None
        return data
    except Exception:
        return None


def clear_session_stash(agent_id: str, profile: str) -> None:
    """Delete the session stash for {agent}@{profile}. Idempotent, never raises."""
    try:
        delete_scratch(_session_stash_key(agent_id, profile))
    except Exception:
        pass


def _shadow_cursor_key(agent_id: str, profile: str) -> str:
    return f"shadow_cursor_{agent_id}@{profile}"


def write_shadow_cursor(agent_id: str, profile: str, cursor: str) -> None:
    """Stash the opaque shadow cursor. TTL'd like the session stash: a cursor
    that outlives its session must expire rather than be replayed. Never raises."""
    try:
        write_scratch(_shadow_cursor_key(agent_id, profile), cursor,
                      ttl_seconds=_session_stash_ttl_seconds())
    except Exception:
        pass


def read_shadow_cursor(agent_id: str, profile: str) -> str | None:
    """The stashed cursor, or None if absent/expired. None means 'ask for a full
    restore' — the safe default."""
    try:
        return read_scratch(_shadow_cursor_key(agent_id, profile))
    except Exception:
        return None


def clear_shadow_cursor(agent_id: str, profile: str) -> None:
    """Idempotent, never raises. Called by precompact: after a compaction the
    agent can no longer vouch for what is still in its context."""
    try:
        delete_scratch(_shadow_cursor_key(agent_id, profile))
    except Exception:
        pass


def resolve_session_id(payload: dict, cfg=None) -> str:
    """IDENTICAL in pre_tool & post_tool (design SS6.2 half #2): payload['session_id']
    if present, else fetch the active session from Bridge GET {rest_base}/sessions,
    else 'unknown'. Never raises -- degrades to 'unknown' on any failure so a
    resolution problem never blocks the hook flow.
    """
    sid = payload.get("session_id")
    if sid:
        return sid
    try:
        ep = resolver.resolve("bridge", cfg=cfg)
        agent = ep.headers.get("X-Agent-Id", "")
        url = f"{ep.rest_base}/sessions?status=active&agent_id={urllib.parse.quote(agent)}&limit=1"
        data = transport.get_json(url, headers=ep.headers, verify=ep.verify)
        sessions = (data or {}).get("sessions", [])
        if sessions and sessions[0].get("session_id"):
            return sessions[0]["session_id"]
    except Exception:
        pass
    return "unknown"
