"""`firekeep_client.background` — the ONE place the hook-spawned background workers
(symdex auto-index, docdex/maildex sync, auto-update) get their Popen kwargs from.

Regression guard for the stray Windows Terminal window that opened on every session
start on Windows (2026-08-25). The workers were spawned with `DETACHED_PROCESS`, which
is "no console at all". On Windows `sys.executable` inside a venv is a LAUNCHER that
re-spawns the base interpreter as a child, and process-creation flags are not
inherited: the launcher ran console-less, so the child — a console application whose
parent has no console — was handed a brand-new one, which Windows 11 delegates to
Windows Terminal. `CREATE_NO_WINDOW` gives the launcher a HIDDEN console instead;
every descendant (base python, git, the updater's powershell) inherits that, and
nothing is ever shown. Reproduced and proven with a plain `python -c sleep` under
both flag sets before the fix was written.
"""
from __future__ import annotations

import inspect
import os
import subprocess

from firekeep_client import background

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def test_windows_gets_a_hidden_console_not_no_console():
    kw = background.popen_kwargs(os_name="nt")
    flags = kw["creationflags"]
    assert flags & CREATE_NO_WINDOW, "the worker needs a hidden console its children inherit"
    assert not flags & DETACHED_PROCESS, (
        "DETACHED_PROCESS leaves the venv launcher console-less, so the base interpreter "
        "it re-spawns is given a VISIBLE console — a Windows Terminal window on Win11")
    assert flags & CREATE_NEW_PROCESS_GROUP, "Ctrl+C in the hook's group must not reach it"
    assert "start_new_session" not in kw


def test_posix_starts_a_new_session():
    kw = background.popen_kwargs(os_name="posix")
    assert kw["start_new_session"] is True, "must survive the hook exiting"
    assert "creationflags" not in kw


def test_no_stream_is_inherited_from_the_hook():
    for name in ("nt", "posix"):
        kw = background.popen_kwargs(os_name=name)
        assert kw["stdin"] is subprocess.DEVNULL, name
        assert kw["stdout"] is subprocess.DEVNULL, name
        assert kw["stderr"] is subprocess.DEVNULL, name
        assert kw["close_fds"] is True, name


def test_defaults_to_the_running_platform():
    kw = background.popen_kwargs()
    if os.name == "nt":
        assert kw["creationflags"] == (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        assert kw["start_new_session"] is True


def test_each_call_returns_a_fresh_dict():
    """Callers add per-site keys (cwd, env); a shared dict would leak them between
    workers."""
    a = background.popen_kwargs(os_name="nt")
    b = background.popen_kwargs(os_name="nt")
    assert a is not b
    a["cwd"] = "x"
    assert "cwd" not in b


def test_constants_match_the_stdlib_on_windows():
    """The helper spells the flags as literals so the POSIX CI run can assert the
    Windows contract; on Windows, pin them to the stdlib's own so a typo can't hide."""
    if os.name != "nt":
        return
    assert background.CREATE_NO_WINDOW == subprocess.CREATE_NO_WINDOW
    assert background.CREATE_NEW_PROCESS_GROUP == subprocess.CREATE_NEW_PROCESS_GROUP
    assert background.DETACHED_PROCESS == subprocess.DETACHED_PROCESS


def test_every_hook_worker_takes_its_kwargs_from_here():
    """Four copies of a subtle Windows flag combination is how one copy drifts back to
    DETACHED_PROCESS. Each worker's spawn must route through the helper and must not
    spell the flag itself."""
    from firekeep_client import autoupdate, docdexsync, maildexsync, symdexindex

    for mod in (autoupdate, docdexsync, maildexsync, symdexindex):
        src = inspect.getsource(mod.maybe_spawn)
        assert "background.popen_kwargs(" in src, f"{mod.__name__}.maybe_spawn bypasses the helper"
        assert "DETACHED_PROCESS" not in src, f"{mod.__name__}.maybe_spawn re-spells the flag"
