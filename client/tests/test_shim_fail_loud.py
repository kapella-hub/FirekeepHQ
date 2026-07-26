"""Task 11 — fail-loud modes: each names service/url/profile, exits non-zero, never echoes the key.

Also carries forward two items from the Task 10 review:
1. Dead-connection detection: the mcp SDK's `post_writer` swallows a failed POST for the
   notifications/initialized notification (sent right after every successful initialize)
   and closes its streams, which `_pump` previously treated as an ordinary clean shutdown
   -- so `serve()`/`run()` returned rc=0 on a dead upstream connection (CONFIRMED via the
   reviewer's repro). Reproduced here with the real trigger sequence: a session-id-bearing
   initialize response (so handle_get_stream's standing GET/SSE connection also fires) and
   a handler that fails both the POST and the GET, not just one code path.
2. Bounded timeouts: a dead upstream must surface, never wedge the runtime (spec §5.2).
"""
import json
import logging
import ssl

import anyio
import httpx
import pytest

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, JSONRPCRequest

from firekeep_client import shim
from firekeep_client.resolver import Endpoint

OFFICE_KEY = "nxs_secret_office_key"


def _write_office_config(tmp_path):
    ca = tmp_path / "firekeep-root-ca.crt"  # ca_path need only be a present key for resolve()
    cfg = tmp_path / "config"
    cfg.write_text(
        "[active]\n"
        "profile = office\n"
        "\n"
        "[office]\n"
        "kind = paths\n"
        "scheme = https\n"
        "base_url = https://firekeep.office.example\n"
        "verify_tls = true\n"
        f"ca_path = {ca}\n"
        f"api_key = {OFFICE_KEY}\n"
        "agent_id = mogan\n",
        encoding="utf-8",
    )
    return cfg


def _initialize_request():
    return SessionMessage(
        JSONRPCMessage(
            JSONRPCRequest(
                jsonrpc="2.0",
                id=1,
                method="initialize",
                params={
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-runtime", "version": "0.0.0"},
                },
            )
        )
    )


def _initialized_notification():
    # The REAL production trigger: every ClientSession sends this immediately
    # after a successful initialize() -- and it's the one notification method
    # the SDK special-cases (_is_initialized_notification) to also kick off
    # handle_get_stream()'s standing GET/SSE connection. Using any other
    # notification would only prove the mechanism fires for *a* notification,
    # not for the one production actually hits first.
    return SessionMessage(
        JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/initialized",
                params={},
            )
        )
    )


def _run_with_mock(monkeypatch, tmp_path, handler, extra_messages=()):
    """Drive run() for 'cortex' against a MockTransport whose handler simulates a failure.

    The injected client carries the REAL office headers (incl. the key) so the
    never-log-key assertion is meaningful. A pre-loaded initialize REQUEST forces
    a POST, so the mock's failure fires inside a request task and surfaces to run().
    `extra_messages` are queued right after initialize (e.g. a notification, to drive
    the dead-connection-after-initialize scenario).
    """
    logging.disable(logging.CRITICAL)  # keep SDK logging off stderr for a clean assertion
    monkeypatch.setenv("FIREKEEP_CONFIG", str(_write_office_config(tmp_path)))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)

    endpoint, _profile = shim.resolve_active("cortex")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=endpoint.headers,
        verify=False,
        base_url="http://mock",
    )
    read_send, read_recv = anyio.create_memory_object_stream(10)
    write_send, write_recv = anyio.create_memory_object_stream(10)
    read_send.send_nowait(_initialize_request())
    for msg in extra_messages:
        read_send.send_nowait(msg)

    try:
        rc = shim.run("cortex", http_client=client, stdio_streams=(read_recv, write_send))
    finally:
        anyio.run(client.aclose)
        logging.disable(logging.NOTSET)
    return rc


def _refuse(request):
    raise httpx.ConnectError("Connection refused", request=request)


def _timeout(request):
    raise httpx.ConnectTimeout("timed out", request=request)


def _unauthorized(request):
    return httpx.Response(401, json={"error": "unauthorized"})


def _tls_fail(request):
    raise httpx.ConnectError("TLS handshake failed", request=request) from (
        ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer")
    )


def _dead_connection(request):
    # Realistic upstream: the initialize response carries Mcp-Session-Id, so the
    # SDK sets transport.session_id -- which means the notifications/initialized
    # notification ALSO triggers handle_get_stream()'s standing GET/SSE
    # connection (transport.py: `if not self.session_id: return` is the early
    # exit that would otherwise skip it). Both the POST and the GET must be
    # made to fail for this to be a faithful "connection died" repro rather
    # than one that happens to dodge the GET path.
    if request.method == "GET":
        raise httpx.ConnectError("connection reset by peer", request=request)
    body = json.loads(request.content)
    if body.get("method") == "initialize":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "stub-upstream", "version": "0.0.0"},
                },
            },
            headers={"content-type": "application/json", "mcp-session-id": "test-session-123"},
        )
    # Any POST after initialize (the notifications/initialized notification
    # queued by the caller) simulates the connection dying mid-session.
    raise httpx.ConnectError("connection reset by peer", request=request)


def test_connection_refused_is_named(capsys, monkeypatch, tmp_path):
    rc = _run_with_mock(monkeypatch, tmp_path, _refuse)
    err = capsys.readouterr().err
    assert rc == 1
    assert "cortex unreachable" in err
    assert "https://firekeep.office.example/mcp/cortex" in err
    assert "profile office" in err
    assert OFFICE_KEY not in err


def test_connect_timeout_is_named_unreachable(capsys, monkeypatch, tmp_path):
    # Bounded timeouts (spec §5.2): a ConnectTimeout is the concrete manifestation
    # of "a dead upstream must surface, never wedge the runtime" -- it must be
    # classified the same as an outright refusal, not left to hang or fall through
    # to the generic fallback.
    rc = _run_with_mock(monkeypatch, tmp_path, _timeout)
    err = capsys.readouterr().err
    assert rc == 1
    assert "cortex unreachable" in err
    assert "profile office" in err
    assert OFFICE_KEY not in err


def test_401_is_named(capsys, monkeypatch, tmp_path):
    rc = _run_with_mock(monkeypatch, tmp_path, _unauthorized)
    err = capsys.readouterr().err
    assert rc == 1
    assert "401 from cortex" in err
    assert "api_key" in err
    assert OFFICE_KEY not in err


def test_tls_failure_is_named(capsys, monkeypatch, tmp_path):
    rc = _run_with_mock(monkeypatch, tmp_path, _tls_fail)
    err = capsys.readouterr().err
    assert rc == 1
    assert "TLS verify failed" in err
    assert "ca_path" in err
    assert OFFICE_KEY not in err


def test_unknown_or_missing_profile_is_named(capsys, monkeypatch, tmp_path):
    missing = tmp_path / "does-not-exist" / "config"
    monkeypatch.setenv("FIREKEEP_CONFIG", str(missing))
    rc = shim.run("cortex")
    err = capsys.readouterr().err
    assert rc == 1
    assert "config error" in err
    assert str(missing) in err
    assert OFFICE_KEY not in err


def test_dead_connection_after_initialize_is_not_silently_swallowed(capsys, monkeypatch, tmp_path):
    """Task 10 review carry-forward (CONFIRMED via repro): mcp's post_writer awaits
    (not tg.start_soon) any non-request POST directly; on failure it
    logs-and-swallows and closes both its streams. Those closures look identical,
    at the anyio level, to an ordinary clean shutdown, so serve()/run() previously
    returned rc=0 on a dead connection.

    This reproduces the REAL production sequence, not just any notification:
    notifications/initialized is what every ClientSession sends immediately after
    a successful initialize(), the mock's initialize response carries
    Mcp-Session-Id (as a real server would), which means the SDK also fires
    handle_get_stream()'s standing GET/SSE connection for it -- and the handler
    fails that GET too, so this is a faithful "the connection actually died"
    repro, not one that happens to dodge the GET path. Confirmed fast (no
    multi-second stall from handle_get_stream's reconnect-retry sleep): once
    _bridge raises, streamable_http_client's own cancel scope cancels that
    background task immediately rather than letting it sleep out its retries."""
    rc = _run_with_mock(
        monkeypatch, tmp_path, _dead_connection, extra_messages=[_initialized_notification()]
    )
    err = capsys.readouterr().err
    assert rc == 1
    assert "cortex unreachable" in err
    assert "https://firekeep.office.example/mcp/cortex" in err
    assert "profile office" in err
    assert OFFICE_KEY not in err


@pytest.mark.parametrize(
    "handler,extra_messages",
    [
        (_refuse, ()),
        (_timeout, ()),
        (_unauthorized, ()),
        (_tls_fail, ()),
        (_dead_connection, [_initialized_notification()]),
    ],
    ids=["refused", "timeout", "401", "tls", "dead_connection"],
)
def test_no_fail_loud_mode_ever_leaks_the_api_key(
    capsys, monkeypatch, tmp_path, handler, extra_messages
):
    rc = _run_with_mock(monkeypatch, tmp_path, handler, extra_messages=extra_messages)
    err = capsys.readouterr().err
    assert rc == 1
    assert OFFICE_KEY not in err


def test_build_client_uses_bounded_timeouts():
    # Bounded timeouts (spec §5.2): every axis must be a finite bound, never None
    # (httpx treats None as "wait forever" -- exactly the wedge this guards against).
    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan"},
        verify=False,
    )

    async def _scenario():
        client = shim.build_client(endpoint)
        try:
            timeout = client.timeout
            assert timeout.connect == shim.CONNECT_TIMEOUT
            assert timeout.read == shim.SSE_READ_TIMEOUT
            assert timeout.write == shim.CONNECT_TIMEOUT
            assert timeout.pool == shim.CONNECT_TIMEOUT
            assert None not in (timeout.connect, timeout.read, timeout.write, timeout.pool)
        finally:
            await client.aclose()

    anyio.run(_scenario)


# --- SDK logging kept off the operator surface (post-T11 hardening) ----------


def test_configure_logging_silences_sdk_loggers(monkeypatch):
    """SDK internals (mcp post_writer's logger.exception on a dead connection)
    must not dump raw tracebacks next to our clean fail-loud one-liner."""
    import logging

    from firekeep_client import shim

    monkeypatch.delenv("FIREKEEP_SHIM_DEBUG", raising=False)
    # Reset levels so the test observes the change.
    for name in ("mcp", "httpx", "httpcore", "anyio"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    shim._configure_logging()

    for name in ("mcp", "httpx", "httpcore", "anyio"):
        assert logging.getLogger(name).level == logging.CRITICAL


def test_configure_logging_debug_env_opts_in(monkeypatch):
    import logging

    from firekeep_client import shim

    monkeypatch.setenv("FIREKEEP_SHIM_DEBUG", "1")
    for name in ("mcp", "httpx"):
        logging.getLogger(name).setLevel(logging.NOTSET)

    shim._configure_logging()

    # Debug mode must NOT silence the SDK loggers.
    for name in ("mcp", "httpx"):
        assert logging.getLogger(name).level != logging.CRITICAL
