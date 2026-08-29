import configparser
import datetime
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from firekeep_client import resolver


PORTS_DEFAULT = {
    "kind": "ports", "scheme": "http", "host": "127.0.0.1",
    "verify_tls": "false", "agent_id": "CHANGEME",
}


def _ports(host: str, *, agent: str = "alice", key: str = "") -> dict:
    out = {
        "kind": "ports", "scheme": "http", "host": host,
        "verify_tls": "false", "agent_id": agent,
    }
    if key:
        out["api_key"] = key
    return out


def _paths(base: str, ca_path: str, *, agent: str = "alice", key: str = "nxs") -> dict:
    return {
        "kind": "paths", "scheme": "https", "base_url": base,
        "verify_tls": "true", "ca_path": ca_path, "api_key": key,
        "agent_id": agent,
    }


def _write_legacy(path: Path, *, active: str = "personal", pins: dict | None = None,
                  sections: dict[str, dict] | None = None) -> bytes:
    cfg = configparser.ConfigParser()
    cfg["active"] = {"profile": active}
    for name, values in (sections or {"personal": _ports("198.51.100.7")}).items():
        cfg[name] = values
    if pins is not None:
        cfg["pins"] = pins
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        cfg.write(handle)
    return path.read_bytes()


def _backup_files(path: Path) -> list[Path]:
    return list(path.parent.glob(path.name + ".bak-profiles-*"))


def test_active_only_migrates_once_and_creates_one_private_backup(firekeep_env):
    path = firekeep_env["config_path"]
    original = _write_legacy(path, sections={"personal": _ports("198.51.100.7", key="nxs")})

    first = resolver.load_config()
    after_first = path.read_bytes()
    second = resolver.load_config()

    assert first.sections() == ["identity", "server"]
    assert second.sections() == ["identity", "server"]
    assert first["server"]["host"] == "198.51.100.7"
    assert first["identity"]["agent_id"] == "alice"
    assert "agent_id" not in first["server"]
    assert after_first == path.read_bytes()
    backups = _backup_files(path)
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    if os.name != "nt":
        assert backups[0].stat().st_mode & 0o777 == 0o600


def test_unreferenced_legacy_profile_sections_are_retired(firekeep_env):
    path = firekeep_env["config_path"]
    _write_legacy(path, sections={
        "personal": _ports("198.51.100.7", key="nxs"),
        "unused": _ports("203.0.113.9", key="other"),
    })

    cfg = resolver.load_config(path)

    assert cfg.sections() == ["identity", "server"]
    assert not cfg.has_section("unused")


def test_same_endpoint_from_active_and_pin_migrates_one_server(firekeep_env):
    path = firekeep_env["config_path"]
    _write_legacy(path, pins={"kiro": "office"}, sections={
        "personal": _ports("198.51.100.7", key="same"),
        "office": _ports("198.51.100.7", agent="other-agent", key="same"),
    })

    cfg = resolver.load_config()

    assert cfg.sections() == ["identity", "server"]
    assert cfg["identity"]["agent_id"] == "alice"


def test_different_endpoints_refuse_without_writing_or_backup(firekeep_env):
    path = firekeep_env["config_path"]
    original = _write_legacy(path, pins={"kiro": "office"}, sections={
        "personal": _ports("198.51.100.7", key="secret-alpha"),
        "office": _ports("203.0.113.9", key="secret-beta"),
    })

    with pytest.raises(resolver.ConfigMigrationConflict) as caught:
        resolver.load_config()

    assert "defines more than one" in str(caught.value)
    assert "server connection" in str(caught.value)
    assert "[personal]" in str(caught.value) and "from [active]" in str(caught.value)
    assert "[office]" in str(caught.value) and "from [pins] kiro" in str(caught.value)
    assert "secret-alpha" not in str(caught.value)
    assert "secret-beta" not in str(caught.value)
    assert path.read_bytes() == original
    assert _backup_files(path) == []


def test_unconfigured_active_with_configured_pin_migrates_the_pin(firekeep_env):
    path = firekeep_env["config_path"]
    _write_legacy(path, pins={"codex": "office"}, sections={
        "personal": PORTS_DEFAULT,
        "office": _ports("203.0.113.9", agent="worker", key="nxs"),
    })

    cfg = resolver.load_config()

    assert cfg["server"]["host"] == "203.0.113.9"
    assert cfg["identity"]["agent_id"] == "worker"


def test_unconfigured_pin_on_a_different_endpoint_is_not_silently_dropped(firekeep_env):
    path = firekeep_env["config_path"]
    original = _write_legacy(path, pins={"kiro": "office"}, sections={
        "personal": _ports("198.51.100.7", key="nxs"),
        "office": PORTS_DEFAULT,
    })

    with pytest.raises(resolver.ConfigMigrationConflict):
        resolver.load_config()

    assert path.read_bytes() == original


def test_fresh_legacy_skeleton_migrates_instead_of_bricking_checkout(firekeep_env):
    path = firekeep_env["config_path"]
    _write_legacy(path, sections={"personal": PORTS_DEFAULT})

    cfg = resolver.load_config()

    assert cfg["server"]["host"] == "127.0.0.1"
    assert cfg["identity"]["agent_id"] == "CHANGEME"


def test_active_missing_section_fails_and_names_it(firekeep_env):
    path = firekeep_env["config_path"]
    original = _write_legacy(path, active="missing", sections={
        "personal": _ports("198.51.100.7", key="nxs"),
    })

    with pytest.raises(resolver.ConfigMigrationConflict) as caught:
        resolver.load_config()

    assert "[active] names 'missing'" in str(caught.value)
    assert path.read_bytes() == original


def test_relative_and_absolute_ca_paths_resolving_to_same_file_are_one_endpoint(
        firekeep_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    ca = tmp_path / "root-ca.pem"
    ca.write_text("test")
    path = firekeep_env["config_path"]
    _write_legacy(path, pins={"kiro": "office"}, sections={
        "personal": _paths("https://fk.example", "root-ca.pem"),
        "office": _paths("https://fk.example", str(ca)),
    })

    cfg = resolver.load_config()

    assert cfg["server"]["base_url"] == "https://fk.example"


def test_dangling_pin_is_reported_but_configured_active_migrates(firekeep_env, capsys):
    path = firekeep_env["config_path"]
    _write_legacy(path, pins={"kiro": "gone"}, sections={
        "personal": _ports("198.51.100.7", key="nxs"),
    })

    cfg = resolver.load_config()

    assert cfg["server"]["host"] == "198.51.100.7"
    assert "kiro -> gone" in capsys.readouterr().err


@pytest.mark.parametrize("profile", ["personal", "office"])
def test_environment_profile_and_agent_do_not_change_migration(
        firekeep_env, monkeypatch, profile):
    path = firekeep_env["config_path"]
    _write_legacy(path, pins={"kiro": "office"}, sections={
        "personal": _ports("198.51.100.7", key="same"),
        "office": _ports("198.51.100.7", key="same"),
    })
    monkeypatch.setenv("FIREKEEP_PROFILE", profile)
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "environment-agent")

    cfg = resolver.load_config()

    assert cfg["identity"]["agent_id"] == "alice"


def test_dist_section_survives_byte_for_byte(firekeep_env):
    path = firekeep_env["config_path"]
    raw = (
        "[active]\nprofile = personal\n\n"
        "[personal]\nkind = ports\nscheme = http\nhost = 198.51.100.7\n"
        "verify_tls = false\nagent_id = alice\napi_key = nxs\n\n"
        "[dist]\n# keep this comment and spacing exactly\n"
        "base_url    = https://dist.example/releases\nauto_update = true\n"
    )
    path.write_text(raw, encoding="utf-8", newline="\n")
    dist_bytes = raw[raw.index("[dist]"):].encode()

    resolver.load_config()

    assert dist_bytes in path.read_bytes()


def test_four_concurrent_loaders_produce_one_valid_migration_and_backup(firekeep_env):
    path = firekeep_env["config_path"]
    _write_legacy(path, sections={"personal": _ports("198.51.100.7", key="nxs")})

    with ThreadPoolExecutor(max_workers=4) as pool:
        configs = list(pool.map(lambda _i: resolver.load_config(), range(4)))

    assert all(cfg.has_section("server") for cfg in configs)
    parsed = configparser.ConfigParser()
    assert parsed.read(path)
    assert parsed.has_section("server")
    assert len(_backup_files(path)) == 1


def _write_lock(path: Path, payload: str, *, age_seconds: float) -> Path:
    from firekeep_client import migrate
    lock = migrate.lock_path(path)
    lock.write_text(payload, encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(lock, (old, old))
    return lock


def test_stale_dead_owner_lock_is_broken(firekeep_env, monkeypatch):
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    _write_legacy(path)
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_STALE_SECONDS", 0.01)
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(seconds=60)).isoformat()
    lock = _write_lock(path, json.dumps({"pid": 99999999, "created_at": stamp}),
                       age_seconds=60)

    cfg = resolver.load_config()

    assert cfg.has_section("server")
    assert not lock.exists()


def test_empty_stale_lock_is_broken(firekeep_env, monkeypatch):
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    _write_legacy(path)
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_STALE_SECONDS", 0.01)
    lock = _write_lock(path, "", age_seconds=60)

    assert resolver.load_config().has_section("server")
    assert not lock.exists()


def test_live_owner_lock_is_waited_on_not_broken(firekeep_env, monkeypatch):
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    _write_legacy(path)
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_STALE_SECONDS", 0.01)
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(seconds=60)).isoformat()
    lock = _write_lock(path, json.dumps({"pid": os.getpid(), "created_at": stamp}),
                       age_seconds=60)

    released = threading.Event()
    def release():
        time.sleep(0.08)
        lock.unlink()
        released.set()
    thread = threading.Thread(target=release)
    thread.start()
    started = time.monotonic()
    try:
        cfg = resolver.load_config()
    finally:
        thread.join()

    assert released.is_set()
    assert time.monotonic() - started >= 0.05
    assert cfg.has_section("server")


# --- Windows DELETE_PENDING: PermissionError from a contended lock ----------
#
# On Windows a file unlinked while any handle is still open enters a
# DELETE_PENDING state: the directory entry survives, and CreateFile -- which
# os.open uses -- answers ERROR_ACCESS_DENIED, which Python raises as
# PermissionError, NOT FileExistsError. Every waiter racing the owner's release
# lands there, and the retry loop caught only FileExistsError, so the exception
# escaped _migration_lock -> migrate_config -> resolver.load_config() and
# crashed whatever command was loading config.
#
# Observed on the Windows CI runner, 2026-08-29:
#   PermissionError: [Errno 13] Permission denied:
#     '...\.firekeep\config.migration.lock'   (migrate.py:127, the os.open)
# with 'firekeep: migrated ... to one [server] connection' on stderr -- i.e. a
# sibling thread had just finished and released. POSIX has no delete-pending
# state, so there a PermissionError is always a genuine EACCES and must surface.


def _deny_for(target, real, exc=None):
    """Wrap `real`, raising PermissionError only for `target`."""
    def wrapper(p, *a, **kw):
        if str(p) == str(target):
            raise exc or PermissionError(13, "Permission denied", str(p))
        return real(p, *a, **kw)
    return wrapper


def test_transient_permission_error_while_acquiring_is_retried(firekeep_env, monkeypatch):
    """THE BUG: one delete-pending answer must not escape the loop."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    lock = migrate.lock_path(path)
    real_open, calls = os.open, {"n": 0}

    def flaky(p, *a, **kw):
        if str(p) == str(lock):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(13, "Permission denied", str(p))
        return real_open(p, *a, **kw)

    monkeypatch.setattr(migrate.os, "open", flaky)
    with migrate._migration_lock(path):
        pass
    assert calls["n"] >= 2, "must retry the acquire after a delete-pending denial"


def test_persistent_permission_error_surfaces_and_does_not_spin_forever(
        firekeep_env, monkeypatch):
    """A genuine EACCES -- read-only ~/.firekeep, a restrictive ACL -- must still
    reach the caller. Retrying it forever would convert this crash into a silent
    hang inside load_config(), which is strictly worse than the bug being fixed."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_CONTENDED_SECONDS", 0.2, raising=False)
    monkeypatch.setattr(
        migrate.os, "open",
        _deny_for(migrate.lock_path(path), os.open))
    started = time.monotonic()
    with pytest.raises(PermissionError):
        with migrate._migration_lock(path):
            pass
    assert time.monotonic() - started < 10, "budget must bound the retrying"


def test_permission_error_releasing_the_lock_does_not_propagate(firekeep_env, monkeypatch):
    """Release is best-effort. A waiter's transient handle can block the owner's
    unlink on Windows; the lock then lingers and _lock_is_stale reclaims it after
    MIGRATION_LOCK_STALE_SECONDS. Crashing the owner AFTER its migration already
    succeeded would be the worse outcome."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    monkeypatch.setattr(
        Path, "unlink",
        _deny_for(migrate.lock_path(path), Path.unlink), raising=True)
    with migrate._migration_lock(path):
        pass  # must not raise


def test_undeletable_stale_lock_surfaces_rather_than_looping(firekeep_env, monkeypatch):
    """The third site: stale-lock recovery also unlinks, and also only tolerated
    FileNotFoundError. If that unlink can never succeed the loop must end, not
    spin."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_STALE_SECONDS", 0.01)
    monkeypatch.setattr(migrate, "MIGRATION_LOCK_CONTENDED_SECONDS", 0.2, raising=False)
    lock = migrate.lock_path(path)
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(seconds=60)).isoformat()
    lock.write_text(json.dumps({"pid": 99999999, "created_at": stamp}), encoding="utf-8")
    old = time.time() - 60
    os.utime(lock, (old, old))
    monkeypatch.setattr(Path, "unlink", _deny_for(lock, Path.unlink))
    started = time.monotonic()
    with pytest.raises(PermissionError):
        with migrate._migration_lock(path):
            pass
    assert time.monotonic() - started < 10, "must not loop on an undeletable stale lock"


def test_ordinary_contention_still_waits_rather_than_failing(firekeep_env):
    """Regression lock: FileExistsError is honest contention, never a denial, and
    must keep its unbounded wait -- a live owner may legitimately hold the lock
    longer than the PermissionError budget."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    seen = []

    def worker():
        with migrate._migration_lock(path):
            seen.append("held")
            time.sleep(0.15)

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)
    with migrate._migration_lock(path):
        seen.append("second")
    t.join()
    assert seen == ["held", "second"]


def test_write_atomic_survives_an_undeletable_temp_file(firekeep_env, monkeypatch):
    """Fourth site, same root cause. On Windows a scanner holding the freshly
    written temp file blocks its deletion. That happens in a `finally`, so raising
    would both crash an already-successful migration and mask any exception that
    sent us there. The temp file is garbage; the migration is not."""
    from firekeep_client import migrate
    path = firekeep_env["config_path"]
    real_unlink = Path.unlink
    payload = b'[server]\nkind = ports\n'

    def deny_temp(self, *a, **kw):
        if ".migrate-" in self.name:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", deny_temp)
    migrate._write_atomic(path, payload)
    assert path.read_bytes() == payload
