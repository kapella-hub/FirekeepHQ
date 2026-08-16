"""`firekeep doctor` must be able to say "you have no server", and say what to do.

The defect this closes, from the 2026-08-15 cold install: the ONLY text in the
entire diagnostic naming `firekeep join` / `firekeep connect` lived inside
`_check_api_key`, and its guidance branch is reached only after a SUCCESSFUL HTTP
round-trip returning 401/403 (`cli.py`, the `except TransportError` arm). With no
server at all the request dies at the socket, raising OSError, which that function
catches and passes -- so the advice was structurally suppressed in exactly the
situation it was written for.

What the user got instead was four identical `[FAIL]` rows whose entire content was
an OS socket error, from the command the installer had just told them to run.

These tests are about ROUTING, not formatting: the row must appear when nothing is
reachable, must NOT appear for an ordinary outage, and must name every path out.
"""
from __future__ import annotations

import configparser
import textwrap

import pytest
from firekeep_client import cli

LOCAL = textwrap.dedent("""\
    [identity]
    agent_id = tester
    [server]
    kind = ports
    scheme = http
    host = 127.0.0.1
    verify_tls = false
""")

REMOTE = LOCAL.replace("host = 127.0.0.1", "host = 10.0.0.9")

SERVICES = ("cortex", "bridge", "sentinel", "relay")


def cfg_of(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read_string(text)
    return parser


def refused(message: str = "[Errno 111] Connection refused"):
    return [(svc, "fail", f"http://127.0.0.1:8100/health: {message}") for svc in SERVICES]


# --- the row appears when it should -----------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "[Errno 111] Connection refused",
        # Windows phrases the same condition differently, and a check that only
        # understands errno 111 is a check that only works on Linux.
        "[WinError 10061] No connection could be made because the target machine "
        "actively refused it",
        "[Errno -2] Name or service not known",
        "[Errno 101] Network is unreachable",
        "timed out",
    ],
)
def test_row_appears_for_every_connection_layer_failure(message):
    row = cli._check_server_connection(cfg_of(LOCAL), refused(message))
    assert row is not None, f"no server-connection row for {message!r}"
    assert row[1] == "fail"


def test_row_names_all_three_ways_out():
    _, _, detail = cli._check_server_connection(cfg_of(LOCAL), refused())
    for command in ("firekeep init", "firekeep join", "firekeep connect"):
        assert command in detail, f"the guidance never names {command!r}"


def test_localhost_and_remote_get_different_sentences():
    local = cli._check_server_connection(cfg_of(LOCAL), refused())[2]
    remote = cli._check_server_connection(cfg_of(REMOTE), refused())[2]
    assert "no server to talk to" in local
    # A remote host that is down is a different problem from an empty machine,
    # and telling someone with a configured server to "run firekeep init" as the
    # first suggestion without naming their host would be actively misleading.
    assert "10.0.0.9" in remote


# --- and stays quiet when it should ------------------------------------------

def test_no_row_when_one_service_is_merely_down():
    """A partial outage is an outage. Announcing "you have no server" over a
    single crashed container would be worse than the four socket errors."""
    health = [
        ("cortex", "ok", "http://127.0.0.1:8100"),
        ("bridge", "fail", "http://127.0.0.1:8070/health: Connection refused"),
        ("sentinel", "ok", "http://127.0.0.1:8060"),
        ("relay", "ok", "http://127.0.0.1:8050"),
    ]
    assert cli._check_server_connection(cfg_of(LOCAL), health) is None


def test_no_row_when_the_server_answers_with_an_error():
    """HTTP 500 from every service means something IS there and is broken --
    a completely different remedy from "provision a server"."""
    health = [(svc, "fail", "http://127.0.0.1:8100/health: HTTP 500") for svc in SERVICES]
    assert cli._check_server_connection(cfg_of(LOCAL), health) is None


def test_no_row_when_everything_is_healthy():
    health = [(svc, "ok", "http://127.0.0.1:8100") for svc in SERVICES]
    assert cli._check_server_connection(cfg_of(LOCAL), health) is None


def test_no_row_for_an_empty_health_list():
    assert cli._check_server_connection(cfg_of(LOCAL), []) is None


# --- and it is actually wired into doctor ------------------------------------

def test_doctor_reports_it_first(tmp_path, monkeypatch):
    """Ordering is the point: the summary sentence has to precede the four rows
    it explains, or it is just a fifth error."""
    monkeypatch.setattr(cli, "_check_health", lambda cfg: refused())
    monkeypatch.setattr(cli, "_config_path", lambda: tmp_path / "config")
    monkeypatch.setattr("firekeep_client.join.sweep_pending", lambda path: None)

    results = cli.run_doctor(cfg_of(LOCAL))
    names = [name for name, _, _ in results]
    assert names[0] == "server", f"server row is not first: {names[:6]}"
    assert names[1:5] == list(SERVICES)
