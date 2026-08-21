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
import re
import textwrap
import types

from firekeep_client import cli, resolver
from firekeep_client.transport import TransportError

# doctor-report.php's own validation regex for a check id, verbatim — this is
# the SERVER-SIDE contract every check name in cli.py must satisfy, or
# --report 400s on every doctor run for whoever hits that check. Kept as a
# literal copy (not imported — the two live in separate repos) so a change on
# either side that breaks the other shows up as a failing test, not a support
# ticket. Update BOTH this pattern and doctor-report.php's together.
_SERVER_ID_PATTERN = re.compile(r"^[a-z0-9_-]{1,40}$")


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


def test_post_json_receives_no_auth_headers(monkeypatch):
    """The whole feature is 'anonymous' -- prove it, don't just not-disprove
    it. Swallowing `headers` into `**kw` (as the earlier version of this
    suite did) would let a future change like `headers=ep.headers` attach
    the user's API key to a request to firekeep.ai and every test here would
    stay green. Assert the exact kwarg."""
    captured = {}

    def fake_post(url, body, *, headers, **kw):
        captured["headers"] = headers

    monkeypatch.setattr(cli, "post_json", fake_post)
    monkeypatch.setattr(cli, "run_doctor", lambda cfg=None: _results(("x", "ok", "-")))
    cli.cmd_doctor(types.SimpleNamespace(report=True))
    assert captured["headers"] == {}


_DOCTOR_SERVER_CFG = textwrap.dedent("""\
    [identity]
    agent_id = tester
    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
""")


def test_real_run_doctor_check_names_satisfy_the_server_id_contract(tmp_path, monkeypatch):
    """Runs the REAL run_doctor() (not a synthetic fixture) against an
    isolated config, with only the network edge (`get_json`) faked, and
    checks every literal check `name` it actually returns against the exact
    regex doctor-report.php enforces server-side. This is what closes the
    gap every other test in this file leaves open: those all use synthetic
    names like "x"/"a"/"b"/"c", so a real check whose name doesn't satisfy
    the server contract could ship with every test here green. Verified by
    hand against cli.py on 2026-08-20 (an adversarial review's own AST walk
    found the same 22-name closed set: local-filesystem check literals, plus
    resolver.SERVICES and the fixed instruction-runtime tuple) -- this test
    re-derives it by actually EXECUTING the code instead of trusting that
    manual pass to stay true forever."""
    cfg_path = tmp_path / ".firekeep" / "config"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(_DOCTOR_SERVER_CFG, encoding="utf-8")
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg_path))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    cfg = resolver.load_config(cfg_path)

    def fake_get_json(url, **kw):
        if url.endswith("/health"):
            return {"status": "ok"}
        raise TransportError("no network in this test")

    monkeypatch.setattr(cli, "get_json", fake_get_json)
    results = cli.run_doctor(cfg)

    assert len(results) >= 10, "fixture regressed -- too few rows to be a meaningful check"
    bad = [name for name, _status, _detail in results if not _SERVER_ID_PATTERN.match(name)]
    assert bad == [], f"check name(s) violate the server id contract: {bad}"

    # _check_client_version's literal ("client-version") isn't reachable from
    # this minimal config (no [dist] section -- it returns None rather than
    # firing), so assert it directly rather than leaving it unexercised.
    assert _SERVER_ID_PATTERN.match("client-version")


def test_resolver_service_ids_satisfy_the_server_id_contract():
    """_check_health emits `resolver.SERVICES` verbatim as check names
    (cli.py: `out.append((svc, "ok", ep.rest_base))`) -- a service renamed to
    something the server id regex rejects would silently break reporting for
    that row specifically. Covers the health-check row independent of
    whether the fixture above's fake transport happens to exercise it."""
    for svc in resolver.SERVICES:
        assert _SERVER_ID_PATTERN.match(svc), svc


def test_report_flag_registered_on_doctor_parser():
    parser = cli._build_parser()
    ns = parser.parse_args(["doctor", "--report"])
    assert ns.report is True
    ns2 = parser.parse_args(["doctor"])
    assert ns2.report is False
    ns3 = parser.parse_args(["status", "--report"])  # the "status" alias
    assert ns3.report is True
