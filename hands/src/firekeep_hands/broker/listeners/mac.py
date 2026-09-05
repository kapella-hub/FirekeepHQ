"""macOS chord listener: a listen-only CGEventTap that ignores everything a
program posted.

Two independent filters, because neither alone is enough. The first is
Hands' own marker: every event Hands posts carries `HANDS_TAG` in
`kCGEventSourceUserData`, so Hands can always recognise its own typing. The
second is the event's source state: `kCGEventSourceStateHIDSystemState` (1)
is the state real hardware events come from, and anything a program
synthesised carries a different one — which catches synthetic input from
programs that are not Hands and set no marker of their own.

**The source-state half is UNVERIFIED on hardware.** It is implemented as
specified and measured in Task 15 on a real MacBook; the tap callback logs
`(keycode, flags, userData, sourceStateID)` at DEBUG (`FIREKEEP_HANDS_LOG=DEBUG`)
precisely so that measurement is possible. Until then the marker filter is
the half known to hold.

Nothing here imports pyobjc at module level: the pure parts are unit-tested
on Windows and Linux CI too.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

from ... import HANDS_TAG
from .. import parse_chord

log = logging.getLogger(__name__)

# CGEventFlags — the modifier bits as they arrive on a key event.
FLAG_SHIFT = 0x20000
FLAG_CONTROL = 0x40000
FLAG_ALT = 0x80000
FLAG_COMMAND = 0x100000

# kCGEventSourceStateHIDSystemState: the source state of events that came
# from real hardware through the HID system.
SOURCE_STATE_HID = 1

_FLAG_FOR_MODIFIER = {
    "ctrl": FLAG_CONTROL,
    "alt": FLAG_ALT,
    "shift": FLAG_SHIFT,
    "cmd": FLAG_COMMAND,
}

# Virtual keycodes are positional, not character-based — they name the
# physical key on an ANSI layout regardless of what it types — so this table
# is static rather than derived from the character.
#
# `backends/mac.py`'s `_KEYCODES` is the reference for these values and this
# table matches it key for key; a chord and a keypress must mean the same
# physical key or a human's chord names one key while Hands presses another.
# The four that were wrong here until 2026-09-05, and what they are:
# `delete` is 51, the key actually labelled "delete" on a Mac keyboard (what
# a PC calls backspace), `forwarddelete` is 117, `return` is 36 and `enter`
# is 76, the keypad's own key. Note this makes `delete` name a different
# physical key on each platform — `listeners/win.py` maps it to VK_DELETE,
# the PC's forward-delete Del — which is right: each table follows the label
# printed on that platform's keyboard, which is what the human reads when
# they choose a chord.
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
    "n": 45, "m": 46,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "enter": 76, "forwarddelete": 117,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


def event_is_real(user_data: int, source_state_id: int) -> bool:
    """False for anything Hands posted (its marker) and for anything whose
    source state is not the HID system (everything else synthetic)."""
    return user_data != HANDS_TAG and source_state_id == SOURCE_STATE_HID


class ChordTracker:
    """macOS delivers the live modifier set on every key event, so unlike
    the Windows tracker this one is stateless — there is no held set to get
    stuck if a key-up is missed while another app has focus."""

    def __init__(self, approve: str, deny: str):
        self._approve = self._compile(approve)
        self._deny = self._compile(deny)

    @staticmethod
    def _compile(chord: str) -> tuple[int, int]:
        modifiers, key = parse_chord(chord)
        mask = 0
        for modifier in modifiers:
            mask |= _FLAG_FOR_MODIFIER[modifier]
        keycode = KEYCODES.get(key)
        if keycode is None:
            raise ValueError(f"no macOS keycode for {key!r}")
        return mask, keycode

    def classify(self, keycode: int) -> str:
        """`"trigger"` | `"other"` — what may be logged about a key without
        logging the key. (No `"modifier"`: the tap watches key-down events and
        modifiers arrive as flags on them, not as keycodes of their own.)"""
        return "trigger" if keycode in (self._approve[1], self._deny[1]) else "other"

    def feed(self, keycode: int, flags: int, real: bool) -> str | None:
        if not real:
            return None
        for decision, (mask, required_keycode) in (
            ("approve", self._approve),
            ("deny", self._deny),
        ):
            if keycode == required_keycode and (flags & mask) == mask:
                return decision
        return None


def trace_keys_enabled() -> bool:
    """Read per call, not at import: the answer is a deliberate opt-in a
    human makes for one debugging run."""
    return os.environ.get("FIREKEEP_HANDS_TRACE_KEYS") == "1"


def log_key_event(tracker: ChordTracker, keycode: int, flags: int, user_data: int,
                  source_state_id: int) -> None:
    """One DEBUG line per key, redacted.

    `flags`, `userData` and `sourceStateID` are all logged in full because
    they are exactly what the unverified source-state claim has to be
    measured against in Task 15, and none of them says which key was struck.
    The keycode does, so it is a class (`trigger`/`other`) unless
    `FIREKEEP_HANDS_TRACE_KEYS=1` — the broker runs for months autostarted
    and must not become a log of the human's passwords."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    if trace_keys_enabled():
        log.debug("key keycode=%s flags=0x%X userData=0x%X sourceStateID=%s",
                  keycode, flags, user_data, source_state_id)
    else:
        log.debug("key kind=%s flags=0x%X userData=0x%X sourceStateID=%s",
                  tracker.classify(keycode), flags, user_data, source_state_id)


def run_listener(tracker: ChordTracker, on_decision: Callable[[str], None]) -> None:
    """Install a listen-only tap and run the loop until the process ends.
    Blocking; the broker runs this on its own thread.

    Listen-only (`kCGEventTapOptionListenOnly`) so the tap can never swallow
    or alter a keystroke — the broker watches the keyboard, it does not
    stand in the way of it."""
    # pyobjc's Quartz re-exports CoreFoundation, so CFRunLoop* and
    # kCFRunLoopCommonModes are attributes of this one module. Reaching them
    # through a `Quartz.CoreFoundation` submodule is not a spelling pyobjc
    # guarantees.
    import Quartz

    # Two out-of-band types the system posts to a tap it has disabled: after
    # a callback took too long, and after the user disabled it. Re-enable, or
    # the chord silently stops working for the rest of the session.
    tap_disabled = (
        Quartz.kCGEventTapDisabledByTimeout,
        Quartz.kCGEventTapDisabledByUserInput,
    )

    state = {"tap": None}

    def callback(proxy, event_type, event, refcon):
        try:
            if event_type in tap_disabled:
                log.warning("event tap disabled (%s); re-enabling", event_type)
                if state["tap"] is not None:
                    Quartz.CGEventTapEnable(state["tap"], True)
                return event
            if Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
                # A held key is one gesture, not thirty a second. Filtered
                # here rather than in the tracker because macOS says so on
                # the event itself, and one press must answer one question.
                return event
            keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            flags = Quartz.CGEventGetFlags(event)
            user_data = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
            source_state = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceStateID)
            log_key_event(tracker, keycode, flags, user_data, source_state)
            decision = tracker.feed(keycode, flags, event_is_real(user_data, source_state))
            if decision:
                on_decision(decision)
        except Exception:  # noqa: BLE001 - a raising tap callback is a dead tap
            log.exception("chord tap callback failed")
        return event

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
        callback,
        None,
    )
    if tap is None:
        raise PermissionError("Input Monitoring permission missing")
    state["tap"] = tap

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
    )
    Quartz.CGEventTapEnable(tap, True)
    log.info("event tap installed")
    Quartz.CFRunLoopRun()
