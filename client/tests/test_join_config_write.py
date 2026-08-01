from __future__ import annotations

import configparser
import os
import stat

import pytest

from firekeep_client.config_write import ConfigWriteError, upsert_server


def values(kind="ports"):
    base = {
        "kind": kind,
        "scheme": "http" if kind == "ports" else "https",
        "verify_tls": "false" if kind == "ports" else "true",
        "api_key": "nxs_" + "a" * 64,
        "credential_id": "1" * 16,
        "device_id": "2" * 16,
        "credential_expires_at": "2026-10-29T00:00:00+00:00",
    }
    base["host" if kind == "ports" else "base_url"] = (
        "127.0.0.1" if kind == "ports" else "https://firekeep.example"
    )
    if kind == "paths":
        base["ca_path"] = "os"
    return base


def test_dist_survives_byte_for_byte_and_enrollment_fields_land(tmp_path):
    path = tmp_path / "config"
    dist = "[dist]\n# preserve this spacing\nbase_url  =  https://dist.example  \n"
    path.write_text(
        "[identity]\nagent_id = old\n\n[server]\nkind = ports\nhost = old\n\n" + dist,
        encoding="utf-8",
    )
    result = upsert_server(path, agent_id="bob-laptop", server=values())
    assert dist.rstrip() in path.read_text(encoding="utf-8")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(path, encoding="utf-8")
    assert cfg["server"]["credential_id"] == "1" * 16
    assert cfg["server"]["device_id"] == "2" * 16
    assert cfg["server"]["credential_expires_at"].startswith("2026-10-29")
    assert all("nxs_" not in change for change in result.changes)
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_kind_change_refused_without_force_and_accepted_with_it(tmp_path):
    path = tmp_path / "config"
    path.write_text("[identity]\nagent_id=x\n[server]\nkind=ports\nhost=x\n", encoding="utf-8")
    with pytest.raises(ConfigWriteError, match="--force"):
        upsert_server(path, agent_id="x", server=values("paths"))
    upsert_server(path, agent_id="x", server=values("paths"), force=True)
    assert "kind = paths" in path.read_text(encoding="utf-8")
