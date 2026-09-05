import sys
import types

import pytest

from firekeep_hands.broker import autostart


def test_windows_autostart_is_a_per_user_run_value():
    """Not a Scheduled Task: measured on Windows 11 on 2026-09-05,
    `schtasks /Create /SC ONLOGON /RL LIMITED` is "Access is denied." for an
    unelevated user, with or without /RU, so every install we expect failed.
    A HKCU Run value needs no elevation."""
    value = autostart.run_value_for(r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert value == r'"C:\v\Scripts\pythonw.exe" -m firekeep_hands.broker run'
    assert autostart.RUN_KEY == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert autostart.RUN_VALUE_NAME == "FirekeepHandsBroker"


def test_macos_plist_content():
    plist = autostart.launch_agent_plist("/v/bin/firekeep-hands-broker")
    assert "ai.firekeep.hands-broker" in plist and "<string>run</string>" in plist and "RunAtLoad" in plist


# --- additions -------------------------------------------------------------


def test_the_run_value_launches_pythonw_so_logon_flashes_no_console():
    value = autostart.run_value_for(r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert "pythonw.exe" in value and "python.exe\"" not in value
    assert value.endswith("-m firekeep_hands.broker run")


def test_the_run_value_quotes_a_path_with_spaces():
    """Windows splits an unquoted Run value at the first space, and the kit's
    venv can sit under a path with one."""
    value = autostart.run_value_for(r"C:\Program Files\v\Scripts\firekeep-hands-broker.exe")
    assert value.startswith('"C:\\Program Files\\v\\Scripts\\pythonw.exe"')
    assert '" -m firekeep_hands.broker run' in value


def test_the_launch_argv_matches_the_run_value():
    """`install()` starts the broker now with the same command the next logon
    will use; they must not drift."""
    argv = autostart.broker_launch_argv(r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert argv == [r"C:\v\Scripts\pythonw.exe", "-m", "firekeep_hands.broker", "run"]
    value = autostart.run_value_for(r"C:\v\Scripts\firekeep-hands-broker.exe")
    assert value == '"' + argv[0] + '" ' + " ".join(argv[1:])


def test_the_launch_argv_falls_back_to_the_console_interpreter_when_pythonw_is_missing(tmp_path):
    """An embeddable or stripped interpreter ships no pythonw.exe; a Run value
    naming a missing exe launches nothing and shows nothing. With python.exe
    present and pythonw.exe absent the fallback is the console interpreter;
    with both present pythonw wins; with neither on disk the intended spelling
    is kept (that is the pure-function case above)."""
    pytest.importorskip("winreg")   # the fallback is a Windows-only concern; POSIX tmp paths would not round-trip through PureWindowsPath
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    script = str(scripts / "firekeep-hands-broker.exe")
    (scripts / "python.exe").write_bytes(b"")
    assert autostart.broker_launch_argv(script)[0] == str(scripts / "python.exe")
    (scripts / "pythonw.exe").write_bytes(b"")
    assert autostart.broker_launch_argv(script)[0] == str(scripts / "pythonw.exe")


def test_command_for_win32_now_refuses_because_there_is_no_command():
    with pytest.raises(ValueError, match="registry value"):
        autostart.command_for("win32", r"C:\v\Scripts\firekeep-hands-broker.exe")


def test_schtasks_is_never_invoked_only_explained():
    """It was removed, not kept as a fallback: a fallback that always fails
    is a second error message, not a second chance. The module docstring
    still names it, because why it is gone is worth more than the code was."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(autostart))
    first = tree.body[0] if tree.body else None
    module_docstring = (
        first.value
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
        else None
    )
    invocations = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "schtasks" in node.value.lower()
        and node is not module_docstring
    ]
    assert invocations == []
    assert "schtasks" in (autostart.__doc__ or "").lower()


# --- install/uninstall against a fake registry ------------------------------


class FakeKey:
    def __init__(self, store, access):
        self.store = store
        self.access = access
    def __enter__(self): return self
    def __exit__(self, *exc): return False


def _fake_winreg(monkeypatch, initial=None):
    """A stand-in `winreg`, injected into sys.modules so the lazy import in
    `autostart._winreg` picks it up. Lets the registry paths be tested on any
    host, and on Windows without touching the real registry."""
    values = dict(initial or {})
    opened = []

    module = types.ModuleType("winreg")
    module.HKEY_CURRENT_USER = "HKCU"
    module.KEY_SET_VALUE = 0x0002
    module.KEY_READ = 0x20019
    module.REG_SZ = 1

    def OpenKey(root, path, reserved, access):
        opened.append((root, path, access))
        return FakeKey(values, access)

    def SetValueEx(key, name, reserved, kind, value):
        assert key.access == module.KEY_SET_VALUE
        assert kind == module.REG_SZ
        values[name] = value

    def DeleteValue(key, name):
        if name not in values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del values[name]

    module.OpenKey = OpenKey
    module.SetValueEx = SetValueEx
    module.DeleteValue = DeleteValue
    monkeypatch.setitem(sys.modules, "winreg", module)
    return values, opened


def test_install_writes_the_run_value_and_starts_the_broker(monkeypatch, isolated_home):
    values, opened = _fake_winreg(monkeypatch)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "broker_script_path",
                        lambda: r"C:\v\Scripts\firekeep-hands-broker.exe")
    monkeypatch.setattr(autostart.BrokerClient, "from_disk",
                        classmethod(lambda cls, timeout=2.0: None))
    spawned = []
    monkeypatch.setattr(autostart.subprocess, "Popen",
                        lambda argv, **kw: spawned.append((argv, kw)))

    autostart.install()

    assert values["FirekeepHandsBroker"] == r'"C:\v\Scripts\pythonw.exe" -m firekeep_hands.broker run'
    assert opened[0][:2] == ("HKCU", autostart.RUN_KEY)
    argv, kwargs = spawned[0]
    assert argv == [r"C:\v\Scripts\pythonw.exe", "-m", "firekeep_hands.broker", "run"]
    assert kwargs["creationflags"] == autostart._DETACHED_PROCESS | autostart._CREATE_NEW_PROCESS_GROUP
    assert kwargs["stdout"] == autostart.subprocess.DEVNULL


def test_install_does_not_start_a_second_broker(monkeypatch, isolated_home):
    """Two brokers would race for broker.json and leave the kit pointing at
    whichever won."""
    values, _opened = _fake_winreg(monkeypatch)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "broker_script_path", lambda: r"C:\v\Scripts\x.exe")
    monkeypatch.setattr(autostart.BrokerClient, "from_disk",
                        classmethod(lambda cls, timeout=2.0: object()))
    spawned = []
    monkeypatch.setattr(autostart.subprocess, "Popen",
                        lambda argv, **kw: spawned.append(argv))

    autostart.install()
    assert values and spawned == []          # value written, no second broker


def test_uninstall_removes_the_run_value(monkeypatch, isolated_home):
    values, _opened = _fake_winreg(monkeypatch, {"FirekeepHandsBroker": "anything"})
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    autostart.uninstall()
    assert "FirekeepHandsBroker" not in values


def test_uninstall_clears_the_pending_file_a_hard_kill_left_behind(monkeypatch, isolated_home):
    """The broker clears it on a clean stop, but `os.kill(SIGTERM)` on
    Windows is TerminateProcess and nothing in the broker runs afterwards —
    observed live on 2026-09-05. A stale file would have `status` describing
    approvals nobody can grant."""
    from firekeep_hands.broker import pending
    from firekeep_hands.broker.permits import PermitStore

    _values, _opened = _fake_winreg(monkeypatch)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    store = PermitStore(ttl_s=60)
    store.request(challenge="c", title="left over", classes=("send",), task_id="t", step_index=0)
    pending.write_pending(store, chord="ctrl+alt+y", deny_chord="ctrl+alt+n")
    assert pending.pending_path().exists()

    autostart.uninstall()
    assert not pending.pending_path().exists()


def test_uninstalling_twice_is_not_an_error(monkeypatch, isolated_home):
    """An uninstall must be safe to repeat, and safe on a machine that never
    installed."""
    _values, _opened = _fake_winreg(monkeypatch)
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    autostart.uninstall()
    autostart.uninstall()
    assert autostart._delete_run_value() is False


def test_a_missing_value_reports_that_nothing_was_removed(monkeypatch):
    values, _opened = _fake_winreg(monkeypatch, {"FirekeepHandsBroker": "x"})
    assert autostart._delete_run_value() is True
    assert autostart._delete_run_value() is False
    assert values == {}


# --- failures carry the tool's own words ------------------------------------


def test_a_failing_command_reports_what_the_tool_said(monkeypatch):
    """"returned non-zero exit status 1" is how "ERROR: Access is denied."
    went unseen and cost this module a design."""
    class Result:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Access is denied."

    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(RuntimeError) as exc:
        autostart._run(["launchctl", "bootstrap"], "launchctl bootstrap")
    assert "Access is denied" in str(exc.value) and "launchctl bootstrap" in str(exc.value)
    assert "exit 1" in str(exc.value)


def test_a_failing_command_with_no_output_still_names_itself(monkeypatch):
    class Result:
        returncode = 5
        stdout = ""
        stderr = ""

    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(RuntimeError, match="no output"):
        autostart._run(["launchctl", "bootout"], "launchctl bootout")


def test_an_unchecked_command_does_not_raise(monkeypatch):
    class Result:
        returncode = 3
        stdout = "Boot-out failed: 3: No such process"
        stderr = ""

    monkeypatch.setattr(autostart.subprocess, "run", lambda *a, **k: Result())
    assert autostart._run(["launchctl", "bootout"], "x", check=False).returncode == 3


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
