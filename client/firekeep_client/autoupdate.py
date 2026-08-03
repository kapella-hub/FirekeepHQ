"""Background client auto-update.

ON by default; opt out with the `FIREKEEP_NO_AUTO_UPDATE` env var or
`[dist] auto_update = false` in ~/.firekeep/config. The session_start hook's
once-a-day version check calls `maybe_spawn()`, which fire-and-forgets a DETACHED
`firekeep update` when a newer release exists.

Why detached + "applies next session": `firekeep update` re-execs the bootstrap and
rebuilds ~/.firekeep/venv. It can't replace the install it is running from, so the
update lands for the NEXT session, never the running one.

The bootstrap now BUILDS BESIDE AND SWAPS (`${VENV}.new` -> rename), which is what
makes running this in the background acceptable. The previous rationale here was
wrong and worth recording, because it reads as reassuring:

    "on POSIX that's unlink-safe (running processes keep old inodes; new files
     land for next time)"

Unlink safety covers files a process has ALREADY MAPPED. It does not cover a new
exec — and every lifecycle hook spawns a fresh `${VENV}/bin/python` (PreToolUse
gates every Edit; PostToolUse, UserPromptSubmit, SessionStart and Stop all fire
per event; the four HTTP-backed stdio MCP shims are fresh execs at agent startup).
The old
`uv venv --clear` deleted the live venv and took 30-120s to repopulate it, so for
that entire window every hook on every live macOS/Linux session failed with
"No such file or directory" — with auto-update ON by default, meaning nobody had
asked for that window to open.

Windows was never exposed to this: the bootstrap there refused outright to
overwrite a venv held by live agent processes. Staging retires that asymmetry
instead of copying the guard to POSIX — neither platform now deletes an in-use
venv, and the swap window is one rename(2) rather than a reinstall.

At most one spawn per calendar day per target version — the daily check caches a
'newer' verdict, so without this guard every session start that day would relaunch.
Never raises: auto-update can only ever cost nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from firekeep_client import state

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
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            # Fully detach so the update outlives this hook process and doesn't
            # inherit its console.
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True  # new session: survives the hook exit
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
