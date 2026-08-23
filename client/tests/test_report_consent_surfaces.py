import builtins
import configparser

import pytest

from firekeep_client import cli


def test_apply_flags_report_failures_writes_true():
    cfg = configparser.ConfigParser()

    class Args:
        agent_id = None
        host = None
        dist_base = None
        report_failures = True
    assert cli._apply_flags(cfg, Args()) is True
    assert cfg.get("report", "failures") == "true"


def test_apply_flags_env_prefill_from_bootstrap(monkeypatch):
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "0")
    cfg = configparser.ConfigParser()

    class Args:
        agent_id = None
        host = None
        dist_base = None
        report_failures = False
    assert cli._apply_flags(cfg, Args()) is True
    assert cfg.get("report", "failures") == "false"


def test_cmd_doctor_asks_once_after_output_and_preserves_exit_code(
        tmp_path, monkeypatch, capsys):
    """Spec, 'Where the asks live': ask AFTER the rows; EOF leaves [report]
    absent and the exit code untouched; a recorded answer is never re-asked."""
    cfg_path = tmp_path / "config"
    cfg_path.write_text("[identity]\nagent_id = t\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_config_path", lambda: cfg_path)
    monkeypatch.setattr(cli, "run_doctor", lambda: [("cortex", "fail", "x")])
    monkeypatch.setattr(cli, "_generic_hint", lambda: None)
    monkeypatch.setattr("sys.stdin",
                        type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)

    class Args:
        report = False
    assert cli.cmd_doctor(Args()) == 1                       # fail row -> rc 1, EOF didn't change it
    assert "[report]" not in cfg_path.read_text()            # EOF recorded nothing
    out = capsys.readouterr().out
    assert out.index("[FAIL] cortex") < out.index("Send anonymous failure reports")

    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    assert cli.cmd_doctor(Args()) == 1
    assert "failures = true" in cfg_path.read_text()

    monkeypatch.setattr(builtins, "input",
                        lambda prompt="": pytest.fail("re-asked after answer"))
    assert cli.cmd_doctor(Args()) == 1
