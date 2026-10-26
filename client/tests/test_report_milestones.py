"""Install milestones: the success half of the funnel, under their own consent
key ([report] installs). Machines enrolled under the failures-only wording must
never send one, and an existing machine opting in later must not count as a
new install."""
import builtins
import configparser
import json

import pytest

from firekeep_client import cli, report


def _cfg(text=""):
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


ON = "[report]\nfailures = true\ninstalls = true\n"


@pytest.fixture(autouse=True)
def report_dir(tmp_path, monkeypatch):
    for var in ("FIREKEEP_NO_FAILURE_REPORT", "FIREKEEP_FAILURE_REPORT",
                "FIREKEEP_REPORT_CONSENT", "FIREKEEP_INSTALL_REPORT_CONSENT",
                "FIREKEEP_BYPASS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FIREKEEP_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(report, "_ANSWERED_HERE", False)
    return tmp_path


@pytest.fixture
def posted(monkeypatch):
    sent = []

    def fake_post(url, body, timeout):
        sent.extend(body["events"])
        return {"accepted": [e["id"] for e in body["events"]], "rejected": []}
    monkeypatch.setattr(report, "_post", fake_post)
    return sent


def test_failures_only_enrollee_never_sends_milestones(posted, monkeypatch):
    monkeypatch.setenv("FIREKEEP_INSTALL_REPORT_CONSENT", "1")
    cfg = _cfg("[report]\nfailures = true\n")
    assert report.milestones_enabled(cfg) is False
    report.milestone("installed", cfg=cfg)
    assert posted == []


def test_failure_env_opt_in_does_not_enable_milestones(monkeypatch):
    """CI sets FIREKEEP_FAILURE_REPORT=1; that must never count as an install."""
    monkeypatch.setenv("FIREKEEP_FAILURE_REPORT", "1")
    assert report.milestones_enabled(_cfg()) is False


def test_opt_out_and_personal_mode_silence_milestones(monkeypatch):
    monkeypatch.setenv("FIREKEEP_NO_FAILURE_REPORT", "1")
    assert report.milestones_enabled(_cfg(ON)) is False
    monkeypatch.delenv("FIREKEEP_NO_FAILURE_REPORT")
    monkeypatch.setenv("FIREKEEP_BYPASS", "1")
    assert report.milestones_enabled(_cfg(ON)) is False


def test_installed_sends_once_on_fresh_consented_install(posted, monkeypatch):
    monkeypatch.setenv("FIREKEEP_INSTALL_REPORT_CONSENT", "1")
    cfg = _cfg(ON)
    report.milestone("installed", cfg=cfg)
    report.milestone("installed", cfg=cfg)
    assert [e["stage"] for e in posted] == ["installed"]
    ev = posted[0]
    assert set(ev) == {"id", "kind", "stage", "os", "arch", "client", "py"}
    assert ev["kind"] == "milestone" and "error" not in ev


def test_installed_not_sent_without_answer_in_this_run(posted):
    """An update re-exec or a later opt-in carries no fresh answer."""
    report.milestone("installed", cfg=_cfg(ON))
    assert posted == []


def test_connected_requires_installed_first(posted, monkeypatch):
    cfg = _cfg(ON)
    report.milestone("connected", cfg=cfg)
    assert posted == []
    monkeypatch.setenv("FIREKEEP_INSTALL_REPORT_CONSENT", "1")
    report.milestone("installed", cfg=cfg)
    report.milestone("connected", cfg=cfg)
    report.milestone("connected", cfg=cfg)
    assert [e["stage"] for e in posted] == ["installed", "connected"]


def test_unknown_stage_dropped(posted):
    assert report.build_milestone("uninstalled") is None


def test_unwritable_marker_means_no_event(posted, monkeypatch, report_dir):
    monkeypatch.setenv("FIREKEEP_INSTALL_REPORT_CONSENT", "1")
    (report_dir / "report-milestones.json").write_text("[]", encoding="utf-8")
    report.milestone("installed", cfg=_cfg(ON))
    assert posted == []


def test_flush_sends_each_event_only_under_its_own_consent(posted, report_dir):
    spool = report_dir / "report-spool.jsonl"
    failure = report.build_event("install", "create-venv", error="disk-full")
    mile = report.build_milestone("installed")
    spool.write_text(json.dumps(failure) + "\n" + json.dumps(mile) + "\n",
                     encoding="utf-8")
    report.flush(cfg=_cfg("[report]\nfailures = false\ninstalls = true\n"))
    assert [e["kind"] for e in posted] == ["milestone"]


def test_interactive_yes_records_both_keys_and_marks_fresh(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")
    assert report.ask_consent(cfg) is True
    assert cfg.get("report", "failures") == "true"
    assert cfg.get("report", "installs") == "true"
    assert report._fresh_install_consented() is True


def test_interactive_no_records_both_keys_false(monkeypatch):
    cfg = _cfg()
    monkeypatch.setattr("sys.stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(builtins, "input", lambda prompt="": "n")
    report.ask_consent(cfg)
    assert cfg.get("report", "installs") == "false"
    assert report._fresh_install_consented() is False


def test_bootstrap_relay_without_install_answer_records_failures_only(monkeypatch):
    """FIREKEEP_FAILURE_REPORT=1 in the bootstrap exports FIREKEEP_REPORT_CONSENT
    but not FIREKEEP_INSTALL_REPORT_CONSENT: installs stays unanswered."""
    cfg = _cfg()
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "1")

    class Args:
        pass
    assert cli._apply_flags(cfg, Args()) is True
    assert cfg.get("report", "failures") == "true"
    assert not cfg.has_option("report", "installs")


def test_bootstrap_relay_with_install_answer_records_both(monkeypatch):
    cfg = _cfg()
    monkeypatch.setenv("FIREKEEP_REPORT_CONSENT", "1")
    monkeypatch.setenv("FIREKEEP_INSTALL_REPORT_CONSENT", "1")

    class Args:
        pass
    cli._apply_flags(cfg, Args())
    assert cfg.get("report", "installs") == "true"


def test_prompt_states_the_milestone_scope():
    assert "Once when this install succeeds" in report.CONSENT_PROMPT
    assert "[report] installs = false" in report.CONSENT_PROMPT


def test_ps1_relays_install_consent_only_from_the_prompt():
    """No PTY harness for PowerShell here: pin the shape textually. The
    FIREKEEP_FAILURE_REPORT branch must not set the install relay; the Read-Host
    branch must set it on both answers, with the milestone wording."""
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "bootstrap" / "install.ps1").read_text(
        encoding="utf-8")
    env_branch = text[text.index("} elseif ($env:FIREKEEP_FAILURE_REPORT) {"):
                      text.index("} elseif ([Environment]::UserInteractive")]
    assert "FIREKEEP_INSTALL_REPORT_CONSENT" not in env_branch
    prompt_branch = text[text.index("} elseif ([Environment]::UserInteractive"):
                         text.index("function Send-FailureReport")]
    assert "Once when this install" in prompt_branch
    assert prompt_branch.count("$env:FIREKEEP_INSTALL_REPORT_CONSENT = ") == 2
