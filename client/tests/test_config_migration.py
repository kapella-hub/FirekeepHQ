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
