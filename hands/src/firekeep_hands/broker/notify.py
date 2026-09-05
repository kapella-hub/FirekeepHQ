"""Telling the human what they are about to approve.

The chord approves the OLDEST pending permit, and until this existed the
broker showed the human nothing: the step's description reached them only if
the runtime chose to relay it — and the runtime is the thing being gated. A
model that wanted an unwatched approval had only to ask for something dull
out loud. The broker now says, in its own voice and out of the runtime's
reach, what the next chord will approve.

**This is informational, not a second gate.** The chord still approves the
oldest pending permit, whatever the notification said; the two are not
transactionally linked. The residual is small and real: a second permit can
arrive between the human reading the toast and pressing the chord, and they
would then approve that one instead. Nothing here closes that — `pending.py`
and `firekeep hands status` let a suspicious human look before pressing, and
the docs disclose it.

Everything is best-effort. A notification that cannot be shown is a DEBUG
line, never a failed permit: an approval must not depend on a toast.

**On the quoting.** Titles are built server-side from the action, but they
are still data and are treated as hostile. Two defences, both needed: the
command is spawned as an argument LIST, so no shell parses it and there is no
shell injection to have; and the text embedded in the PowerShell or
AppleScript source is escaped for *that* language, because argv alone would
not stop a title from closing a string literal and running its own script.
Text is also collapsed to one line and truncated, which removes newlines as a
class rather than escaping them.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

_TITLE_LIMIT = 120
_CLASS_LIMIT = 32
_CHORD_LIMIT = 40
_BALLOON_MS = 8000

# Windows process-creation flags, spelled out so this module imports anywhere.
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _one_line(text: object, limit: int) -> str:
    """Collapse to a single line and truncate. `split()` with no argument
    splits on every kind of whitespace, so newlines, tabs and carriage
    returns are gone as a class rather than escaped one by one."""
    collapsed = " ".join(str(text).split())
    return collapsed[:limit]


def _powershell_quote(text: str) -> str:
    """A PowerShell single-quoted string. Inside one, PowerShell performs no
    expansion at all — no `$var`, no subexpressions, no backtick escapes — so
    doubling the single quote is the whole of the escaping."""
    return "'" + text.replace("'", "''") + "'"


def _applescript_quote(text: str) -> str:
    """An AppleScript double-quoted string: backslash first, then quote."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notification_body(title, classes, chord: str, deny_chord: str) -> tuple[str, str]:
    """`(body, subtitle)` — what the human reads. Sanitised here, once, so
    every platform branch below starts from safe single-line text."""
    class_text = ", ".join(_one_line(c, _CLASS_LIMIT) for c in classes) or "unclassified"
    body = f"{_one_line(title, _TITLE_LIMIT)} — classes: {class_text}"
    subtitle = (
        f"{_one_line(chord, _CHORD_LIMIT)} approves · "
        f"{_one_line(deny_chord, _CHORD_LIMIT)} denies"
    )
    return body, subtitle


def notification_argv(platform: str, title, classes, chord: str, deny_chord: str) -> list[str]:
    """The exact command that would be spawned, or `[]` where there is no
    notification to show. Pure, so the quoting is testable without running
    anything."""
    body, subtitle = notification_body(title, classes, chord, deny_chord)

    if platform == "darwin":
        script = (
            f"display notification {_applescript_quote(body)} "
            f'with title "Firekeep Hands" '
            f"subtitle {_applescript_quote(subtitle)}"
        )
        return ["osascript", "-e", script]

    if platform == "win32":
        # NotifyIcon is the only balloon available without extra modules. The
        # balloon dies with its process, hence the sleep; the process is
        # spawned detached so that wait costs the listener nothing.
        text = _powershell_quote(f"{body} — {subtitle}")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "$n = New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon = [System.Drawing.SystemIcons]::Information;"
            "$n.Visible = $true;"
            f"$n.ShowBalloonTip({_BALLOON_MS}, 'Firekeep Hands', {text}, "
            "[System.Windows.Forms.ToolTipIcon]::Info);"
            f"Start-Sleep -Milliseconds {_BALLOON_MS};"
            "$n.Dispose()"
        )
        return [
            "powershell", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-Command", script,
        ]

    # Linux and anything else: no notifier is assumed to exist. Silence beats
    # a dependency on libnotify being installed.
    return []


def announce(title, classes, chord: str, deny_chord: str) -> bool:
    """Show the notification and return immediately. True when a command was
    spawned — not that the human saw anything, which nothing can know.

    Never raises and never waits: this is called from the permit path, and a
    missing `osascript` must not delay or fail an approval."""
    argv = notification_argv(sys.platform, title, classes, chord, deny_chord)
    if not argv:
        return False
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell, text escaped above
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - a toast is never worth a failed permit
        log.debug("could not show a permit notification: %s", exc)
        return False
    return True
