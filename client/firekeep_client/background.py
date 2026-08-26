"""One Popen recipe for every background worker a hook fires and forgets.

Used by `autoupdate`, `symdexindex`, `docdexsync` and `maildexsync` — the four things
a SessionStart hook launches and walks away from. They share three needs: outlive the
hook that spawned them, inherit none of its streams, and be invisible. The first two
are easy; the third is where four hand-rolled copies of this went wrong.

Windows: a HIDDEN console, never NO console.
    The obvious flag for "run in the background" is `DETACHED_PROCESS` ("no console"),
    and that is what all four sites used. It is wrong here for a reason specific to
    how the workers are addressed: `sys.executable` inside a venv (and the
    `firekeep.exe` console script) is a LAUNCHER that re-spawns the real interpreter
    as a child, and process-creation flags are not inherited. The launcher ran
    console-less, so its child — a console application whose parent has no console —
    was handed a brand-new one by Win32, and Windows 11 delegates new consoles to
    Windows Terminal: a Terminal window opened on every session start for as long as
    the index or sync ran (2026-08-25; reproduced with `python -c "time.sleep(4)"`
    under each flag set — DETACHED pops the window, CREATE_NO_WINDOW does not; git was
    NOT the trigger, it inherited the console the interpreter had already been given).

    `CREATE_NO_WINDOW` gives the launcher a console with no window. The base
    interpreter, git, the updater's powershell — every descendant — inherit that
    hidden console and nothing is ever shown. The worker still outlives the hook: the
    console is its own, not the hook's, so the hook exiting closes nothing it holds.
    `CREATE_NEW_PROCESS_GROUP` keeps a Ctrl+C aimed at the hook's group from reaching
    it. The flags are spelled as literals so the POSIX test run can assert the Windows
    contract (`subprocess.CREATE_NO_WINDOW` does not exist on Linux);
    `tests/test_background.py` pins them to the stdlib's values on Windows.

POSIX: `start_new_session=True` (setsid) — its own session, so the hook's terminal
    going away sends no SIGHUP.

Streams: all three to DEVNULL and `close_fds=True`. A worker that inherited the
    hook's stdout would write into the runtime's hook protocol, and one that kept the
    hook's stdin could hold that pipe open past the hook's exit.

Callers add per-site keys on top of the returned dict — and never a `cwd` inside a
workspace the user may delete or switch branches under (each site says why).
"""
from __future__ import annotations

import os
import subprocess

# Win32 process-creation flags, as literals: see the module docstring.
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008  # named only so tests can assert it is ABSENT


def popen_kwargs(*, os_name: str | None = None) -> dict:
    """Fresh `subprocess.Popen` kwargs for a fire-and-forget background worker.

    `os_name` defaults to `os.name`; tests pass "nt"/"posix" explicitly so both
    platforms' contracts are checked on every CI runner, not just the one it runs on.
    """
    name = os.name if os_name is None else os_name
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # survives the hook exit
    return kwargs
