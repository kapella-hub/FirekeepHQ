"""Tri-state consent (spec Decision 1): unset = OFF, never mirrors
autoupdate.is_enabled's default-ON."""
import builtins
import configparser

import pytest

from firekeep_client import report


def _cfg(text=""):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("FIREKEEP_NO_FAILURE_REPORT", "FIREKEEP_FAILURE_REPORT",
                "FIREKEEP_REPORT_CONSENT", "FIREKEEP_BYPASS"):
        monkeypatch.delenv(var, raising=False)


def test_unset_means_off():
    assert report.is_enabled(_cfg()) is False
    assert report.is_enabled(_cfg("[report]\n")) is False
    assert report.is_enabled(_cfg("[report]\nfailures =\n")) is False


def test_explicit_true_means_on_and_false_off():
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is True
    assert report.is_enabled(_cfg("[report]\nfailures = false\n")) is False


def test_env_off_beats_config_true(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_FAILURE_REPORT", "1")
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is False


def test_env_on_is_session_opt_in(monkeypatch):
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    assert report.is_enabled(_cfg()) is True


def test_personal_mode_silences(monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert report.is_enabled(_cfg("[report]\nfailures = true\n")) is False


def test_ask_consent_eof_records_nothing(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    def raise_eof(prompt=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert report.ask_consent(cfg) is False
    assert report.has_answer(cfg) is False


def test_ask_consent_enter_is_yes(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "true"


def test_ask_consent_no(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "false"


def test_ask_consent_env_prefill_no_tty(monkeypatch):
    """The bootstrap answers once; the wizard hand-off must not re-ask
    (FIREKEEP_REPORT_CONSENT set by install.sh/install.ps1, Task 8/9)."""
    cfg = _cfg()
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "1")
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: False})())
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "true"


def test_ask_consent_never_reasks(monkeypatch):
    cfg = _cfg("[report]\nfailures = false\n")
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("re-asked"))
    assert report.ask_consent(cfg) is False


def test_ask_consent_keyboard_interrupt_records_nothing(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    def raise_keyboard_interrupt(prompt=""):
        raise KeyboardInterrupt
    monkeypatch.setattr(builtins, "input", raise_keyboard_interrupt)
    assert report.ask_consent(cfg) is False
    assert report.has_answer(cfg) is False


def test_is_enabled_load_config_failure(monkeypatch):
    """When resolver.load_config raises, is_enabled(None) fails closed to False."""
    def raise_error():
        raise RuntimeError("config read failed")
    monkeypatch.setattr("firekeep_client.resolver.load_config", raise_error)
    assert report.is_enabled(None) is False
