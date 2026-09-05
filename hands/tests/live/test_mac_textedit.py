"""Live macOS checks. These drive the real machine: they open TextEdit, type
into it, and post real keystrokes. Nothing here runs unless
`FIREKEEP_HANDS_LIVE=1` on darwin, and the module skips before importing any
framework so a default `pytest` run on Windows or Linux CI collects it
harmlessly.

Run on the MacBook (Task 15):

    FIREKEEP_HANDS_LIVE=1 python -m pytest tests/live/test_mac_textedit.py -q -s

Both Accessibility and (for the tap) Input Monitoring must be granted to the
interpreter running pytest, or the tests skip with a message saying so.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

if sys.platform != "darwin":
    pytest.skip("macOS only", allow_module_level=True)
if os.environ.get("FIREKEEP_HANDS_LIVE") != "1":
    pytest.skip("set FIREKEEP_HANDS_LIVE=1 to drive the real machine",
                allow_module_level=True)


def wait_for(predicate, timeout=10.0, interval=0.25):
    """Poll until `predicate` returns something truthy. A real UI appears when
    it appears; a fixed sleep is either flaky or slow."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


@pytest.fixture
def backend():
    from firekeep_hands.backends.mac import MacBackend

    mac = MacBackend()
    permissions = mac.permissions()
    if permissions["accessibility"] != "ok":
        pytest.skip("Accessibility permission missing for this interpreter")
    return mac


def test_textedit_type_select_copy_and_close(backend):
    """The end-to-end shape of a Hands session: open an app, find a control
    in its accessibility tree, type into it, and read the result back out
    through the clipboard."""
    original_clipboard = backend.clipboard_get()
    try:
        assert backend.open_app("TextEdit") is True
        assert wait_for(lambda: backend.focus_app("TextEdit")), "TextEdit never focused"

        window = wait_for(lambda: backend.active_window())
        assert window is not None and window.app == "TextEdit"

        area = wait_for(lambda: backend.find(
            "", role="AXTextArea", app="TextEdit", limit=1))
        assert area, "no AXTextArea in TextEdit's window"
        backend.click(area[0].rect.center())

        backend.type_text("hands live")
        time.sleep(0.5)

        backend.key("cmd+a")
        backend.key("cmd+c")
        copied = wait_for(lambda: "hands live" in backend.clipboard_get())
        assert copied, f"clipboard was {backend.clipboard_get()!r}"
    finally:
        # Only ever send the close chords at TextEdit. If the test failed
        # before TextEdit came forward, an unconditional cmd+w would close
        # whatever the operator happens to have open.
        current = backend.active_window()
        if current is not None and current.app == "TextEdit":
            backend.key("cmd+w")
            time.sleep(0.7)
            backend.key("cmd+d")      # "Don't Save" in the close sheet
            time.sleep(0.5)
        backend.clipboard_set(original_clipboard)


def test_measure_source_state_of_untagged_synthetic_event():
    """MEASUREMENT, not an assertion about macOS.

    The broker's listener rejects an event on two independent grounds: the
    Hands tag, and `kCGEventSourceStateID != kCGEventSourceStateHIDSystemState`.
    The second half is unverified — it is the plan's open question. This posts
    an UNTAGGED synthetic key event, records what a listen-only tap actually
    sees, and prints it. It asserts only that the tap recorded something, so
    it can never fail for reporting an inconvenient answer.

    Side effect: this posts a real "a" keystroke to whatever is frontmost.
    Run it with a scratch window focused.
    """
    import Quartz

    recorded: list[tuple[int, int, int, int]] = []

    def callback(proxy, event_type, event, refcon):
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            return event
        recorded.append((
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode),
            Quartz.CGEventGetFlags(event),
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData),
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceStateID),
        ))
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
        pytest.skip("Input Monitoring permission missing for this interpreter")

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)

    # Posted AFTER the tap is live, and deliberately untagged: a tagged event
    # would tell us only what we already know.
    for pressed in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, 0, pressed)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

    deadline = time.time() + 3.0
    while not recorded and time.time() < deadline:
        # The listener uses CFRunLoopRun(), which never returns; a test has to
        # pump the loop in slices instead.
        Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.25, False)

    Quartz.CGEventTapEnable(tap, False)

    print("\nMEASURED (keycode, flags, kCGEventSourceUserData, kCGEventSourceStateID):")
    for row in recorded:
        print("   ", row)
    print("  kCGEventSourceStateHIDSystemState ==",
          getattr(Quartz, "kCGEventSourceStateHIDSystemState", 1))
    assert recorded, "the tap recorded nothing — the measurement did not run"
