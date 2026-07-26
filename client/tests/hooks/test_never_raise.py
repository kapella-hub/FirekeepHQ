"""SP1b §6.3 "availability over enforcement": every hook core's run() calls
resolver.load_config()/active_profile()/agent_id() UNGUARDED at the top. A
missing/malformed ~/.firekeep config must degrade the hook (return its safe
default), never crash the caller's process with ConfigError.

pre_tool/post_tool's safe default is 0 (allow — same as the existing
server-unreachable path). session_start/stop/prompt's safe default is {}
(no systemMessage).
"""
from __future__ import annotations

import pytest

from firekeep_client.hooks import post_tool, pre_tool, prompt, session_start, stop

_CORES_AND_DEFAULTS = [
    (pre_tool, 0),
    (post_tool, 0),
    (session_start, {}),
    (stop, {}),
    (prompt, {}),
]
_IDS = [core.__name__.rsplit(".", 1)[-1] for core, _ in _CORES_AND_DEFAULTS]


@pytest.fixture
def no_config_env(tmp_path, monkeypatch):
    """Point FIREKEEP_CONFIG at a path that does not exist -- resolver.load_config()
    raises ConfigError. Cache/log dirs are still isolated under tmp_path so the
    guard's hooklog write is observable and no real ~/.firekeep is touched."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "does-not-exist" / "config"))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(logs))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return {"logs": logs}


@pytest.mark.parametrize("core,default", _CORES_AND_DEFAULTS, ids=_IDS)
def test_run_never_raises_on_missing_config(core, default, no_config_env):
    # Must not raise ConfigError (or anything else) -- must return the safe default.
    result = core.run({})
    assert result == default

    # A hooklog entry was written, tagged with this hook's name.
    log_file = no_config_env["logs"] / "hooks.log"
    assert log_file.exists()
    hook_name = core.__name__.rsplit(".", 1)[-1]
    contents = log_file.read_text(encoding="utf-8")
    assert hook_name in contents
