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

# Session stash: the current Bridge session_id + briefing_id for {agent}@{profile},
# written by the session_start hook (briefing_id) and the bridge shim's
# ctx_start_session response tap (session_id), read by the shim's per-request
# X-Session-Id Auth so agent memory calls are attributed WITHOUT the agent
# passing session_id. TTL is self-enforced via an embedded timestamp — reap_stale
# does not sweep scratch/, so a crashed session's stale id must age out on its own.
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


def read_scratch(name: str) -> str | None:
    f = _scratch_file(name)
    return f.read_text(encoding="utf-8") if f.exists() else None


def write_scratch(name: str, value: str) -> None:
    f = _scratch_file(name)
    f.write_text(value, encoding="utf-8")
    _private(f)


def delete_scratch(name: str) -> None:
    _scratch_file(name).unlink(missing_ok=True)


def delete_prestate(action_id: str) -> None:
    """Remove a reconciled action's prestate snapshot (bash parity: the
    original unlinked the snapshot file after every reconciliation)."""
    _prestate_file(action_id).unlink(missing_ok=True)


def reap_stale(max_age_seconds: float = 3600) -> None:
    """Best-effort cleanup of orphaned action-queue/prestate files older than
    max_age_seconds (bash parity: precheck reaped /tmp snapshots >60min).
    Orphans accumulate when a pre_tool fires without a matching post_tool
    (crash, killed session). Never raises."""
    import time
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
