"""Opportunistic Night Shift drain from session start (spec decision 2).

Nothing scheduled the drain before this: `firekeep night-shift` ran only when a
human typed it, so the fleet queue — distill tasks from every session end, and
since the job catalog the re-author and verdict tasks cortex enqueues nightly —
sat until someone remembered. This module is the fifth entry in the
session_start nudge chain, built like the other four (autoupdate, symdexindex,
docdexsync, maildexsync): a DETACHED spawn, an ATOMIC O_EXCL claim per interval
bucket so three windows opening together launch one shift, one env off-switch,
one banner line naming it, and it never raises.

It adds one precondition its siblings do not need: a LOCAL MODEL must be
listening. Night Shift refuses cloud models and aborts fast with no backend,
but a hook that spawned a process every six hours on a machine with no LM Studio
or Ollama would print a "draining" line about a shift that immediately quit. So
the nudge does a ≤250 ms TCP connect to the configured base (or the two default
ports) first and stays silent when nothing answers.

Private-session mode is not checked here on purpose: the dispatcher
short-circuits `session_start` while bypassed, and `nightshift.run()` refuses on
its own — two layers already.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from firekeep_client import background, state

_FALSEY = ("", "0", "false", "no", "off")
_DISABLE = ("0", "false", "no", "off")
SECTION = "nightshift"
DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_MAX_TASKS = 5
_DEFAULT_PORTS = (("127.0.0.1", 1234), ("127.0.0.1", 11434))  # LM Studio, Ollama
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
LAST_RUN_KEY = "night_shift_last"


def is_enabled(cfg) -> bool:
    """`FIREKEEP_NO_AUTO_NIGHTSHIFT` (env, wins) then `[nightshift] auto_drain`
    — the exact semantics of the four sibling triggers: the env var disables on
    any value not in _FALSEY; the config key disables only on an explicit false."""
    if os.environ.get("FIREKEEP_NO_AUTO_NIGHTSHIFT", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get(SECTION, "auto_drain", fallback="true")
           if cfg.has_section(SECTION) else "true").strip().lower()
    return val not in _DISABLE


def drain_interval_hours(cfg=None) -> float:
    """Env `FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS`, then `[nightshift]
    auto_drain_hours`, default 6. Unparseable or non-positive → default."""
    raw = os.environ.get("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "").strip()
    if not raw and cfg is not None and cfg.has_section(SECTION):
        raw = cfg.get(SECTION, "auto_drain_hours", fallback="").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS
    return value if value > 0 else DEFAULT_INTERVAL_HOURS


def _probe_targets() -> list[tuple[str, int]]:
    base = os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE", "").strip()
    if not base:
        return list(_DEFAULT_PORTS)
    parts = urlsplit(base)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return [(host, port)]


def local_llm_listening(timeout: float = 0.25) -> bool:
    """A TCP connect, nothing more: cheap enough for a hook, and 'a port is open'
    is the only question worth asking before spawning — the shift does the real
    /models probe itself."""
    for host, port in _probe_targets():
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            continue
        try:
            sock.close()
        except OSError:
            pass
        return True
    return False


def should_drain(now: float | None = None, cfg=None) -> str:
    """The claim stamp: the interval bucket. Every session start inside one
    bucket shares a claim, and a shift that never landed retries next bucket."""
    now = time.time() if now is None else now
    return str(int(now // (drain_interval_hours(cfg) * 3600.0)))


def _claim_path(stamp: str) -> Path:
    tag = _UNSAFE.sub("_", stamp)[:40].strip("_") or "none"
    return state._scratch_file(f"night_shift.{tag}")


def _firekeep_exe() -> Path:
    """The `firekeep` console script next to the running interpreter — the venv
    this hook executes from; no PATH dependency (same as autoupdate)."""
    return Path(sys.executable).parent / ("firekeep.exe" if os.name == "nt" else "firekeep")


def maybe_spawn(cfg, stamp: str) -> bool:
    """True when a shift is in flight (spawned now, or already claimed for this
    stamp). False only when it can't run. Never raises."""
    try:
        if not is_enabled(cfg):
            return False
        exe = _firekeep_exe()
        if not exe.exists():
            return False
        claim = _claim_path(stamp)
        try:
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return True
        kwargs = background.popen_kwargs()
        # No cwd: the shift talks to the Keep and a local model, never the workspace.
        argv = [str(exe), "night-shift", "--max", str(DEFAULT_MAX_TASKS)]
        try:
            subprocess.Popen(argv, **kwargs)  # noqa: S603 — fixed argv, not shell-interpolated
        except Exception:  # noqa: BLE001
            try:
                claim.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — a drain must never cost a session
        return False


def last_run_line() -> str:
    """One line about the LAST shift, printed once: only when it left something
    for a human to review (draft skills or verdict proposals)."""
    try:
        raw = state.read_scratch(LAST_RUN_KEY)
        if not raw:
            return ""
        rec = json.loads(raw)
        if not isinstance(rec, dict) or rec.get("reported"):
            return ""
        counts = rec.get("counts") or {}
        drafts = int(counts.get("draft_skills") or 0)
        proposals = int(counts.get("proposed") or 0)
        if drafts <= 0 and proposals <= 0:
            return ""
        rec["reported"] = True
        state.write_scratch(LAST_RUN_KEY, json.dumps(rec), ttl_seconds=7 * 86400)
        parts = []
        if drafts:
            parts.append(f"{drafts} draft skill(s)")
        if proposals:
            parts.append(f"{proposals} verdict proposal(s)")
        return (f"\n\n[firekeep] night shift: {' and '.join(parts)} await review — "
                f"dashboard → Skills / Autopilot")
    except Exception:  # noqa: BLE001
        return ""


def drain_nudge(cfg) -> str:
    """What the session-start chain calls. Never raises; '' when nothing to say."""
    try:
        report = last_run_line()
        if not is_enabled(cfg) or not local_llm_listening():
            return report
        if not maybe_spawn(cfg, should_drain(cfg=cfg)):
            return report
        return (report + "\n\n[firekeep] night shift draining the fleet queue in background "
                "(local model; disable with `FIREKEEP_NO_AUTO_NIGHTSHIFT=1`)")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        return ""
