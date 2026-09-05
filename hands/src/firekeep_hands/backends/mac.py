"""The macOS Backend: accessibility trees through AXUIElement, synthetic
input through tagged CGEvents, screenshots through `screencapture`.

PLATFORM-MODULE RULE: no framework is imported at module level. Everything
Apple-specific is reached through `_mac_ax.AX`, which resolves pyobjc in its
own constructor — so this file imports on Windows and Linux CI and the unit
tests drive a `MacBackend` built against fake framework modules.

**UNVERIFIED UNTIL THE MACBOOK RUN (Task 15).** The whole file is written to
the documented APIs and proven only against fakes. The parts most likely to
need a touch-up are called out where they occur: geometry unwrapping (in
`_mac_ax`), the CGPoint-from-tuple bridging on mouse events, and the
double-click event sequence.

Two macOS facts shape the design:

* **There is no UAC.** `WindowInfo.elevated` is therefore always False here.
  A macOS process either has accessibility trust or it does not, and that is
  a per-process TCC grant reported by `permissions()` — there is no
  per-window privilege boundary for a backend to detect, and pretending
  otherwise would make the Windows elevation guard look portable when it is
  not.
* **Input Monitoring is not this process's problem.** `permissions()`
  reports `input: "unknown"` because the permission that matters for input
  belongs to the approval broker's event tap, in another process, which
  reports its own.
"""
from __future__ import annotations

import io
import os
import subprocess
import tempfile
from pathlib import Path

from .. import HANDS_TAG
from ._mac_ax import AX
from .base import Control, HandsError, Observation, Rect, WindowInfo

# Roles worth handing to a model: things a person can press, type into, pick
# or follow. Everything else in an AX tree is scaffolding.
_INTERACTIVE_ROLES = frozenset({
    "AXButton", "AXTextField", "AXTextArea", "AXSecureTextField", "AXCheckBox",
    "AXRadioButton", "AXPopUpButton", "AXMenuItem", "AXMenuBarItem", "AXLink",
    "AXRow", "AXCell", "AXTab", "AXComboBox",
})

# Static text is kept only when it is short enough to be a label rather than
# a document: a text view's contents arrive as AXStaticText too, and dumping
# a whole page of prose into the control list buries the buttons.
_STATIC_TEXT_ROLE = "AXStaticText"
_STATIC_TEXT_LIMIT = 80

# A password field's value is never read — not to build an observation, not
# to answer `find`. The role is on this list so the guard is a lookup rather
# than a condition someone can forget to repeat.
_SECRET_VALUE_ROLES = frozenset({"AXSecureTextField"})

# A hard ceiling on nodes visited, independent of `max_nodes`: a pathological
# tree (or a cycle a buggy app reports) must not hang the server. Hitting it
# is reported as truncation, same as hitting the control cap.
_MAX_VISITED_NODES = 20_000

# `find` observes first, so it needs its own node budget — generous, because
# the thing being searched for is often deep in a menu.
_FIND_MAX_NODES = 2_000

# Both shell-outs sit on the MCP server's request path, so neither may block
# it forever. `screencapture` can stall on a hung window server; `open` can
# block while Gatekeeper verifies a first launch, which is why it gets longer.
_SCREENCAPTURE_TIMEOUT = 10
_OPEN_TIMEOUT = 15

# CGEventFlags. Same four bits the broker's listener reads off real key
# events; spelled out again here rather than imported, because a backend must
# not depend on the broker.
_FLAG_SHIFT = 0x20000
_FLAG_CONTROL = 0x40000
_FLAG_ALTERNATE = 0x80000
_FLAG_COMMAND = 0x100000

_MODIFIER_FLAGS = {
    "shift": _FLAG_SHIFT,
    "ctrl": _FLAG_CONTROL,
    "control": _FLAG_CONTROL,
    "alt": _FLAG_ALTERNATE,
    "option": _FLAG_ALTERNATE,
    "opt": _FLAG_ALTERNATE,
    "cmd": _FLAG_COMMAND,
    "command": _FLAG_COMMAND,
    # Folded onto Command so one chord string means the same physical key —
    # the one beside the space bar — on both platforms.
    "win": _FLAG_COMMAND,
    "meta": _FLAG_COMMAND,
    "super": _FLAG_COMMAND,
}

# Virtual keycodes are positional: they name a physical key on an ANSI
# layout, not the character it produces. That is why this table is static and
# why `type_text` does not use it — typing goes through Unicode strings, so
# it works on any layout.
#
# This table is the reference: `delete` is 51 — the key actually labelled
# "delete" on a Mac keyboard — and 117 is `forwarddelete`. The approval
# broker's chord table (`broker/listeners/mac.py`) mirrors it entry for entry,
# and a broker test asserts the two are equal so neither can drift alone.
_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45,
    "m": 46,
    "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28,
    "9": 25, "0": 29,
    "return": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53, "enter": 76, "forwarddelete": 117,
    "home": 115, "pageup": 116, "end": 119, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# CGEventKeyboardSetUnicodeString takes a UniChar count, and Apple's guidance
# is to keep each call short; 20 UTF-16 units per event is the conventional
# chunk. Astral characters (emoji) are two units each, which is why the
# chunker counts UTF-16 rather than Python characters.
_UNICODE_CHUNK = 20


def _utf16_chunks(text: str, limit: int):
    """Split on character boundaries such that no chunk exceeds `limit`
    UTF-16 code units — never mid-surrogate-pair, which would post half an
    emoji."""
    chunk: list[str] = []
    used = 0
    for character in text:
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > limit and chunk:
            yield "".join(chunk)
            chunk, used = [], 0
        chunk.append(character)
        used += width
    if chunk:
        yield "".join(chunk)


class MacBackend:
    """One instance per process. Holds no element references between calls:
    every control is addressed by a `p<pid>:<index path>` ref that is
    re-walked from the application element on use, so a control that moved or
    was replaced is reported as `stale_ref` rather than acted on blind."""

    name = "mac"

    def __init__(self, ax: AX | None = None):
        # `ax` is injectable for the live run: if one framework call turns
        # out to need a different spelling on hardware, a subclass of AX is
        # the whole fix.
        self._ax = ax if ax is not None else AX()
        self._generation = 0

    # --- permissions ------------------------------------------------------

    def permissions(self) -> dict[str, str]:
        return {
            "accessibility": "ok" if self._ax.trusted() else "missing",
            "screen": "ok" if self._ax.screen_ok() else "missing",
            "input": "unknown",
        }

    # --- windows ----------------------------------------------------------

    def active_window(self) -> WindowInfo | None:
        element, pid = self._ax.focused_app()
        if element is None or pid is None:
            return None
        return self._window_info(self._focused_window(element), pid,
                                 self._app_name(pid, element))

    def windows(self) -> list[WindowInfo]:
        """One entry per regular app that currently has a window.

        `activationPolicy() == NSApplicationActivationPolicyRegular (0)` is
        the filter: menu-bar extras and background agents own AX elements too,
        and listing them as windows would be noise a model has to wade
        through."""
        found: list[WindowInfo] = []
        for running in self._ax.running_apps():
            try:
                if int(running.activationPolicy()) != 0:
                    continue
                pid = int(running.processIdentifier())
            except Exception:  # noqa: BLE001 - the app may have just quit
                continue
            element = self._ax.app_for_pid(pid)
            info = self._window_info(self._focused_window(element), pid,
                                     str(running.localizedName() or ""))
            if info is not None:
                found.append(info)
        return found

    # --- observation ------------------------------------------------------

    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int,
                text_budget: int, screenshot: bool, max_width: int) -> Observation:
        element, pid, app_name = self._target_app(app)
        self._generation += 1
        window_element = self._focused_window(element)
        window = self._window_info(window_element, pid, app_name)
        controls, truncated = self._walk(element, pid, app_name, max_nodes, region,
                                         window_element)
        image = None
        if screenshot:
            image = self._screenshot(
                region if region is not None else (window.rect if window else None),
                max_width,
            )
        return Observation(
            generation=self._generation,
            window=window,
            controls=controls,
            text=self._text(window, controls, text_budget),
            screenshot_png=image,
            truncated=truncated,
        )

    def find(self, query: str, *, role: str | None, app: str | None,
             limit: int) -> list[Control]:
        """A search that matches nothing returns nothing, including when the
        app named is not running — the same semantics as `FakeBackend`, which
        callers are written against. `observe` still raises `not_found` there,
        because being asked to observe a named app and finding none is a
        failed instruction rather than an empty result.

        Note this bumps the observation generation, since it observes to
        search."""
        needle = query.lower()
        matches: list[Control] = []
        try:
            observation = self.observe(app=app, region=None, max_nodes=_FIND_MAX_NODES,
                                       text_budget=0, screenshot=False, max_width=0)
        except HandsError as exc:
            if exc.code == "not_found":
                return []
            raise
        for control in observation.controls:
            if role is not None and control.role != role:
                continue
            if needle in control.name.lower() or needle in control.value.lower():
                matches.append(control)
                if len(matches) >= limit:
                    break
        return matches

    # --- acting on a control ----------------------------------------------

    def invoke(self, control: Control) -> None:
        element = self._resolve(control)
        if "AXPress" not in self._ax.actions(element):
            raise HandsError("invalid_action",
                             f"{control.role} {control.name!r} has no AXPress action")
        self._ax.perform(element, "AXPress")

    def set_value(self, control: Control, value: str) -> None:
        element = self._resolve(control)
        if not self._ax.settable(element, "AXValue"):
            raise HandsError("invalid_action",
                             f"{control.role} {control.name!r} has no settable AXValue")
        self._ax.set_attr(element, "AXValue", value)

    # --- synthetic input --------------------------------------------------

    def click(self, point: tuple[int, int], *, button: str = "left",
              double: bool = False) -> None:
        quartz = self._ax.quartz
        try:
            down, up, which = {
                "left": (quartz.kCGEventLeftMouseDown, quartz.kCGEventLeftMouseUp,
                         quartz.kCGMouseButtonLeft),
                "right": (quartz.kCGEventRightMouseDown, quartz.kCGEventRightMouseUp,
                          quartz.kCGMouseButtonRight),
            }[button]
        except KeyError:
            raise HandsError("invalid_action",
                             f"unknown mouse button {button!r}") from None
        # A double click is two full press/release pairs whose click state
        # counts up, not one pair labelled "2" — that is what apps actually
        # match on, and it is the sequence Apple's own sample code posts.
        # (The task text's "1 or 2" reads as one event; this is the
        # deliberate deviation, flagged for the hardware run.)
        for state in ((1, 2) if double else (1,)):
            for event_type in (down, up):
                event = quartz.CGEventCreateMouseEvent(None, event_type, point, which)
                quartz.CGEventSetIntegerValueField(
                    event, quartz.kCGMouseEventClickState, state)
                self._post(event)

    def type_text(self, text: str) -> None:
        """Types by Unicode string rather than by keycode, so it is layout
        independent: no mapping from character to physical key, and accented
        or non-Latin text arrives intact."""
        quartz = self._ax.quartz
        for chunk in _utf16_chunks(text, _UNICODE_CHUNK):
            units = len(chunk.encode("utf-16-le")) // 2
            for pressed in (True, False):
                event = quartz.CGEventCreateKeyboardEvent(None, 0, pressed)
                # Set on the release too: some apps read the string off
                # whichever of the pair they handle.
                quartz.CGEventKeyboardSetUnicodeString(event, units, chunk)
                self._post(event)

    def key(self, chord: str) -> None:
        """`"cmd+s"`, `"cmd+shift+z"`, or a bare `"escape"`.

        Modifiers are flags on the key event itself, not separate key events
        as on Windows — that is how macOS delivers a chord."""
        parts = [part.strip().lower() for part in chord.split("+")]
        if not parts or any(not part for part in parts):
            raise HandsError("invalid_action", f"not a key chord: {chord!r}")
        *modifiers, trigger = parts
        flags = 0
        for modifier in modifiers:
            flag = _MODIFIER_FLAGS.get(modifier)
            if flag is None:
                raise HandsError("invalid_action",
                                 f"unknown modifier {modifier!r} in {chord!r}")
            flags |= flag
        keycode = _KEYCODES.get(trigger)
        if keycode is None:
            raise HandsError("invalid_action",
                             f"no macOS keycode for {trigger!r} in {chord!r}")
        quartz = self._ax.quartz
        for pressed in (True, False):
            event = quartz.CGEventCreateKeyboardEvent(None, keycode, pressed)
            quartz.CGEventSetFlags(event, flags)
            self._post(event)

    def scroll(self, point: tuple[int, int], dy: int) -> None:
        """The wheel event carries no location, so the cursor is moved first:
        macOS delivers a scroll to whatever is under the pointer."""
        quartz = self._ax.quartz
        self._post(quartz.CGEventCreateMouseEvent(
            None, quartz.kCGEventMouseMoved, point, quartz.kCGMouseButtonLeft))
        self._post(quartz.CGEventCreateScrollWheelEvent(
            None, quartz.kCGScrollEventUnitLine, 1, dy))

    # --- apps and clipboard ------------------------------------------------

    def focus_app(self, app: str) -> bool:
        needle = app.strip().lower()
        for running in self._ax.running_apps():
            try:
                name = str(running.localizedName() or "")
                bundle = str(running.bundleIdentifier() or "")
            except Exception:  # noqa: BLE001 - the app may have just quit
                continue
            if needle in (name.lower(), bundle.lower()):
                self._ax.activate(running)
                return True
        return False

    def open_app(self, app: str) -> bool:
        """`open` handles the three ways a person names an app: a path, a
        bundle identifier, or the name in the Dock."""
        if "/" in app or app.startswith("~"):
            argv = ["open", app]
        elif "." in app and " " not in app:
            argv = ["open", "-b", app]
        else:
            argv = ["open", "-a", app]
        try:
            completed = subprocess.run(argv, check=False, timeout=_OPEN_TIMEOUT)
        except subprocess.TimeoutExpired:
            # `open` returns as soon as the launch is handed off; taking
            # longer than this means it is stuck, not that the app is slow.
            return False
        except OSError as exc:
            raise HandsError("backend", f"could not run open: {exc}") from exc
        return completed.returncode == 0

    def clipboard_get(self) -> str:
        return str(self._ax.pasteboard_string() or "")

    def clipboard_set(self, text: str) -> None:
        self._ax.pasteboard_set(text)

    # --- internals: events -------------------------------------------------

    def _post(self, event) -> None:
        """The one route to the event stream, so tagging cannot be forgotten:
        every event Hands posts carries HANDS_TAG in kCGEventSourceUserData,
        and the broker's tap ignores anything that does."""
        quartz = self._ax.quartz
        quartz.CGEventSetIntegerValueField(
            event, quartz.kCGEventSourceUserData, HANDS_TAG)
        quartz.CGEventPost(quartz.kCGHIDEventTap, event)

    # --- internals: the tree -----------------------------------------------

    def _target_app(self, app: str | None):
        """`(app element, pid, app name)` for the app to observe."""
        if app is None:
            element, pid = self._ax.focused_app()
            if element is None or pid is None:
                raise HandsError("not_found", "no frontmost application")
            return element, pid, self._app_name(pid, element)

        needle = app.strip().lower()
        for running in self._ax.running_apps():
            try:
                name = str(running.localizedName() or "")
                bundle = str(running.bundleIdentifier() or "")
                pid = int(running.processIdentifier())
            except Exception:  # noqa: BLE001
                continue
            if needle in (name.lower(), bundle.lower()):
                return self._ax.app_for_pid(pid), pid, name
        raise HandsError("not_found", f"no running application matches {app!r}")

    def _app_name(self, pid: int, element) -> str:
        for running in self._ax.running_apps():
            try:
                if int(running.processIdentifier()) == pid:
                    return str(running.localizedName() or "")
            except Exception:  # noqa: BLE001
                continue
        return str(self._ax.attr(element, "AXTitle") or "")

    def _focused_window(self, element):
        for attribute in ("AXFocusedWindow", "AXMainWindow"):
            window = self._ax.attr(element, attribute)
            if window is not None:
                return window
        for child in self._ax.children(element):
            if self._ax.attr(child, "AXRole") == "AXWindow":
                return child
        return None

    def _window_info(self, window, pid: int, app_name: str) -> WindowInfo | None:
        if window is None:
            return None
        return WindowInfo(
            app=app_name,
            title=str(self._ax.attr(window, "AXTitle") or ""),
            pid=pid,
            # A window is still worth reporting when its geometry will not
            # read — it is identified by pid and title, and the screenshot
            # path already falls back to the full screen for a zero rect.
            rect=self._rect(window) or Rect(0, 0, 0, 0),
            # No UAC analogue on macOS — see the module docstring.
            elevated=False,
        )

    def _walk(self, root, pid: int, app_name: str, max_nodes: int,
              region: Rect | None, focused_window=None) -> tuple[list[Control], bool]:
        """Depth-first, pre-order, from the application element. The ref of a
        control is its index path from that element, which is what makes a ref
        re-walkable without holding an AXUIElement across calls."""
        controls: list[Control] = []
        truncated = False
        visited = 0
        stack: list[tuple[tuple[int, ...], object]] = [((), root)]
        while stack:
            path, element = stack.pop()
            visited += 1
            if visited > _MAX_VISITED_NODES:
                truncated = True
                break
            if path:  # the application element itself is not a control
                control = self._control(element, path, pid, app_name)
                if control is not None and self._in_region(control.rect, region):
                    if len(controls) >= max_nodes:
                        truncated = True
                        break
                    controls.append(control)
            ordered = self._ordered_children(element, path, focused_window)
            for index, child in reversed(ordered):
                stack.append((path + (index,), child))
        return controls, truncated

    def _ordered_children(self, element, path: tuple[int, ...],
                          focused_window) -> list[tuple[int, object]]:
        """`(true index, child)` in the order to walk them.

        Only the application element is reordered, and only to put the focused
        window ahead of everything else. A real `AXApplication` lists its
        `AXMenuBar` — hundreds of `AXMenuItem`s, all of them interactive —
        alongside every open window, in no order Apple documents. Walked as
        they come, a tight `max_nodes` can return a menu and truncate before
        reaching the window the user is actually looking at.

        The index in each pair is the child's REAL position, so reordering the
        walk never changes a ref: `_resolve` re-walks the original order."""
        indexed = list(enumerate(self._ax.children(element)))
        if path:
            return indexed
        # Stable sort: ties keep the application's own ordering.
        indexed.sort(key=lambda pair: self._root_priority(pair[1], focused_window))
        return indexed

    def _root_priority(self, child, focused_window) -> int:
        if focused_window is not None:
            try:
                # pyobjc routes == on an AXUIElement to CFEqual.
                if child == focused_window:
                    return 0
            except Exception:  # noqa: BLE001 - a dead element must not stop the walk
                pass
        return 1 if self._ax.attr(child, "AXRole") == "AXWindow" else 2

    def _control(self, element, path: tuple[int, ...], pid: int,
                 app_name: str) -> Control | None:
        role = self._ax.attr(element, "AXRole")
        if not isinstance(role, str):
            return None
        name = self._name(element)
        if role == _STATIC_TEXT_ROLE:
            label = name or self._value(element, role)
            if not label or len(label) > _STATIC_TEXT_LIMIT:
                return None
        elif role not in _INTERACTIVE_ROLES:
            return None
        rect = self._rect(element)
        if rect is None:
            # An element Hands cannot locate is one it cannot safely click.
            # Dropping it is better than handing back a control whose centre
            # is somewhere else entirely.
            return None
        actions = self._ax.actions(element)
        patterns = []
        if "AXPress" in actions:
            patterns.append("AXPress")
        if self._ax.settable(element, "AXValue"):
            patterns.append("AXValue")
        enabled = self._ax.attr(element, "AXEnabled")
        return Control(
            ref=f"p{pid}:" + ".".join(str(index) for index in path),
            role=role,
            name=name,
            value=self._value(element, role),
            rect=rect,
            app=app_name,
            patterns=tuple(patterns),
            enabled=True if enabled is None else bool(enabled),
        )

    def _name(self, element) -> str:
        for attribute in ("AXTitle", "AXDescription", "AXPlaceholderValue"):
            value = self._ax.attr(element, attribute)
            if value:
                return str(value)
        return ""

    def _value(self, element, role: str) -> str:
        """A password field's value is not read at all — not redacted after
        the fact, never fetched. An observation crosses to a model and into
        the evidence log; the only way to keep a password out of both is to
        leave it in the app."""
        if role in _SECRET_VALUE_ROLES:
            return ""
        value = self._ax.attr(element, "AXValue")
        return "" if value is None else str(value)

    def _rect(self, element) -> Rect | None:
        """None when the geometry cannot be read, never a zero rect: a caller
        would take `Rect(0, 0, 0, 0).center()` for a real target and click the
        top-left of the main display, which on macOS is the Apple menu."""
        position = self._ax.point(self._ax.attr(element, "AXPosition"))
        size = self._ax.size(self._ax.attr(element, "AXSize"))
        if position is None or size is None:
            return None
        return Rect(round(position[0]), round(position[1]),
                    round(size[0]), round(size[1]))

    @staticmethod
    def _in_region(rect: Rect, region: Rect | None) -> bool:
        if region is None:
            return True
        if rect.w <= 0 or rect.h <= 0:
            return False
        return not (rect.x >= region.x + region.w
                    or rect.x + rect.w <= region.x
                    or rect.y >= region.y + region.h
                    or rect.y + rect.h <= region.y)

    def _text(self, window: WindowInfo | None, controls: list[Control],
              budget: int) -> str:
        parts: list[str] = []
        if window is not None and window.title:
            parts.append(window.title)
        for control in controls:
            if control.name:
                parts.append(control.name)
            if control.value:
                parts.append(control.value)
        return "\n".join(parts)[:budget]

    def _resolve(self, control: Control):
        """Re-walk a ref's index path and prove the element at the end is
        still the one the caller saw. A path is not an identity: an app that
        inserted a row would otherwise have Hands press whatever slid into
        the old index, which is the failure mode this check exists for."""
        pid, path = self._parse_ref(control.ref)
        element = self._ax.app_for_pid(pid)
        for index in path:
            children = self._ax.children(element)
            if index >= len(children):
                raise HandsError(
                    "stale_ref", f"{control.ref} no longer exists (observe again)")
            element = children[index]
        role = self._ax.attr(element, "AXRole")
        if role != control.role:
            raise HandsError(
                "stale_ref",
                f"{control.ref} is now {role!r}, was {control.role!r} (observe again)")
        return element

    @staticmethod
    def _parse_ref(ref: str) -> tuple[int, list[int]]:
        try:
            head, _, tail = ref.partition(":")
            if not head.startswith("p"):
                raise ValueError(ref)
            pid = int(head[1:])
            path = [int(part) for part in tail.split(".")] if tail else []
        except ValueError:
            raise HandsError("stale_ref", f"not a macOS control ref: {ref!r}") from None
        if any(index < 0 for index in path):
            raise HandsError("stale_ref", f"not a macOS control ref: {ref!r}")
        return pid, path

    # --- internals: screenshots --------------------------------------------

    def _screenshot(self, rect: Rect | None, max_width: int) -> bytes:
        """`screencapture` rather than a CGWindowList capture: it is the
        supported path, it honours the TCC grant the same way, and it needs
        no window id — which matters because a region can span windows.

        `-x` suppresses the shutter sound, `-o` omits the window shadow."""
        if not self._ax.screen_ok():
            raise HandsError(
                "permission",
                "Screen Recording permission missing "
                "(System Settings > Privacy & Security > Screen Recording)")
        handle, name = tempfile.mkstemp(suffix=".png")
        target = Path(name)
        try:
            os.close(handle)
            argv = ["screencapture", "-x", "-o"]
            if rect is not None and rect.w > 0 and rect.h > 0:
                argv += ["-R", f"{rect.x},{rect.y},{rect.w},{rect.h}"]
            argv.append(str(target))
            try:
                subprocess.run(argv, check=True, timeout=_SCREENCAPTURE_TIMEOUT)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    OSError) as exc:
                raise HandsError("backend", f"screencapture failed: {exc}") from exc
            raw = target.read_bytes()
        finally:
            target.unlink(missing_ok=True)
        return self._downscale(raw, max_width)

    @staticmethod
    def _downscale(raw: bytes, max_width: int) -> bytes:
        try:
            from PIL import Image
        except ImportError as exc:
            # Pillow is a declared dependency, so this means a broken install
            # rather than a bug — but it must arrive as a HandsError like
            # every other backend failure, not an ImportError the caller has
            # no branch for.
            raise HandsError("backend", f"pillow missing: {exc}") from exc

        image = Image.open(io.BytesIO(raw))
        image.load()
        if max_width > 0:
            # A huge height is deliberate: `thumbnail` preserves aspect ratio
            # and must constrain width only.
            image.thumbnail((max_width, 10 ** 6))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
