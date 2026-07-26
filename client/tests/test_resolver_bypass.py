"""The bypass gate: resolver.is_bypassed / is_personal / set_personal / marker.

Personal mode is a transient MARKER FILE next to the config (never the config
itself). is_bypassed() is the single gate the dispatcher, shim, and decision
server consult; it must fail toward NOT-bypassed so a bug can't silently stop
team logging.
"""
from __future__ import annotations

import os
import time

import pytest

from firekeep_client import resolver


@pytest.fixture
def firekeep_home(tmp_path, monkeypatch):
    """Point FIREKEEP_CONFIG at a tmp config so the marker lands in tmp, never real ~."""
    cfg = tmp_path / "config"
    cfg.write_text("[active]\nprofile = personal\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    monkeypatch.delenv("FIREKEEP_PERSONAL_TTL_HOURS", raising=False)
    return tmp_path


def test_marker_path_is_beside_config_not_config(firekeep_home):
    marker = resolver.personal_marker_path()
    assert marker == firekeep_home / "personal"
    assert marker.name == "personal"
    # It must never BE the config file — toggling it can't rewrite config.
    assert marker != firekeep_home / "config"


def test_set_personal_round_trips(firekeep_home):
    assert resolver.is_personal() is False
    assert resolver.set_personal(True) is True
    assert resolver.personal_marker_path().exists()
    assert resolver.is_personal() is True
    assert resolver.set_personal(False) is False
    assert not resolver.personal_marker_path().exists()
    assert resolver.is_personal() is False


def test_set_personal_off_is_idempotent_when_absent(firekeep_home):
    # No marker yet — turning it off must be a harmless no-op, not an error.
    assert resolver.set_personal(False) is False


def test_stale_marker_is_treated_as_off_and_removed(firekeep_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_PERSONAL_TTL_HOURS", "1")  # 1h TTL
    resolver.set_personal(True)
    marker = resolver.personal_marker_path()
    # Backdate the marker's mtime to 2h ago -> stale.
    old = time.time() - 2 * 3600
    os.utime(marker, (old, old))
    assert resolver.is_personal() is False
    assert not marker.exists()  # crash backstop swept it


def test_fresh_marker_within_ttl_is_on(firekeep_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_PERSONAL_TTL_HOURS", "12")
    resolver.set_personal(True)
    marker = resolver.personal_marker_path()
    old = time.time() - 3600  # 1h ago, within the 12h TTL
    os.utime(marker, (old, old))
    assert resolver.is_personal() is True


def test_is_bypassed_true_when_personal_on(firekeep_home):
    resolver.set_personal(True)
    assert resolver.is_bypassed() is True


def test_is_bypassed_env_forces_true_without_marker(firekeep_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert not resolver.personal_marker_path().exists()
    assert resolver.is_bypassed() is True


@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("yes", True), ("on", True), ("anything", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("", False),
])
def test_is_bypassed_env_truthiness(firekeep_home, monkeypatch, val, expected):
    monkeypatch.setenv("FIREKEEP_BYPASS", val)
    assert resolver.is_bypassed() is expected


def test_is_bypassed_false_by_default(firekeep_home):
    assert resolver.is_bypassed() is False


def test_set_personal_off_reports_still_on_when_unlink_fails(firekeep_home, monkeypatch):
    """The stuck-ON footgun: if unlink fails (Windows PermissionError etc.), the marker
    survives and personal mode is STILL ON — set_personal(False) must report True (the
    observed state), never a false 'off' that lets team logging silently stop."""
    resolver.set_personal(True)
    marker = resolver.personal_marker_path()

    orig_unlink = type(marker).unlink

    def boom(self, *a, **k):
        if self == marker:
            raise PermissionError("locked")
        return orig_unlink(self, *a, **k)
    monkeypatch.setattr(type(marker), "unlink", boom)

    result = resolver.set_personal(False)
    assert result is True                     # honest: still on (unlink failed)
    assert marker.exists()                    # marker was NOT removed
    assert resolver.is_personal() is True


def test_set_personal_on_reports_observed_true(firekeep_home):
    assert resolver.set_personal(True) is True
    assert resolver.personal_marker_path().exists()


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e999"])
def test_nonfinite_ttl_falls_back_to_default_not_infinite(firekeep_home, monkeypatch, bad):
    """nan/inf/overflow must NOT disable the staleness sweep — they fall back to the
    12h default, so a stale marker is still reaped."""
    import time as _t
    monkeypatch.setenv("FIREKEEP_PERSONAL_TTL_HOURS", bad)
    resolver.set_personal(True)
    marker = resolver.personal_marker_path()
    old = _t.time() - 24 * 3600  # 24h ago — stale under the 12h default
    import os as _os
    _os.utime(marker, (old, old))
    assert resolver.is_personal() is False    # swept, TTL not silently infinite
    assert not marker.exists()


def test_gate_never_raises_on_bad_marker_path(monkeypatch):
    # A resolver failure (e.g. personal_marker_path blowing up) must resolve to
    # NOT bypassed, never propagate — team logging must survive a bug here.
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    monkeypatch.setattr(resolver, "personal_marker_path",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert resolver.is_bypassed() is False
    assert resolver.is_personal() is False
