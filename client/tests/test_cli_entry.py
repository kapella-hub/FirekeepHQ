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


def test_version_reports_both_client_and_server(tmp_path, monkeypatch, capsys):
    """`firekeep version` reports both sides and asserts nothing about their
    relationship. The old test stubbed the server as returning the CLIENT's
    version and asserted the word "both" — an impossible input describing an
    "in sync" state that cannot exist across two independent tag series."""
    _write_cfg(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "get_json", lambda url, **kw: {"version": "0.6.0"})
    assert cli.main(["version"]) == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "0.6.0" in out
    assert "skew" not in out.lower()   # no verdict is rendered any more


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
