"""Background client auto-update.

ON by default; opt out with the `FIREKEEP_NO_AUTO_UPDATE` env var or
`[dist] auto_update = false` in ~/.firekeep/config. The session_start hook's
once-a-day version check calls `maybe_spawn()`, which fire-and-forgets a DETACHED
`firekeep update` when a newer release exists.

Why detached + "applies next session": `firekeep update` re-execs the bootstrap,
which provisions the NEW version's venv beside the running one and flips the
`current` alias — the install this process runs from is never touched, so the
update lands for the NEXT session (and next fresh hook exec), never the running one.

History, because an earlier version of this docstring described a design that had
NOT shipped. 0.1.26 tried BUILD BESIDE AND RENAME (`${VENV}.new` -> mv) and it
failed — a uv venv is not relocatable (pyvenv.cfg and every console-script
interpreter line bake the absolute path), so the renamed venv's scripts pointed
at a dead path (e2e gate: exit 127). The in-place `uv venv --clear` it reverted
to deleted the live venv for 30-120s, during which every hook on every live
macOS/Linux session failed with "No such file or directory" — and every fresh
exec matters, because unlink safety only covers files a process has ALREADY
MAPPED, while every lifecycle hook spawns a fresh `${VENV}/bin/python`.

The side-by-side layout (client 0.1.35, venvs/<version> + `current`) is the
design that actually solves it: each venv is provisioned AT its final path and
never moved, so nothing is relocated; nothing in-use is ever deleted (GC
rename-probes before removing); and the swap window is an atomic rename(2) on
POSIX and a millisecond junction flip on Windows — down from the 30-120s hole,
and from Windows' previous outright refusal while any session was open.

At most one spawn per calendar day per target version — the daily check caches a
'newer' verdict, so without this guard every session start that day would relaunch.
Never raises: auto-update can only ever cost nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from firekeep_client import background, state

_FALSEY = ("", "0", "false", "no", "off")
_DISABLE = ("0", "false", "no", "off")  # explicit disable values (NOT blank)


def is_enabled(cfg) -> bool:
    """Default ON. FIREKEEP_NO_AUTO_UPDATE (env) wins over the config; `[dist]
    auto_update = false` disables it persistently. A blank value (`auto_update =`)
    means 'unset' → the default (ON), NOT disabled — only the explicit disable
    words turn it off."""
    if os.environ.get("FIREKEEP_NO_AUTO_UPDATE", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get("dist", "auto_update", fallback="true")
           if cfg.has_section("dist") else "true").strip().lower()
    return val not in _DISABLE


def _firekeep_exe() -> Path:
    """The `firekeep` console script next to the running interpreter (the venv this
    hook is executing from) — no PATH dependency."""
    d = Path(sys.executable).parent
    return d / ("firekeep.exe" if os.name == "nt" else "firekeep")


def _claim_path(today: str, latest: str) -> Path:
    # One claim file per (day, target). `|`/`.` would be illegal on Windows, so the
    # name is composed from already-safe pieces (date has only digits+`-`; version
    # digits+`.` — dots are fine in filenames on both platforms).
    return state._scratch_file(f"auto_update.{today}.{latest}")


def maybe_spawn(cfg, latest: str, today: str) -> bool:
    """Ensure a background `firekeep update` toward `latest` is (or has been) launched
    today. Returns True when an update is in flight — either this call spawned it OR
    another session already claimed today's (day, target) slot. Returns False only
    when it can't run: disabled, launcher missing, or the spawn itself failed.
    Never raises.

    The once-per-(day, target) guard is an ATOMIC O_EXCL file claim, so two
    session_start hooks racing on the first start of the day (two Claude/kiro windows
    opening together) cannot BOTH launch `firekeep update` — a double `uv venv --clear`
    on the same venv could corrupt it."""
    try:
        if not is_enabled(cfg):
            return False
        exe = _firekeep_exe()
        if not exe.exists():
            return False
        claim = _claim_path(today, latest)
        try:
            # Atomic test-and-set: only the FIRST caller creates the file; a
            # concurrent second caller gets FileExistsError and defers.
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return True  # already claimed today for this target — in flight
        # Outlives this hook process and inherits none of its streams; on Windows a
        # HIDDEN console rather than none — `firekeep.exe` is a launcher that re-spawns
        # python, and a console-less parent hands that child a visible console (see
        # firekeep_client.background).
        kwargs = background.popen_kwargs()
        try:
            subprocess.Popen([str(exe), "update"], **kwargs)  # noqa: S603 — fixed argv
        except Exception:  # noqa: BLE001
            # Release the claim so a later session can retry a failed launch.
            try:
                claim.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — auto-update must never cost a session
        return False
