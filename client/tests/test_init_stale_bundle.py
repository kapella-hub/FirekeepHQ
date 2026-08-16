"""`firekeep init` must not reuse a STALE managed server bundle.

The managed cache at ~/.firekeep/server was reused whenever install.sh +
docker-compose.yml + .env.example existed, and only re-downloaded when --version
was passed. So a retry after a failed init silently kept a superseded bundle — a
real dead-end shipped once (the published bundle moved v0.4.4 -> v0.4.5 and a
retry stayed on v0.4.4). Now the cache is refreshed to latest when it is older,
the network check never blocks init, and a source checkout is never touched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from firekeep_client import cli, serverinit


def _write_bundle(path: Path, version: str, *, git: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", "docker-compose.yml", ".env.example"):
        (path / name).write_text("# test\n", encoding="utf-8")
    (path / "SERVER_BUNDLE.json").write_text(
        json.dumps({"version": version, "distribution": "container-images"}),
        encoding="utf-8",
    )
    if git:
        (path / ".git").mkdir()
    return path


def _manifest(version: str) -> serverinit.ServerManifest:
    return serverinit.ServerManifest(
        version, f"firekeep-server-{version}.tar.gz", "ab" * 32
    )


@pytest.fixture
def init_env(tmp_path, monkeypatch):
    """A managed ~/.firekeep/server cache at v0.4.4, with the server installer and
    the enrolment tail stubbed so cmd_init exercises only the refresh decision."""
    home = tmp_path / ".firekeep"
    server = _write_bundle(home / "server", "v0.4.4")
    monkeypatch.setattr(cli, "_firekeep_home", lambda: home)
    monkeypatch.setattr(cli, "_server_source_dir", lambda explicit: server.resolve())
    monkeypatch.setattr(cli, "_server_dist_base", lambda explicit: "https://dist.example")
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})()
    )
    monkeypatch.setattr(cli, "_finish_server_provision", lambda root, bash, args: 0)
    return home, server


def test_stale_cache_is_refreshed(init_env, monkeypatch, capsys):
    home, server = init_env
    monkeypatch.setattr(serverinit, "fetch_manifest", lambda base, **k: _manifest("v0.4.5"))
    downloaded: dict[str, str] = {}

    def fake_download(base, dest, *, version=None, timeout=None):
        downloaded["version"] = version
        (dest / "SERVER_BUNDLE.json").write_text(
            json.dumps({"version": version}), encoding="utf-8"
        )
        return dest

    monkeypatch.setattr(serverinit, "download_bundle", fake_download)

    assert cli.main(["init"]) == 0
    assert downloaded["version"] == "v0.4.5"
    assert "refreshed server bundle v0.4.4 -> v0.4.5" in capsys.readouterr().out


def test_current_cache_is_not_refreshed(init_env, monkeypatch, capsys):
    home, server = init_env
    monkeypatch.setattr(serverinit, "fetch_manifest", lambda base, **k: _manifest("v0.4.4"))
    called: list[int] = []
    monkeypatch.setattr(serverinit, "download_bundle", lambda *a, **k: called.append(1))

    assert cli.main(["init"]) == 0
    assert called == [], "a current cache must not be re-downloaded"
    assert "refreshed" not in capsys.readouterr().out


def test_network_failure_falls_back_to_reuse_with_warning(init_env, monkeypatch, capsys):
    home, server = init_env

    def boom(base, **k):
        raise serverinit.ServerInitError("cannot reach the manifest")

    monkeypatch.setattr(serverinit, "fetch_manifest", boom)
    called: list[int] = []
    monkeypatch.setattr(serverinit, "download_bundle", lambda *a, **k: called.append(1))

    assert cli.main(["init"]) == 0
    assert called == [], "a transient network failure must reuse the cache, not re-download"
    assert "reusing the cached bundle v0.4.4" in capsys.readouterr().err


def test_source_checkout_is_never_auto_refreshed(tmp_path, monkeypatch):
    """A .git checkout sitting at ~/.firekeep/server must not be network-checked."""
    home = tmp_path / ".firekeep"
    server = _write_bundle(home / "server", "v0.4.4", git=True)
    monkeypatch.setattr(cli, "_firekeep_home", lambda: home)
    monkeypatch.setattr(cli, "_server_source_dir", lambda explicit: server.resolve())
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})()
    )
    monkeypatch.setattr(cli, "_finish_server_provision", lambda root, bash, args: 0)
    fetched: list[int] = []
    monkeypatch.setattr(serverinit, "fetch_manifest", lambda *a, **k: fetched.append(1))

    assert cli.main(["init"]) == 0
    assert fetched == [], "a source checkout must never be network-checked for a refresh"


def test_explicit_server_dir_is_never_auto_refreshed(tmp_path, monkeypatch):
    """--server-dir names a source location the user manages; leave it alone."""
    home = tmp_path / ".firekeep"
    monkeypatch.setattr(cli, "_firekeep_home", lambda: home)
    src = _write_bundle(tmp_path / "checkout", "v0.4.4")
    monkeypatch.setattr(cli, "_server_source_dir", lambda explicit: src.resolve())
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **kw: type("R", (), {"returncode": 0})()
    )
    monkeypatch.setattr(cli, "_finish_server_provision", lambda root, bash, args: 0)
    fetched: list[int] = []
    monkeypatch.setattr(serverinit, "fetch_manifest", lambda *a, **k: fetched.append(1))

    assert cli.main(["init", "--server-dir", str(src)]) == 0
    assert fetched == [], "--server-dir must never trigger an auto-refresh"
