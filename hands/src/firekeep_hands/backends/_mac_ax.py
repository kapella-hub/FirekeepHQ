"""The pyobjc façade — the one place in Hands that names an Apple framework
symbol, so `mac.py` can be unit-tested against fake modules and so a single
file is all that needs patching if an API turns out to behave differently on
real hardware.

PLATFORM-MODULE RULE: nothing is imported at module level. `Quartz`,
`ApplicationServices` and `AppKit` are resolved inside `AX.__init__`, so this
file imports cleanly on Windows and Linux CI, and constructing `AX()` is the
only thing that can fail for want of pyobjc.

**UNVERIFIED UNTIL THE MACBOOK RUN (Task 15).** Everything here is written to
the documented pyobjc calling conventions and exercised against fakes that
mirror them, but no line of it has executed against a real framework:

* out-parameter functions (`AXUIElementCopyAttributeValue`,
  `AXUIElementCopyActionNames`, `AXUIElementIsAttributeSettable`,
  `AXUIElementGetPid`, `AXValueGetValue`) are called with a trailing `None`
  and expected to return `(error, value)`;
* `AXUIElementPerformAction` / `AXUIElementSetAttributeValue` return a bare
  `AXError`;
* geometry unwrapping (`point()` / `size()`) is the least certain of the lot,
  which is why it is a four-step ladder rather than one call.
"""
from __future__ import annotations

import re

from .base import HandsError

# kAXErrorSuccess. Every other AXError value is a failure; the callers here
# do not branch on which, they only need "did it work".
_AX_SUCCESS = 0

# Fallbacks for the two AXValue type tags, used only if pyobjc does not
# export the constants under these names. The numeric values are stable
# public API (AXValueType: CGPoint = 1, CGSize = 2).
_AX_VALUE_CGPOINT = 1
_AX_VALUE_CGSIZE = 2

# `<AXValue 0x… {type = kAXValueCGPointType; value = x:12.000000 y:34.000000}>`
# — the last-resort read of an AXValueRef, for pyobjc builds whose
# AXValueGetValue wrapper does not hand back a usable struct. Ugly, and
# deliberately last.
_PAIR_IN_REPR = r"{a}:(-?[0-9.]+)\s+{b}:(-?[0-9.]+)"


class AX:
    """A thin, stateless wrapper over the accessibility, event and AppKit
    APIs. Methods return plain Python values (or None) rather than propagating
    AXError codes, because every caller wants the same thing: the value, or
    nothing."""

    def __init__(self) -> None:
        # The imports live here, not at module scope — see the rule above.
        import AppKit
        import ApplicationServices
        import Quartz

        self.appkit = AppKit
        self.services = ApplicationServices
        # `mac.py` builds CGEvents directly off this handle: the event
        # constructors are numerous, and wrapping each in a method would
        # move the backend's logic into the façade without hiding anything.
        # Every other framework call goes through a method below.
        self.quartz = Quartz

        self._point_type = getattr(
            ApplicationServices, "kAXValueCGPointType", _AX_VALUE_CGPOINT)
        self._size_type = getattr(
            ApplicationServices, "kAXValueCGSizeType", _AX_VALUE_CGSIZE)
        self._prompt_key = getattr(
            ApplicationServices, "kAXTrustedCheckOptionPrompt",
            "AXTrustedCheckOptionPrompt")

    # --- elements ---------------------------------------------------------

    def system_wide(self):
        """The system-wide element — the only route to `AXFocusedApplication`
        when AppKit has no frontmost application to report."""
        return self.services.AXUIElementCreateSystemWide()

    def app_for_pid(self, pid: int):
        return self.services.AXUIElementCreateApplication(pid)

    def focused_app(self) -> tuple[object | None, int | None]:
        """`(app element, pid)` for whatever the user is looking at.

        AppKit first because `frontmostApplication()` gives the pid directly;
        the system-wide element is the fallback for the window where AppKit
        reports nothing (during an app switch, or from a process with no
        connection to the window server)."""
        running = self.appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if running is not None:
            pid = int(running.processIdentifier())
            return self.app_for_pid(pid), pid
        element = self.attr(self.system_wide(), "AXFocusedApplication")
        if element is None:
            return None, None
        return element, self.pid_of(element)

    def pid_of(self, element) -> int | None:
        try:
            error, pid = self.services.AXUIElementGetPid(element, None)
        except Exception:  # noqa: BLE001 - a dead element must not crash a walk
            return None
        return int(pid) if error == _AX_SUCCESS else None

    # --- attributes and actions -------------------------------------------

    def attr(self, element, name: str):
        """One attribute, or None for any non-zero AXError.

        Missing and unreadable are the same answer on purpose: an AX tree is
        full of elements that do not implement a given attribute, and a walk
        that had to branch on eight error codes would be unreadable for no
        gain."""
        if element is None:
            return None
        try:
            error, value = self.services.AXUIElementCopyAttributeValue(
                element, name, None)
        except Exception:  # noqa: BLE001 - an element can die mid-walk
            return None
        return value if error == _AX_SUCCESS else None

    def children(self, element) -> list:
        value = self.attr(element, "AXChildren")
        if isinstance(value, (list, tuple)):
            return list(value)
        return []

    def actions(self, element) -> tuple[str, ...]:
        try:
            error, names = self.services.AXUIElementCopyActionNames(element, None)
        except Exception:  # noqa: BLE001
            return ()
        if error != _AX_SUCCESS or not names:
            return ()
        return tuple(str(name) for name in names)

    def settable(self, element, name: str) -> bool:
        try:
            error, is_settable = self.services.AXUIElementIsAttributeSettable(
                element, name, None)
        except Exception:  # noqa: BLE001
            return False
        return bool(is_settable) if error == _AX_SUCCESS else False

    def perform(self, element, action: str) -> None:
        error = self.services.AXUIElementPerformAction(element, action)
        if error != _AX_SUCCESS:
            raise HandsError("backend", f"{action} failed (AXError {error})")

    def set_attr(self, element, name: str, value) -> None:
        error = self.services.AXUIElementSetAttributeValue(element, name, value)
        if error != _AX_SUCCESS:
            raise HandsError("backend", f"setting {name} failed (AXError {error})")

    # --- geometry ---------------------------------------------------------

    def point(self, value) -> tuple[float, float] | None:
        return self._pair(value, self._point_type, ("x", "y"), ("x", "y"))

    def size(self, value) -> tuple[float, float] | None:
        return self._pair(value, self._size_type, ("width", "height"), ("w", "h"))

    def _pair(self, value, ax_type, attributes, repr_keys):
        """AXPosition and AXSize come back in whichever shape this pyobjc
        build hands over, and that is the detail most likely to differ on
        hardware. Four shapes are accepted, cheapest first: an already-plain
        pair, a bridged struct with named fields, an AXValueRef that
        `AXValueGetValue` opens, and — last — the numbers in the repr."""
        if value is None:
            return None

        # pyobjc's NSPoint/NSSize are sequence-like as well as attributed,
        # so this branch also catches an already-bridged struct.
        if isinstance(value, (tuple, list)) and len(value) == 2:
            pair = self._as_floats(value[0], value[1])
            if pair is not None:
                return pair

        pair = self._as_floats(getattr(value, attributes[0], None),
                               getattr(value, attributes[1], None))
        if pair is not None:
            return pair

        unwrapped = self._ax_value_get(value, ax_type)
        if unwrapped is not None:
            pair = self._as_floats(getattr(unwrapped, attributes[0], None),
                                   getattr(unwrapped, attributes[1], None))
            if pair is not None:
                return pair
            if isinstance(unwrapped, (tuple, list)) and len(unwrapped) == 2:
                pair = self._as_floats(unwrapped[0], unwrapped[1])
                if pair is not None:
                    return pair

        match = re.search(
            _PAIR_IN_REPR.format(a=repr_keys[0], b=repr_keys[1]), repr(value))
        if match:
            return float(match.group(1)), float(match.group(2))
        return None

    @staticmethod
    def _as_floats(a, b) -> tuple[float, float] | None:
        if isinstance(a, bool) or isinstance(b, bool):
            return None
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return float(a), float(b)
        return None

    def _ax_value_get(self, value, ax_type):
        getter = getattr(self.services, "AXValueGetValue", None)
        if getter is None:
            return None
        try:
            result = getter(value, ax_type, None)
        except Exception:  # noqa: BLE001 - wrong AXValue type, or no wrapper
            return None
        if isinstance(result, tuple) and len(result) == 2:
            succeeded, unwrapped = result
            return unwrapped if succeeded else None
        return result

    # --- permissions ------------------------------------------------------

    def trusted(self) -> bool:
        """Is this process allowed to drive the accessibility API?

        The prompt option is explicitly False: a background MCP server that
        throws up a system dialog nobody asked for is worse than one that
        reports `accessibility: missing` and lets `firekeep hands doctor`
        explain what to click."""
        options = {self._prompt_key: False}
        return bool(self.services.AXIsProcessTrustedWithOptions(options))

    def screen_ok(self) -> bool:
        """Screen Recording permission, without prompting. Absent on macOS
        before Catalina, where screen capture needed no permission at all —
        hence the True default rather than a raise."""
        preflight = getattr(self.quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is None:
            return True
        return bool(preflight())

    # --- AppKit -----------------------------------------------------------

    def running_apps(self) -> list:
        return list(self.appkit.NSWorkspace.sharedWorkspace().runningApplications())

    def activate(self, running_app) -> bool:
        """Bring an app to the front. `IgnoringOtherApps` because Hands is
        acting for a human who just asked for this app; a polite activation
        that loses to whatever is frontmost would silently target the wrong
        window."""
        return bool(running_app.activateWithOptions_(
            self.appkit.NSApplicationActivateIgnoringOtherApps))

    def pasteboard_string(self) -> str | None:
        board = self.appkit.NSPasteboard.generalPasteboard()
        return board.stringForType_(self.appkit.NSPasteboardTypeString)

    def pasteboard_set(self, text: str) -> None:
        board = self.appkit.NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, self.appkit.NSPasteboardTypeString)
