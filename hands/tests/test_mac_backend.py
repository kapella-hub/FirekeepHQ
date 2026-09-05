"""Unit tests for the macOS backend, driven entirely through fake
`Quartz` / `ApplicationServices` / `AppKit` modules injected into `sys.modules`.

None of this can run on macOS in CI, so the fakes carry the whole burden of
fidelity. They mirror pyobjc's calling shapes exactly:

* out-parameter functions take a trailing `None` and return a tuple —
  `AXUIElementCopyAttributeValue(el, name, None) -> (err, value)` — and the
  fakes *arity-check* that trailing argument, so the two-argument spelling
  (which a lenient fake would accept and hardware would reject) cannot pass
  here;
* ObjC selectors keep their real names, trailing underscores included
  (`processIdentifier()`, `activateWithOptions_()`, `setString_forType_()`).

Every framework call is recorded, so the tests assert on what the backend
*did* to the frameworks rather than on its return values alone. The real
hardware check is Task 15.
"""
from __future__ import annotations

import collections.abc
import io
import re
import subprocess
import sys
import types

import pytest

from firekeep_hands import HANDS_TAG
from firekeep_hands.backends.base import Control, HandsError, Rect

# --- AXError codes the fakes return (the real numeric values) --------------
AX_SUCCESS = 0
AX_ATTRIBUTE_UNSUPPORTED = -25205
AX_ACTION_UNSUPPORTED = -25206


class NSArrayLike(collections.abc.Sequence):
    """What pyobjc actually hands back for an array-valued AX attribute: a
    proxy over a CFArrayRef that implements the sequence protocol but is
    **not** a `list` or `tuple` subclass.

    The fakes use it everywhere pyobjc would, because a fake that returns a
    plain list hides the whole class of bug where the backend
    isinstance-checks against `(list, tuple)`: green here, an empty
    accessibility tree on hardware."""

    def __init__(self, items):
        self._items = list(items)

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self):
        return len(self._items)


class Recorder:
    """Every framework call, in order, as `(name, args)`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    def record(self, name: str, *args) -> None:
        self.calls.append((name, args))

    def named(self, name: str) -> list[tuple]:
        return [args for called, args in self.calls if called == name]


# --- the four shapes an AXPosition/AXSize can arrive in --------------------
# The backend must cope with all of them; each element in the fake tree uses
# a different one so every branch of the unwrap ladder is exercised.


class PointLike:
    """A CGPoint/CGSize already bridged to a Python object with attributes."""

    def __init__(self, a: float, b: float, *, size: bool = False):
        if size:
            self.width, self.height = a, b
        else:
            self.x, self.y = a, b


class FakeAXValue:
    """An opaque AXValueRef that only `AXValueGetValue` can open."""

    def __init__(self, payload: PointLike):
        self.payload = payload


class ReprOnlyAXValue:
    """An AXValueRef that `AXValueGetValue` refuses, leaving only the repr —
    the community workaround for pyobjc builds where the wrapper misbehaves."""

    def __init__(self, text: str):
        self._text = text

    def __repr__(self) -> str:  # pragma: no cover - exercised via the backend
        return self._text


class FakeElement:
    """One AXUIElement. `attrs` is what AXUIElementCopyAttributeValue reads,
    `actions` what AXUIElementCopyActionNames returns, `settable` the set of
    attribute names AXUIElementIsAttributeSettable answers True for."""

    def __init__(self, role: str, *, children=(), actions=(), settable=(), **attrs):
        self.attrs: dict[str, object] = {"AXRole": role}
        self.attrs.update(attrs)
        self.children = list(children)
        self.actions = list(actions)
        self.settable = set(settable)


def build_tree() -> tuple[FakeElement, dict[str, FakeElement]]:
    """The scene from the task brief, plus the elements the guarantees need:
    a secure field (whose value must never be read), an over-long static text
    (dropped), and a group holding a disabled button (proves the walk
    descends through containers it does not keep)."""
    save = FakeElement(
        "AXButton", AXTitle="Save", actions=["AXPress"],
        AXPosition=(10, 10), AXSize=(80, 30),            # plain tuples
    )
    text_area = FakeElement(
        "AXTextArea", settable=["AXValue"], AXValue="body text",
        AXPlaceholderValue="Type here",
        AXPosition=PointLike(0, 50), AXSize=PointLike(600, 400, size=True),
    )
    secure = FakeElement(
        "AXSecureTextField", AXTitle="Password", AXValue="hunter2",
        actions=["AXPress"], settable=["AXValue"],
        AXPosition=ReprOnlyAXValue(
            "<AXValue 0x0 {type = kAXValueCGPointType; value = x:5.000000 y:90.000000}>"),
        AXSize=ReprOnlyAXValue(
            "<AXValue 0x0 {type = kAXValueCGSizeType; value = w:200.000000 h:24.000000}>"),
    )
    ready = FakeElement(
        "AXStaticText", AXTitle="Ready", AXPosition=(0, 460), AXSize=(100, 16),
    )
    too_long = FakeElement(
        "AXStaticText", AXTitle="x" * 101, AXPosition=(0, 480), AXSize=(100, 16),
    )
    cancel = FakeElement(
        "AXButton", AXTitle="Cancel", actions=["AXPress"], AXEnabled=False,
        AXPosition=(200, 10), AXSize=(80, 30),
    )
    group = FakeElement("AXGroup", children=[cancel],
                        AXPosition=(0, 0), AXSize=(700, 500))
    window = FakeElement(
        "AXWindow", AXTitle="Untitled",
        children=[save, text_area, secure, ready, too_long, group],
        AXPosition=FakeAXValue(PointLike(0, 0)),
        AXSize=FakeAXValue(PointLike(700, 500, size=True)),
    )
    app = FakeElement("AXApplication", AXTitle="TextEdit", children=[window],
                      AXFocusedWindow=window)
    return app, {
        "app": app, "window": window, "save": save, "text_area": text_area,
        "secure": secure, "ready": ready, "too_long": too_long,
        "group": group, "cancel": cancel,
    }


class FakeEvent:
    def __init__(self, kind: str, **info):
        self.kind = kind
        self.info = info
        self.fields: dict[int, int] = {}
        self.flags = 0
        self.unicode: tuple[int, str] | None = None

    @property
    def tag(self) -> int | None:
        return self.fields.get(FakeQuartzConstants.kCGEventSourceUserData)


class FakeQuartzConstants:
    """Distinct integers standing in for the real Quartz constants; the
    backend must never depend on their values, only on identity."""

    kCGEventLeftMouseDown = 1
    kCGEventLeftMouseUp = 2
    kCGEventRightMouseDown = 3
    kCGEventRightMouseUp = 4
    kCGEventMouseMoved = 5
    kCGMouseButtonLeft = 10
    kCGMouseButtonRight = 11
    kCGMouseEventClickState = 20
    kCGEventSourceUserData = 21
    kCGEventSourceStateID = 22
    kCGHIDEventTap = 30
    kCGScrollEventUnitLine = 40


class FakeRunningApp:
    def __init__(self, pid: int, name: str, bundle: str, policy: int, rec: Recorder):
        self._pid, self._name, self._bundle, self._policy = pid, name, bundle, policy
        self._rec = rec

    def processIdentifier(self):
        return self._pid

    def localizedName(self):
        return self._name

    def bundleIdentifier(self):
        return self._bundle

    def activationPolicy(self):
        return self._policy

    def activateWithOptions_(self, options):
        self._rec.record("activateWithOptions_", self._name, options)
        return True


class FakePasteboard:
    def __init__(self, rec: Recorder):
        self._rec = rec
        self.contents = "clipboard start"

    def stringForType_(self, type_):
        self._rec.record("stringForType_", type_)
        return self.contents

    def clearContents(self):
        self._rec.record("clearContents")
        self.contents = ""
        return 1

    def setString_forType_(self, text, type_):
        self._rec.record("setString_forType_", text, type_)
        self.contents = text
        return True


class Frameworks:
    """Everything a test needs to reach into the fakes after the fact."""

    def __init__(self):
        self.rec = Recorder()
        self.app, self.nodes = build_tree()
        self.posted: list[tuple[int, FakeEvent]] = []
        self.trusted = True
        self.screen = True
        self.pasteboard = FakePasteboard(self.rec)
        self.frontmost: FakeRunningApp | None = None
        self.running: list[FakeRunningApp] = []
        self.apps_by_pid: dict[int, FakeElement] = {}
        self.system_wide = FakeElement("AXSystemWide")
        self.pids: dict[int, FakeElement] = {}


def install_fakes(monkeypatch) -> Frameworks:
    fw = Frameworks()
    rec = fw.rec

    text_edit = FakeRunningApp(42, "TextEdit", "com.apple.TextEdit", 0, rec)
    finder = FakeRunningApp(7, "Finder", "com.apple.finder", 0, rec)
    agent = FakeRunningApp(9, "SomeAgent", "com.example.agent", 1, rec)
    fw.frontmost = text_edit
    fw.running = [text_edit, finder, agent]
    fw.apps_by_pid = {42: fw.app, 7: FakeElement("AXApplication", AXTitle="Finder")}

    # --- ApplicationServices ------------------------------------------------
    app_services = types.ModuleType("ApplicationServices")

    def AXUIElementCreateSystemWide():
        rec.record("AXUIElementCreateSystemWide")
        return fw.system_wide

    def AXUIElementCreateApplication(pid):
        rec.record("AXUIElementCreateApplication", pid)
        return fw.apps_by_pid.get(pid, FakeElement("AXApplication"))

    def AXUIElementGetPid(element, out):
        assert out is None, "AXUIElementGetPid takes a trailing None out-parameter"
        for pid, el in fw.apps_by_pid.items():
            if el is element:
                return (AX_SUCCESS, pid)
        return (AX_ATTRIBUTE_UNSUPPORTED, 0)

    def AXUIElementCopyAttributeValue(element, name, out):
        assert out is None, "pyobjc out-parameter must be passed as None"
        rec.record("AXUIElementCopyAttributeValue", element, name)
        if name == "AXChildren":
            return (AX_SUCCESS, NSArrayLike(element.children))
        if name in element.attrs:
            return (AX_SUCCESS, element.attrs[name])
        return (AX_ATTRIBUTE_UNSUPPORTED, None)

    def AXUIElementCopyActionNames(element, out):
        assert out is None, "pyobjc out-parameter must be passed as None"
        rec.record("AXUIElementCopyActionNames", element)
        return (AX_SUCCESS, NSArrayLike(element.actions))

    def AXUIElementIsAttributeSettable(element, name, out):
        assert out is None, "pyobjc out-parameter must be passed as None"
        rec.record("AXUIElementIsAttributeSettable", element, name)
        return (AX_SUCCESS, name in element.settable)

    def AXUIElementPerformAction(element, action):
        rec.record("AXUIElementPerformAction", element, action)
        if action not in element.actions:
            return AX_ACTION_UNSUPPORTED
        return AX_SUCCESS

    def AXUIElementSetAttributeValue(element, name, value):
        rec.record("AXUIElementSetAttributeValue", element, name, value)
        if name not in element.settable:
            return AX_ATTRIBUTE_UNSUPPORTED
        element.attrs[name] = value
        return AX_SUCCESS

    def AXIsProcessTrustedWithOptions(options):
        rec.record("AXIsProcessTrustedWithOptions", options)
        return fw.trusted

    def AXValueGetValue(value, type_, out):
        assert out is None, "pyobjc out-parameter must be passed as None"
        rec.record("AXValueGetValue", value, type_)
        if isinstance(value, FakeAXValue):
            return (True, value.payload)
        return (False, None)

    for name, obj in [
        ("AXUIElementCreateSystemWide", AXUIElementCreateSystemWide),
        ("AXUIElementCreateApplication", AXUIElementCreateApplication),
        ("AXUIElementGetPid", AXUIElementGetPid),
        ("AXUIElementCopyAttributeValue", AXUIElementCopyAttributeValue),
        ("AXUIElementCopyActionNames", AXUIElementCopyActionNames),
        ("AXUIElementIsAttributeSettable", AXUIElementIsAttributeSettable),
        ("AXUIElementPerformAction", AXUIElementPerformAction),
        ("AXUIElementSetAttributeValue", AXUIElementSetAttributeValue),
        ("AXIsProcessTrustedWithOptions", AXIsProcessTrustedWithOptions),
        ("AXValueGetValue", AXValueGetValue),
        ("kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt"),
        ("kAXValueCGPointType", 1),
        ("kAXValueCGSizeType", 2),
    ]:
        setattr(app_services, name, obj)

    # --- Quartz -------------------------------------------------------------
    quartz = types.ModuleType("Quartz")
    for name in dir(FakeQuartzConstants):
        if name.startswith("k"):
            setattr(quartz, name, getattr(FakeQuartzConstants, name))

    def CGPreflightScreenCaptureAccess():
        rec.record("CGPreflightScreenCaptureAccess")
        return fw.screen

    def CGEventCreateMouseEvent(source, type_, point, button):
        rec.record("CGEventCreateMouseEvent", source, type_, point, button)
        return FakeEvent("mouse", type=type_, point=point, button=button)

    def CGEventCreateKeyboardEvent(source, keycode, down):
        rec.record("CGEventCreateKeyboardEvent", source, keycode, down)
        return FakeEvent("key", keycode=keycode, down=down)

    def CGEventCreateScrollWheelEvent(source, unit, count, dy):
        rec.record("CGEventCreateScrollWheelEvent", source, unit, count, dy)
        return FakeEvent("scroll", unit=unit, count=count, dy=dy)

    def CGEventKeyboardSetUnicodeString(event, length, text):
        rec.record("CGEventKeyboardSetUnicodeString", event, length, text)
        event.unicode = (length, text)

    def CGEventSetIntegerValueField(event, field, value):
        rec.record("CGEventSetIntegerValueField", event, field, value)
        event.fields[field] = value

    def CGEventSetFlags(event, flags):
        rec.record("CGEventSetFlags", event, flags)
        event.flags = flags

    def CGEventPost(tap, event):
        rec.record("CGEventPost", tap, event)
        fw.posted.append((tap, event))

    for name, obj in [
        ("CGPreflightScreenCaptureAccess", CGPreflightScreenCaptureAccess),
        ("CGEventCreateMouseEvent", CGEventCreateMouseEvent),
        ("CGEventCreateKeyboardEvent", CGEventCreateKeyboardEvent),
        ("CGEventCreateScrollWheelEvent", CGEventCreateScrollWheelEvent),
        ("CGEventKeyboardSetUnicodeString", CGEventKeyboardSetUnicodeString),
        ("CGEventSetIntegerValueField", CGEventSetIntegerValueField),
        ("CGEventSetFlags", CGEventSetFlags),
        ("CGEventPost", CGEventPost),
    ]:
        setattr(quartz, name, obj)

    # --- AppKit -------------------------------------------------------------
    appkit = types.ModuleType("AppKit")

    class FakeWorkspace:
        def frontmostApplication(self):
            return fw.frontmost

        def runningApplications(self):
            return NSArrayLike(fw.running)

    class NSWorkspace:
        @staticmethod
        def sharedWorkspace():
            return FakeWorkspace()

    class NSPasteboard:
        @staticmethod
        def generalPasteboard():
            return fw.pasteboard

    appkit.NSWorkspace = NSWorkspace
    appkit.NSPasteboard = NSPasteboard
    appkit.NSPasteboardTypeString = "public.utf8-plain-text"
    appkit.NSApplicationActivateIgnoringOtherApps = 2

    monkeypatch.setitem(sys.modules, "ApplicationServices", app_services)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    return fw


@pytest.fixture
def fw(monkeypatch) -> Frameworks:
    return install_fakes(monkeypatch)


@pytest.fixture
def backend(fw):
    from firekeep_hands.backends.mac import MacBackend

    return MacBackend()


def observe(backend, **overrides):
    kwargs = dict(app=None, region=None, max_nodes=200, text_budget=2000,
                  screenshot=False, max_width=1280)
    kwargs.update(overrides)
    return backend.observe(**kwargs)


# --- the platform-module rule ---------------------------------------------


@pytest.mark.parametrize("module_name", [
    "firekeep_hands.backends.mac",
    "firekeep_hands.backends._mac_ax",
])
def test_no_framework_import_at_module_level(module_name):
    """PLATFORM-MODULE RULE: both files must import on Windows and Linux CI,
    so every pyobjc import lives inside a function or method body (indented),
    never at column 0."""
    import importlib
    import inspect

    module = importlib.import_module(module_name)
    banned = re.compile(r"^(?:import|from)\s+(Quartz|AppKit|ApplicationServices|Cocoa|objc)\b")
    offenders = [
        line for line in inspect.getsource(module).splitlines()
        if banned.match(line)
    ]
    assert offenders == [], f"{module_name} imports a framework at module level: {offenders}"


# --- observe ---------------------------------------------------------------


def test_children_accepts_an_nsarray_proxy(fw):
    """pyobjc returns AXChildren as an NSArray proxy: a sequence, but not a
    list or tuple subclass. An isinstance check against (list, tuple) passes
    every test where the fake returns a real list and finds an empty
    accessibility tree on hardware — silently, because AXFocusedWindow keeps
    working, so `observe` still names the right window while reporting no
    controls at all."""
    import ApplicationServices

    from firekeep_hands.backends._mac_ax import AX

    raw = ApplicationServices.AXUIElementCopyAttributeValue(
        fw.app, "AXChildren", None)[1]
    assert not isinstance(raw, (list, tuple)), "the fake stopped mirroring pyobjc"
    assert len(raw) == 1

    assert AX().children(fw.app) == [fw.nodes["window"]]


def test_action_names_accept_an_nsarray_proxy_too(fw):
    from firekeep_hands.backends._mac_ax import AX

    assert AX().actions(fw.nodes["save"]) == ("AXPress",)


def test_a_bridged_point_need_not_be_a_tuple_subclass(fw):
    """The same trap for geometry: a bridged NSPoint is sequence-like without
    being a tuple."""
    from firekeep_hands.backends._mac_ax import AX

    assert AX().point(NSArrayLike([12.4, 34.6])) == (12.4, 34.6)
    assert AX().size(NSArrayLike([100, 50])) == (100.0, 50.0)


def test_observe_yields_refs_rects_patterns_and_a_first_generation(backend):
    obs = observe(backend)
    by_ref = {c.ref: c for c in obs.controls}

    assert obs.generation == 1
    assert [c.ref for c in obs.controls] == [
        "p42:0.0", "p42:0.1", "p42:0.2", "p42:0.3", "p42:0.5.0",
    ]

    save = by_ref["p42:0.0"]
    assert save.role == "AXButton" and save.name == "Save"
    assert save.rect == Rect(10, 10, 80, 30)          # plain-tuple geometry
    assert save.patterns == ("AXPress",)
    assert save.app == "TextEdit" and save.enabled is True

    area = by_ref["p42:0.1"]
    assert area.role == "AXTextArea" and area.value == "body text"
    assert area.name == "Type here"                    # AXPlaceholderValue fallback
    assert area.rect == Rect(0, 50, 600, 400)          # .x/.y + .width/.height
    assert area.patterns == ("AXValue",)               # settable but not pressable

    assert by_ref["p42:0.2"].rect == Rect(5, 90, 200, 24)   # repr-only AXValue
    assert set(by_ref["p42:0.2"].patterns) == {"AXPress", "AXValue"}


def test_the_window_and_its_axvalue_geometry_are_reported(backend):
    obs = observe(backend)
    assert obs.window is not None
    assert obs.window.app == "TextEdit"
    assert obs.window.title == "Untitled"
    assert obs.window.pid == 42
    assert obs.window.rect == Rect(0, 0, 700, 500)     # AXValueGetValue branch
    assert obs.window.elevated is False


def test_a_disabled_control_is_reported_disabled(backend):
    by_ref = {c.ref: c for c in observe(backend).controls}
    assert by_ref["p42:0.5.0"].name == "Cancel"
    assert by_ref["p42:0.5.0"].enabled is False


def test_static_text_longer_than_eighty_characters_is_dropped(backend):
    names = [c.name for c in observe(backend).controls]
    assert "Ready" in names
    assert not any(len(n) > 80 for n in names)


def test_max_nodes_truncates_and_says_so(backend):
    obs = observe(backend, max_nodes=1)
    assert len(obs.controls) == 1 and obs.truncated is True
    assert observe(backend, max_nodes=200).truncated is False


def test_text_is_the_window_title_plus_names_and_values_within_budget(backend):
    obs = observe(backend, text_budget=40)
    assert obs.text.startswith("Untitled")
    assert len(obs.text) <= 40
    assert "hunter2" not in observe(backend, text_budget=4000).text


def test_a_region_keeps_only_the_controls_inside_it(backend):
    obs = observe(backend, region=Rect(0, 0, 150, 45))
    assert [c.ref for c in obs.controls] == ["p42:0.0"]


def menu_bar() -> FakeElement:
    items = [
        FakeElement("AXMenuBarItem", AXTitle=f"Menu {n}", actions=["AXPress"],
                    AXPosition=(n * 60, 0), AXSize=(60, 24))
        for n in range(5)
    ]
    return FakeElement("AXMenuBar", children=items,
                       AXPosition=(0, 0), AXSize=(1440, 24))


def test_the_focused_window_is_walked_before_the_menu_bar(backend, fw):
    """A real AXApplication lists its AXMenuBar — hundreds of interactive
    AXMenuItems — alongside its windows, in no documented order. Walked as
    they come, a tight max_nodes returns a menu and truncates before reaching
    what the user is looking at."""
    fw.app.children.insert(0, menu_bar())
    obs = observe(backend, max_nodes=2)
    assert [c.name for c in obs.controls] == ["Save", "Type here"]


def test_reordering_the_walk_does_not_change_a_ref(backend, fw):
    """The ref is the child's real index, not its position in the walk — so
    it still re-walks correctly."""
    fw.app.children.insert(0, menu_bar())
    control = {c.name: c for c in observe(backend).controls}["Save"]
    assert control.ref == "p42:1.0"
    backend.invoke(control)
    assert fw.rec.named("AXUIElementPerformAction") == [(fw.nodes["save"], "AXPress")]


def test_a_window_still_sorts_ahead_of_a_menu_bar_with_no_focused_window(backend, fw):
    """The fallback when AXFocusedWindow is unavailable: role AXWindow wins."""
    fw.app.children.insert(0, menu_bar())
    del fw.app.attrs["AXFocusedWindow"]
    assert [c.name for c in observe(backend, max_nodes=2).controls] == \
        ["Save", "Type here"]


def test_observing_an_unknown_app_is_not_found(backend):
    with pytest.raises(HandsError) as excinfo:
        observe(backend, app="Nope")
    assert excinfo.value.code == "not_found"


def test_a_control_with_unreadable_geometry_is_skipped(backend, fw):
    """A Rect(0, 0, 0, 0) would put `center()` on the Apple menu, so a control
    Hands cannot locate is not offered at all."""
    del fw.nodes["save"].attrs["AXPosition"]
    refs = [c.ref for c in observe(backend).controls]
    assert "p42:0.0" not in refs
    assert "p42:0.1" in refs


def test_a_window_with_unreadable_geometry_is_still_reported(backend, fw):
    """A window is identified by pid and title, and the screenshot path falls
    back to the full screen for a zero rect — so it stays."""
    del fw.nodes["window"].attrs["AXSize"]
    window = backend.active_window()
    assert window is not None
    assert window.title == "Untitled" and window.rect == Rect(0, 0, 0, 0)


def test_a_screenshot_with_no_window_rect_captures_the_whole_screen(
        backend, fw, monkeypatch):
    del fw.nodes["window"].attrs["AXSize"]
    run = png_writer()
    monkeypatch.setattr(subprocess, "run", run)
    observe(backend, screenshot=True)
    assert "-R" not in run.seen[0]


def test_observing_a_named_app_uses_that_app(backend, fw):
    obs = observe(backend, app="com.apple.TextEdit")
    assert obs.window is not None and obs.window.pid == 42


# --- the secure-field guarantee -------------------------------------------


def test_a_secure_fields_value_is_never_even_read(backend, fw):
    secure = fw.nodes["secure"]
    control = {c.ref: c for c in observe(backend).controls}["p42:0.2"]
    assert control.role == "AXSecureTextField"
    assert control.value == ""
    reads = [args for args in fw.rec.named("AXUIElementCopyAttributeValue")
             if args[0] is secure and args[1] == "AXValue"]
    assert reads == [], "the backend read a password field's value"


# --- find ------------------------------------------------------------------


def test_find_matches_by_name_case_insensitively(backend):
    hits = backend.find("save", role=None, app=None, limit=10)
    assert [c.ref for c in hits] == ["p42:0.0"]
    assert hits[0].name == "Save"


def test_find_filters_by_role_and_honours_the_limit(backend):
    assert backend.find("a", role="AXButton", app=None, limit=10) != []
    assert all(c.role == "AXButton"
               for c in backend.find("a", role="AXButton", app=None, limit=10))
    assert len(backend.find("e", role=None, app=None, limit=1)) == 1


def test_find_matches_a_value_as_well_as_a_name(backend):
    assert [c.ref for c in backend.find("body text", role=None, app=None, limit=5)] \
        == ["p42:0.1"]


def test_find_on_an_app_that_is_not_running_is_empty_not_an_error(backend):
    """FakeBackend's semantics, which the session layer is written against: a
    search that matches nothing returns nothing. `observe` still raises,
    because being told to observe a named app is an instruction, not a
    query."""
    assert backend.find("save", role=None, app="Nope", limit=5) == []


# --- invoke / set_value / staleness ---------------------------------------


def test_invoke_performs_axpress_on_the_element_at_the_ref(backend, fw):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.0"]
    backend.invoke(control)
    assert fw.rec.named("AXUIElementPerformAction") == [(fw.nodes["save"], "AXPress")]


def test_invoking_something_that_cannot_be_pressed_is_an_invalid_action(backend):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.1"]
    with pytest.raises(HandsError) as excinfo:
        backend.invoke(control)
    assert excinfo.value.code == "invalid_action"


def test_set_value_sets_axvalue_on_the_element_at_the_ref(backend, fw):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.1"]
    backend.set_value(control, "hello")
    assert fw.rec.named("AXUIElementSetAttributeValue") == [
        (fw.nodes["text_area"], "AXValue", "hello")
    ]
    assert fw.nodes["text_area"].attrs["AXValue"] == "hello"


def test_setting_a_value_on_a_read_only_control_is_an_invalid_action(backend):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.0"]
    with pytest.raises(HandsError) as excinfo:
        backend.set_value(control, "hello")
    assert excinfo.value.code == "invalid_action"


def test_a_changed_role_at_the_path_is_a_stale_ref(backend, fw):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.0"]
    fw.nodes["save"].attrs["AXRole"] = "AXStaticText"
    with pytest.raises(HandsError) as excinfo:
        backend.invoke(control)
    assert excinfo.value.code == "stale_ref"


def test_a_vanished_element_at_the_path_is_a_stale_ref(backend, fw):
    control = {c.ref: c for c in observe(backend).controls}["p42:0.5.0"]
    fw.nodes["group"].children.clear()
    with pytest.raises(HandsError) as excinfo:
        backend.invoke(control)
    assert excinfo.value.code == "stale_ref"


def test_a_ref_from_a_different_process_is_stale_not_a_crash(backend):
    bogus = Control(ref="p999:0.0", role="AXButton", name="Save", value="",
                    rect=Rect(0, 0, 1, 1), app="TextEdit", patterns=("AXPress",))
    with pytest.raises(HandsError) as excinfo:
        backend.invoke(bogus)
    assert excinfo.value.code == "stale_ref"


# --- permissions -----------------------------------------------------------


def test_permissions_reflect_the_trust_and_capture_checks(backend, fw):
    assert backend.permissions() == {
        "accessibility": "ok", "screen": "ok", "input": "unknown",
    }
    fw.trusted = False
    fw.screen = False
    assert backend.permissions() == {
        "accessibility": "missing", "screen": "missing", "input": "unknown",
    }


def test_the_trust_check_never_prompts(backend, fw):
    backend.permissions()
    options = fw.rec.named("AXIsProcessTrustedWithOptions")[0][0]
    assert options == {"AXTrustedCheckOptionPrompt": False}


# --- synthetic input -------------------------------------------------------


def tags(fw) -> list[int | None]:
    return [event.tag for _, event in fw.posted]


def test_click_posts_a_tagged_down_and_up_at_the_point(backend, fw):
    backend.click((50, 25))
    kinds = [(e.info["type"], e.info["point"]) for _, e in fw.posted]
    assert kinds == [
        (FakeQuartzConstants.kCGEventLeftMouseDown, (50, 25)),
        (FakeQuartzConstants.kCGEventLeftMouseUp, (50, 25)),
    ]
    assert tags(fw) == [HANDS_TAG, HANDS_TAG]
    assert all(tap == FakeQuartzConstants.kCGHIDEventTap for tap, _ in fw.posted)
    click_states = [e.fields[FakeQuartzConstants.kCGMouseEventClickState]
                    for _, e in fw.posted]
    assert click_states == [1, 1]


def test_a_double_click_walks_the_click_state_up(backend, fw):
    backend.click((5, 6), double=True)
    assert [e.fields[FakeQuartzConstants.kCGMouseEventClickState]
            for _, e in fw.posted] == [1, 1, 2, 2]
    assert tags(fw) == [HANDS_TAG] * 4


def test_a_right_click_uses_the_right_button_events(backend, fw):
    backend.click((1, 2), button="right")
    assert [e.info["type"] for _, e in fw.posted] == [
        FakeQuartzConstants.kCGEventRightMouseDown,
        FakeQuartzConstants.kCGEventRightMouseUp,
    ]
    assert all(e.info["button"] == FakeQuartzConstants.kCGMouseButtonRight
               for _, e in fw.posted)


def test_an_unknown_mouse_button_is_an_invalid_action(backend):
    with pytest.raises(HandsError) as excinfo:
        backend.click((1, 2), button="middle")
    assert excinfo.value.code == "invalid_action"


def test_type_text_posts_a_tagged_down_and_up_carrying_the_unicode(backend, fw):
    backend.type_text("hé")
    assert [e.info["down"] for _, e in fw.posted] == [True, False]
    assert all(e.info["keycode"] == 0 for _, e in fw.posted)
    assert [e.unicode for _, e in fw.posted] == [(2, "hé"), (2, "hé")]
    assert tags(fw) == [HANDS_TAG, HANDS_TAG]


def test_type_text_chunks_by_utf16_code_units(backend, fw):
    backend.type_text("a" * 25)
    chunks = [e.unicode for _, e in fw.posted if e.info["down"]]
    assert [text for _, text in chunks] == ["a" * 20, "a" * 5]
    assert [length for length, _ in chunks] == [20, 5]


def test_an_astral_character_counts_as_two_utf16_units(backend, fw):
    backend.type_text("😀" * 11)
    chunks = [e.unicode for _, e in fw.posted if e.info["down"]]
    assert [length for length, _ in chunks] == [20, 2]
    assert [len(text) for _, text in chunks] == [10, 1]


def test_typing_nothing_posts_nothing(backend, fw):
    backend.type_text("")
    assert fw.posted == []


def test_key_posts_the_keycode_with_the_modifier_flag_set(backend, fw):
    backend.key("cmd+s")
    assert [e.info["keycode"] for _, e in fw.posted] == [1, 1]
    assert [e.info["down"] for _, e in fw.posted] == [True, False]
    assert [e.flags for _, e in fw.posted] == [0x100000, 0x100000]
    assert tags(fw) == [HANDS_TAG, HANDS_TAG]


def test_key_combines_several_modifiers_and_accepts_their_aliases(backend, fw):
    backend.key("command+shift+option+control+a")
    assert fw.posted[0][1].flags == 0x100000 | 0x20000 | 0x80000 | 0x40000
    assert fw.posted[0][1].info["keycode"] == 0


def test_key_accepts_a_bare_named_key(backend, fw):
    backend.key("escape")
    assert [e.info["keycode"] for _, e in fw.posted] == [53, 53]
    assert [e.flags for _, e in fw.posted] == [0, 0]


def test_an_unknown_key_or_modifier_is_an_invalid_action_not_a_key_error(backend):
    for chord in ("cmd+nosuchkey", "hyper+s", ""):
        with pytest.raises(HandsError) as excinfo:
            backend.key(chord)
        assert excinfo.value.code == "invalid_action"


def test_scroll_moves_the_cursor_then_posts_a_tagged_wheel_event(backend, fw):
    backend.scroll((300, 400), -3)
    move, wheel = [e for _, e in fw.posted]
    assert move.info["type"] == FakeQuartzConstants.kCGEventMouseMoved
    assert move.info["point"] == (300, 400)
    assert wheel.kind == "scroll"
    assert wheel.info == {
        "unit": FakeQuartzConstants.kCGScrollEventUnitLine, "count": 1, "dy": -3,
    }
    assert tags(fw) == [HANDS_TAG, HANDS_TAG]


def test_every_posted_event_of_every_kind_carries_the_tag(backend, fw):
    backend.click((1, 1), double=True)
    backend.type_text("hi")
    backend.key("cmd+c")
    backend.scroll((2, 2), 1)
    assert fw.posted, "nothing was posted"
    assert all(event.tag == HANDS_TAG for _, event in fw.posted)


# --- screenshots -----------------------------------------------------------


def png_writer(size=(40, 20)):
    """A `subprocess.run` stand-in that writes a real PNG where screencapture
    would have written one, so the Pillow downscale runs for real."""
    from PIL import Image

    seen: list[list[str]] = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        Image.new("RGB", size, (200, 30, 30)).save(argv[-1], format="PNG")
        return subprocess.CompletedProcess(argv, 0)

    run.seen = seen
    return run


def test_a_screenshot_shells_out_to_screencapture_and_downscales(backend, monkeypatch):
    run = png_writer((400, 200))
    monkeypatch.setattr(subprocess, "run", run)
    obs = observe(backend, screenshot=True, max_width=100)

    assert obs.screenshot_png is not None
    assert obs.screenshot_png[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    assert Image.open(io.BytesIO(obs.screenshot_png)).size == (100, 50)

    argv = run.seen[0]
    assert argv[0] == "screencapture" and "-x" in argv and "-o" in argv
    assert argv[argv.index("-R") + 1] == "0,0,700,500"     # the window rect


def test_a_screenshot_of_a_region_captures_that_region(backend, monkeypatch):
    run = png_writer()
    monkeypatch.setattr(subprocess, "run", run)
    observe(backend, region=Rect(10, 20, 30, 40), screenshot=True)
    argv = run.seen[0]
    assert argv[argv.index("-R") + 1] == "10,20,30,40"


def test_screencapture_runs_under_a_timeout(backend, monkeypatch):
    """Both shell-outs sit on the MCP server's request path; neither may block
    it forever."""
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        from PIL import Image

        Image.new("RGB", (10, 10)).save(argv[-1], format="PNG")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    observe(backend, screenshot=True)
    assert seen[0]["timeout"] == 10


def test_a_hung_screencapture_is_a_backend_error(backend, monkeypatch):
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(HandsError) as excinfo:
        observe(backend, screenshot=True)
    assert excinfo.value.code == "backend"


def test_a_broken_pillow_install_is_a_backend_error_not_an_import_error(
        backend, monkeypatch):
    # The PNG is built BEFORE Pillow is taken away, so the fake screencapture
    # still works and the only thing that can fail is the downscale.
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (40, 20)).save(buffer, format="PNG")
    raw = buffer.getvalue()

    def run(argv, **kwargs):
        with open(argv[-1], "wb") as handle:
            handle.write(raw)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(HandsError) as excinfo:
        observe(backend, screenshot=True)
    assert excinfo.value.code == "backend"


def test_no_screen_permission_refuses_before_shelling_out(backend, fw, monkeypatch):
    fw.screen = False
    run = png_writer()
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(HandsError) as excinfo:
        observe(backend, screenshot=True)
    assert excinfo.value.code == "permission"
    assert run.seen == [], "screencapture ran without Screen Recording permission"


# --- windows ---------------------------------------------------------------


def test_active_window_is_the_frontmost_apps_focused_window(backend):
    window = backend.active_window()
    assert window is not None
    assert (window.app, window.title, window.pid) == ("TextEdit", "Untitled", 42)


def test_windows_lists_regular_apps_that_have_a_window(backend):
    windows = backend.windows()
    assert [(w.app, w.pid) for w in windows] == [("TextEdit", 42)]


def test_no_frontmost_application_falls_back_to_the_system_wide_element(backend, fw):
    fw.frontmost = None
    fw.system_wide.attrs["AXFocusedApplication"] = fw.app
    window = backend.active_window()
    assert window is not None and window.pid == 42


def test_no_frontmost_application_and_no_fallback_means_no_window(backend, fw):
    fw.frontmost = None
    assert backend.active_window() is None


# --- apps ------------------------------------------------------------------


@pytest.mark.parametrize("app, expected", [
    ("TextEdit", ["open", "-a", "TextEdit"]),
    ("com.apple.TextEdit", ["open", "-b", "com.apple.TextEdit"]),
    ("/Applications/TextEdit.app", ["open", "/Applications/TextEdit.app"]),
    ("Visual Studio Code", ["open", "-a", "Visual Studio Code"]),
])
def test_open_app_picks_the_right_open_flag(backend, monkeypatch, app, expected):
    seen = []

    def run(argv, **kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.open_app(app) is True
    assert seen == [expected]


def test_open_app_reports_a_failure_rather_than_raising(backend, monkeypatch):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.open_app("Nope") is False


def test_open_app_runs_under_a_timeout_and_a_hang_is_just_false(backend, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", run)
    assert backend.open_app("TextEdit") is False
    assert seen[0]["timeout"] == 15


def test_focus_app_activates_the_running_application(backend, fw):
    assert backend.focus_app("textedit") is True
    assert fw.rec.named("activateWithOptions_") == [("TextEdit", 2)]


def test_focus_app_matches_a_bundle_id_too(backend, fw):
    assert backend.focus_app("com.apple.finder") is True
    assert fw.rec.named("activateWithOptions_") == [("Finder", 2)]


def test_focusing_an_app_that_is_not_running_is_false(backend, fw):
    assert backend.focus_app("Nope") is False
    assert fw.rec.named("activateWithOptions_") == []


# --- clipboard -------------------------------------------------------------


def test_clipboard_get_reads_the_string_type(backend, fw):
    assert backend.clipboard_get() == "clipboard start"
    assert fw.rec.named("stringForType_") == [("public.utf8-plain-text",)]


def test_clipboard_get_is_empty_rather_than_none_when_there_is_no_text(backend, fw):
    fw.pasteboard.contents = None
    assert backend.clipboard_get() == ""


def test_clipboard_set_clears_before_writing(backend, fw):
    backend.clipboard_set("written")
    assert [name for name, _ in fw.rec.calls if name in
            ("clearContents", "setString_forType_")] == \
        ["clearContents", "setString_forType_"]
    assert fw.pasteboard.contents == "written"


# --- the backend's own identity -------------------------------------------


def test_the_backend_names_itself_and_covers_the_protocol(backend):
    from firekeep_hands.backends.base import Backend

    assert backend.name == "mac"
    for method in [m for m in dir(Backend) if not m.startswith("_")]:
        assert callable(getattr(backend, method)), f"MacBackend is missing {method}"


def test_every_method_has_exactly_the_protocol_signature(backend):
    """Callable is not enough: a caller written against `Backend` passes these
    arguments by keyword, so a drifted default or a renamed parameter is a
    runtime failure the protocol cannot catch on its own."""
    import inspect

    from firekeep_hands.backends.base import Backend

    for method in [m for m in dir(Backend) if not m.startswith("_")]:
        expected = inspect.signature(getattr(Backend, method))
        actual = inspect.signature(getattr(type(backend), method))
        assert actual == expected, f"{method}{actual} does not match {method}{expected}"


def test_a_malformed_ref_is_a_stale_ref_not_a_value_error(backend):
    for ref in ("nonsense", "p:0", "pxx:0", "p42:0.x", "p42:-1"):
        control = Control(ref=ref, role="AXButton", name="", value="",
                          rect=Rect(0, 0, 1, 1), app="TextEdit", patterns=())
        with pytest.raises(HandsError) as excinfo:
            backend.invoke(control)
        assert excinfo.value.code == "stale_ref", ref


def test_the_visit_ceiling_stops_the_walk_and_reports_truncation(backend, monkeypatch):
    """A pathological (or cyclic) tree must bound the walk even when the
    control cap is never reached, because most nodes are not controls."""
    from firekeep_hands.backends import mac

    monkeypatch.setattr(mac, "_MAX_VISITED_NODES", 3)
    obs = observe(backend, max_nodes=1000)
    assert obs.truncated is True
    assert len(obs.controls) < 5


def test_open_app_turns_a_missing_open_binary_into_a_backend_error(backend, monkeypatch):
    def run(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(HandsError) as excinfo:
        backend.open_app("TextEdit")
    assert excinfo.value.code == "backend"
