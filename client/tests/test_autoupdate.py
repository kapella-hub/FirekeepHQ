"""Background client auto-update: spawn a detached `firekeep update` when the
session_start daily check finds a newer version. ON by default; opt out via
FIREKEEP_NO_AUTO_UPDATE or `[dist] auto_update = false`. Never blocks/fails a session;
at most one spawn per day per target (a cached 'newer' verdict must not re-launch
every session start)."""
import configparser
import os
import subprocess

import pytest

from firekeep_client import autoupdate


def _cfg(text=""):
    c = configparser.ConfigParser()
    c.read_string(text)
    return c


# --- enable / opt-out --------------------------------------------------------

def test_enabled_by_default():
    assert autoupdate.is_enabled(_cfg()) is True


def test_env_opt_out(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_UPDATE", "1")
    assert autoupdate.is_enabled(_cfg()) is False


def test_env_falsey_does_not_opt_out(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_UPDATE", "0")
    assert autoupdate.is_enabled(_cfg()) is True


def test_config_opt_out():
    assert autoupdate.is_enabled(_cfg("[dist]\nauto_update = false\n")) is False
    assert autoupdate.is_enabled(_cfg("[dist]\nauto_update = true\n")) is True


def test_blank_config_value_stays_enabled():
    # A half-edited `auto_update =` (blank) means 'unset' -> default ON, NOT disabled.
    assert autoupdate.is_enabled(_cfg("[dist]\nauto_update =\n")) is True


# --- maybe_spawn -------------------------------------------------------------

def _spawns(calls):
    """Only the `firekeep update` launches. On Windows the claim write hardens the
    cache dir via `icacls` (state._private), and subprocess.run routes through
    the SAME mocked Popen — those ACL calls are real cache hygiene, not spawns."""
    return [c for c in calls if "icacls" not in str(c[0])]


@pytest.fixture
def fake_exe(tmp_path, monkeypatch):
    exe = tmp_path / "firekeep"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(autoupdate, "_firekeep_exe", lambda: exe)
    return exe


def test_maybe_spawn_launches_detached_and_claims(monkeypatch, fake_exe):
    calls = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: calls.append((argv, kw)))
    spawned = autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17")
    assert spawned is True
    spawns = _spawns(calls)
    assert spawns, "must spawn a background update"
    argv, kw = spawns[0]
    assert argv == [str(fake_exe), "update"]
    # Detached + non-blocking so it survives the hook exiting. The detach carrier
    # is per-platform (autoupdate.maybe_spawn): POSIX starts a new session,
    # Windows uses DETACHED_PROCESS creation flags.
    if os.name == "nt":
        assert kw.get("creationflags") == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        assert kw.get("start_new_session") is True
    assert kw.get("stdout") == subprocess.DEVNULL
    # Atomic claim written so it does not re-launch on the next session start today.
    assert autoupdate._claim_path("2026-07-17", "9.9.9").exists()


def test_maybe_spawn_is_once_per_day_per_target(monkeypatch, fake_exe):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    first = autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17")
    second = autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17")  # same day+target
    assert len(_spawns(calls)) == 1, "the atomic claim must prevent a second launch"
    assert first is True
    # Second call still reports 'in flight' (an update was already claimed today).
    assert second is True


def test_maybe_spawn_releases_claim_on_spawn_failure(monkeypatch, fake_exe):
    """A failed Popen must release the claim so a later session can retry — otherwise
    a transient fork failure would suppress auto-update for the rest of the day."""
    def boom(argv, **kw):
        raise OSError("cannot fork")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17") is False
    assert not autoupdate._claim_path("2026-07-17", "9.9.9").exists(), \
        "claim must be released after a failed launch"


def test_maybe_spawn_relaunches_for_a_new_target(monkeypatch, fake_exe):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17")
    autoupdate.maybe_spawn(_cfg(), "9.9.10", "2026-07-17")  # newer target appeared
    assert len(_spawns(calls)) == 2


def test_maybe_spawn_skips_when_disabled(monkeypatch, fake_exe):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_UPDATE", "1")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17") is False
    assert _spawns(calls) == []


def test_maybe_spawn_skips_when_launcher_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(autoupdate, "_firekeep_exe", lambda: tmp_path / "nope")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: calls.append(argv))
    assert autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17") is False
    assert _spawns(calls) == []


def test_maybe_spawn_never_raises(monkeypatch, fake_exe):
    def boom(*a, **k):
        raise OSError("cannot fork")

    monkeypatch.setattr(subprocess, "Popen", boom)
    # Must swallow and return False — auto-update can never cost a session.
    assert autoupdate.maybe_spawn(_cfg(), "9.9.9", "2026-07-17") is False
