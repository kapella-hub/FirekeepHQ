"""The notification the broker shows before a chord grants anything.

No subprocess is spawned anywhere in this file: `notification_argv` is pure,
which is the point of separating it from `announce`.
"""
import pytest

from firekeep_hands.broker import notify


def _argv(platform, title="Invoke \u201cSend\u201d in Mail", classes=("send",)):
    return notify.notification_argv(platform, title, classes, "ctrl+alt+y", "ctrl+alt+n")


def test_macos_uses_osascript_with_a_display_notification():
    argv = _argv("darwin")
    assert argv[0] == "osascript" and argv[1] == "-e" and len(argv) == 3
    script = argv[2]
    assert script.startswith("display notification ")
    assert 'with title "Firekeep Hands"' in script
    assert "ctrl+alt+y approves" in script and "ctrl+alt+n denies" in script
    assert "classes: send" in script


def test_windows_uses_a_notifyicon_balloon_through_powershell():
    argv = _argv("win32")
    assert argv[0] == "powershell"
    for flag in ("-NoProfile", "-NonInteractive", "-Command"):
        assert flag in argv
    script = argv[argv.index("-Command") + 1]
    assert "System.Windows.Forms" in script and "ShowBalloonTip" in script
    assert "ctrl+alt+y approves" in script and "classes: send" in script


def test_linux_has_no_notification_rather_than_a_dependency():
    assert _argv("linux") == []
    assert _argv("freebsd13") == []


def test_the_body_names_the_step_the_classes_and_both_chords():
    body, subtitle = notify.notification_body("Invoke Send in Mail", ("send", "boundary"),
                                              "ctrl+alt+y", "ctrl+alt+n")
    assert body == "Invoke Send in Mail — classes: send, boundary"
    assert subtitle == "ctrl+alt+y approves · ctrl+alt+n denies"


def test_a_permit_with_no_classes_still_reads_sensibly():
    body, _ = notify.notification_body("Something", (), "ctrl+alt+y", "ctrl+alt+n")
    assert body.endswith("classes: unclassified")


# -- the title is data, and hostile ------------------------------------------


def test_a_title_cannot_close_the_applescript_string_and_run_its_own():
    hostile = 'x" with title "pwned" -- and then do shell script "rm -rf ~'
    script = notify.notification_argv("darwin", hostile, ("send",),
                                      "ctrl+alt+y", "ctrl+alt+n")[2]
    # every quote from the title is escaped, so exactly the three string
    # literals this module wrote are open in the script
    assert script.count('"') - script.count('\\"') == 6
    assert 'with title "Firekeep Hands"' in script
    assert 'with title "pwned"' not in script
    assert script.endswith('"')


def test_a_title_cannot_close_the_powershell_string_and_run_its_own():
    hostile = "x'; Remove-Item C:\\ -Recurse; '"
    argv = notify.notification_argv("win32", hostile, ("send",), "ctrl+alt+y", "ctrl+alt+n")
    script = argv[argv.index("-Command") + 1]
    # Every quote the title contributed is doubled, which PowerShell reads as
    # one literal quote inside the string. Remove the doubled pairs and what
    # is left must be exactly the four quotes this module wrote itself:
    # 'Firekeep Hands' and the message.
    assert script.replace("''", "").count("'") == 4
    assert "''; Remove-Item" in script          # doubled, so it is text, not a statement


def test_a_title_cannot_inject_through_a_dollar_sign_or_backtick():
    """PowerShell does no expansion at all inside single quotes, so these are
    literal — the test pins that they are never moved into a double-quoted
    string by a later edit."""
    argv = notify.notification_argv("win32", "$(Get-Content secret.txt)`n", ("send",),
                                    "ctrl+alt+y", "ctrl+alt+n")
    script = argv[argv.index("-Command") + 1]
    quoted = script[script.index("ShowBalloonTip"):]
    assert "$(Get-Content secret.txt)" in quoted
    assert '"' not in quoted.split("[System.Windows.Forms.ToolTipIcon]")[0]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_newlines_are_removed_from_the_text_entirely(platform):
    """Collapsed rather than escaped: a class of problem removed instead of
    a set of characters handled."""
    argv = notify.notification_argv(platform, "line one\nline two\r\tand three", ("send",),
                                    "ctrl+alt+y", "ctrl+alt+n")
    blob = " ".join(argv)
    assert "\n" not in blob and "\r" not in blob and "\t" not in blob
    assert "line one line two and three" in blob


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_huge_title_is_truncated(platform):
    argv = notify.notification_argv(platform, "A" * 5000, ("send",), "ctrl+alt+y", "ctrl+alt+n")
    assert "A" * 121 not in " ".join(argv)


def test_the_chords_are_sanitised_too():
    """They come from a user-editable config file, so they are no more
    trusted than the title."""
    script = notify.notification_argv("darwin", "t", ("send",),
                                      'ctrl+alt+"y', "ctrl+alt+n")[2]
    assert '\\"y' in script


# -- announce ----------------------------------------------------------------


def test_announce_spawns_nothing_where_there_is_no_notifier(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "linux")
    spawned = []
    monkeypatch.setattr(notify.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert notify.announce("t", ("send",), "ctrl+alt+y", "ctrl+alt+n") is False
    assert spawned == []


def test_announce_never_raises_when_the_notifier_is_missing(monkeypatch):
    """A machine without osascript must still grant permits."""
    monkeypatch.setattr(notify.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(notify.subprocess, "Popen", boom)
    assert notify.announce("t", ("send",), "ctrl+alt+y", "ctrl+alt+n") is False


def test_announce_spawns_detached_and_never_waits(monkeypatch):
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    calls = []

    class FakePopen:
        def __init__(self, argv, **kw):
            calls.append((argv, kw))

    monkeypatch.setattr(notify.subprocess, "Popen", FakePopen)
    assert notify.announce("t", ("send",), "ctrl+alt+y", "ctrl+alt+n") is True
    argv, kw = calls[0]
    assert argv[0] == "osascript"
    assert kw["start_new_session"] is True
    assert kw["stdout"] == notify.subprocess.DEVNULL
    assert kw["stderr"] == notify.subprocess.DEVNULL
    assert kw["stdin"] == notify.subprocess.DEVNULL
