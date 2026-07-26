import textwrap
from pathlib import Path

from firekeep_client import cli, __version__

CLIENT_ROOT = Path(__file__).resolve().parent.parent
PERSONAL = textwrap.dedent("""\
    [active]
    profile = personal
    [personal]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    agent_id = tester
""")


def _write_cfg(tmp_path, monkeypatch):
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(PERSONAL, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))


def test_version_prints_version_and_skew_in_sync(tmp_path, monkeypatch, capsys):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": __version__})
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "skew:" in out
    assert "both" in out  # in-sync detail from _check_skew


def test_version_reports_skew_on_mismatch(tmp_path, monkeypatch, capsys):
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": "8.8.8"})
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert "8.8.8" in out


def test_version_no_config_is_soft(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "missing" / "config"))
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "skew:" in out


def test_posix_install_entry_invokes_cli():
    script = (CLIENT_ROOT / "install").read_text(encoding="utf-8")
    assert script.startswith("#!/bin/sh")
    assert "firekeep_client.cli install" in script
    assert "PYTHONPATH" in script


def test_windows_install_entry_invokes_cli():
    script = (CLIENT_ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "firekeep_client.cli install" in script
    assert "PYTHONPATH" in script
    assert "$LASTEXITCODE" in script
