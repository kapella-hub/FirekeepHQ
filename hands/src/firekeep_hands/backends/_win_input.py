"""Synthetic keyboard and mouse input on Windows, via `SendInput`.

Split out of `win.py` so the parts that decide *what* to send — the struct
layout, the chord grammar, the UTF-16 expansion, the absolute-coordinate
maths — can be unit-tested with no `uiautomation`, no `mss` and, crucially,
no Windows: `ctypes.WinDLL` is never touched at import time, only inside
`user32()` at send time. A layout error is exactly the bug a Windows-only
test cannot catch, because `wintypes.LONG` is `c_long`, which is 4 bytes on
Windows and 8 on Linux x64; every field below is therefore fixed-width.

Every event carries `HANDS_TAG` in `dwExtraInfo`. The broker's input listener
already drops anything flagged `LLKHF_INJECTED`, so Hands cannot approve its
own steps in any case; the tag is the second, positive half of that — it
lets a reader of an event stream say "this one was Hands" rather than only
"this one was not a human".
"""
from __future__ import annotations

import ctypes
import sys
import time

from .. import HANDS_TAG
from .base import HandsError

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

WHEEL_DELTA = 120

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# Seconds between characters in `send_text`.
#
# Not a politeness delay — a correctness one. A modern text control (Notepad's
# WinUI RichEdit, and anything else on the Text Services Framework) resolves
# an injected `KEYEVENTF_UNICODE` event on its own input thread rather than by
# taking the character out of the message, and an unpaced burst arrives with
# the right *number* of characters and the wrong ones. Measured against
# Notepad on Windows 11, 2026-09-05: "hands live: the quick brown fox…"
# arrived as "hands ééééé…" — correct length, every character after the first
# few replaced by the last one sent. It reproduced identically whether the
# string went as one SendInput call, one call per character, or one call per
# event, which is what rules out batching as the cause.
#
# Trials of 36 characters, counting exact round-trips read back through the
# ValuePattern: 5 ms and 12 ms both dropped or duplicated characters, 20 ms
# was 5/5 and 25 ms was 12/12. 25 ms it is — about 40 characters a second.
#
# Sending only the key-down half (a `KEYEVENTF_UNICODE` character comes from
# the down event; the up carries none) measured better still — 12/12 at 12 ms
# — and was not taken: a keystroke that never lifts is not what the rest of
# the system is told it is watching, and the honest event stream is worth
# half a second on a line of text.
TYPE_PACING_SECONDS = 0.025

VK_RETURN = 0x0D

_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# Both spellings of every modifier a human might write, folded onto the four
# gestures Windows has virtual-keys for. `win`/`meta`/`super` fold onto the
# key beside the space bar so one chord string means the same physical
# gesture here as it does in the macOS backend.
_MODIFIER_VKS = {
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12, "option": 0x12, "opt": 0x12,
    "shift": 0x10,
    "cmd": 0x5B, "command": 0x5B, "win": 0x5B, "meta": 0x5B, "super": 0x5B,
}

_NAMED_VKS = {
    "space": 0x20, "enter": VK_RETURN, "return": VK_RETURN, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{n}": 0x70 + n - 1 for n in range(1, 13)},
}

# The navigation cluster shares its virtual-keys with the numeric keypad, and
# which one Windows delivers is decided by KEYEVENTF_EXTENDEDKEY, not by the
# virtual-key. Omit it and a Delete arrives as the numpad '.' whenever NumLock
# happens to be on — wrong, and silently so.
_EXTENDED_KEYS = frozenset({
    "insert", "delete", "home", "end", "pageup", "pagedown",
    "left", "up", "right", "down",
})


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    # The union is named rather than anonymous so callers and tests can say
    # `inp.union.ki` and be explicit about which arm they mean.
    _fields_ = [("type", ctypes.c_uint32), ("union", _U)]


_user32 = None


def user32():
    """The one place `WinDLL` is called. Cached, and reached only from
    `send()` and `virtual_screen()`, so importing this module on Linux CI does
    nothing that can fail."""
    global _user32
    if _user32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        lib.SendInput.restype = ctypes.c_uint
        lib.GetSystemMetrics.argtypes = [ctypes.c_int]
        lib.GetSystemMetrics.restype = ctypes.c_int
        _user32 = lib
    return _user32


# Nominal rectangle used only when there is no user32 to ask, i.e. never on
# the machine an event is actually delivered to — `send()` is Windows-only.
# It exists so the pure builders can be exercised on CI.
_FALLBACK_VIRTUAL_SCREEN = (0, 0, 1920, 1080)


def virtual_screen() -> tuple[int, int, int, int]:
    """`(left, top, width, height)` of the whole virtual desktop — every
    monitor, not just the primary one.

    Deliberately not `SM_CXSCREEN`: that is the primary monitor's size, so
    normalising against it puts every point on a second monitor somewhere on
    the first. The virtual desktop's origin can be negative (a monitor placed
    left of or above the primary starts at a negative x or y), which is why
    the origin is read rather than assumed to be zero.

    Read at send time rather than cached: a laptop can be docked, undocked or
    rotated between two actions.

    Precondition: the process must already be per-monitor DPI aware, which
    `WinBackend.__init__` sees to. `GetSystemMetrics` answers in the calling
    thread's DPI context, so from an unaware process a 3840x2160 display at
    150% scaling measures 2560x1440 — while UI Automation rectangles stay in
    physical pixels, and every click computed from one would land two thirds
    of the way to where it was aimed.
    """
    if sys.platform != "win32":
        return _FALLBACK_VIRTUAL_SCREEN
    lib = user32()
    return (
        lib.GetSystemMetrics(SM_XVIRTUALSCREEN),
        lib.GetSystemMetrics(SM_YVIRTUALSCREEN),
        lib.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        lib.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def absolute_coords(x: int, y: int, vx: int, vy: int, vw: int, vh: int) -> tuple[int, int]:
    """Screen pixels to the 0..65535 space `MOUSEEVENTF_ABSOLUTE` uses,
    normalised over the virtual desktop.

    Subtracting the virtual origin is what makes a monitor left of or above
    the primary reachable at all: its points have negative screen
    coordinates, and `SM_XVIRTUALSCREEN` is where the desktop actually
    starts. The divisor is `size - 1`, not `size`, so 65535 lands on the last
    addressable pixel rather than one past it.

    Pure: no Win32 call, so the arithmetic is unit-tested on any platform
    for monitor layouts this machine does not have.
    """
    return (
        round((x - vx) * 65535 / max(vw - 1, 1)),
        round((y - vy) * 65535 / max(vh - 1, 1)),
    )


def _key_event(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.union.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0,
                              dwExtraInfo=HANDS_TAG)
    return inp


def _mouse_event(dx: int, dy: int, data: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    # mouseData is unsigned in the struct but signed in meaning (a wheel
    # notch can be negative), so wrap explicitly rather than relying on
    # ctypes' conversion.
    inp.union.mi = MOUSEINPUT(dx=dx, dy=dy, mouseData=data & 0xFFFFFFFF,
                              dwFlags=flags, time=0, dwExtraInfo=HANDS_TAG)
    return inp


def parse_chord(chord: str) -> tuple[list[str], str]:
    """`"ctrl+alt+y"` -> `(["ctrl", "alt"], "y")`, order preserved.

    Deliberately *not* `firekeep_hands.broker.parse_chord`: that one rejects
    a modifier-less chord, because a bare approval key would silently
    disable the broker's safety boundary. Here the opposite is true —
    `key("enter")` and `key("n")` (answering a dialog) are ordinary actions,
    so a bare key is accepted and only unknown names are refused.
    """
    if not isinstance(chord, str) or not chord.strip():
        raise HandsError("invalid_action", f"not a chord: {chord!r}")
    parts = [part.strip().lower() for part in chord.split("+")]
    if any(not part for part in parts):
        raise HandsError("invalid_action", f"not a chord: {chord!r}")
    *modifiers, trigger = parts
    canonical: list[str] = []
    for modifier in modifiers:
        vk = _MODIFIER_VKS.get(modifier)
        if vk is None:
            raise HandsError("invalid_action", f"unknown modifier {modifier!r} in {chord!r}")
        if modifier in canonical:
            raise HandsError("invalid_action", f"modifier {modifier!r} repeated in {chord!r}")
        canonical.append(modifier)
    if not _trigger_is_known(trigger):
        raise HandsError("invalid_action", f"unknown key {trigger!r} in {chord!r}")
    return canonical, trigger


def _trigger_is_known(trigger: str) -> bool:
    return (len(trigger) == 1 and trigger.isascii() and trigger.isalnum()) or trigger in _NAMED_VKS


def _trigger_vk(trigger: str) -> tuple[int, int]:
    """(virtual-key, extra flags) for a chord's trigger key."""
    if len(trigger) == 1:
        return (ord(trigger.upper()), 0)
    flags = KEYEVENTF_EXTENDEDKEY if trigger in _EXTENDED_KEYS else 0
    return (_NAMED_VKS[trigger], flags)


def build_key_chord(chord: str) -> list[INPUT]:
    """Modifiers down in the order written, the trigger down then up, then
    the modifiers up in reverse — so nothing is left held if a later event
    in the same batch is dropped."""
    modifiers, trigger = parse_chord(chord)
    vk, extra = _trigger_vk(trigger)
    events = [_key_event(_MODIFIER_VKS[m], 0, 0) for m in modifiers]
    events.append(_key_event(vk, 0, extra))
    events.append(_key_event(vk, 0, extra | KEYEVENTF_KEYUP))
    events.extend(_key_event(_MODIFIER_VKS[m], 0, KEYEVENTF_KEYUP) for m in reversed(modifiers))
    return events


def build_text(text: str) -> list[INPUT]:
    """One down/up pair per UTF-16 code unit, as `KEYEVENTF_UNICODE`.

    Going through Unicode rather than virtual-keys means the text arrives
    the same whatever keyboard layout is active — a VK-based path types
    'q' for 'a' on AZERTY. A code unit rather than a code point, so an
    astral character is delivered as its surrogate pair, which is what
    Windows expects. Newlines are the one exception: `\\n` as a Unicode
    event is swallowed by most edit controls, so it becomes a real
    VK_RETURN, and a `\\r` is dropped so CRLF is one line break, not two.
    """
    events: list[INPUT] = []
    for char in text:
        if char == "\r":
            continue
        if char == "\n":
            events.append(_key_event(VK_RETURN, 0, 0))
            events.append(_key_event(VK_RETURN, 0, KEYEVENTF_KEYUP))
            continue
        for unit in _utf16_units(char):
            events.append(_key_event(0, unit, KEYEVENTF_UNICODE))
            events.append(_key_event(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    return events


def _utf16_units(char: str) -> list[int]:
    raw = char.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


# A move is normalised over the virtual desktop, so it must say so: without
# MOUSEEVENTF_VIRTUALDESK, Windows reads the same 0..65535 pair against the
# primary monitor and the pointer lands there instead.
MOVE_FLAGS = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK


def _absolute(x: int, y: int, virtual: tuple[int, int, int, int] | None) -> tuple[int, int]:
    return absolute_coords(x, y, *(virtual or virtual_screen()))


def build_click(x: int, y: int, button: str = "left", double: bool = False,
                virtual: tuple[int, int, int, int] | None = None) -> list[INPUT]:
    down, up = _BUTTONS.get(button, (None, None))
    if down is None:
        raise HandsError("invalid_action", f"unknown mouse button {button!r}")
    dx, dy = _absolute(x, y, virtual)
    events = [_mouse_event(dx, dy, 0, MOVE_FLAGS)]
    for _ in range(2 if double else 1):
        events.append(_mouse_event(dx, dy, 0, down))
        events.append(_mouse_event(dx, dy, 0, up))
    return events


def build_scroll(x: int, y: int, dy: int,
                 virtual: tuple[int, int, int, int] | None = None) -> list[INPUT]:
    """`dy` is in wheel notches, positive being away from the user — the same
    sign convention as the Win32 wheel message."""
    ax, ay = _absolute(x, y, virtual)
    return [
        _mouse_event(ax, ay, 0, MOVE_FLAGS),
        _mouse_event(ax, ay, dy * WHEEL_DELTA, MOUSEEVENTF_WHEEL),
    ]


def send(batch: list[INPUT]) -> int:
    """Deliver a batch atomically. A short count means another process holds
    the input desktop (a UAC prompt, the lock screen, an elevated window's
    UIPI barrier) — an error rather than a partial, silently half-typed
    action."""
    if not batch:
        return 0
    array = (INPUT * len(batch))(*batch)
    sent = user32().SendInput(len(batch), array, ctypes.sizeof(INPUT))
    if sent != len(batch):
        raise HandsError(
            "backend",
            f"SendInput delivered {sent}/{len(batch)} events "
            f"(GetLastError {ctypes.get_last_error()})",
        )
    return sent


def send_key_chord(chord: str) -> int:
    return send(build_key_chord(chord))


def send_text(text: str, pacing: float = TYPE_PACING_SECONDS) -> int:
    """One character per `SendInput`, paced. See `TYPE_PACING_SECONDS` —
    typing a whole string in one batch produces the right *number* of
    characters and the wrong ones."""
    sent = 0
    for index, char in enumerate(text):
        if index:
            time.sleep(pacing)
        sent += send(build_text(char))
    return sent


def send_click(x: int, y: int, button: str = "left", double: bool = False) -> int:
    return send(build_click(x, y, button, double))


def send_scroll(x: int, y: int, dy: int) -> int:
    return send(build_scroll(x, y, dy))
