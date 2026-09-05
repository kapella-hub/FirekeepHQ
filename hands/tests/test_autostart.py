import pytest

from firekeep_hands.broker import autostart


def test_windows_command_is_a_logon_task_at_limited_rights():
    argv = autostart.command_for("win32", r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert argv[:3] == ["schtasks", "/Create", "/TN"] and "FirekeepHandsBroker" in argv
    assert "/SC" in argv and argv[argv.index("/SC") + 1] == "ONLOGON"
    assert "/RL" in argv and argv[argv.index("/RL") + 1] == "LIMITED"


def test_macos_plist_content():
    plist = autostart.launch_agent_plist("/v/bin/firekeep-hands-broker")
    assert "ai.firekeep.hands-broker" in plist and "<string>run</string>" in plist and "RunAtLoad" in plist


# --- additions -------------------------------------------------------------


def test_the_windows_task_runs_the_broker_with_run():
    argv = autostart.command_for("win32", r"C:\v\Scripts\firekeep-hands-broker.exe")
    tr = argv[argv.index("/TR") + 1]
    assert "firekeep-hands-broker.exe" in tr and tr.rstrip().endswith("run")
    assert "/F" in argv                      # replace an existing task rather than fail


def test_the_windows_task_quotes_a_path_with_spaces():
    argv = autostart.command_for("win32", r"C:\Program Files\v\Scripts\firekeep-hands-broker.exe")
    tr = argv[argv.index("/TR") + 1]
    assert tr.startswith('"') and '" run' in tr


def test_the_macos_command_bootstraps_the_agent_into_the_gui_domain():
    argv = autostart.command_for("darwin", "/v/bin/firekeep-hands-broker")
    assert argv[:2] == ["launchctl", "bootstrap"]
    assert argv[2].startswith("gui/") and argv[3].endswith("ai.firekeep.hands-broker.plist")


def test_an_unsupported_platform_has_no_autostart():
    with pytest.raises(ValueError):
        autostart.command_for("linux", "/usr/bin/firekeep-hands-broker")


def test_the_plist_is_well_formed_xml_naming_the_binary_and_run():
    import plistlib
    plist = autostart.launch_agent_plist("/v/bin/firekeep-hands-broker")
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["Label"] == "ai.firekeep.hands-broker"
    assert parsed["ProgramArguments"] == ["/v/bin/firekeep-hands-broker", "run"]
    assert parsed["RunAtLoad"] is True and parsed["KeepAlive"] is True


def test_the_plist_escapes_a_path_that_would_break_the_xml():
    import plistlib
    plist = autostart.launch_agent_plist("/v/bin/a&b<c>/firekeep-hands-broker")
    parsed = plistlib.loads(plist.encode("utf-8"))
    assert parsed["ProgramArguments"][0] == "/v/bin/a&b<c>/firekeep-hands-broker"


def test_install_and_uninstall_exist_and_are_not_approval_paths():
    """Autostart starts the broker; it must never be able to answer for the
    human. Guarded here because it is a security rule, not a style one."""
    import inspect
    source = inspect.getsource(autostart)
    for banned in ("decide(", "decide_oldest", "approve\"", "'approve'"):
        assert banned not in source, f"autostart must not touch permits ({banned})"
    assert callable(autostart.install) and callable(autostart.uninstall)
