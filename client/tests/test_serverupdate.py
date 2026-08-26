"""serverupdate.check(): live cortex read, day-cached manifest, four-way
relation, per-version ack (spec decisions 2, 5, 6)."""
import configparser

import pytest

from firekeep_client import serverupdate, state, updater


def _cfg(extra=""):
    cfg = configparser.ConfigParser()
    cfg.read_string("[server]\nkind = ports\nhost = 127.0.0.1\n"
                    "[dist]\nbase_url = https://dist.example\n" + extra)
    return cfg


@pytest.fixture(autouse=True)
def scratch(tmp_path, monkeypatch):
    # state.cache_dir() honors FIREKEEP_CACHE_DIR (not FIREKEEP_SCRATCH_DIR) --
    # see state.py's cache_dir(). Isolating it here keeps _fetch_latest's
    # day-cache scratch key from touching the real ~/.cache/firekeep (POSIX)
    # or %LOCALAPPDATA%\firekeep (Windows) across test runs.
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path))
    return tmp_path


def _wire(monkeypatch, running="v1.2.0", latest="v1.3.0"):
    calls = {"manifest": 0}
    monkeypatch.setattr(serverupdate, "_fetch_running",
                        lambda cfg: running)
    def fake_manifest(cfg):
        calls["manifest"] += 1
        return latest
    monkeypatch.setattr(serverupdate, "_fetch_latest_uncached", fake_manifest)
    return calls


@pytest.mark.parametrize("running,latest,relation", [
    ("v1.2.0", "v1.3.0", "behind"),
    ("v1.3.0", "v1.3.0", "current"),
    ("v1.3.0", "v1.2.1", "ahead"),
    ("v1.2.1-67-g040d0ed", "v1.3.0", "unjudged"),
    ("v1.2.0", None, "unjudged"),
    ("v1.2.0", "not-a-version", "unjudged"),
])
def test_relation_matrix(monkeypatch, running, latest, relation):
    _wire(monkeypatch, running=running, latest=latest)
    status = serverupdate.check(_cfg())
    assert status is not None and status.relation == relation
    assert status.running == running


def test_none_only_when_cortex_silent(monkeypatch):
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: None)
    assert serverupdate.check(_cfg()) is None


def test_manifest_day_cached_but_running_live(monkeypatch):
    calls = _wire(monkeypatch)
    serverupdate.check(_cfg())
    serverupdate.check(_cfg())
    assert calls["manifest"] == 1  # decision 5: fetch cached...
    # ...but a live running-version change shows immediately (post-update run)
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: "v1.3.0")
    status = serverupdate.check(_cfg())
    assert status.relation == "current"  # no stale 'behind' from any cache


def test_negative_manifest_cached(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(serverupdate, "_fetch_running", lambda cfg: "v1.2.0")
    def fail(cfg):
        calls["n"] += 1
        raise updater.UpdateError("down")
    monkeypatch.setattr(serverupdate, "_fetch_latest_uncached", fail)
    assert serverupdate.check(_cfg()).relation == "unjudged"
    assert serverupdate.check(_cfg()).relation == "unjudged"
    assert calls["n"] == 1  # one 3s cost per day, not per call


def test_ack_matches_exact_version_and_rearms(monkeypatch):
    _wire(monkeypatch, running="v1.2.0", latest="v1.3.0")
    assert serverupdate.check(_cfg("server_update_ack = v1.3.0\n")).ack is True
    assert serverupdate.check(_cfg("server_update_ack = v1.2.9\n")).ack is False
    # Decision 5's day-cache means a rewired manifest is invisible until the
    # cache period turns over (test_manifest_day_cached_but_running_live and
    # test_negative_manifest_cached both pin that behavior for the SAME day).
    # Decision 6's re-arm is specifically "a newer latest on the NEXT fetch" --
    # simulate that boundary by clearing the cached manifest, exactly what a
    # real day rollover does to the "today|..." key.
    state.delete_scratch(serverupdate._CACHE_KEY)
    _wire(monkeypatch, running="v1.2.0", latest="v1.4.0")
    assert serverupdate.check(_cfg("server_update_ack = v1.3.0\n")).ack is False


def test_no_dist_section_is_unjudged_not_none(monkeypatch):
    monkeypatch.setattr(serverupdate, "_fetch_running",
                        lambda cfg: "v1.2.1-67-g040d0ed")
    cfg = configparser.ConfigParser()
    cfg.read_string("[server]\nkind = ports\nhost = 127.0.0.1\n")
    status = serverupdate.check(cfg)
    assert status is not None and status.relation == "unjudged"
    assert status.latest is None  # source-checkout row needs only cortex


def test_check_never_raises(monkeypatch):
    def explode(cfg):
        raise RuntimeError("boom")
    monkeypatch.setattr(serverupdate, "_fetch_running", explode)
    assert serverupdate.check(_cfg()) is None


def test_is_clean_release():
    assert serverupdate.is_clean_release("v1.2.0") is True
    assert serverupdate.is_clean_release("v1.2.1-67-g040d0ed") is False


def test_nudge_line():
    s = serverupdate.ServerUpdateStatus("v1.2.0", "v1.3.0", "behind", False)
    line = serverupdate.nudge_line(s)
    assert "v1.2.0 -> v1.3.0" in line and "update.sh --to v1.3.0" in line
    for quiet in [
        serverupdate.ServerUpdateStatus("v1.2.0", "v1.3.0", "behind", True),
        serverupdate.ServerUpdateStatus("v1.3.0", "v1.3.0", "current", False),
        serverupdate.ServerUpdateStatus("v1.3.0", "v1.2.1", "ahead", False),
        serverupdate.ServerUpdateStatus("x", None, "unjudged", False),
    ]:
        assert serverupdate.nudge_line(quiet) == ""
