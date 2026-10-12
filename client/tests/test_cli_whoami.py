"""`firekeep whoami` — identity facts for local clients (Firekeep Studio 0.5,
spec §7.1). Studio must never read ~/.firekeep/config itself: it holds the key."""
import json
import textwrap

import pytest

from firekeep_client import __version__, cli

SECRET = "fk_live_do_not_print_me"

CONFIG = textwrap.dedent(f"""\
    [identity]
    agent_id = tester

    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
    api_key = {SECRET}
""")


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    cfg = tmp_path / ".firekeep" / "config"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.setattr("firekeep_client.state._private", lambda _p: None)
    monkeypatch.setattr("firekeep_client.resolver.is_personal", lambda *a, **k: False)
    return cfg


def test_whoami_json_reports_identity(config_file, capsys):
    assert cli.main(["whoami", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "agent_id": "tester",
        "kit_version": __version__,
        "personal_mode": False,
        "server_configured": True,
    }


def test_whoami_never_prints_the_api_key(config_file, capsys):
    cli.main(["whoami", "--json"])
    cli.main(["whoami"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_whoami_honours_agent_id_env(config_file, monkeypatch, capsys):
    monkeypatch.setenv("FIREKEEP_AGENT_ID", "tester-studio-panel")
    cli.main(["whoami", "--json"])
    assert json.loads(capsys.readouterr().out)["agent_id"] == "tester-studio-panel"


def test_whoami_reports_personal_mode(config_file, monkeypatch, capsys):
    monkeypatch.setattr("firekeep_client.resolver.is_personal", lambda *a, **k: True)
    cli.main(["whoami", "--json"])
    assert json.loads(capsys.readouterr().out)["personal_mode"] is True


def test_whoami_without_config_is_null_not_a_crash(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "missing" / "config"))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.setattr("firekeep_client.resolver.is_personal", lambda *a, **k: False)
    assert cli.main(["whoami", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["agent_id"] is None
    assert data["server_configured"] is False


def test_whoami_plain_text_is_one_fact_per_line(config_file, capsys):
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "agent_id: tester"
    assert f"kit_version: {__version__}" in out
