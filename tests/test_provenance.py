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


# --- the fallback must not be mistakable for a release ----------------------
#
# Root cause of the "server update available: 0.6.0 -> v1.3.1" false alarm.
# A bare `docker compose build` leaves GIT_SHA/BUILD_TIME/APP_VERSION unset, so
# compose stamped its own `${APP_VERSION:-0.6.0}` default into the image. The
# client then read `0.6.0` off /version, PARSED it as a clean release, and
# compared it to the published series -- announcing a twenty-tag jump from a
# version that never shipped. The string that means "I do not know what I am"
# must not be shaped like an answer.

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every file that can stamp APP_VERSION into a built image, or print it.
BUILD_PLUMBING = (
    "Dockerfile", "cortex/Dockerfile", "bridge/Dockerfile",
    "sentinel/Dockerfile", "relay/Dockerfile",
    "docker-compose.yml", "benchmarks/memory/docker-compose.bench.yml",
    "deploy/lib.sh", "update.sh", "install.sh",
)

DOCKERFILE_ARG = re.compile(r"^ARG\s+APP_VERSION=(\S+)", re.M)
COMPOSE_DEFAULT = re.compile(r"APP_VERSION:\s*\$\{APP_VERSION:-([^}]+)\}")


def test_fallback_version_does_not_parse_as_major_minor_patch():
    """The exact predicate the client's updater.parse_version applies."""
    parts = provenance._FALLBACK_VERSION.split(".")
    assert not (len(parts) == 3 and all(p.isdigit() for p in parts)), (
        f"{provenance._FALLBACK_VERSION!r} parses as a release version, so a "
        "client will compare it against the published series")


def test_every_build_default_is_the_one_shared_sentinel():
    """The guard that ties the shell/compose layer to the Python constant.

    Before this, `_FALLBACK_VERSION` was only ever reached when APP_VERSION was
    unset *in Python* -- but compose and the Dockerfiles carried their own copy
    of the literal, so they won first and the Python constant was dead code in
    the exact scenario it existed for. Changing one without the others would
    silently reintroduce the bug.
    """
    sentinel = provenance._FALLBACK_VERSION
    found = 0
    for rel in BUILD_PLUMBING:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for match in DOCKERFILE_ARG.findall(text) + COMPOSE_DEFAULT.findall(text):
            found += 1
            assert match.strip() == sentinel, (
                f"{rel} defaults APP_VERSION to {match!r}, not the shared "
                f"sentinel {sentinel!r}")
    assert found >= 12, f"expected to find every build default, saw {found}"


def test_the_old_release_shaped_sentinel_is_gone_from_build_plumbing():
    """0.6.0 is the specific string that shipped the false alarm. It must not
    survive anywhere that can stamp or print a version."""
    for rel in BUILD_PLUMBING:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "0.6.0" not in text, (
            f"{rel} still carries the release-shaped 0.6.0 fallback")
