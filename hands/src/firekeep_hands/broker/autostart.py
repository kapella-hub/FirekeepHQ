"""Making the broker come back by itself.

The broker is the thing that says no. If it is not running, every protected
step is refused — safe, but useless — so it has to survive a reboot without
the human remembering to start it. Windows gets a logon Scheduled Task at
LIMITED rights (it needs no privilege: a low-level keyboard hook and a
loopback socket are both unprivileged, and running it elevated would only
widen what a bug in it could reach). macOS gets a per-user LaunchAgent,
which is already unprivileged and, being in the GUI domain, can see the
session's input events at all.

`command_for` and `launch_agent_plist` are pure so the exact argv and plist
this module would apply are testable on any host; `install`/`uninstall`
touch the live system and are exercised by hand.
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
from .client import BrokerClient

TASK_NAME = "FirekeepHandsBroker"
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


def command_for(platform: str, script_path: str) -> list[str]:
    """The argv `install()` would run on `platform`. Pure, so a reviewer and
    a test can read the exact command without one being run."""
    if platform == "win32":
        # /TR is one string parsed by the task scheduler, so the path is
        # quoted: "C:\Program Files\..." would otherwise split at the space.
        return [
            "schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", f'"{script_path}" run',
            "/SC", "ONLOGON",
            "/RL", "LIMITED",
            "/F",
        ]
    if platform == "darwin":
        return ["launchctl", "bootstrap", f"gui/{_uid()}", str(launch_agent_path())]
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


def install() -> None:
    script = broker_script_path()
    if sys.platform == "win32":
        subprocess.run(
            command_for("win32", script), check=True, capture_output=True, timeout=_TIMEOUT_S
        )
        # A logon task does not run at creation, so start one now — unless a
        # broker is already answering, in which case a second one would race
        # it for broker.json and leave the kit pointing at whichever won.
        if BrokerClient.from_disk(timeout=1.0) is None:
            subprocess.Popen(  # noqa: S603 - our own console script
                [script, "run"],
                creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
    elif sys.platform == "darwin":
        path = launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(launch_agent_plist(script), encoding="utf-8")
        # Tear down any previous registration first; bootstrap fails on a
        # label that is already loaded, which would look like a broken
        # install when it is really a stale one.
        subprocess.run(
            ["launchctl", "bootout", f"gui/{_uid()}/{LAUNCH_LABEL}"],
            check=False, capture_output=True, timeout=_TIMEOUT_S,
        )
        # RunAtLoad means bootstrap also starts it; no separate spawn here.
        subprocess.run(
            command_for("darwin", script), check=True, capture_output=True, timeout=_TIMEOUT_S
        )
    else:
        raise RuntimeError(f"no autostart mechanism for platform {sys.platform!r}")


def uninstall() -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            check=False, capture_output=True, timeout=_TIMEOUT_S,
        )
    elif sys.platform == "darwin":
        subprocess.run(
            ["launchctl", "bootout", f"gui/{_uid()}/{LAUNCH_LABEL}"],
            check=False, capture_output=True, timeout=_TIMEOUT_S,
        )
        launch_agent_path().unlink(missing_ok=True)
    _stop_running_broker()


def _stop_running_broker() -> None:
    """Terminate the broker described by broker.json, then remove the file.

    The pid is only trusted when the broker at that port answers /health with
    the token from the same file: pids are recycled, and a stale file must
    not become an instruction to kill some unrelated process."""
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
