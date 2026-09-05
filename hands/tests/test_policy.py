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
