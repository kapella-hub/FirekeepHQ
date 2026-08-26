"""Doctor must name the gap between "the stack is up" and "your memories are findable".

install.sh no longer blocks for ~15 minutes on the 3.3GB model pull — it returns as
soon as the services are up and finishes the download in the background. That is a
better install and a NEW honesty obligation: in that window `memory_learn` returns
HTTP 200 with status="partial", stores the memory, queues it for backfill, and does
not make it recallable. A successful-looking write with a surprising consequence has
to be stated somewhere the user already looks, or it gets discovered later by
someone wondering why recall is empty.

WARN, never FAIL: nothing is broken and there is nothing to do.
"""
from __future__ import annotations

import configparser
import textwrap

import pytest
from firekeep_client import cli, resolver
from firekeep_client.transport import TransportError

CFG = textwrap.dedent("""\
    [identity]
    agent_id = tester
    [server]
    kind = ports
    scheme = http
    host = 10.0.0.5
    verify_tls = false
""")


def cfg():
    parser = configparser.ConfigParser()
    parser.read_string(CFG)
    return parser


def health(embeddings: dict | None):
    services = {"redis": {"status": "connected"}}
    if embeddings is not None:
        services["embeddings"] = embeddings
    return {"status": "ok", "services": services}


def stub(monkeypatch, payload):
    monkeypatch.setattr(cli, "get_json", lambda *a, **k: payload)


def test_ready_reports_ok(monkeypatch):
    stub(monkeypatch, health({"status": "connected", "detail": "mxbai-embed-large (1024-dim)"}))
    row = cli._check_embeddings(cfg())
    assert row[0] == "embeddings"
    assert row[1] == "ok"
    assert "1024-dim" in row[2]


def test_warming_warns_and_says_what_it_means(monkeypatch):
    stub(monkeypatch, health({
        "status": "warming",
        "detail": "model 'mxbai-embed-large' is not pulled yet",
    }))
    name, status, detail = cli._check_embeddings(cfg())
    assert status == "warn", "a transient, self-resolving state must not read as broken"
    # The consequence, not just the state. "warming" alone tells a user nothing
    # about why the thing they just wrote cannot be found.
    assert "not searchable" in detail
    assert "stored" in detail.lower()
    assert "ollama-pull" in detail, "must name the command that shows progress"


def test_silent_when_the_server_does_not_report_the_field(monkeypatch):
    """An older server predates the probe. No row beats an alarming unknown."""
    stub(monkeypatch, health(None))
    assert cli._check_embeddings(cfg()) is None


@pytest.mark.parametrize("payload", ["not a dict", None, 42, []])
def test_silent_on_a_malformed_health_body(monkeypatch, payload):
    stub(monkeypatch, payload)
    assert cli._check_embeddings(cfg()) is None


def test_silent_when_the_server_is_unreachable(monkeypatch):
    """That is _check_health's row to report. Two rows for one fact is noise, and
    this one would be guessing."""
    def boom(*a, **k):
        raise TransportError("connection refused")

    monkeypatch.setattr(cli, "get_json", boom)
    assert cli._check_embeddings(cfg()) is None


def test_silent_on_a_broken_config(monkeypatch):
    def boom(*a, **k):
        raise resolver.ConfigError("no server section")

    monkeypatch.setattr(cli.resolver, "resolve", boom)
    assert cli._check_embeddings(cfg()) is None


def test_doctor_skips_the_row_entirely_when_there_is_no_server(tmp_path, monkeypatch):
    """With nothing reachable, the routing row already said the one useful thing.
    "embeddings: unknown" underneath it is noise stacked on a diagnosis."""
    refused = [
        (svc, "fail", "http://10.0.0.5:8100/health: Connection refused")
        for svc in ("cortex", "bridge", "sentinel", "relay")
    ]
    monkeypatch.setattr(cli, "_check_health", lambda c: refused)
    monkeypatch.setattr(cli, "_config_path", lambda: tmp_path / "config")
    monkeypatch.setattr("firekeep_client.join.sweep_pending", lambda path: None)

    called = []
    monkeypatch.setattr(cli, "_check_embeddings", lambda c: called.append(1))
    # cfg's host (10.0.0.5) is unroutable; without this the new server-version
    # row would make a real, slow network call via serverupdate.check(cfg).
    monkeypatch.setattr(cli, "_check_server_version", lambda c: None)
    cli.run_doctor(cfg())
    assert called == []
