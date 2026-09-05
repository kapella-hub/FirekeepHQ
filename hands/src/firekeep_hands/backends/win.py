"""The Windows Backend: UI Automation to see, tagged SendInput to act.

Observation goes through UI Automation rather than pixels wherever it can —
a `Control` carries a name, a role and the patterns it supports, which is
what lets `routing.py` press a button through `InvokePattern` instead of
simulating a mouse. Pixel input is the fallback, never the first choice.

Platform-module rule: nothing here touches Win32 or `uiautomation` at import
time. Both optional modules are imported inside `__init__` (and a failure is
*reported* through `permissions()`, not raised — a missing dependency should
show up in `firekeep doctor`, not as a crash at backend selection), and every
`WinDLL` lookup lives in a cached accessor called at use time. So this module
imports on Linux and macOS, and its pure parts — ref parsing, rect maths,
tree compaction — are unit-tested there against an injected fake
`uiautomation` in `tests/test_win_backend.py`.
"""
from __future__ import annotations

import ctypes
import io
import os
import subprocess
import sys
import time

from . import _win_input as wi
from .base import Control, HandsError, Observation, Rect, WindowInfo

# The roles worth showing a model. Everything else in a real tree — panes,
# groups, separators, custom containers — is structure, not something you can
# act on, and a Chrome window has thousands of them.
_INTERACTIVE_ROLES = frozenset({
    "Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "MenuItem",
    "ListItem", "TreeItem", "TabItem", "Hyperlink", "Document", "SplitButton",
    "PasswordBox",
})

# Roles that can turn out to be a secure field. UI Automation reports one as
# an `Edit` with `IsPassword` set; `Document` is here because a rich editor
# can present as one and failing to notice is the expensive direction.
_PASSWORD_CAPABLE_ROLES = frozenset({"Edit", "Document"})

# A `Text` node is kept only when it is short enough to be a label rather
# than a paragraph: labels identify the controls beside them, paragraphs are
# body copy that belongs in `Observation.text`, not in the control list.
_TEXT_ROLE_MAX_NAME = 80

# Checked in this order, so `patterns` is stable between observations.
_PATTERN_NAMES = ("Invoke", "Value", "Toggle", "SelectionItem",
                  "ExpandCollapse", "Scroll")

# Nodes the walk may visit per node it is allowed to keep. A browser or an
# IDE has tens of thousands of elements and each property read is a
# cross-process COM call, so an uncapped walk is a hang, not a slow answer.
_VISIT_BUDGET_PER_NODE = 20
_MIN_VISIT_BUDGET = 2000
_FIND_MAX_NODES = 500

# Characters typed between two elevation re-checks. See `type_text`.
_TYPE_GUARD_CHUNK = 100

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
ERROR_ACCESS_DENIED = 5
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
SW_SHOW = 5
SW_RESTORE = 9
ASFW_ANY = 0xFFFFFFFF

_FOREGROUND_SETTLE_SECONDS = 1.0
_FOREGROUND_POLL_SECONDS = 0.02

# Characters cmd.exe would read as syntax rather than as part of a name.
# `open_app` hands its argument to `cmd /c start`, so a name containing one
# of these is refused rather than parsed.
_CMD_METACHARACTERS = '&|<>^"\n\r'

_CLIPBOARD_ATTEMPTS = 10
_CLIPBOARD_RETRY_SECONDS = 0.05

_user32 = None
_kernel32 = None
_advapi32 = None


def _import_optional(name: str):
    """Indirection so a test can simulate a machine without `uiautomation`
    or `mss` without touching the real import system."""
    return __import__(name)


def _set_dpi_aware() -> None:
    """Make this process per-monitor DPI aware, so UI Automation rectangles
    and screenshot pixels are the same coordinate space. `uiautomation`
    already does this when it imports; calling it again returns
    E_ACCESSDENIED ("awareness is already set"), which is why every failure
    here is swallowed rather than reported."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001 - includes "already set" and "no shcore"
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def _u32():
    global _user32
    if _user32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        void, i32, u32 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32
        lib.IsWindowVisible.argtypes = [void]
        lib.IsWindowVisible.restype = i32
        lib.IsIconic.argtypes = [void]
        lib.IsIconic.restype = i32
        lib.ShowWindow.argtypes = [void, i32]
        lib.ShowWindow.restype = i32
        lib.BringWindowToTop.argtypes = [void]
        lib.BringWindowToTop.restype = i32
        lib.SetForegroundWindow.argtypes = [void]
        lib.SetForegroundWindow.restype = i32
        lib.GetForegroundWindow.argtypes = []
        lib.GetForegroundWindow.restype = void
        lib.AllowSetForegroundWindow.argtypes = [u32]
        lib.AllowSetForegroundWindow.restype = i32
        lib.AttachThreadInput.argtypes = [u32, u32, i32]
        lib.AttachThreadInput.restype = i32
        lib.GetWindowThreadProcessId.argtypes = [void, void]
        lib.GetWindowThreadProcessId.restype = u32
        lib.OpenClipboard.argtypes = [void]
        lib.OpenClipboard.restype = i32
        lib.CloseClipboard.argtypes = []
        lib.CloseClipboard.restype = i32
        lib.EmptyClipboard.argtypes = []
        lib.EmptyClipboard.restype = i32
        lib.GetClipboardData.argtypes = [u32]
        lib.GetClipboardData.restype = void
        lib.SetClipboardData.argtypes = [u32, void]
        lib.SetClipboardData.restype = void
        _user32 = lib
    return _user32


def _k32():
    global _kernel32
    if _kernel32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        void, i32, u32, size = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32, ctypes.c_size_t
        lib.OpenProcess.argtypes = [u32, i32, u32]
        lib.OpenProcess.restype = void
        lib.CloseHandle.argtypes = [void]
        lib.CloseHandle.restype = i32
        lib.QueryFullProcessImageNameW.argtypes = [void, u32, ctypes.c_wchar_p,
                                                   ctypes.POINTER(u32)]
        lib.QueryFullProcessImageNameW.restype = i32
        lib.GlobalAlloc.argtypes = [u32, size]
        lib.GlobalAlloc.restype = void
        lib.GlobalLock.argtypes = [void]
        lib.GlobalLock.restype = void
        lib.GlobalUnlock.argtypes = [void]
        lib.GlobalUnlock.restype = i32
        lib.GlobalFree.argtypes = [void]
        lib.GlobalFree.restype = void
        lib.GetCurrentThreadId.argtypes = []
        lib.GetCurrentThreadId.restype = u32
        _kernel32 = lib
    return _kernel32


def _a32():
    global _advapi32
    if _advapi32 is None:
        lib = ctypes.WinDLL("advapi32", use_last_error=True)
        void, i32, u32 = ctypes.c_void_p, ctypes.c_int32, ctypes.c_uint32
        lib.OpenProcessToken.argtypes = [void, u32, ctypes.POINTER(void)]
        lib.OpenProcessToken.restype = i32
        lib.GetTokenInformation.argtypes = [void, i32, void, u32, ctypes.POINTER(u32)]
        lib.GetTokenInformation.restype = i32
        _advapi32 = lib
    return _advapi32


def _token_is_elevated(process_handle) -> bool:
    """`TOKEN_ELEVATION.TokenIsElevated` for an already-open process handle.

    This, not the `OpenProcess` result, is the signal that actually fires:
    a medium-integrity process *can* open a same-user elevated process with
    PROCESS_QUERY_LIMITED_INFORMATION. Access denied on the token is itself
    an answer — a medium-IL process cannot open a high-IL process's token —
    so it counts as elevated rather than as unknown.
    """
    token = ctypes.c_void_p()
    if not _a32().OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(token)):
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        elevated = ctypes.c_uint32(0)
        returned = ctypes.c_uint32(0)
        ok = _a32().GetTokenInformation(
            token, TOKEN_ELEVATION, ctypes.byref(elevated),
            ctypes.sizeof(elevated), ctypes.byref(returned),
        )
        return bool(ok and elevated.value)
    finally:
        _k32().CloseHandle(token)


def _process_info(pid: int) -> tuple[str, bool]:
    """`(image name without ".exe", elevated)` for a process id.

    Module-level, and small, because it is the one thing in observation that
    needs a real process behind a real pid — every unit test replaces it.
    A process that refuses to open at all is reported elevated: that is the
    protected-process case, which is at least as far out of Hands' reach as
    an elevated one. `consent.exe` (the UAC prompt itself) is named
    explicitly because it is the window a runaway agent would most like to
    click, and it is worth refusing by name as well as by token.
    """
    if sys.platform != "win32" or not pid:
        return ("", False)
    handle = _k32().OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ("", ctypes.get_last_error() == ERROR_ACCESS_DENIED)
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buffer))
        name = ""
        if _k32().QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            name = os.path.splitext(os.path.basename(buffer.value))[0]
        return (name, name.lower() == "consent" or _token_is_elevated(handle))
    finally:
        _k32().CloseHandle(handle)


def _window_is_visible(hwnd: int) -> bool:
    if sys.platform != "win32":
        return True
    return bool(_u32().IsWindowVisible(hwnd))


def _parse_ref(ref: str) -> tuple[int, tuple[int, ...]]:
    """`"w30530:42.1"` -> `(0x30530, (42, 1))`.

    A ref names both the window that held the element and the element's UI
    Automation runtime id, because an element object cannot outlive the
    observation that produced it — `invoke` re-walks the window and matches
    the runtime id rather than holding a COM pointer that may already be
    dead.
    """
    if not isinstance(ref, str) or not ref.startswith("w") or ":" not in ref:
        raise HandsError("invalid_action", f"not a control ref: {ref!r}")
    handle, _, runtime = ref[1:].partition(":")
    try:
        return (int(handle, 16), tuple(int(part) for part in runtime.split(".") if part))
    except ValueError:
        raise HandsError("invalid_action", f"not a control ref: {ref!r}") from None


def _intersects(rect: Rect, bounds: Rect) -> bool:
    return (rect.w > 0 and rect.h > 0
            and rect.x < bounds.x + bounds.w and bounds.x < rect.x + rect.w
            and rect.y < bounds.y + bounds.h and bounds.y < rect.y + rect.h)


class WinBackend:
    """UI Automation to see, tagged SendInput to act.

    Threading: `uiautomation`'s COM client binds to the first thread that
    uses it, so an instance must be constructed and called on one and the
    same thread — a caller that hands work to a pool needs a dedicated
    backend thread, or `uiautomation.InitializeUIAutomationInCurrentThread()`
    on each one.
    """

    name = "win"

    def __init__(self):
        self._generation = 0
        self._uia = None
        self._mss = None
        self._uia_error: str | None = None
        self._mss_error: str | None = None
        try:
            self._uia = _import_optional("uiautomation")
        except Exception as exc:  # noqa: BLE001
            self._uia_error = str(exc)
        try:
            self._mss = _import_optional("mss")
        except Exception as exc:  # noqa: BLE001
            self._mss_error = str(exc)
        _set_dpi_aware()

    # -- capability reporting ---------------------------------------------

    def permissions(self) -> dict[str, str]:
        """Windows has no per-app consent gate for accessibility, screen
        capture or input — the only way any of the three can be unavailable
        is a dependency that failed to import, so that is what is reported."""
        return {
            "accessibility": "ok" if self._uia_error is None else "missing",
            "screen": "ok" if self._mss_error is None else "missing",
            "input": "ok",
        }

    def _auto(self):
        if self._uia is None:
            raise HandsError("permission", f"uiautomation is unavailable: {self._uia_error}")
        return self._uia

    # -- windows -----------------------------------------------------------

    def active_window(self) -> WindowInfo | None:
        element = self._foreground_element()
        return None if element is None else self._window_info(element)

    def windows(self) -> list[WindowInfo]:
        return [self._window_info(element) for element in self._top_level_elements()]

    def _foreground_element(self):
        try:
            return self._auto().GetForegroundControl()
        except HandsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HandsError("backend", f"could not read the foreground window: {exc}") from exc

    def _top_level_elements(self) -> list:
        try:
            children = self._auto().GetRootControl().GetChildren()
        except HandsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HandsError("backend", f"could not enumerate windows: {exc}") from exc
        elements = []
        for child in children:
            try:
                hwnd = int(child.NativeWindowHandle or 0)
                if not hwnd or not _window_is_visible(hwnd):
                    continue
                if _rect_of(child) is None:
                    continue
            except Exception:  # noqa: BLE001 - a window that closed mid-walk
                continue
            elements.append(child)
        return elements

    def _window_element(self, app: str):
        """The window `app` names: an exact process-name match wins, then any
        window whose process name or title contains it. Title matching is the
        fallback a human expects — "Yahoo Mail" names a Chrome tab, not a
        process."""
        needle = app.lower()
        fallback = None
        for element in self._top_level_elements():
            info = self._window_info(element)
            if info.app.lower() == needle:
                return element
            if fallback is None and (needle in info.app.lower() or needle in info.title.lower()):
                fallback = element
        return fallback

    def _window_info(self, element) -> WindowInfo:
        title = _text_property(element, "Name")
        try:
            pid = int(element.ProcessId or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        app, elevated = _process_info(pid)
        return WindowInfo(
            app=app or title,
            title=title,
            pid=pid,
            rect=_rect_of(element) or Rect(0, 0, 0, 0),
            elevated=elevated,
        )

    # -- observation -------------------------------------------------------

    def observe(self, *, app: str | None, region: Rect | None, max_nodes: int,
                text_budget: int, screenshot: bool, max_width: int) -> Observation:
        self._generation += 1
        element = self._window_element(app) if app else self._foreground_element()
        if element is None:
            return Observation(self._generation, None, [], "", None, False)
        window = self._window_info(element)
        bounds = region or window.rect
        # A window whose rectangle could not be read would otherwise clip
        # every control away and return an empty scene; None means unclipped.
        clip = bounds if bounds.w > 0 and bounds.h > 0 else None
        try:
            hwnd = int(element.NativeWindowHandle or 0)
        except Exception:  # noqa: BLE001
            hwnd = 0
        controls, truncated = self._compact(element, hwnd, window.app, clip, max_nodes)
        return Observation(
            generation=self._generation,
            window=window,
            controls=controls,
            text=_text_of(window.title, controls, text_budget),
            screenshot_png=self._screenshot(bounds, max_width) if screenshot else None,
            truncated=truncated,
        )

    def _compact(self, root, hwnd: int, app: str, bounds: Rect | None,
                 max_nodes: int) -> tuple[list[Control], bool]:
        """Depth-first, document order, keeping only what a model could act
        on. Every property read is wrapped: an element can be destroyed
        between two calls (a menu closing, a page navigating) and a COM error
        on one node must not lose the whole observation."""
        controls: list[Control] = []
        budget = max(_MIN_VISIT_BUDGET, max_nodes * _VISIT_BUDGET_PER_NODE)
        stack = [root]
        visited = 0
        while stack:
            element = stack.pop()
            visited += 1
            if visited > budget:
                return controls, True
            try:
                stack.extend(reversed(element.GetChildren()))
            except Exception:  # noqa: BLE001
                pass
            if element is root:
                continue
            control = self._control_of(element, hwnd, app, bounds)
            if control is None:
                continue
            controls.append(control)
            if len(controls) >= max_nodes:
                return controls, bool(stack)
        return controls, False

    def _control_of(self, element, hwnd: int, app: str,
                    bounds: Rect | None) -> Control | None:
        role = _role_of(element)
        if role is None:
            return None
        name = _text_property(element, "Name")
        if role not in _INTERACTIVE_ROLES:
            if role != "Text" or not 0 < len(name) <= _TEXT_ROLE_MAX_NAME:
                return None
        rect = _rect_of(element)
        if rect is None or (bounds is not None and not _intersects(rect, bounds)):
            return None
        try:
            runtime_id = tuple(int(part) for part in element.GetRuntimeId())
        except Exception:  # noqa: BLE001
            return None
        # One query per pattern, and only for a node we are keeping: each is
        # a cross-process COM call. The value comes out of the ValuePattern
        # already in hand rather than costing a second query.
        found = {p: _pattern(element, p) for p in _PATTERN_NAMES}
        patterns = tuple(p for p in _PATTERN_NAMES if found[p] is not None)
        # A secure field's contents are never read. Not into the Control, so
        # not into the observation a runtime sees, not into `text`, and not
        # into evidence. `patterns` still reports Value, so a runtime can
        # still fill the field — it just cannot read what is in it.
        value = "" if role == "PasswordBox" else _text_property(found["Value"], "Value")
        try:
            enabled = bool(element.IsEnabled)
        except Exception:  # noqa: BLE001
            enabled = True
        return Control(
            ref="w{:x}:{}".format(hwnd, ".".join(str(part) for part in runtime_id)),
            role=role, name=name, value=value, rect=rect, app=app,
            patterns=patterns, enabled=enabled,
        )

    def find(self, query: str, *, role: str | None, app: str | None,
             limit: int) -> list[Control]:
        observation = self.observe(app=app, region=None, max_nodes=_FIND_MAX_NODES,
                                   text_budget=0, screenshot=False, max_width=0)
        needle = query.lower()
        # Case-folded, like the window lookup above: `app` is a process name a
        # caller typed, and comparing it exactly against the image-name case
        # Windows reports ("Notepad") filters out every control in the very
        # window the caller just asked for.
        app_needle = app.lower() if app is not None else None
        matches = []
        for control in observation.controls:
            if role is not None and control.role != role:
                continue
            if app_needle is not None and control.app.lower() != app_needle:
                continue
            if needle in control.name.lower() or needle in control.value.lower():
                matches.append(control)
                if len(matches) >= limit:
                    break
        return matches

    def _screenshot(self, rect: Rect, max_width: int) -> bytes | None:
        if self._mss is None:
            raise HandsError("permission", f"mss is unavailable: {self._mss_error}")
        if rect.w <= 0 or rect.h <= 0:
            return None
        from PIL import Image

        # mss 10.2 renamed the factory and deprecated the old spelling; the
        # wheel allows >=9.0, where only the old one exists.
        factory = getattr(self._mss, "MSS", None) or self._mss.mss
        with factory() as sct:
            raw = sct.grab({"left": rect.x, "top": rect.y, "width": rect.w, "height": rect.h})
        image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        # Height is unbounded on purpose: a tall window should be narrowed to
        # the token budget, not squashed until its text stops being readable.
        image.thumbnail((max_width, 10 ** 6))
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    # -- acting ------------------------------------------------------------

    def _guard(self, window: WindowInfo | None) -> None:
        if window is not None and window.elevated:
            raise HandsError(
                "elevated_target",
                f"{(window.app or window.title)!r} runs elevated; Windows blocks input "
                "from Hands to it, and Hands would not send it if it did not",
            )

    def _resolve(self, control: Control):
        """The element behind a ref, re-found by runtime id. Also the point
        where the elevation guard runs for control-targeted actions: the ref
        names the window, so the guard does not depend on what happens to be
        in the foreground when the action arrives."""
        hwnd, runtime_id = _parse_ref(control.ref)
        auto = self._auto()
        try:
            window_element = auto.ControlFromHandle(hwnd)
        except Exception:  # noqa: BLE001
            window_element = None
        if window_element is None:
            raise HandsError("stale_ref", f"the window behind {control.ref!r} is gone")
        self._guard(self._window_info(window_element))
        element = _find_by_runtime_id(window_element, runtime_id)
        if element is None:
            raise HandsError("stale_ref", f"{control.ref!r} is no longer in the tree")
        return element

    def invoke(self, control: Control) -> None:
        element = self._resolve(control)
        pattern = _pattern(element, "Invoke")
        if pattern is None:
            raise HandsError("unsupported", f"{control.ref!r} has no InvokePattern")
        pattern.Invoke()

    def set_value(self, control: Control, value: str) -> None:
        element = self._resolve(control)
        pattern = _pattern(element, "Value")
        if pattern is None:
            raise HandsError("unsupported", f"{control.ref!r} has no ValuePattern")
        pattern.SetValue(value)

    def click(self, point: tuple[int, int], *, button: str = "left",
              double: bool = False) -> None:
        self._guard(self.active_window())
        wi.send_click(point[0], point[1], button, double)

    def type_text(self, text: str) -> None:
        """Typed in chunks, with the elevation guard re-checked between them.

        Every other action is instantaneous, so one check up front is the
        whole story. Typing is not: characters are paced, so a long string is
        a stretch of time during which the foreground can change underneath
        the keystrokes — including to something elevated. `routing.py` caps
        the length; this bounds how far the keys can run past a foreground
        that has become off-limits. The chunk check costs one UI Automation
        round trip per hundred characters, which is also comfortably longer
        than the inter-character pace, so it introduces no extra stutter.
        """
        self._guard(self.active_window())
        for start in range(0, len(text), _TYPE_GUARD_CHUNK):
            if start:
                self._guard(self.active_window())
            wi.send_text(text[start:start + _TYPE_GUARD_CHUNK])

    def key(self, chord: str) -> None:
        self._guard(self.active_window())
        wi.send_key_chord(chord)

    def scroll(self, point: tuple[int, int], dy: int) -> None:
        # Guarded like the rest: a wheel event is still synthetic input aimed
        # at whatever window is in front, and UIPI would drop it silently for
        # an elevated one. Failing loudly beats scrolling nothing.
        self._guard(self.active_window())
        wi.send_scroll(point[0], point[1], dy)

    # -- applications ------------------------------------------------------

    def focus_app(self, app: str) -> bool:
        element = self._window_element(app)
        if element is None:
            return False
        try:
            hwnd = int(element.NativeWindowHandle or 0)
        except Exception:  # noqa: BLE001
            return False
        if not hwnd or sys.platform != "win32":
            return False
        return _force_foreground(hwnd)

    def open_app(self, app: str) -> bool:
        """A filesystem path is launched directly; anything else (a command
        name, a `shell:AppsFolder\\…` AppUserModelId) goes through
        `cmd /c start`, which is the only launcher that resolves all of
        them."""
        if not app or any(char in app for char in _CMD_METACHARACTERS):
            raise HandsError("invalid_action", f"not a launchable application name: {app!r}")
        if sys.platform != "win32":
            raise HandsError("unsupported", "open_app needs Windows")
        expanded = os.path.expandvars(app)
        # "Looks like a path" has to mean more than "a file by that name
        # happens to exist here", or `open_app("notepad")` would launch a
        # stray file called `notepad` out of the working directory.
        looks_like_path = os.path.isabs(expanded) or any(
            sep and sep in expanded for sep in (os.sep, os.altsep))
        if looks_like_path and os.path.exists(expanded):
            os.startfile(expanded)  # noqa: S606 - a real path the caller named
            return True
        subprocess.Popen(  # noqa: S603
            ["cmd", "/c", "start", "", app],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True

    # -- clipboard ---------------------------------------------------------

    def clipboard_get(self) -> str:
        lib = _u32()
        _open_clipboard()
        try:
            handle = lib.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = _k32().GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                _k32().GlobalUnlock(handle)
        finally:
            lib.CloseClipboard()

    def clipboard_set(self, text: str) -> None:
        # The clipboard takes ownership of the block, so it is allocated
        # movable and never freed on the success path.
        payload = (text + "\0").encode("utf-16-le")
        handle = _k32().GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise HandsError("backend", "GlobalAlloc failed for the clipboard payload")
        pointer = _k32().GlobalLock(handle)
        if not pointer:
            _k32().GlobalFree(handle)
            raise HandsError("backend", "GlobalLock failed for the clipboard payload")
        ctypes.memmove(pointer, payload, len(payload))
        _k32().GlobalUnlock(handle)
        _open_clipboard()
        try:
            _u32().EmptyClipboard()
            if not _u32().SetClipboardData(CF_UNICODETEXT, handle):
                _k32().GlobalFree(handle)
                raise HandsError("backend", f"SetClipboardData failed "
                                            f"(GetLastError {ctypes.get_last_error()})")
        finally:
            _u32().CloseClipboard()


# -- element helpers (module level: pure, and shared by the walk) -----------


def _role_of(element) -> str | None:
    """`"ButtonControl"` -> `"Button"`. UI Automation's own name for the
    type, minus the suffix every one of them carries.

    With one deliberate translation: UI Automation has no password control
    type — it reports a secure field as an ordinary `Edit` carrying
    `IsPassword`. `policy.py` gates its `credential` class on
    `role in {"PasswordBox", "AXSecureTextField"}`, so passing `"Edit"`
    through would leave a typed password classified only by whether its
    *label* happened to say "password". The backend owns the translation
    into the cross-platform `Control.role` vocabulary, exactly as the macOS
    side maps to `AXSecureTextField`.
    """
    try:
        role = str(element.ControlTypeName or "")
    except Exception:  # noqa: BLE001
        return None
    if role.endswith("Control"):
        role = role[: -len("Control")]
    if role in _PASSWORD_CAPABLE_ROLES and _is_password(element):
        return "PasswordBox"
    return role or None


def _is_password(element) -> bool:
    """Only asked of the roles that can carry it — it is one more
    cross-process property read per node, and the walk visits thousands."""
    try:
        return bool(element.IsPassword)
    except Exception:  # noqa: BLE001
        return False


def _text_property(obj, attribute: str) -> str:
    try:
        return str(getattr(obj, attribute, "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _rect_of(element) -> Rect | None:
    """None for an element with no on-screen extent — collapsed, scrolled
    out of view, or destroyed between two property reads."""
    try:
        raw = element.BoundingRectangle
        left, top = int(raw.left), int(raw.top)
        width, height = int(raw.right) - left, int(raw.bottom) - top
    except Exception:  # noqa: BLE001
        return None
    if width <= 0 or height <= 0:
        return None
    return Rect(left, top, width, height)


def _pattern(element, name: str):
    """The typed getter when the element's class has one (`GetInvokePattern`
    lives on `ButtonControl`, not on `Control`), otherwise the generic
    `GetPattern`. Both return None for an unsupported pattern."""
    getter = getattr(element, f"Get{name}Pattern", None)
    if getter is not None:
        try:
            return getter()
        except Exception:  # noqa: BLE001
            return None
    generic = getattr(element, "GetPattern", None)
    if generic is None:
        return None
    try:
        import uiautomation

        pattern_id = getattr(uiautomation.PatternId, f"{name}Pattern", None)
        return None if pattern_id is None else generic(pattern_id)
    except Exception:  # noqa: BLE001
        return None


def _find_by_runtime_id(root, runtime_id: tuple[int, ...], budget: int = 20000):
    stack = [root]
    visited = 0
    while stack:
        element = stack.pop()
        visited += 1
        if visited > budget:
            return None
        try:
            if tuple(int(part) for part in element.GetRuntimeId()) == runtime_id:
                return element
        except Exception:  # noqa: BLE001
            pass
        try:
            stack.extend(reversed(element.GetChildren()))
        except Exception:  # noqa: BLE001
            pass
    return None


def _wait_for_foreground(hwnd: int, seconds: float) -> bool:
    """Whether `hwnd` actually reached the foreground. Read back rather than
    inferred: `SetForegroundWindow` returns non-zero in cases where the
    window still did not come forward, and the window manager takes a moment
    either way."""
    lib = _u32()
    deadline = time.monotonic() + seconds
    while True:
        if (lib.GetForegroundWindow() or 0) == hwnd:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_FOREGROUND_POLL_SECONDS)


def _force_foreground(hwnd: int) -> bool:
    """Bring a window to the front.

    Windows only lets the process that already owns the foreground change
    it; from anywhere else `SetForegroundWindow` flashes the taskbar button
    and returns. The documented way round it is to join the *foreground*
    window's input queue with `AttachThreadInput` for the duration of the
    call — attaching to the target's thread instead is the plausible-looking
    mistake that makes this silently do nothing, which is what it did here
    before the Notepad run.
    """
    lib = _u32()
    if lib.IsIconic(hwnd):
        lib.ShowWindow(hwnd, SW_RESTORE)
    lib.SetForegroundWindow(hwnd)
    if _wait_for_foreground(hwnd, 0.3):
        return True
    current = _k32().GetCurrentThreadId()
    foreground_thread = lib.GetWindowThreadProcessId(lib.GetForegroundWindow(), None)
    attached = (bool(foreground_thread) and foreground_thread != current
                and bool(lib.AttachThreadInput(current, foreground_thread, True)))
    try:
        lib.AllowSetForegroundWindow(ASFW_ANY)
        lib.BringWindowToTop(hwnd)
        lib.ShowWindow(hwnd, SW_SHOW)
        lib.SetForegroundWindow(hwnd)
    finally:
        if attached:
            lib.AttachThreadInput(current, foreground_thread, False)
    return _wait_for_foreground(hwnd, _FOREGROUND_SETTLE_SECONDS)


def _text_of(title: str, controls: list[Control], budget: int) -> str:
    body = " ".join(part for control in controls
                    for part in (control.name, control.value) if part)[:budget]
    return "\n".join(part for part in (title, body) if part)


def _open_clipboard() -> None:
    """Retry: the clipboard is a single global lock and the app that just
    handled Ctrl+C often still holds it. One attempt turns a routine race
    into a flaky failure."""
    lib = _u32()
    for attempt in range(_CLIPBOARD_ATTEMPTS):
        if lib.OpenClipboard(None):
            return
        time.sleep(_CLIPBOARD_RETRY_SECONDS)
    raise HandsError("backend", f"the clipboard stayed locked by another process "
                                f"after {_CLIPBOARD_ATTEMPTS} attempts")
