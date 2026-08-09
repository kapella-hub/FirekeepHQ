import sys

import pytest

from firekeep_client.adapters.base import (
    Adapter, shim_servers, hook_command, merge_owned, HOOK_MARKER, FIREKEEP_MCP_KEYS,
)
from firekeep_client.adapters import get_adapter


def _exe(path):
    """Expected console-script path for the CURRENT (real, unmocked) host platform."""
    text = str(path)
    return text + ".exe" if sys.platform == "win32" else text


def test_adapter_abc_cannot_instantiate():
    with pytest.raises(TypeError):
        Adapter()


def test_shim_servers_renders_one_gateway(tmp_path):
    venv_bin = tmp_path / "venv" / "bin"
    servers = shim_servers(venv_bin)
    assert servers == {"firekeep": (_exe(venv_bin / "firekeep"), ["gateway"])}
    assert set(servers) == set(FIREKEEP_MCP_KEYS)


def test_shim_servers_appends_exe_on_win32(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    venv_bin = tmp_path / "Scripts"
    servers = shim_servers(venv_bin)
    assert servers["firekeep"][0] == str(venv_bin / "firekeep") + ".exe"


def test_shim_servers_no_exe_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    venv_bin = tmp_path / "bin"
    servers = shim_servers(venv_bin)
    assert servers["firekeep"][0] == str(venv_bin / "firekeep")


def test_hook_command_appends_exe_on_win32(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    venv_bin = tmp_path / "Scripts"
    cmd = hook_command(venv_bin, "session_start")
    # forward slashes: hook commands are bash-executed shell strings on Windows too
    expected_py = str(venv_bin / "python").replace("\\", "/") + ".exe"
    assert cmd == f"{expected_py} -m firekeep_client.hooks session_start"


def test_hook_command_no_exe_on_posix(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    venv_bin = tmp_path / "bin"
    cmd = hook_command(venv_bin, "session_start")
    expected_py = str(venv_bin / "python").replace("\\", "/")
    assert cmd == f"{expected_py} -m firekeep_client.hooks session_start"


def test_hook_command_carries_stable_marker(tmp_path):
    venv_bin = tmp_path / "Scripts"
    cmd = hook_command(venv_bin, "session_start")
    assert HOOK_MARKER in cmd                       # venv-independent unrender marker
    assert cmd.endswith("-m firekeep_client.hooks session_start")
    assert str(venv_bin / "python").replace("\\", "/") in cmd


def test_hook_command_forward_slashes_survive_bash_on_windows(monkeypatch):
    """Claude runs {"type":"command"} hooks through bash even on Windows. An unquoted
    Windows backslash interpreter path (C:\\Users\\mogan\\...) has its backslashes eaten as
    bash escape chars -> `C:Usersmogan...: command not found` (the reported bug). Render
    forward slashes, which survive bash AND remain valid for Windows CreateProcess.
    Uses PureWindowsPath so real backslashes are exercised regardless of the host OS running
    this test -- the win32 tests above use tmp_path, a POSIX path on CI, so they never saw a
    real backslash, which is exactly how this shipped."""
    from pathlib import PureWindowsPath
    monkeypatch.setattr(sys, "platform", "win32")
    # `current` is the side-by-side layout's alias (the junction every rendered
    # surface routes through) — what cmd_install actually passes as venv_bin.
    venv_bin = PureWindowsPath(r"C:\Users\mogan\.firekeep\current\Scripts")
    cmd = hook_command(venv_bin, "prompt")
    assert "\\" not in cmd                          # no backslash reaches the bash string
    assert cmd == "C:/Users/mogan/.firekeep/current/Scripts/python.exe -m firekeep_client.hooks prompt"


def test_hook_command_quotes_spaced_windows_path_with_forward_slashes(monkeypatch):
    """A venv under a path with spaces must still be quoted (bash word-splits an unquoted
    spaced path) AND use forward slashes (bash-safe) -- both concerns handled together."""
    from pathlib import PureWindowsPath
    monkeypatch.setattr(sys, "platform", "win32")
    venv_bin = PureWindowsPath(r"C:\Users\First Last\.firekeep\current\Scripts")
    cmd = hook_command(venv_bin, "session_start")
    assert "\\" not in cmd
    assert cmd == '"C:/Users/First Last/.firekeep/current/Scripts/python.exe" -m firekeep_client.hooks session_start'


def test_hook_command_dispatcher_form_is_module_not_submodule(tmp_path):
    """The rendered command must invoke the `firekeep_client.hooks` PACKAGE (so its
    __main__.py dispatcher runs and actually calls run()), NOT `firekeep_client.hooks.<core>`
    as a submodule import -- the latter has no __main__ and silently no-ops (exit 0,
    run() never called). This is the dead-hook bug the dispatcher form fixes."""
    venv_bin = tmp_path / "Scripts"
    cmd = hook_command(venv_bin, "pre_tool")
    assert " -m firekeep_client.hooks pre_tool" in cmd
    assert " -m firekeep_client.hooks.pre_tool" not in cmd


def test_hook_command_extra_args_appended(tmp_path):
    venv_bin = tmp_path / "Scripts"
    cmd = hook_command(venv_bin, "pre_tool", extra_args="--block-exit 2")
    assert cmd.endswith("-m firekeep_client.hooks pre_tool --block-exit 2")


def test_hook_command_no_extra_args_by_default(tmp_path):
    venv_bin = tmp_path / "Scripts"
    cmd = hook_command(venv_bin, "post_tool")
    assert cmd.endswith("-m firekeep_client.hooks post_tool")


def test_merge_owned_preserves_foreign():
    existing = {"foreign": 1}
    out = merge_owned(existing, {"firekeep-cortex": 2})
    assert out is existing
    assert existing == {"foreign": 1, "firekeep-cortex": 2}


def test_firekeep_mcp_keys_frozen():
    assert FIREKEEP_MCP_KEYS == ("firekeep",)


def test_get_adapter_unknown_raises():
    with pytest.raises(ValueError):
        get_adapter("bogus")


def test_hook_command_quotes_paths_with_spaces(monkeypatch, tmp_path):
    r"""settings.json hook commands are shell strings; a venv under a path
    with spaces (C:\Users\First Last\...) must not word-split."""
    import sys as _sys

    from firekeep_client.adapters import base

    monkeypatch.setattr(_sys, "platform", "win32")
    venv_bin = tmp_path / "First Last" / ".firekeep" / "current" / "Scripts"
    cmd = base.hook_command(venv_bin, "pre_tool", extra_args="--block-exit 2")
    assert cmd.startswith('"') and '" -m firekeep_client.hooks pre_tool' in cmd
    assert cmd.endswith("--block-exit 2")


def test_hook_command_unquoted_when_no_spaces(tmp_path):
    from firekeep_client.adapters import base

    venv_bin = tmp_path / "venv" / "bin"
    cmd = base.hook_command(venv_bin, "stop")
    assert not cmd.startswith('"')
