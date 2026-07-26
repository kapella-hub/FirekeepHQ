"""Tests for build provenance: the version module and /version endpoint."""

from __future__ import annotations

import importlib


def test_version_info_defaults(monkeypatch):
    """With no build env vars set, version info uses safe defaults."""
    monkeypatch.delenv("GIT_SHA", raising=False)
    monkeypatch.delenv("BUILD_TIME", raising=False)
    monkeypatch.delenv("APP_VERSION", raising=False)

    import app.version as version_mod
    importlib.reload(version_mod)

    info = version_mod.get_version_info()
    assert info["git_sha"] == "unknown"
    assert info["build_time"] == "unknown"
    assert info["version"]  # non-empty fallback version string


def test_version_info_reads_env(monkeypatch):
    """Build env vars flow through to version info."""
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_TIME", "2026-05-30T00:00:00Z")
    monkeypatch.setenv("APP_VERSION", "9.9.9")

    import app.version as version_mod
    importlib.reload(version_mod)

    info = version_mod.get_version_info()
    assert info["git_sha"] == "abc1234"
    assert info["build_time"] == "2026-05-30T00:00:00Z"
    assert info["version"] == "9.9.9"


def test_version_endpoint(test_client, monkeypatch):
    """GET /version returns provenance without probing backends."""
    r = test_client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"version", "git_sha", "build_time"}
    assert isinstance(body["version"], str) and body["version"]
