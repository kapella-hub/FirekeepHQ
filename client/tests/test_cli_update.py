import configparser
import os

import pytest

from firekeep_client import cli, updater


@pytest.fixture
def update_env(tmp_path, monkeypatch):
    home = tmp_path / ".firekeep"
    home.mkdir()
    cfg = home / "config"
    cfg.write_text(
        "[active]\nprofile = personal\n"
        "[personal]\nkind = ports\nscheme = http\nhost = 10.0.0.1\n"
        "verify_tls = false\nagent_id = tester\n"
        "[dist]\nbase_url = http://gl/rel\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    execs = []
    monkeypatch.setattr(cli, "_exec_bootstrap",
                        lambda script, version, base: execs.append((script, version, base)))
    return {"home": home, "execs": execs}


def test_update_auto_off_writes_config_and_does_not_update(update_env, monkeypatch):
    # --auto only flips the preference; it must NOT fetch a manifest or exec the bootstrap.
    def boom(*a, **k):
        raise AssertionError("--auto must not run an update")

    monkeypatch.setattr(updater, "fetch_manifest", boom)
    rc = cli.main(["update", "--auto", "off"])
    assert rc == 0
    assert update_env["execs"] == []
    cfg = configparser.ConfigParser()
    cfg.read(update_env["home"] / "config")
    assert cfg["dist"]["auto_update"] == "false"


def test_update_auto_on_writes_config(update_env):
    cli.main(["update", "--auto", "off"])
    cli.main(["update", "--auto", "on"])
    cfg = configparser.ConfigParser()
    cfg.read(update_env["home"] / "config")
    assert cfg["dist"]["auto_update"] == "true"


def test_exec_bootstrap_passes_the_dist_base_through(monkeypatch, tmp_path):
    """install.sh fail-louds on an unset FIREKEEP_DIST_BASE, and an exec'd script inherits none
    of our config — so the handoff MUST carry it or every update dies on the first line."""
    seen = {}
    monkeypatch.setattr(cli.os, "execve",
                        lambda path, argv, env: seen.update(env) or (_ for _ in ()).throw(SystemExit(0)))
    monkeypatch.setattr(cli.os, "name", "posix")
    with pytest.raises(SystemExit):
        cli._exec_bootstrap(tmp_path / "install.sh", "1.2.3", "http://gl/rel")
    assert seen["FIREKEEP_DIST_BASE"] == "http://gl/rel"
    assert seen["FIREKEEP_VERSION"] == "1.2.3"


def _manifest(monkeypatch, version):
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            version, bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )


def _fake_download(seen=None):
    """Stand-in for updater.download() that behaves like the real one: it creates dest's
    parent. Every fake in this file uses it — a stub that silently skipped the mkdir would
    force production code to add a redundant one, letting the test shape the source."""
    def _download(url, dest, *, sha256, **kw):
        if seen is not None:
            seen["url"], seen["sha256"] = url, sha256
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("#!/bin/sh\n")
        return dest
    return _download


def test_update_verifies_the_bootstrap_before_executing_it(update_env, monkeypatch):
    """We are about to EXECUTE this script. Verifying uv inside install.sh while exec'ing an
    unverified install.sh would be theatre — assert the manifest's hash reaches download()."""
    _manifest(monkeypatch, "9.9.9")
    seen = {}
    monkeypatch.setattr(cli.updater, "download", _fake_download(seen))
    assert cli.main(["update"]) == 0
    expected = "ef" * 32 if os.name == "nt" else "cd" * 32
    assert seen["sha256"] == expected


def test_update_check_only_reports_and_changes_nothing(update_env, monkeypatch, capsys):
    _manifest(monkeypatch, "9.9.9")
    rc = cli.main(["update", "--check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "9.9.9" in out and "firekeep update" in out
    assert update_env["execs"] == [], "--check must never exec the bootstrap"


def test_update_when_already_current_does_nothing(update_env, monkeypatch, capsys):
    from firekeep_client import __version__
    _manifest(monkeypatch, __version__)
    rc = cli.main(["update"])
    assert rc == 0
    assert "already up to date" in capsys.readouterr().out
    assert update_env["execs"] == []


def test_update_downloads_bootstrap_and_execs_it(update_env, monkeypatch):
    _manifest(monkeypatch, "9.9.9")
    seen = {}
    monkeypatch.setattr(cli.updater, "download", _fake_download(seen))
    rc = cli.main(["update"])
    assert rc == 0
    assert seen["url"].endswith(("/install.sh", "/install.ps1"))
    assert len(update_env["execs"]) == 1
    _script, version, base = update_env["execs"][0]
    assert version == "9.9.9"
    assert base == "http://gl/rel"


def test_update_to_pins_a_version_and_allows_rollback(update_env, monkeypatch):
    """--to is also the rollback: there is no second mechanism to keep working."""
    _manifest(monkeypatch, "9.9.9")
    # Same fake as the other tests: it creates dest's parent, exactly as the real
    # updater.download() does. A stub that skips the mkdir would push production code into
    # growing a redundant one just to satisfy the stub — the test would be shaping the
    # source, not checking it.
    monkeypatch.setattr(cli.updater, "download", _fake_download())
    rc = cli.main(["update", "--to", "0.0.1"])
    assert rc == 0
    _script, version, _base = update_env["execs"][0]
    assert version == "0.0.1", "--to must win over the manifest's latest"


def test_update_on_a_malformed_manifest_version_fails_loud(update_env, monkeypatch, capsys):
    """fetch_manifest only checks that `version` is a str, not that it parses — so a bad
    release (the manifest is fetched over plain HTTP, unsigned) reaches is_newer() and
    raises. A teammate must get `firekeep: ...`, never a raw traceback."""
    monkeypatch.setattr(
        cli.updater, "fetch_manifest",
        lambda base, **kw: updater.Manifest(
            "not-a-version", bootstrap_sha256="cd" * 32, bootstrap_ps1_sha256="ef" * 32,
        ),
    )
    rc = cli.main(["update"])
    assert rc == 1
    assert "unparseable version" in capsys.readouterr().err
    assert update_env["execs"] == []


def test_update_without_dist_base_is_fail_loud(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config"
    cfg.write_text("[active]\nprofile = personal\n[personal]\nagent_id = t\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    rc = cli.main(["update"])
    assert rc == 1
    assert "no [dist] base_url" in capsys.readouterr().err


def test_update_unreachable_manifest_is_fail_loud(update_env, monkeypatch, capsys):
    def boom(base, **kw):
        raise updater.UpdateError("cannot reach the release manifest at http://gl/rel/latest.json")

    monkeypatch.setattr(cli.updater, "fetch_manifest", boom)
    rc = cli.main(["update"])
    assert rc == 1
    assert "cannot reach" in capsys.readouterr().err
