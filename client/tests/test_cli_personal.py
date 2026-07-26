"""`firekeep personal on|off|status|toggle` + the doctor personal-mode row."""
from __future__ import annotations

import pytest

from firekeep_client import cli, resolver


@pytest.fixture
def firekeep_home(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.write_text("[active]\nprofile = personal\n", encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.delenv("FIREKEEP_BYPASS", raising=False)
    monkeypatch.delenv("FIREKEEP_PERSONAL_TTL_HOURS", raising=False)
    return tmp_path


def test_on_then_off(firekeep_home, capsys):
    assert cli.main(["personal", "on"]) == 0
    assert resolver.is_personal() is True
    assert "ON" in capsys.readouterr().out

    assert cli.main(["personal", "off"]) == 0
    assert resolver.is_personal() is False
    assert "OFF" in capsys.readouterr().out


def test_toggle_flips(firekeep_home):
    assert resolver.is_personal() is False
    cli.main(["personal", "toggle"])
    assert resolver.is_personal() is True
    cli.main(["personal", "toggle"])
    assert resolver.is_personal() is False


def test_bare_personal_defaults_to_toggle(firekeep_home):
    cli.main(["personal"])
    assert resolver.is_personal() is True


def test_status_does_not_change_state(firekeep_home, capsys):
    cli.main(["personal", "on"])
    capsys.readouterr()
    assert cli.main(["personal", "status"]) == 0
    assert resolver.is_personal() is True  # unchanged
    assert "ON" in capsys.readouterr().out


def test_status_off_when_never_set(firekeep_home, capsys):
    assert cli.main(["personal", "status"]) == 0
    assert "OFF" in capsys.readouterr().out
    assert not resolver.personal_marker_path().exists()  # status must not create it


def test_doctor_row_warns_when_personal_on(firekeep_home):
    resolver.set_personal(True)
    name, status, detail = cli._check_personal_mode()
    assert name == "personal-mode"
    assert status == "warn"
    assert "ON" in detail


def test_doctor_row_ok_when_off(firekeep_home):
    name, status, detail = cli._check_personal_mode()
    assert status == "ok"
    assert "off" in detail.lower()


def test_doctor_row_warns_on_env_bypass(firekeep_home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    name, status, detail = cli._check_personal_mode()
    assert status == "warn"
    assert "FIREKEEP_BYPASS" in detail


def test_status_reports_env_bypass_not_false_team_mode(firekeep_home, monkeypatch, capsys):
    """Under FIREKEEP_BYPASS the session IS bypassed — status must not claim team mode."""
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert cli.main(["personal", "status"]) == 0
    out = capsys.readouterr().out
    assert "ON via FIREKEEP_BYPASS" in out
    assert "team mode (Firekeep active)" not in out


def test_off_under_env_bypass_owns_up_instead_of_claiming_team(firekeep_home, monkeypatch, capsys):
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert cli.main(["personal", "off"]) == 0          # clears the marker (none present)
    out = capsys.readouterr().out
    assert "FIREKEEP_BYPASS" in out                        # honest: env bypass still active
    assert "team mode (Firekeep active)" not in out
