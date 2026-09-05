"""The one test that drives a real desktop: Notepad, end to end.

Everything else in the Windows suite runs against a fake `uiautomation`, so
this is the only place that proves the three real seams actually connect —
UI Automation finds the editor, tagged `SendInput` reaches it, and the
Windows clipboard hands the text back. It is opt-in (`FIREKEEP_HANDS_LIVE=1`)
because it steals the foreground and types on whatever machine runs it.

Safety, which is the reason this does not look like the sketch in the plan:
Windows 11's Notepad restores its previous session, so opening it can put a
stack of the human's own unsaved tabs on screen next to ours. So the test
opens a *file it created* under tmp_path, and refuses to type until it has
read that file's own contents back out of the focused Document — if the
foreground is anything other than our scratch tab, the test fails instead of
typing into someone's notes. It closes with Ctrl+S then Ctrl+W (saving our
own temp file, which makes the tab closable with no dialog) rather than
Alt+F4 plus a guessed "Don't save" access key, which would have aimed at the
whole window and every tab in it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FIREKEEP_HANDS_LIVE") == "1" and sys.platform == "win32"),
    reason="drives the real desktop; set FIREKEEP_HANDS_LIVE=1 on Windows to run it",
)

SEED = "firekeep-hands-live-seed"
# 219 characters, numbered so a corrupt run is legible in the output, and
# non-ASCII so UTF-16 expansion is exercised. The length is deliberate on two
# counts: it is a regression test for the unpaced-injection bug (which
# produced the right character *count* and the wrong characters), and it
# crosses `_TYPE_GUARD_CHUNK` twice, so the elevation re-check between chunks
# has to hand back to typing without dropping the boundary character.
TYPED = " ".join(
    f"[{n}] the quick brown fox jumps over 13 lazy dogs, café."
    for n in range(1, 5)
)
STEM = "firekeep-hands-live"


def _notepad_pids() -> set[int]:
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq notepad.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        fields = [field.strip('"') for field in line.split('","')]
        if len(fields) > 1 and fields[0].lower() == "notepad.exe":
            pids.add(int(fields[1]))
    return pids


def _until(predicate, seconds: float, interval: float = 0.25):
    """Poll `predicate` until it returns something truthy. Every wait here is
    on a real UI settling, so there is no deterministic event to await."""
    deadline = time.monotonic() + seconds
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def _window(backend):
    for window in backend.windows():
        if STEM in window.title.lower():
            return window
    return None


def _editor(backend):
    hits = backend.find("Text editor", role="Document", app=None, limit=3)
    return hits[0] if hits else None


def _foreground_is_ours(backend):
    window = backend.active_window()
    return window if window is not None and STEM in window.title.lower() else None


def _is_dirty(backend):
    """Notepad marks an unsaved tab with a leading asterisk in the title."""
    window = _window(backend)
    return window is not None and window.title.startswith("*")


def _dismiss_save_prompt(backend) -> bool:
    """Answer Notepad's unsaved-changes prompt through UI Automation.

    It is drawn inside the window rather than as a window of its own, and
    while it is up it swallows every keystroke — so more `key()` calls
    cannot clear it, and a run that leaves one open makes every later run
    fail in a way that looks like broken typing. Pressing its button through
    the InvokePattern is both the reliable way out and the only place the
    suite drives a real InvokePattern.
    """
    observation = backend.observe(app="Notepad", region=None, max_nodes=400,
                                  text_budget=0, screenshot=False, max_width=0)
    for control in observation.controls:
        if control.role == "Button" and control.name == "Don't save":
            backend.invoke(control)
            return True
    return False


@pytest.mark.timeout(180)
def test_notepad_round_trip(tmp_path):
    from firekeep_hands.backends.win import WinBackend

    backend = WinBackend()
    assert backend.permissions() == {"accessibility": "ok", "screen": "ok", "input": "ok"}

    path = tmp_path / f"{STEM}.txt"
    path.write_text(SEED, encoding="utf-8")

    before = _notepad_pids()
    restore_clipboard = backend.clipboard_get()
    opened = None
    try:
        assert backend.open_app(str(path)) is True
        opened = _until(lambda: _window(backend), 30)
        assert opened is not None, "no window opened the scratch file"
        if opened.app.lower() != "notepad":
            pytest.skip(f"the default .txt handler here is {opened.app!r}, not Notepad")
        print(f"window: {opened}")

        assert backend.focus_app(STEM) is True, "could not bring the scratch tab forward"
        # Wait for *our* window specifically. Taking whatever active_window()
        # returns first is how an earlier version of this test started typing
        # into the browser that still had the foreground.
        active = _until(lambda: _foreground_is_ours(backend), 10)
        assert active is not None, f"foreground is {backend.active_window()}"
        assert active.elevated is False

        # The interlock: only type once the focused document is provably the
        # file this test wrote.
        editor = _until(lambda: _editor(backend), 10)
        assert editor is not None, "no Document control in the focused window"
        assert editor.value == SEED, f"focused document is not ours: {editor.value[:60]!r}"
        assert "Value" in editor.patterns
        print(f"editor: {editor.ref} value={editor.value!r}")

        backend.key("ctrl+a")
        backend.type_text(TYPED)
        typed = _until(lambda: (_editor(backend) or editor).value == TYPED, 10)
        assert typed, f"typing did not land: {(_editor(backend) or editor).value!r}"
        print(f"after type_text: {_editor(backend).value!r}")

        backend.key("ctrl+a")
        backend.key("ctrl+c")
        clip = _until(lambda: TYPED in backend.clipboard_get(), 10)
        assert clip, f"clipboard held {backend.clipboard_get()!r}"
        print(f"clipboard: {backend.clipboard_get()!r}")

        # Saving our own scratch file proves the keystrokes reached the real
        # document and not just the accessibility layer, and it is what lets
        # Ctrl+W close the tab without a prompt.
        backend.key("ctrl+s")
        saved = _until(lambda: path.read_text(encoding="utf-8-sig") == TYPED, 15)
        assert saved, f"file on disk holds {path.read_text(encoding='utf-8-sig')!r}"
        print(f"file on disk: {path.read_text(encoding='utf-8-sig')!r}")

        # Only close a clean tab. Ctrl+W on a dirty one raises the in-window
        # save prompt, which then eats every keystroke that follows.
        clean = _until(lambda: not _is_dirty(backend), 15)
        assert clean, f"tab still dirty: {_window(backend).title!r}"
        backend.key("ctrl+w")
        closed = _until(lambda: _window(backend) is None, 15)
        assert closed, "the scratch tab did not close"
        print("tab closed")
    finally:
        backend.clipboard_set(restore_clipboard)
        # Every step below sends keystrokes or kills a process, so all of it
        # is gated on the window actually being Notepad. On the skip path the
        # file opened in the human's own editor, and cleaning up there would
        # mean closing their tabs and force-killing their unsaved work.
        is_notepad = opened is not None and opened.app.lower() == "notepad"
        if is_notepad:
            # Leave no scratch tab and, above all, no open save prompt: one
            # left behind swallows the keystrokes of every later run.
            for _ in range(5):
                if _dismiss_save_prompt(backend):
                    time.sleep(1.0)
                    continue
                if _window(backend) is None:
                    break
                # Never send Ctrl+W on faith. The path that reaches cleanup
                # most often is the body failing *because* focus could not be
                # brought forward — precisely when a blind keystroke lands on
                # whatever the human actually has in front. A tab left open is
                # the cheap failure; closing someone else's is not.
                backend.focus_app(STEM)
                time.sleep(0.5)
                if _foreground_is_ours(backend) is None:
                    print("cleanup: could not bring the scratch tab forward, so "
                          "Notepad is deliberately left open rather than sending "
                          "Ctrl+W to another window")
                    break
                backend.key("ctrl+w")
                time.sleep(1.0)
            # Only a Notepad this test started, never one the human already had.
            if opened.pid not in before:
                subprocess.run(["taskkill", "/PID", str(opened.pid), "/F"],
                               capture_output=True)


@pytest.mark.timeout(60)
def test_absolute_pointer_coordinates_land_on_the_pixel_they_name():
    """Move the pointer and read it back with GetCursorPos.

    Only the move half of a click is sent, so this touches no application:
    it is checking the 0..65535 normalisation, which is the one piece of the
    pointer path that has no other way to be wrong out loud. It also catches
    the DPI trap — from a process that is not per-monitor aware a scaled
    display measures smaller than it is, and every one of these would land
    short. The cursor is put back where the human left it.
    """
    import ctypes

    from firekeep_hands.backends import _win_input as wi
    from firekeep_hands.backends.win import WinBackend

    WinBackend()  # sets per-monitor DPI awareness before any metric is read

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def cursor():
        point = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return (point.x, point.y)

    def move_to(x, y):
        wi.send(wi.build_click(x, y, "left", False)[:1])  # the move event only
        time.sleep(0.15)
        return cursor()

    origin = cursor()
    vx, vy, vw, vh = wi.virtual_screen()
    print(f"virtual desktop {vw}x{vh} at ({vx}, {vy}), cursor was at {origin}")
    try:
        # Corners and centre of the whole virtual desktop, not the primary
        # monitor: on a multi-monitor machine the first and last of these are
        # exactly the points the old SM_CXSCREEN maths could not reach.
        for target in [(vx, vy), (vx + 100, vy + 100),
                       (vx + vw // 2, vy + vh // 2),
                       (vx + vw - 1, vy + vh - 1)]:
            landed = move_to(*target)
            print(f"  aimed {target} -> landed {landed}")
            assert landed == target
    finally:
        move_to(*origin)


@pytest.mark.timeout(60)
def test_screenshot_of_the_foreground_window_is_a_png():
    from firekeep_hands.backends.win import WinBackend

    backend = WinBackend()
    observation = backend.observe(app=None, region=None, max_nodes=40, text_budget=400,
                                 screenshot=True, max_width=640)
    assert observation.window is not None
    assert observation.screenshot_png is not None
    assert observation.screenshot_png[:8] == b"\x89PNG\r\n\x1a\n"
    print(f"screenshot: {len(observation.screenshot_png)} bytes of "
          f"{observation.window.rect} at max_width=640")
