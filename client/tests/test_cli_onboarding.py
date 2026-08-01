from __future__ import annotations

import subprocess

from firekeep_client import cli
from firekeep_client.transport import TransportError


def _server_bundle(path):
    path.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", "docker-compose.yml", ".env.example"):
        (path / name).write_text("# test\n", encoding="utf-8")


def test_init_runs_the_existing_server_installer(tmp_path, monkeypatch, capsys):
    _server_bundle(tmp_path)
    calls = []
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert cli.main(["init", "--server-dir", str(tmp_path), "--pull", "--office"]) == 0
    command, kwargs = calls[0]
    assert command == [
        "/usr/bin/bash", str(tmp_path / "install.sh"), "--pull", "--office",
    ]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["check"] is True
    assert "server provisioned" in capsys.readouterr().out


def test_init_fails_without_a_server_bundle(tmp_path, monkeypatch, capsys):
    def unavailable(*args, **kwargs):
        raise cli.serverinit.ServerInitError("release unavailable")

    monkeypatch.setattr(cli.serverinit, "download_bundle", unavailable)
    assert cli.main(["init", "--server-dir", str(tmp_path)]) == 2
    assert "release unavailable" in capsys.readouterr().err


def test_init_downloads_public_bundle_and_automatically_pulls_images(
    tmp_path, monkeypatch, capsys
):
    destination = tmp_path / "server"
    calls = []

    def download(base, target, **kwargs):
        assert base == "https://dist.example"
        assert target == destination
        _server_bundle(destination)
        (destination / "SERVER_BUNDLE.json").write_text("{}", encoding="utf-8")
        return destination

    monkeypatch.setattr(cli.serverinit, "download_bundle", download)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    assert cli.main([
        "init", "--server-dir", str(destination), "--dist-base", "https://dist.example"
    ]) == 0
    assert calls[0][0] == ["/usr/bin/bash", str(destination / "install.sh"), "--pull"]
    assert "verified server bundle" in capsys.readouterr().out


def test_init_preserves_installer_exit_status(tmp_path, monkeypatch, capsys):
    _server_bundle(tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/bash")

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(cli.subprocess, "run", fail)
    assert cli.main(["init", "--server-dir", str(tmp_path)]) == 7
    assert "exited with status 7" in capsys.readouterr().err


def test_login_self_hosted_server_points_to_join(monkeypatch, capsys):
    def missing_metadata(*args, **kwargs):
        raise TransportError("not found", status=404)

    monkeypatch.setattr(cli, "get_json", missing_metadata)
    assert cli.main(["login", "https://firekeep.example"]) == 2
    assert "firekeep join <code>" in capsys.readouterr().out


def test_login_rejects_credentials_in_url(capsys):
    assert cli.main(["login", "https://alice:secret@firekeep.example"]) == 2
    assert "without credentials" in capsys.readouterr().err


def test_login_detects_but_does_not_fake_hosted_oauth(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "get_json",
        lambda *args, **kwargs: {"authorization_servers": ["https://auth.example"]},
    )
    assert cli.main(["login", "https://firekeep.example/api/cortex"]) == 2
    captured = capsys.readouterr()
    assert "hosted OAuth" in captured.err
    assert "not included" in captured.err
