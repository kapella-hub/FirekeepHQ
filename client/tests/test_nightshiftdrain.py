"""The session-start night-shift drain: spawns only when a local model is
listening, once per interval across every window that opens, off with one
env var, and never costs the briefing anything."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time

import pytest

from firekeep_client import nightshiftdrain, state


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    (tmp_path / "config").write_text("[dist]\nversion=1\n", encoding="utf-8")
    monkeypatch.delenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", raising=False)
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", raising=False)
    return tmp_path


def _cfg(auto_drain=None, hours=None):
    import configparser
    cfg = configparser.ConfigParser()
    if auto_drain is not None or hours is not None:
        cfg["nightshift"] = {}
        if auto_drain is not None:
            cfg["nightshift"]["auto_drain"] = auto_drain
        if hours is not None:
            cfg["nightshift"]["auto_drain_hours"] = hours
    return cfg


def _listening(monkeypatch, value: bool):
    monkeypatch.setattr(nightshiftdrain, "local_llm_listening", lambda timeout=0.25: value)


def _record_spawns(monkeypatch) -> list:
    real = subprocess.Popen
    seen: list = []

    def spy(argv, **kw):
        if "night-shift" in argv:
            seen.append({"argv": argv, "kw": kw})
            return object()
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _forbid_spawn(monkeypatch, why: str) -> None:
    real = subprocess.Popen

    def guard(argv, **kw):
        if "night-shift" in argv:
            pytest.fail(why)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", guard)


# --- gates -------------------------------------------------------------------

def test_enabled_by_default(home):
    assert nightshiftdrain.is_enabled(_cfg()) is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_env_off_switch(home, monkeypatch, val):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", val)
    assert nightshiftdrain.is_enabled(_cfg()) is False


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off"])
def test_env_falsey_values_keep_it_on(home, monkeypatch, val):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", val)
    assert nightshiftdrain.is_enabled(_cfg()) is True


def test_config_off_switch_only_on_explicit_false(home):
    assert nightshiftdrain.is_enabled(_cfg(auto_drain="false")) is False
    assert nightshiftdrain.is_enabled(_cfg(auto_drain="")) is True


def test_no_local_llm_means_silence_and_no_spawn(home, monkeypatch):
    _listening(monkeypatch, False)
    _forbid_spawn(monkeypatch, "spawned with no local model listening")
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


# --- probe -------------------------------------------------------------------

def test_probe_hits_configured_base_only(home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", "http://10.0.0.5:8080/v1")
    asked = []

    def fake_connect(addr, timeout=None):
        asked.append(addr)
        raise OSError("closed")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert nightshiftdrain.local_llm_listening() is False
    assert asked == [("10.0.0.5", 8080)]


def test_probe_defaults_to_lm_studio_then_ollama(home, monkeypatch):
    asked = []

    class _Sock:
        def close(self):
            pass

    def fake_connect(addr, timeout=None):
        asked.append(addr)
        if addr == ("127.0.0.1", 11434):
            return _Sock()
        raise OSError("closed")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert nightshiftdrain.local_llm_listening() is True
    assert asked == [("127.0.0.1", 1234), ("127.0.0.1", 11434)]


# --- cadence + claim ---------------------------------------------------------

def test_stamp_is_the_interval_bucket(home):
    six_h = 6 * 3600
    assert nightshiftdrain.should_drain(now=10 * six_h + 5) == "10"
    assert nightshiftdrain.should_drain(now=11 * six_h) == "11"


def test_interval_env_and_config(home, monkeypatch):
    assert nightshiftdrain.drain_interval_hours(_cfg()) == 6.0
    assert nightshiftdrain.drain_interval_hours(_cfg(hours="2")) == 2.0
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "3")
    assert nightshiftdrain.drain_interval_hours(_cfg(hours="2")) == 3.0
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "junk")
    assert nightshiftdrain.drain_interval_hours(_cfg()) == 6.0


def test_spawn_once_per_stamp_with_detached_argv(home, monkeypatch):
    _listening(monkeypatch, True)
    seen = _record_spawns(monkeypatch)
    import sys
    from pathlib import Path
    exe = Path(sys.executable)
    monkeypatch.setattr(nightshiftdrain, "_firekeep_exe", lambda: exe)
    assert nightshiftdrain.maybe_spawn(_cfg(), "77") is True
    assert nightshiftdrain.maybe_spawn(_cfg(), "77") is True  # already claimed — in flight
    assert len(seen) == 1
    assert seen[0]["argv"][1:] == ["night-shift", "--max", "5"]
    kw = seen[0]["kw"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["close_fds"] is True
    assert state._scratch_file("night_shift.77").exists()


def test_failed_spawn_releases_the_claim(home, monkeypatch):
    _listening(monkeypatch, True)
    import sys
    from pathlib import Path
    exe = Path(sys.executable)
    monkeypatch.setattr(nightshiftdrain, "_firekeep_exe", lambda: exe)

    def boom(argv, **kw):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert nightshiftdrain.maybe_spawn(_cfg(), "78") is False
    assert not state._scratch_file("night_shift.78").exists()


# --- the nudge ---------------------------------------------------------------

def test_nudge_names_the_off_switch(home, monkeypatch):
    _listening(monkeypatch, True)
    monkeypatch.setattr(nightshiftdrain, "maybe_spawn", lambda cfg, stamp: True)
    line = nightshiftdrain.drain_nudge(_cfg())
    assert line.startswith("\n\n[firekeep] night shift draining the fleet queue in background")
    assert "FIREKEEP_NO_AUTO_NIGHTSHIFT=1" in line


def test_nudge_is_silent_when_spawn_cannot_run(home, monkeypatch):
    _listening(monkeypatch, True)
    monkeypatch.setattr(nightshiftdrain, "maybe_spawn", lambda cfg, stamp: False)
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


def test_last_run_line_reports_once(home, monkeypatch):
    _listening(monkeypatch, False)  # no spawn today; the report still prints
    state.write_scratch("night_shift_last", json.dumps({
        "at": time.time() - 3600, "reported": False, "error": None,
        "counts": {"distilled": 1, "reauthored": 2, "proposed": 1, "draft_skills": 3}}))
    line = nightshiftdrain.drain_nudge(_cfg())
    assert "3 draft skill(s)" in line and "1 verdict proposal(s)" in line and "Autopilot" in line
    assert nightshiftdrain.drain_nudge(_cfg()) == ""  # marked reported
    assert json.loads(state.read_scratch("night_shift_last"))["reported"] is True


def test_last_run_with_nothing_to_review_is_silent(home, monkeypatch):
    _listening(monkeypatch, False)
    state.write_scratch("night_shift_last", json.dumps({
        "at": time.time(), "reported": False, "error": None,
        "counts": {"distilled": 2, "reauthored": 0, "proposed": 0, "draft_skills": 0}}))
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


def test_nudge_never_raises(home, monkeypatch):
    monkeypatch.setattr(nightshiftdrain, "is_enabled", lambda cfg: (_ for _ in ()).throw(RuntimeError()))
    assert nightshiftdrain.drain_nudge(_cfg()) == ""
