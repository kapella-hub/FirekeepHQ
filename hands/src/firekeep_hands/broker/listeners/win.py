"""Windows chord listener: a low-level keyboard hook that ignores everything
a program typed.

`KBDLLHOOKSTRUCT.flags` carries two injection bits, and both must be clear
for an event to count. `LLKHF_INJECTED` (0x10) is set on anything delivered
through `SendInput`/`keybd_event` — including every key Hands itself types,
which is why Hands cannot approve its own steps even in principle.
`LLKHF_LOWER_IL_INJECTED` (0x02) additionally marks injection from a
lower-integrity process. Verified on hardware 2026-09-05: `SendInput`
events arrive here with 0x10 set, and a physical keypress arrives with
neither bit.

Residual, documented in the threat model: a kernel-mode input driver can
originate events with no injection bit set. This filter stops user-mode
malware and honest mistakes, not a rootkit.

Nothing here touches user32 at import time — the module's pure parts are
tested on Linux CI and macOS too, so every Win32 lookup lives inside
`run_listener`.
"""
from __future__ import annotations

import ctypes
import logging
import os
from typing import Callable

from .. import parse_chord

log = logging.getLogger(__name__)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

LLKHF_LOWER_IL_INJECTED = 0x02
LLKHF_INJECTED = 0x10

# Both the generic virtual-key and the left/right specific ones: a low-level
# hook reports VK_LCONTROL/VK_RCONTROL, but nothing stops a caller feeding
# the generic VK_CONTROL, and for a chord they mean the same gesture.
_MODIFIER_VKS = {
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x5B: "cmd", 0x5C: "cmd",
}

# Two names resolve differently here than in `listeners/mac.py`, in both
# cases because each table follows the label printed on that platform's
# keyboard — which is what the human reads when they pick a chord.
# `delete` is the PC's Del, a forward delete, so it and `forwarddelete` are
# the same VK here while on a Mac `delete` is the backspace-labelled key.
# `enter` and `return` are both VK_RETURN because Windows sends that for the
# keypad's Enter too (only an extended-key flag separates them), whereas
# macOS gives the keypad its own keycode.
_NAMED_VKS = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "escape": 0x1B, "esc": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "forwarddelete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{n}": 0x70 + n - 1 for n in range(1, 13)},
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    """Fixed-width fields rather than `DWORD`/`ULONG_PTR` so this class is
    definable on any host — the layout is only ever used against a real
    Windows callback, but the module has to import on Linux CI."""

    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


def kb_event_is_real(flags: int) -> bool:
    """False for anything a program generated. Only the two injection bits
    are consulted: 0x80 is `LLKHF_UP`, which is a perfectly real key being
    released, and treating it as synthetic would jam every modifier down."""
    return not (flags & LLKHF_INJECTED) and not (flags & LLKHF_LOWER_IL_INJECTED)


def _trigger_vk(key: str) -> int:
    if len(key) == 1:
        return ord(key.upper())
    vk = _NAMED_VKS.get(key)
    if vk is None:
        raise ValueError(f"no Windows virtual-key for {key!r}")
    return vk


class ChordTracker:
    """Which modifiers are currently held, and whether the key just pressed
    completes the approve or the deny chord.

    Synthetic events are ignored *entirely* — they neither trigger a decision
    nor change the held set. That second half matters: if injected modifier
    presses accumulated, a program could hold the chord's modifiers down
    invisibly and wait for the human to press a bare `y` for some other
    reason.

    One press is one answer. Windows repeats `WM_KEYDOWN` about thirty times
    a second for a held key, so without edge-triggering a human holding the
    chord for half a second would answer every question in the queue instead
    of the one in front of them — the phone path grants one permit per tap
    and the keyboard has to match it.

    Both held sets can go stale if a key-up is delivered somewhere this hook
    is not — a UAC prompt or a fast user switch taking the keyboard mid-press
    — and the two failures point in opposite directions, both safe. A
    phantom MODIFIER only ever adds to `_held`, and `required <= self._held`
    is a superset test, so the chord still fires; the cost is that a chord
    with an extra modifier held would also fire, which grants nothing the
    human did not press. A phantom TRIGGER sits in `_pressed` and suppresses
    the next press of that key as if it were auto-repeat, so the human's next
    chord does nothing and they press it again — one lost approval, and the
    press-release cycle clears it. Fails closed: a missed key-up can cost an
    approval, never cause one."""

    def __init__(self, approve: str, deny: str):
        approve_mods, approve_key = parse_chord(approve)
        deny_mods, deny_key = parse_chord(deny)
        self._approve = (approve_mods, _trigger_vk(approve_key))
        self._deny = (deny_mods, _trigger_vk(deny_key))
        self._held: set[str] = set()
        # Trigger keys currently down. Only ever holds the two trigger
        # virtual-keys, so it cannot grow with ordinary typing.
        self._pressed: set[int] = set()

    def classify(self, vk: int) -> str:
        """`"modifier"` | `"trigger"` | `"other"` — what may be logged about a
        key without logging the key. See `log_key_event`."""
        if vk in _MODIFIER_VKS:
            return "modifier"
        if vk == self._approve[1] or vk == self._deny[1]:
            return "trigger"
        return "other"

    def feed(self, vk: int, down: bool, real: bool) -> str | None:
        if not real:
            return None
        modifier = _MODIFIER_VKS.get(vk)
        if modifier is not None:
            # Modifier repeats are harmless: adding to a set twice is adding
            # to it once.
            if down:
                self._held.add(modifier)
            else:
                self._held.discard(modifier)
            return None
        if vk != self._approve[1] and vk != self._deny[1]:
            return None
        if not down:
            self._pressed.discard(vk)
            return None
        if vk in self._pressed:
            return None  # auto-repeat of a key already answered with
        self._pressed.add(vk)
        for decision, (required, trigger_vk) in (
            ("approve", self._approve),
            ("deny", self._deny),
        ):
            if vk == trigger_vk and required <= self._held:
                return decision
        return None


def trace_keys_enabled() -> bool:
    """Read per call, not at import: the answer is a deliberate opt-in a
    human makes for one debugging run."""
    return os.environ.get("FIREKEEP_HANDS_TRACE_KEYS") == "1"


def log_key_event(tracker: ChordTracker, vk: int, down: bool, real: bool, flags: int) -> None:
    """One DEBUG line per key, redacted.

    The broker sees every keystroke on the machine, and it is meant to run
    autostarted for months — so DEBUG must not turn it into a keystroke log
    of the human's passwords. What is recorded is the class of the key
    (`modifier`/`trigger`/`other`), never which one, plus the flags and the
    real/synthetic verdict, which is all the chord logic depends on. The raw
    virtual-key needs `FIREKEEP_HANDS_TRACE_KEYS=1` on top of DEBUG."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    if trace_keys_enabled():
        log.debug("key vk=0x%02X down=%s flags=0x%02X real=%s", vk, down, flags, real)
    else:
        log.debug("key kind=%s down=%s flags=0x%02X real=%s",
                  tracker.classify(vk), down, flags, real)


def run_listener(tracker: ChordTracker, on_decision: Callable[[str], None]) -> None:
    """Install the hook and pump messages until the process ends. Blocking;
    the broker runs this on its own thread.

    Every Win32 name is resolved here, not at import: this module's pure
    parts are unit-tested on Linux CI and macOS."""
    import ctypes.wintypes as w

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
    user32.SetWindowsHookExW.restype = w.HHOOK
    user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, w.WPARAM, w.LPARAM]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]

    def proc(nCode, wParam, lParam):
        if nCode >= 0:
            try:
                ks = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                real = kb_event_is_real(ks.flags)
                log_key_event(tracker, ks.vkCode, down, real, ks.flags)
                decision = tracker.feed(ks.vkCode, down, real)
                if decision:
                    on_decision(decision)
            except Exception:  # noqa: BLE001
                # An exception escaping a ctypes callback prints a traceback
                # and leaves the hook in an undefined state; Windows also
                # unhooks a callback that is too slow. Swallow, log, carry on.
                log.exception("chord hook callback failed")
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    callback = HOOKPROC(proc)  # must outlive the hook, or the callback is freed
    # hMod=None: a low-level hook runs in the installing process, so there is
    # no DLL to name. This is what the 2026-09-05 hardware probe used.
    hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, callback, None, 0)
    if not hook:
        raise OSError(ctypes.get_last_error(), "SetWindowsHookExW failed")
    log.info("keyboard hook installed")
    try:
        msg = w.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
    finally:
        user32.UnhookWindowsHookEx(hook)
