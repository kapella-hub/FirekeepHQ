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


# --- unprovenanced: a build that cannot say what it is ----------------------
#
# The failure this fixes: a bare `docker compose build` leaves GIT_SHA/
# BUILD_TIME/APP_VERSION unset, so compose stamps its own `${APP_VERSION:-...}`
# default into the image. The old default was `0.6.0` — a string that PARSES as
# a clean release, so `_relation` judged it against the published series and
# reported "0.6.0 -> v1.3.1": a jump across twenty tags, naming a version that
# never shipped. `is_clean_release` did not catch it because it tests
# parseability, and the one input meaning "I do not know what I am" was the one
# input that parsed perfectly.
#
# No v0.6.0 server tag has ever existed (the series runs v0.1.0..v0.4.7,
# v1.0.0+), so treating it as a sentinel cannot shadow a real release.


@pytest.mark.parametrize("running", [
    "0.0.0-unprovenanced",   # the current sentinel
    "0.6.0",                 # legacy: every image built before the fix
    "v0.6.0",                # ...and the v-prefixed spelling of it
])
def test_unprovenanced_server_is_never_judged_against_the_release_series(
        monkeypatch, running):
    _wire(monkeypatch, running=running, latest="v1.3.1")
    status = serverupdate.check(_cfg())
    assert status is not None
    assert status.relation == "unprovenanced", (
        "a build with no provenance must not be compared to a release tag")


def test_unprovenanced_is_decided_before_the_manifest_is_consulted(monkeypatch):
    """Provenance is a property of the SERVER, not of dist-host reachability —
    so the verdict must not degrade to 'unjudged' when the manifest is absent."""
    _wire(monkeypatch, running="0.6.0", latest=None)
    assert serverupdate.check(_cfg()).relation == "unprovenanced"


def test_unprovenanced_nudge_is_silent(monkeypatch):
    """The nudge's contract is 'an update IS available'. Here we cannot know
    that, so we do not claim it — doctor's warn row carries this instead."""
    s = serverupdate.ServerUpdateStatus("0.6.0", "v1.3.1", "unprovenanced", False)
    assert serverupdate.nudge_line(s) == ""


def test_is_clean_release_rejects_the_unprovenanced_sentinels():
    """`is_clean_release` and `_relation` must never disagree on an input (its
    own docstring's invariant), so the sentinel check has to live in both."""
    assert serverupdate.is_clean_release("0.6.0") is False
    assert serverupdate.is_clean_release("0.0.0-unprovenanced") is False


def test_real_releases_still_judge_normally(monkeypatch):
    """Regression guard: the sentinel check must not swallow ordinary versions,
    including the neighbouring 0.5.0/0.7.0 that are NOT sentinels."""
    for running, relation in (("v0.5.0", "behind"), ("v0.7.0", "behind"),
                              ("v1.3.1", "current")):
        _wire(monkeypatch, running=running, latest="v1.3.1")
        state.delete_scratch(serverupdate._CACHE_KEY)
        assert serverupdate.check(_cfg()).relation == relation
