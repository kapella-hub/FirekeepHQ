"""Build identity, shared by every service.

Read on every call rather than cached at import: tests reload the module and
callers may set env after import. cortex/tests/test_version.py already depends
on this behaviour.
"""
import importlib

import provenance


def test_defaults_when_env_unset(monkeypatch):
    for var in ("APP_VERSION", "GIT_SHA", "BUILD_TIME"):
        monkeypatch.delenv(var, raising=False)
    info = provenance.get_version_info("bridge")
    assert info["service"] == "bridge"
    assert info["git_sha"] == "unknown"
    assert info["build_time"] == "unknown"
    assert info["version"], "must fall back to a non-empty version string"


def test_reads_env(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_TIME", "2026-07-26T00:00:00Z")
    info = provenance.get_version_info("relay")
    assert info == {
        "service": "relay",
        "version": "9.9.9",
        "git_sha": "abc1234",
        "build_time": "2026-07-26T00:00:00Z",
    }


def test_env_is_read_per_call_not_cached_at_import(monkeypatch):
    """The regression that matters: caching at import makes every service
    report whatever was set when the first one happened to be imported."""
    monkeypatch.setenv("APP_VERSION", "1.1.1")
    first = provenance.get_version_info("bridge")["version"]
    monkeypatch.setenv("APP_VERSION", "2.2.2")
    second = provenance.get_version_info("bridge")["version"]
    assert (first, second) == ("1.1.1", "2.2.2")


def test_empty_env_var_falls_back_rather_than_reporting_empty(monkeypatch):
    """An empty APP_VERSION is a build misconfiguration, not a version."""
    monkeypatch.setenv("APP_VERSION", "")
    monkeypatch.setenv("GIT_SHA", "")
    info = provenance.get_version_info("sentinel")
    assert info["version"]
    assert info["git_sha"] == "unknown"


def test_service_name_is_required_and_passed_through():
    assert provenance.get_version_info("cortex")["service"] == "cortex"


def test_module_reloads_cleanly(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "3.3.3")
    importlib.reload(provenance)
    assert provenance.get_version_info("bridge")["version"] == "3.3.3"
