"""`firekeep doctor --report` — opt-in, per-invocation, redacted.

The invariants under test:
  - no flag -> no network call, ever (the default path is silent).
  - --report -> exactly one POST, to the exact URL, with a payload that
    structurally CANNOT carry `detail` — the redaction drops the field
    entirely rather than scrubbing it, so a secret-shaped detail (a path, a
    hostname, a config value) must never appear anywhere in the sent body.
  - a failed send never changes doctor's exit code or hides its printed rows.
  - status values round-trip faithfully (ok/warn/fail all reach the payload).
"""
from __future__ import annotations

import json
import types

import pytest

from firekeep_client import cli
from firekeep_client.transport import TransportError


def _results(*rows):
    return list(rows)


def test_no_flag_makes_no_network_call(monkeypatch, capsys):
    def boom(*a, **k):
        raise AssertionError("post_json must not be called without --report")

    monkeypatch.setattr(cli, "post_json", boom)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: _results(("x", "ok", "fine")))
    assert cli.cmd_doctor(types.SimpleNamespace()) == 0
    assert "report" not in capsys.readouterr().out.lower()


def test_report_sends_exactly_one_post_to_the_exact_url(monkeypatch, capsys):
    calls = []

    def fake_post(url, body, **kw):
        calls.append((url, body))
        return None

    monkeypatch.setattr(cli, "post_json", fake_post)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: _results(("x", "ok", "fine")))
    cli.cmd_doctor(types.SimpleNamespace(report=True))

    assert len(calls) == 1
    url, body = calls[0]
    assert url == cli.DOCTOR_REPORT_URL == "https://firekeep.ai/doctor-report.php"
    assert body == {"client_version": cli.__version__, "checks": [{"id": "x", "status": "ok"}]}
    assert "report sent" in capsys.readouterr().out


def test_detail_never_reaches_the_payload(monkeypatch):
    """The redaction is structural, not a scrub — plant a detail string that
    LOOKS like it could leak (a Windows path, a hostname, a config value) and
    confirm it is nowhere in the JSON-serialized body, by construction."""
    secret_looking_detail = r"C:\Users\mogan\.firekeep\config: base_url=https://internal.example:8100 key=nxs_abc123"
    calls = []
    monkeypatch.setattr(cli, "post_json", lambda url, body, **kw: calls.append(body))
    monkeypatch.setattr(
        cli, "run_doctor",
        lambda cfg=None: _results(("client-version", "warn", secret_looking_detail)),
    )
    cli.cmd_doctor(types.SimpleNamespace(report=True))

    [body] = calls
    serialized = json.dumps(body)
    assert secret_looking_detail not in serialized
    assert "mogan" not in serialized
    assert "internal.example" not in serialized
    assert "nxs_abc123" not in serialized
    assert body["checks"] == [{"id": "client-version", "status": "warn"}]


def test_all_three_statuses_round_trip(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "post_json", lambda url, body, **kw: calls.append(body))
    monkeypatch.setattr(
        cli, "run_doctor",
        lambda cfg=None: _results(
            ("a", "ok", "-"), ("b", "warn", "-"), ("c", "fail", "-"),
        ),
    )
    cli.cmd_doctor(types.SimpleNamespace(report=True))
    [body] = calls
    assert body["checks"] == [
        {"id": "a", "status": "ok"},
        {"id": "b", "status": "warn"},
        {"id": "c", "status": "fail"},
    ]


def test_send_failure_does_not_change_exit_code_or_hide_rows(monkeypatch, capsys):
    def boom(*a, **k):
        raise TransportError("connection refused")

    monkeypatch.setattr(cli, "post_json", boom)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: _results(("x", "fail", "boom")))
    rc = cli.cmd_doctor(types.SimpleNamespace(report=True))

    assert rc == 1  # the FAIL row, not the send failure, decides this
    out = capsys.readouterr().out
    assert "[FAIL] x: boom" in out
    assert "report NOT sent" in out


def test_redact_for_report_drops_detail_structurally():
    payload = cli._redact_for_report([
        ("docdex", "warn", "1 source, last sync 4h ago, /Users/x/OneDrive"),
        ("client-version", "ok", "client 1.4.2 is current"),
    ])
    assert payload == {
        "client_version": cli.__version__,
        "checks": [
            {"id": "docdex", "status": "warn"},
            {"id": "client-version", "status": "ok"},
        ],
    }
    assert "detail" not in json.dumps(payload)
    assert "OneDrive" not in json.dumps(payload)


def test_report_flag_registered_on_doctor_parser():
    parser = cli._build_parser()
    ns = parser.parse_args(["doctor", "--report"])
    assert ns.report is True
    ns2 = parser.parse_args(["doctor"])
    assert ns2.report is False
    ns3 = parser.parse_args(["status", "--report"])  # the "status" alias
    assert ns3.report is True
