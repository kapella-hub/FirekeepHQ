import datetime as dt
import pytest
from firekeep_hands import policy
from firekeep_hands.config import Policy, Remembered
from firekeep_hands.backends.base import Control, Rect, WindowInfo

W = WindowInfo("Mail", "Inbox — Mail", 1, Rect(0, 0, 800, 600))
def C(name, role="Button", value="", app="Mail", patterns=("Invoke",)):
    return Control("r", role, name, value, Rect(0, 0, 10, 10), app, patterns)

@pytest.mark.parametrize("action,control,url,expected", [
    ({"kind": "invoke", "ref": "r"}, C("Send"), None, ("send",)),
    ({"kind": "click", "ref": "r"}, C("Place order"), None, ("money",)),
    ({"kind": "invoke", "ref": "r"}, C("Delete permanently"), None, ("destroy",)),
    ({"kind": "set_value", "ref": "r", "value": "x"}, C("Password", role="PasswordBox", patterns=("Value",)), None, ("credential",)),
    ({"kind": "type", "text": "hunter2"}, C("Password", role="Edit"), None, ("credential",)),
    ({"kind": "invoke", "ref": "r"}, C("Install"), None, ("install",)),
    ({"kind": "open_url", "url": "https://evil.example/x"}, None, "https://evil.example/x", ("boundary",)),
    ({"kind": "invoke", "ref": "r"}, C("Save"), None, ()),
    ({"kind": "key", "chord": "ctrl+enter"}, None, None, ("send",)),
    ({"kind": "wait", "seconds": 1}, None, None, ()),
])
def test_classify_table(action, control, url, expected):
    assert policy.classify(action, control, W, url, Policy([], [], []), ["Mail"]) == expected

def test_allowlisted_domain_is_not_a_boundary():
    pol = Policy([], ["example.com"], [])
    assert policy.classify({"kind": "open_url", "url": "https://docs.example.com/a"}, None, W, "https://docs.example.com/a", pol, []) == ()

def test_app_outside_task_apps_is_a_boundary_unless_allowlisted():
    assert policy.classify({"kind": "open_app", "app": "Terminal"}, None, W, None, Policy([], [], []), ["Mail"]) == ("boundary",)
    assert policy.classify({"kind": "open_app", "app": "Terminal"}, None, W, None, Policy(["Terminal"], [], []), ["Mail"]) == ()

def test_remembered_approval_downgrades_to_allow_until_expiry():
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    pol = Policy([], [], [Remembered("send", "Mail", "send", "2026-10-01T00:00:00Z")])
    d = policy.decide({"kind": "invoke", "ref": "r"}, C("Send"), W, None, pol, ["Mail"], now=now)
    assert d.verdict == "allow"
    late = dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
    assert policy.decide({"kind": "invoke", "ref": "r"}, C("Send"), W, None, pol, ["Mail"], now=late).verdict == "permit"

def test_remember_writes_a_30_day_entry():
    pol = Policy([], [], [])
    now = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
    policy.remember(pol, "money", "Amazon", "place order", now=now)
    assert pol.remembered[0].until == "2026-10-05T00:00:00Z"

@pytest.mark.parametrize("until", ["not-a-date", ""])
def test_malformed_remembered_until_does_not_raise_and_stays_permit(until):
    pol = Policy([], [], [Remembered("send", "Mail", "send", until)])
    d = policy.decide({"kind": "invoke", "ref": "r"}, C("Send"), W, None, pol, ["Mail"])
    assert d.verdict == "permit"

def _window(app):
    return WindowInfo(app, f"{app} window", 1, Rect(0, 0, 800, 600))

@pytest.mark.parametrize("chord,app,expected", [
    ("delete", "Explorer", ("destroy",)),
    ("delete", "Finder", ("destroy",)),
    ("delete", "Notepad", ()),
    ("shift+delete", "Explorer", ("destroy",)),
    ("shift+delete", "Notepad", ()),
    ("cmd+backspace", "Finder", ("destroy",)),
    ("cmd+backspace", "Notepad", ()),
    ("cmd+delete", "Finder", ("destroy",)),
    ("cmd+delete", "Notepad", ()),
])
def test_destroy_chord_carve_out_is_scoped_to_explorer_and_finder(chord, app, expected):
    win = _window(app)
    assert policy.classify({"kind": "key", "chord": chord}, None, win, None, Policy([], [], []), [app]) == expected

@pytest.mark.parametrize("chord", ["cmd+enter", "cmd+shift+d"])
def test_send_chords_beyond_ctrl_enter(chord):
    assert policy.classify({"kind": "key", "chord": chord}, None, W, None, Policy([], [], []), ["Mail"]) == ("send",)

def test_clipboard_secret_token_shape():
    assert policy.classify({"kind": "clipboard_set", "text": "a" * 32}, None, W, None, Policy([], [], []), ["Mail"]) == ("credential",)
    assert policy.classify({"kind": "clipboard_set", "text": "hello world"}, None, W, None, Policy([], [], []), ["Mail"]) == ()

def test_install_via_open_app_path_outside_policy_apps():
    app = "C:\\installers\\setup.msi"
    assert policy.classify({"kind": "open_app", "app": app}, None, W, None, Policy([], [], []), [app]) == ("install",)


# -- boundary is the catch-all, not a rule about two verbs ------------------
#
# Before this, `boundary` fired only on the app *crossing* — `open_app`,
# `focus_app`, and a navigation. A task started with `apps=[]` could therefore
# click, type and invoke inside whatever window happened to be in front with no
# permit at all, which is the opposite of what the guide, the tool description
# and the threat model all say.

_NOTHING = Policy([], [], [])


@pytest.mark.parametrize("action", [
    {"kind": "click", "ref": "r"},
    {"kind": "invoke", "ref": "r"},
    {"kind": "set_value", "ref": "r", "value": "x"},
    {"kind": "type", "text": "x"},
    {"kind": "key", "chord": "ctrl+p"},
    {"kind": "scroll", "ref": "r", "dy": -3},
])
def test_every_window_scoped_action_in_an_undeclared_app_is_a_boundary(action):
    assert policy.classify(action, C("Save"), W, None, _NOTHING, []) == ("boundary",)


@pytest.mark.parametrize("action", [
    {"kind": "click", "ref": "r"},
    {"kind": "type", "text": "x"},
    {"kind": "key", "chord": "ctrl+p"},
])
def test_a_declared_or_allowlisted_app_is_never_a_boundary(action):
    assert policy.classify(action, C("Save"), W, None, _NOTHING, ["Mail"]) == ()
    assert policy.classify(action, C("Save"), W, None, Policy(["Mail"], [], []), []) == ()


def test_the_app_match_is_case_insensitive():
    """A human writes `apps=["notepad"]`; Windows reports "Notepad". A permit
    prompt on every click of a task the human explicitly scoped is the fastest
    way to teach somebody to stop reading the prompts."""
    assert policy.classify({"kind": "click", "ref": "r"}, C("Save"), W, None,
                           _NOTHING, ["mail"]) == ()
    assert policy.classify({"kind": "open_app", "app": "TERMINAL"}, None, W, None,
                           Policy(["terminal"], [], []), []) == ()


def test_a_control_in_another_app_than_the_window_is_still_a_boundary():
    """`hands_find(app=…)` hands back controls that are not in the foreground
    window, so the control's own app has to be consulted too."""
    other = C("Save", app="Excel")
    assert policy.classify({"kind": "click", "ref": "r"}, other, W, None,
                           _NOTHING, ["Mail"]) == ("boundary",)


def test_kinds_that_target_no_window_are_not_boundary_steps():
    """A `wait` reaches nothing at all, and a clipboard write is machine-wide
    rather than scoped to whichever window happens to be in front. Prompting
    for either would be noise, not protection."""
    assert policy.classify({"kind": "wait", "seconds": 1}, None, W, None, _NOTHING, []) == ()
    assert policy.classify({"kind": "clipboard_set", "text": "hello"}, None, W, None,
                           _NOTHING, []) == ()


@pytest.mark.parametrize("app", ["", "   ", "\t"])
def test_a_blank_app_the_model_chose_is_a_crossing_not_an_exemption(app):
    """The empty-name exemption is for a window Hands could not READ. This
    name is one the model WROTE, and "" names nothing that could have been
    declared — so it must not inherit the exemption. `routing` refuses it
    first; this is the second layer, and both have to be wrong for a blank to
    reach the desktop."""
    for kind in ("focus_app", "open_app"):
        assert policy.classify({"kind": kind, "app": app}, None, W, None,
                               _NOTHING, ["Mail"]) == ("boundary",)
        assert policy.boundary_apps({"kind": kind, "app": app}, None, W, None,
                                    _NOTHING, ["Mail"]) == [app]


def test_a_blank_app_is_a_crossing_even_when_something_is_allowlisted():
    """The blank check runs before the declared-set lookup, so a policy that
    happens to allowlist an app cannot launder it."""
    assert policy.classify({"kind": "open_app", "app": ""}, None, W, None,
                           Policy(["Terminal"], [], []), ["Mail", ""]) == ("boundary",)


def test_an_unnamed_window_is_not_treated_as_a_crossing():
    """A backend that could not name the foreground window returns "". Reading
    that as "an app you did not declare" would put a permit in front of every
    step with nothing the human could add to `apps` to clear it."""
    blank = WindowInfo("", "", 1, Rect(0, 0, 0, 0))
    assert policy.classify({"kind": "click", "ref": "r"},
                           Control("r", "Button", "Save", "", Rect(0, 0, 1, 1), "", ("Invoke",)),
                           blank, None, _NOTHING, []) == ()


def test_boundary_apps_names_what_was_crossed_for_every_shape():
    """The one function both halves use: it raises the class, and the session
    widens the task with exactly what it returned."""
    assert policy.boundary_apps({"kind": "click", "ref": "r"}, C("Save"), W, None,
                                _NOTHING, []) == ["Mail"]
    assert policy.boundary_apps({"kind": "focus_app", "app": "Excel"}, None, W, None,
                                _NOTHING, ["Mail"]) == ["Excel"]
    assert policy.boundary_apps({"kind": "open_url", "url": "https://evil.example/x"},
                                None, W, "https://evil.example/x",
                                _NOTHING, ["Mail"]) == ["evil.example"]
    assert policy.boundary_apps({"kind": "wait", "seconds": 1}, None, W, None,
                                _NOTHING, []) == []


def test_a_task_scoped_host_matches_exactly_where_the_allowlist_matches_a_parent():
    """`firekeep hands allow domain example.com` covers its subdomains, on
    purpose. A host a human approved once inside one task does not: they
    approved `pay.example.com`, not everything under `example.com`."""
    action = {"kind": "open_url", "url": "https://pay.example.com/x"}
    assert policy.classify(action, None, W, action["url"], _NOTHING, ["Mail"],
                           task_hosts=["pay.example.com"]) == ()
    assert policy.classify(action, None, W, action["url"], _NOTHING, ["Mail"],
                           task_hosts=["example.com"]) == ("boundary",)
    assert policy.classify(action, None, W, action["url"],
                           Policy([], ["example.com"], []), ["Mail"]) == ()


def test_an_app_declaration_never_clears_a_navigation_to_a_host_of_that_name():
    """Apps and hosts were one list, and a name is a name once both live in
    the same bag: `apps=["intranet"]` cleared `http://intranet/secret`. An app
    declaration says which programs are in scope, not where the browser may
    go."""
    action = {"kind": "open_url", "url": "http://intranet/secret"}
    assert policy.classify(action, None, W, action["url"], _NOTHING,
                           ["intranet"]) == ("boundary",)
    assert policy.classify(action, None, W, action["url"], _NOTHING, [],
                           task_hosts=["intranet"]) == ()
    assert policy.classify(action, None, W, action["url"],
                           Policy(["intranet"], [], []), []) == ("boundary",)


def test_the_browser_token_clears_a_browser_step_and_never_a_native_window():
    """`browser` is a reserved declaration token, and also a real Windows
    image name — Yandex ships `browser.exe`. A native step in a window whose
    process is called "browser" must not be cleared by a declaration that was
    about the web."""
    native = WindowInfo("browser", "Some window", 1, Rect(0, 0, 800, 600))
    control = Control("r", "Button", "Save", "", Rect(0, 0, 10, 10), "browser", ("Invoke",))
    click = {"kind": "click", "ref": "r"}

    assert policy.classify(click, control, native, None, _NOTHING,
                           ["browser"]) == ("boundary",)
    assert policy.classify(click, control, native, None, Policy(["browser"], [], []),
                           []) == ("boundary",)
    # The same declaration clears the step it was meant for.
    assert policy.classify(click, control, native, None, _NOTHING, ["browser"],
                           browser_step=True) == ()
    # And a native window really called "browser" is still declarable by name
    # once the task means the program rather than the web.
    assert policy.classify(click, control, native, None, _NOTHING,
                           ["browser", "Browser.exe"]) == ("boundary",)
