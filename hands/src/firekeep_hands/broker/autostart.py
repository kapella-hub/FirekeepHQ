"""Making the broker come back by itself.

The broker is the thing that says no. If it is not running, every protected
step is refused — safe, but useless — so it has to survive a reboot without
the human remembering to start it.

**Windows uses a per-user `Run` registry value, not a Scheduled Task.** The
task was the original design and it does not work: measured on Windows 11 on
2026-09-05, `schtasks /Create /SC ONLOGON /RL LIMITED` returns "ERROR: Access
is denied." in an ordinary user session, with or without `/RU <the current
user>`. Creating even a limited-rights logon task wants administrator, so
every unelevated install failed — which is every install we expect. Writing
`HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run` needs no elevation,
was verified writable and removable in the same session, and is per-user by
construction. It runs `pythonw.exe`, the windowless interpreter that ships
beside `python.exe` in the kit's venv, so a logon does not flash a console.

macOS gets a per-user LaunchAgent, which is already unprivileged and, being
in the GUI domain, can see the session's input events at all.

Neither path wants privilege, and that is deliberate rather than incidental:
a low-level keyboard hook and a loopback socket are both unprivileged, and
running the broker elevated would only widen what a bug in it could reach.

`run_value_for`, `command_for` and `launch_agent_plist` are pure, so the
exact registry value, argv and plist this module would apply are readable and
testable on any host; `install`/`uninstall` touch the live system.
"""
from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

from .. import paths
from . import pending
from .client import BrokerClient

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "FirekeepHandsBroker"
LAUNCH_LABEL = "ai.firekeep.hands-broker"

_TIMEOUT_S = 30

# Windows process-creation flags, spelled out rather than imported from
# subprocess so this module imports on every platform.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _uid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def broker_script_path() -> str:
    """The console script beside the interpreter running us — the kit's own
    venv, wherever it has been installed."""
    name = "firekeep-hands-broker.exe" if sys.platform == "win32" else "firekeep-hands-broker"
    return str(Path(sys.executable).parent / name)


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"


def broker_launch_argv(script_path: str) -> list[str]:
    """How the broker is started on Windows, at logon and by `install()`.

    `pythonw.exe -m firekeep_hands.broker run` rather than the console
    script: `pythonw` is the windowless interpreter beside `python.exe` in
    the same venv, so a logon does not flash a console window at the human,
    and `-m` needs no second executable on disk to stay in step with it."""
    pythonw = Path(script_path).parent / "pythonw.exe"
    return [str(pythonw), "-m", "firekeep_hands.broker", "run"]


def run_value_for(script_path: str) -> str:
    """The exact `Run` value `install()` writes. Pure, so the quoting is
    readable and testable without touching the registry.

    The path is quoted because Windows splits an unquoted `Run` value at the
    first space, and the kit's venv can sit under `C:\\Program Files` or a
    user folder with a space in it."""
    pythonw, *arguments = broker_launch_argv(script_path)
    return " ".join([f'"{pythonw}"', *arguments])


def command_for(platform: str, script_path: str) -> list[str]:
    """The argv `install()` would run on `platform`. Pure, so a reviewer and
    a test can read the exact command without one being run.

    Windows raises: it runs no external command at all any more, it writes a
    registry value — see `run_value_for`."""
    if platform == "darwin":
        return ["launchctl", "bootstrap", f"gui/{_uid()}", str(launch_agent_path())]
    if platform == "win32":
        raise ValueError("Windows autostart is a registry value, not a command — see run_value_for")
    raise ValueError(f"no autostart mechanism for platform {platform!r}")


def launch_agent_plist(binary_path: str) -> str:
    """Built with plistlib rather than a format string: a path containing an
    ampersand or an angle bracket would otherwise produce a plist launchd
    refuses to parse, and the agent would silently never start."""
    return plistlib.dumps(
        {
            "Label": LAUNCH_LABEL,
            "ProgramArguments": [binary_path, "run"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Interactive",
        }
    ).decode("utf-8")


def _run(argv: list[str], what: str, *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a tool and, on failure, raise something a human can act on.

    `subprocess.run(check=True)` raises `CalledProcessError`, whose message is
    only "returned non-zero exit status 1" — which is how "ERROR: Access is
    denied." went unseen and cost this module a design. The tool's own words
    are the whole diagnosis, so they go in the exception."""
    result = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
    if check and result.returncode != 0:
        detail = ((result.stderr or "") + (result.stdout or "")).strip() or "no output"
        raise RuntimeError(f"{what} failed (exit {result.returncode}): {detail}")
    return result


def _winreg():
    """Imported here, never at module level: this module must import on macOS
    and on Linux CI, where `winreg` does not exist."""
    import winreg

    return winreg


def _write_run_value(value: str) -> None:
    winreg = _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, value)


def _delete_run_value() -> bool:
    """True if a value was removed. A missing one is not an error — an
    uninstall must be safe to run twice, and safe on a machine that never
    installed."""
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    return True


def _start_broker_now(script: str) -> None:
    """Start the broker detached and windowless, unless one already answers —
    a second broker would race the first for `broker.json` and leave the kit
    pointing at whichever won."""
    if BrokerClient.from_disk(timeout=1.0) is not None:
        return
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(  # noqa: S603 - our own interpreter, fixed argv
        broker_launch_argv(script),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **kwargs,
    )


def install() -> None:
    script = broker_script_path()
    if sys.platform == "win32":
        # A Run value does not fire until the next logon, so start one now.
        _write_run_value(run_value_for(script))
        _start_broker_now(script)
    elif sys.platform == "darwin":
        path = launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launch_agent_plist(script), encoding="utf-8")
        # Tear down any previous registration first; bootstrap fails on a
        # label that is already loaded, which would look like a broken
        # install when it is really a stale one.
        _run(["launchctl", "bootout", f"gui/{_uid()}/{LAUNCH_LABEL}"],
             "launchctl bootout", check=False)
        # RunAtLoad means bootstrap also starts it; no separate spawn here.
        _run(command_for("darwin", script), "launchctl bootstrap")
    else:
        raise RuntimeError(f"no autostart mechanism for platform {sys.platform!r}")


def uninstall() -> None:
    if sys.platform == "win32":
        _delete_run_value()
    elif sys.platform == "darwin":
        _run(["launchctl", "bootout", f"gui/{_uid()}/{LAUNCH_LABEL}"],
             "launchctl bootout", check=False)
        launch_agent_path().unlink(missing_ok=True)
    _stop_running_broker()
    # The broker clears this on a clean stop, but `_stop_running_broker` uses
    # `os.kill(SIGTERM)`, which on Windows is TerminateProcess — nothing in
    # the broker runs afterwards. Observed live 2026-09-05: a hard-killed
    # broker left pending.json behind, describing approvals nobody can grant.
    # Unconditional, because uninstall's job is to leave nothing behind even
    # when there was no broker.json to find.
    pending.clear_pending()


def _stop_running_broker() -> None:
    """Terminate the broker described by broker.json, then remove the file.

    What is actually checked, stated exactly: that *a* broker is listening on
    the port in this file and accepts the token in this file. That is strong
    evidence the file is current rather than a leftover from a process that
    died — a stale file's port is usually closed or held by something that
    rejects the token — so it is what stands between "uninstall" and killing
    an unrelated process that inherited a recycled pid.

    It is not proof that the pid field names the listening process. The pid
    and the port were written together by the same broker in the same file,
    and nothing but a broker writes this file, so they agree in every case
    short of a corrupted or hand-edited `broker.json`. The residual is one
    SIGTERM to a wrong pid in that case, which is why the kill is guarded by
    the health check rather than done on the pid alone."""
    path = paths.broker_info_path()
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    pid = info.get("pid")
    if BrokerClient.from_disk(timeout=1.0) is not None and isinstance(pid, int) and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
