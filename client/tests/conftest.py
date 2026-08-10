"""Shared client-test fixtures.

Every fixture isolates client state under ``tmp_path`` and monkeypatches the
three override env vars the frozen contracts define, so tests NEVER read or
write the real ``~/.firekeep``:
    FIREKEEP_CONFIG    -> resolver config path   (resolver.CONFIG_PATH override)
    FIREKEEP_CACHE_DIR -> state cache dir         (state.cache_dir override)
    FIREKEEP_LOG_DIR   -> hooklog dir             (hooklog.LOG_PATH override)
"""
import configparser
import importlib.util
import platform
from pathlib import Path

import pytest

# CI's client job (and the Windows job) runs on PURE stdlib + pytest — that is the
# point: the import-boundary guard must not itself install the deps it forbids
# (.github/workflows/ci.yml). The shim/decision suites, however, legitimately test
# the dep-bearing side of the boundary (httpx/mcp/anyio live in the shim by
# design), so in a deps-free environment they must be skipped at COLLECTION time —
# a top-level `import anyio` otherwise aborts the whole session before a single
# test runs. Everywhere the deps exist (dev machines, the office pipeline) these
# files still run in full.
_SHIM_DEPS = ("anyio", "httpx", "mcp")
_DEP_BEARING_TESTS = (
    "test_decision_bypass.py",
    "test_decision_server.py",
    "test_shim_bridge.py",
    "test_shim_bypass.py",
    "test_shim_fail_loud.py",
    "test_shim_identity.py",
    "test_shim_recovery.py",
    "test_shim_skeleton.py",
)
if any(importlib.util.find_spec(dep) is None for dep in _SHIM_DEPS):
    collect_ignore = list(_DEP_BEARING_TESTS)


def _uv_target():
    """The uv filename install.sh will ask for on THIS host, mirroring the ``case`` statement
    in client/bootstrap/install.sh exactly. Shared here (rather than defined separately in
    test_bootstrap_sh.py and test_e2e_bootstrap.py) so the two suites cannot drift apart --
    install.sh fetches `uv-<target>` and greps SHA256SUMS for ` uv-<target>$`, so a mismatched
    target name silently tests nothing rather than failing loudly.

    Raises KeyError on any (system, machine) pair outside the four supported targets; callers
    should catch it and skip rather than let an unsupported CI runner error out."""
    system, machine = platform.system(), platform.machine()
    target = {
        ("Darwin", "arm64"): "aarch64-apple-darwin",
        ("Darwin", "x86_64"): "x86_64-apple-darwin",
        ("Linux", "x86_64"): "x86_64-unknown-linux-gnu",
        ("Linux", "aarch64"): "aarch64-unknown-linux-gnu",
    }[(system, machine)]
    # Mirror install.sh's libc probe: on a musl host (Alpine test runner) the
    # script fetches the -musl uv, and a mapping frozen to -gnu would make the
    # artifact server serve a name the script never requests — tests would fail
    # on the fetch instead of on anything real.
    if system == "Linux" and Path(f"/lib/ld-musl-{machine}.so.1").exists():
        target = target.replace("-gnu", "-musl")
    return target


# Reusable profile bodies matching the frozen ~/.firekeep INI schema.
DEFAULT_PERSONAL = {
    "kind": "ports",
    "scheme": "http",
    "host": "198.51.100.7",
    "verify_tls": "false",
    "agent_id": "mogan",
}
DEFAULT_OFFICE = {
    "kind": "paths",
    "scheme": "https",
    "base_url": "https://firekeep.office.example",
    "verify_tls": "true",
    "ca_path": "~/.firekeep/firekeep-root-ca.crt",
    "api_key": "nxs_test_key_do_not_log",
    "agent_id": "mogan",
}


@pytest.fixture(autouse=True)
def _isolate_firekeep_home(tmp_path, monkeypatch):
    """Structural guarantee behind this module's docstring: NO test touches the real
    ~/.firekeep, whether or not it opted into a fixture below.

    It was opt-in before, and the tests that forgot leaked: the in-process dispatcher cases
    in tests/hooks/test_dispatcher.py left `dispatcher crashed: RuntimeError('boom')` in the
    developer's OWN ~/.firekeep/logs/hooks.log, where it reads exactly like a real hook failure.
    A test suite that fabricates evidence of production breakage is worse than a noisy one.

    Opt-in fixtures (firekeep_env, client_env) re-point these afterwards; autouse just means
    the floor is never the real home."""
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "_isolated" / "config"))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "_isolated" / "cache"))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(tmp_path / "_isolated" / "logs"))


@pytest.fixture
def firekeep_env(tmp_path, monkeypatch):
    """Point all client state at tmp_path and return the resolved paths."""
    home = tmp_path / ".firekeep"
    home.mkdir(parents=True, exist_ok=True)
    paths = {
        "home": home,
        "config_path": home / "config",
        "cache_dir": tmp_path / "cache",
        "log_dir": home / "logs",
    }
    monkeypatch.setenv("FIREKEEP_CONFIG", str(paths["config_path"]))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(paths["cache_dir"]))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(paths["log_dir"]))
    # Profile agent_id is authoritative unless a test opts into the override.
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    return paths


@pytest.fixture
def write_config(firekeep_env):
    """Return a helper that writes ~/.firekeep/config (INI) and returns its Path.

    Usage:
        from tests.conftest import DEFAULT_PERSONAL, DEFAULT_OFFICE
        cfg_path = write_config(active="personal", personal=DEFAULT_PERSONAL)
    """
    def _write(active="personal", personal=None, office=None):
        cfg = configparser.ConfigParser()
        cfg["active"] = {"profile": active}
        if personal is not None:
            cfg["personal"] = dict(personal)
        if office is not None:
            cfg["office"] = dict(office)
        with open(firekeep_env["config_path"], "w", encoding="utf-8") as fh:
            cfg.write(fh)
        return firekeep_env["config_path"]

    return _write
