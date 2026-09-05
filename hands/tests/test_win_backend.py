"""Unit tests for `WinBackend` against an injected fake `uiautomation`.

These run on every platform on purpose. The interesting logic in the Windows
backend — which nodes survive tree compaction, how a ref is built and parsed,
when the elevation guard fires — is pure, and testing it only on a Windows
runner would leave CI unable to catch a regression in it. The real UI
Automation client is exercised by `tests/live/test_win_notepad.py`.
"""
import sys
import types

import pytest

from firekeep_hands.backends.base import HandsError, Rect, WindowInfo

HWND = 0x30530


# --------------------------------------------------------------------------
# A fake `uiautomation`: just the surface WinBackend actually touches.
# --------------------------------------------------------------------------
class FakeRect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class FakePattern:
    def __init__(self, value=""):
        self.Value = value
        self.invoked = 0
        self.set_values = []

    def Invoke(self):
        self.invoked += 1

    def SetValue(self, value):
        self.set_values.append(value)
        self.Value = value


class FakeControl:
    def __init__(self, type_name, name, rect, runtime_id, *, hwnd=0, pid=4242,
                 children=(), invoke=None, value=None, enabled=True,
                 is_password=False):
        self.ControlTypeName = type_name
        self.Name = name
        self.BoundingRectangle = FakeRect(*rect)
        self.NativeWindowHandle = hwnd
        self.ProcessId = pid
        self.IsEnabled = enabled
        self.IsPassword = is_password
        self._runtime_id = tuple(runtime_id)
        self._children = list(children)
        self._invoke = invoke
        self._value = value

    def GetChildren(self):
        return list(self._children)

    def GetRuntimeId(self):
        return self._runtime_id

    def GetInvokePattern(self):
        return self._invoke

    def GetValuePattern(self):
        return self._value


def _scene(*, extra_children=()):
    save = FakeControl("ButtonControl", "Save", (10, 10, 90, 40), (42, 1),
                       invoke=FakePattern())
    editor = FakeControl("DocumentControl", "Text Editor", (0, 50, 600, 450), (42, 2),
                         value=FakePattern("hello"))
    offscreen = FakeControl("ButtonControl", "Ghost", (-900, -900, -800, -800), (42, 3))
    decoration = FakeControl("SeparatorControl", "", (0, 45, 600, 46), (42, 4))
    pane = FakeControl("PaneControl", "body", (0, 0, 800, 600), (42, 5),
                       children=[save, editor, offscreen, decoration, *extra_children])
    window = FakeControl("WindowControl", "Untitled - Notepad", (0, 0, 800, 600), (42, 0),
                         hwnd=HWND, children=[pane])
    return window, save, editor


def _fake_module(window):
    module = types.ModuleType("uiautomation")
    module.GetForegroundControl = lambda: window
    module.GetRootControl = lambda: FakeControl(
        "PaneControl", "Desktop", (0, 0, 1920, 1080), (42, 99), children=[window])
    module.ControlFromHandle = lambda handle: window if handle == HWND else None
    return module


@pytest.fixture
def backend(monkeypatch):
    """Build a WinBackend over the fake scene. `uiautomation` is swapped in
    sys.modules *before* construction because the backend imports it there,
    and `_process_info` is stubbed because the fake window's pid names no
    real process."""
    window, save, editor = _scene()
    fake_uia = _fake_module(window)
    monkeypatch.setitem(sys.modules, "uiautomation", fake_uia)
    from firekeep_hands.backends import win as win_module

    def fake_import(name):
        # `mss` is a Windows-only dependency of the wheel, so it is stubbed
        # rather than imported: permissions() must report the same thing on
        # the Linux runner as it does here.
        return {"uiautomation": fake_uia, "mss": types.ModuleType("mss")}[name]

    monkeypatch.setattr(win_module, "_import_optional", fake_import)
    monkeypatch.setattr(win_module, "_process_info", lambda pid: ("notepad", False))
    monkeypatch.setattr(win_module, "_set_dpi_aware", lambda: None)
    be = win_module.WinBackend()
    return types.SimpleNamespace(be=be, window=window, save=save, editor=editor,
                                 module=win_module)


def _observe(be, **over):
    kwargs = dict(app=None, region=None, max_nodes=200, text_budget=4000,
                  screenshot=False, max_width=1280)
    kwargs.update(over)
    return be.observe(**kwargs)


# --------------------------------------------------------------------------


def test_observe_reports_the_window_and_the_interesting_controls(backend):
    obs = _observe(backend.be)
    assert obs.generation == 1
    assert obs.window.title == "Untitled - Notepad"
    assert obs.window.app == "notepad"
    assert obs.window.rect == Rect(0, 0, 800, 600)
    assert obs.window.elevated is False
    # Save and the editor survive; the offscreen button, the separator (not an
    # interactive role) and the containing pane do not.
    assert [c.name for c in obs.controls] == ["Save", "Text Editor"]
    assert obs.truncated is False


def test_observe_builds_refs_rects_patterns_and_values(backend):
    save, editor = _observe(backend.be).controls
    assert save.ref == f"w{HWND:x}:42.1"
    assert save.role == "Button"
    assert save.rect == Rect(10, 10, 80, 30)
    assert save.patterns == ("Invoke",)
    assert save.value == ""
    assert save.app == "notepad"
    assert save.enabled is True
    assert editor.ref == f"w{HWND:x}:42.2"
    assert editor.role == "Document"
    assert editor.patterns == ("Value",)
    assert editor.value == "hello"


def test_observe_text_is_the_title_then_the_names_and_values(backend):
    obs = _observe(backend.be)
    assert obs.text == "Untitled - Notepad\nSave Text Editor hello"
    assert _observe(backend.be, text_budget=4).text == "Untitled - Notepad\nSave"


def test_max_nodes_truncates(backend):
    obs = _observe(backend.be, max_nodes=1)
    assert [c.name for c in obs.controls] == ["Save"]
    assert obs.truncated is True


def test_a_region_clips_the_controls(backend):
    obs = _observe(backend.be, region=Rect(0, 0, 100, 45))
    assert [c.name for c in obs.controls] == ["Save"]


def test_a_window_with_no_readable_rect_is_not_clipped_to_nothing(backend):
    """An empty window rectangle means "could not read it", not "the window
    has no area" — clipping every control away would turn that into a
    silently empty observation."""
    backend.window.BoundingRectangle = FakeRect(0, 0, 0, 0)
    obs = _observe(backend.be)
    assert obs.window.rect == Rect(0, 0, 0, 0)
    assert [c.name for c in obs.controls] == ["Save", "Text Editor", "Ghost"]


def test_find_matches_by_name_and_filters_by_role(backend):
    hits = backend.be.find("save", role=None, app=None, limit=5)
    assert [c.ref for c in hits] == [f"w{HWND:x}:42.1"]
    assert backend.be.find("e", role="Document", app=None, limit=5)[0].name == "Text Editor"
    assert backend.be.find("save", role="Document", app=None, limit=5) == []
    assert backend.be.find("nothing here", role=None, app=None, limit=5) == []


def test_find_honours_the_limit(backend):
    assert len(backend.be.find("e", role=None, app=None, limit=1)) == 1


def test_a_password_field_is_reported_as_a_passwordbox_with_no_value(monkeypatch):
    """UI Automation has no password control type — a secure field is an
    `Edit` carrying `IsPassword`. `policy.py` gates its `credential` class on
    `role == "PasswordBox"` (and the macOS `AXSecureTextField`), so the
    backend owns the translation into that shared vocabulary. Its contents
    are never read into the observation."""
    secret = FakeControl("EditControl", "Password", (10, 100, 300, 130), (42, 7),
                         value=FakePattern("hunter2"), is_password=True)
    plain = FakeControl("EditControl", "Username", (10, 140, 300, 170), (42, 8),
                        value=FakePattern("mogan"))
    window, _, _ = _scene(extra_children=(secret, plain))
    fake_uia = _fake_module(window)
    monkeypatch.setitem(sys.modules, "uiautomation", fake_uia)
    from firekeep_hands.backends import win as win_module

    monkeypatch.setattr(win_module, "_import_optional",
                        lambda name: {"uiautomation": fake_uia,
                                      "mss": types.ModuleType("mss")}[name])
    monkeypatch.setattr(win_module, "_process_info", lambda pid: ("notepad", False))
    monkeypatch.setattr(win_module, "_set_dpi_aware", lambda: None)
    obs = _observe(win_module.WinBackend())

    by_name = {c.name: c for c in obs.controls}
    assert by_name["Password"].role == "PasswordBox"
    assert by_name["Password"].value == ""
    # Still fillable: the pattern is reported, only the contents are withheld.
    assert "Value" in by_name["Password"].patterns
    # An ordinary Edit beside it is untouched.
    assert by_name["Username"].role == "Edit"
    assert by_name["Username"].value == "mogan"
    # And the secret is not in the text blob either.
    assert "hunter2" not in obs.text


def test_find_matches_the_app_regardless_of_case(backend):
    """Windows reports an image name in its own case ("Notepad"); a caller
    types whatever they like. An exact comparison here filters out every
    control in the window the caller just asked for."""
    for spelling in ("notepad", "Notepad", "NOTEPAD"):
        hits = backend.be.find("save", role=None, app=spelling, limit=5)
        assert [c.name for c in hits] == ["Save"], spelling
    assert backend.be.find("save", role=None, app="chrome", limit=5) == []


def test_invoke_uses_the_invoke_pattern(backend):
    save = _observe(backend.be).controls[0]
    backend.be.invoke(save)
    assert backend.save._invoke.invoked == 1


def test_set_value_uses_the_value_pattern(backend):
    editor = _observe(backend.be).controls[1]
    backend.be.set_value(editor, "hands live")
    assert backend.editor._value.set_values == ["hands live"]


def test_invoke_without_an_invoke_pattern_is_unsupported(backend):
    editor = _observe(backend.be).controls[1]
    with pytest.raises(HandsError) as exc:
        backend.be.invoke(editor)
    assert exc.value.code == "unsupported"


def test_a_ref_whose_element_is_gone_raises_stale_ref(backend):
    save = _observe(backend.be).controls[0]
    gone = type(save)(ref=f"w{HWND:x}:42.999", role=save.role, name=save.name,
                      value=save.value, rect=save.rect, app=save.app,
                      patterns=save.patterns)
    with pytest.raises(HandsError) as exc:
        backend.be.invoke(gone)
    assert exc.value.code == "stale_ref"


def test_a_ref_for_a_window_that_closed_raises_stale_ref(backend):
    save = _observe(backend.be).controls[0]
    gone = type(save)(ref="wdeadbe:42.1", role=save.role, name=save.name,
                      value=save.value, rect=save.rect, app=save.app,
                      patterns=save.patterns)
    with pytest.raises(HandsError) as exc:
        backend.be.invoke(gone)
    assert exc.value.code == "stale_ref"


def test_a_malformed_ref_is_an_invalid_action(backend):
    save = _observe(backend.be).controls[0]
    bad = type(save)(ref="not-a-ref", role=save.role, name=save.name,
                     value=save.value, rect=save.rect, app=save.app,
                     patterns=save.patterns)
    with pytest.raises(HandsError) as exc:
        backend.be.invoke(bad)
    assert exc.value.code == "invalid_action"


def test_every_window_targeting_action_refuses_an_elevated_window(backend, monkeypatch):
    save = _observe(backend.be).controls[0]
    monkeypatch.setattr(backend.module, "_process_info", lambda pid: ("consent", True))
    assert backend.be.active_window().elevated is True

    def raises(call):
        with pytest.raises(HandsError) as exc:
            call()
        assert exc.value.code == "elevated_target"

    raises(lambda: backend.be.invoke(save))
    raises(lambda: backend.be.set_value(save, "x"))
    raises(lambda: backend.be.click((5, 5)))
    raises(lambda: backend.be.type_text("x"))
    raises(lambda: backend.be.key("ctrl+s"))
    raises(lambda: backend.be.scroll((5, 5), -1))


def test_open_app_refuses_a_name_cmd_would_read_as_syntax(backend):
    """`open_app` hands its argument to `cmd /c start`, and the argument
    comes from a model. A name carrying shell syntax is refused rather than
    parsed."""
    for bad in ("", "notepad & calc", 'say"hi"', "x|y", "a>b"):
        with pytest.raises(HandsError) as exc:
            backend.be.open_app(bad)
        assert exc.value.code == "invalid_action"


def _window_info(app, elevated):
    return WindowInfo(app, f"{app} window", 1, Rect(0, 0, 800, 600), elevated)


def test_type_text_rechecks_the_elevation_guard_between_chunks(backend, monkeypatch):
    """Every other action is instantaneous, so one guard up front says it
    all. Typing is paced, so a long string is a stretch of time in which the
    foreground can become something Hands must not type into."""
    from firekeep_hands.backends import _win_input as wi

    sent = []
    monkeypatch.setattr(wi, "send_text", lambda text: sent.append(text))
    # Safe for the first chunk, elevated from then on.
    monkeypatch.setattr(backend.be, "active_window",
                        lambda: _window_info("notepad", False) if not sent
                        else _window_info("consent", True))

    with pytest.raises(HandsError) as exc:
        backend.be.type_text("x" * 250)
    assert exc.value.code == "elevated_target"
    assert sent == ["x" * 100], "typing kept going after the foreground turned elevated"


def test_type_text_sends_a_short_string_in_one_chunk(backend, monkeypatch):
    from firekeep_hands.backends import _win_input as wi

    sent = []
    monkeypatch.setattr(wi, "send_text", lambda text: sent.append(text))
    backend.be.type_text("hands live")
    assert sent == ["hands live"]


def test_windows_lists_the_top_level_windows(backend, monkeypatch):
    monkeypatch.setattr(backend.module, "_window_is_visible", lambda hwnd: True)
    assert [w.title for w in backend.be.windows()] == ["Untitled - Notepad"]


def test_permissions_are_ok_when_both_modules_imported(backend):
    assert backend.be.permissions() == {"accessibility": "ok", "screen": "ok", "input": "ok"}


def test_a_missing_uiautomation_is_reported_not_raised(monkeypatch):
    from firekeep_hands.backends import win as win_module

    monkeypatch.setattr(win_module, "_set_dpi_aware", lambda: None)
    monkeypatch.setattr(win_module, "_import_optional",
                        lambda name: (_ for _ in ()).throw(ImportError("no " + name)))
    be = win_module.WinBackend()
    assert be.permissions()["accessibility"] == "missing"
    with pytest.raises(HandsError) as exc:
        be.active_window()
    assert exc.value.code == "permission"


def test_construction_sets_dpi_awareness(monkeypatch):
    """Absolute pointer coordinates come from GetSystemMetrics, which answers
    in the caller's DPI context: read from a process that is not per-monitor
    aware, a scaled display measures smaller than it is and every click lands
    short. Construction is the only place that ordering is guaranteed."""
    calls = []
    fake_uia = _fake_module(_scene()[0])
    monkeypatch.setitem(sys.modules, "uiautomation", fake_uia)
    from firekeep_hands.backends import win as win_module

    monkeypatch.setattr(win_module, "_import_optional", lambda name: fake_uia)
    monkeypatch.setattr(win_module, "_set_dpi_aware", lambda: calls.append("set"))
    win_module.WinBackend()
    assert calls == ["set"]


def test_no_foreground_window_observes_to_an_empty_scene(backend, monkeypatch):
    monkeypatch.setattr(sys.modules["uiautomation"], "GetForegroundControl", lambda: None)
    obs = _observe(backend.be)
    assert obs.window is None and obs.controls == [] and obs.text == ""
    assert backend.be.active_window() is None
