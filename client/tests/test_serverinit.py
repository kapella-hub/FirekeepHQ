from __future__ import annotations

import hashlib
import io
import json
import tarfile

import pytest

from firekeep_client import serverinit


def _archive(path, *, version="v1.2.3", unsafe_name=None):
    root = f"firekeep-server-{version}"
    files = {
        "install.sh": b"#!/bin/sh\n",
        "docker-compose.yml": b"services: {}\n",
        ".env.example": b"IMAGE_TAG=dev\n",
        "SERVER_BUNDLE.json": json.dumps(
            {"version": version, "distribution": "container-images"}
        ).encode(),
    }
    with tarfile.open(path, "w:gz") as tar:
        if unsafe_name:
            info = tarfile.TarInfo(unsafe_name)
            info.size = 4
            tar.addfile(info, io.BytesIO(b"nope"))
        for name, body in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(body)
            info.mode = 0o755 if name == "install.sh" else 0o644
            tar.addfile(info, io.BytesIO(body))


def test_fetch_manifest_uses_latest_public_server_path(monkeypatch):
    seen = {}

    def fake(url, **kwargs):
        seen["url"] = url
        return {
            "version": "v1.2.3",
            "file": "firekeep-server-v1.2.3.tar.gz",
            "sha256": "ab" * 32,
        }

    monkeypatch.setattr(serverinit, "get_json", fake)
    result = serverinit.fetch_manifest("https://dist.example/")
    assert seen["url"] == "https://dist.example/server/latest/server.json"
    assert result.version == "v1.2.3"


def test_download_bundle_verifies_then_publishes_complete_directory(tmp_path, monkeypatch):
    archive = tmp_path / "source.tar.gz"
    _archive(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        serverinit,
        "fetch_manifest",
        lambda *a, **k: serverinit.ServerManifest(
            "v1.2.3", "firekeep-server-v1.2.3.tar.gz", digest
        ),
    )

    def fake_download(url, dest, *, sha256, **kwargs):
        assert sha256 == digest
        dest.write_bytes(archive.read_bytes())
        return dest

    monkeypatch.setattr(serverinit.updater, "download", fake_download)
    destination = tmp_path / "installed"
    assert serverinit.download_bundle("https://dist.example", destination) == destination
    assert (destination / "install.sh").is_file()
    assert json.loads((destination / "SERVER_BUNDLE.json").read_text())["version"] == "v1.2.3"


def test_safe_extract_refuses_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    _archive(archive, unsafe_name="firekeep-server-v1.2.3/../../escape")
    with pytest.raises(serverinit.ServerInitError, match="unsafe entry"):
        serverinit._safe_extract(archive, tmp_path / "out", "firekeep-server-v1.2.3")
    assert not (tmp_path / "escape").exists()


def test_safe_extract_refuses_windows_separator_traversal(tmp_path):
    archive = tmp_path / "unsafe-windows.tar.gz"
    _archive(archive, unsafe_name="firekeep-server-v1.2.3/..\\escape")
    with pytest.raises(serverinit.ServerInitError, match="unsafe entry"):
        serverinit._safe_extract(archive, tmp_path / "out", "firekeep-server-v1.2.3")


def test_versioned_update_preserves_runtime_state_and_moves_backups(tmp_path, monkeypatch):
    destination = tmp_path / "server"
    _archive(tmp_path / "old.tar.gz", version="v1.2.2")
    serverinit._safe_extract(
        tmp_path / "old.tar.gz", tmp_path / "old", "firekeep-server-v1.2.2"
    ).replace(destination)
    (destination / ".env").write_text("IMAGE_TAG=v1.2.2\nSECRET=keep\n", encoding="utf-8")
    (destination / "dashboard").mkdir(exist_ok=True)
    (destination / "dashboard/.htpasswd").write_text("admin:hash\n", encoding="utf-8")
    (destination / "backups").mkdir()
    (destination / "backups/keep.tar.gz").write_bytes(b"backup")

    archive = tmp_path / "new.tar.gz"
    _archive(archive, version="v1.2.3")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        serverinit,
        "fetch_manifest",
        lambda *a, **k: serverinit.ServerManifest(
            "v1.2.3", "firekeep-server-v1.2.3.tar.gz", digest
        ),
    )
    def fake_download(url, dest, **kwargs):
        dest.write_bytes(archive.read_bytes())
        return dest

    monkeypatch.setattr(serverinit.updater, "download", fake_download)

    serverinit.download_bundle("https://dist.example", destination, version="v1.2.3")
    env = (destination / ".env").read_text(encoding="utf-8")
    assert "IMAGE_TAG=v1.2.3" in env
    assert "SECRET=keep" in env
    assert (destination / "dashboard/.htpasswd").read_text() == "admin:hash\n"
    assert (destination / "backups/keep.tar.gz").read_bytes() == b"backup"
    assert (tmp_path / ".server.previous-v1.2.2").is_dir()
