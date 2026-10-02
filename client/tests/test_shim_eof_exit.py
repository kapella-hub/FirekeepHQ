"""A shim whose runtime closes stdin must EXIT, not park in the SDK's task group.

Regression test for the orphaned-shim leak (observed 2026-08-25: 71 shims, 59
with a dead parent). The mechanism is not a missing Windows job object — it is
this: `mcp.server.stdio.stdio_server` is implemented as a task group that JOINS
both of its pumps on `__aexit__`, and its `stdout_writer` ends only when the
send side of the write stream is closed. `serve()` passed that stream to
`_bridge` and never closed it, so once `_bridge` returned on `stdio_src_eof`,
exiting `_open_stdio` blocked forever. Measured: the real shim was still alive
25 s after its gateway was hard-killed; with the send side closed it exits in
~0.05 s.

Why the existing suite could not see it: every other shim test injects
`stdio_streams=` directly, which takes `_open_stdio`'s early branch and never
constructs a real `stdio_server` -- so the task group whose join is the bug is
never in the picture. (test_shim_bridge.py's own comment calls that branch
"infeasible to unit test".) It is feasible: `stdio_server` accepts `stdin` and
`stdout` overrides, so this test monkeypatches the shim's reference with those
bound to in-memory files holding a real handshake followed by EOF.

The hang is masked in production by `Backend.close()`, which calls
`terminate()` on a clean gateway shutdown. It is exposed whenever the host
kills the gateway instead of closing stdin -- the normal session-end path.
"""
import functools
import io
import json

import anyio
import httpx

from mcp.server import stdio as mcp_stdio

from firekeep_client import shim
from firekeep_client.resolver import Endpoint

# Generous: the failure mode is an unbounded hang, so any finite bound catches
# it; the fixed path finishes in well under a second.
HANG_BOUND_SECONDS = 8


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-runtime", "version": "0.0.0"},
        },
    }


def _initialized_notification() -> dict:
    # The real production sequence: every client sends this right after a
    # successful initialize. It is also the notification the SDK special-cases
    # to kick off its standing GET/SSE connection.
    return {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}


def _handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body.get("method") == "initialize":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "stub-upstream", "version": "0.0.0"},
                },
            },
            headers={
                "content-type": "application/json",
                "mcp-session-id": "test-session-eof",
            },
        )
    # notifications/initialized (and any other notification): accepted.
    return httpx.Response(202)


def _endpoint() -> Endpoint:
    # `verify` is a required Endpoint field (no default). False is inert here:
    # the client below is a MockTransport, which answers in-process and never
    # opens a socket, so there is no TLS handshake to verify. Same value the
    # other shim tests construct against their mocks.
    return Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan"},
        verify=False,
    )


def test_serve_exits_when_stdin_reaches_eof(monkeypatch):
    """serve() must return once stdin drains -- not hang in stdio_server.__aexit__."""
    stdin_text = (
        json.dumps(_initialize_request()) + "\n"
        + json.dumps(_initialized_notification()) + "\n"
    )

    # Bind the real stdio_server to in-memory files. StringIO ends after the
    # two lines above, so stdin_reader sees EOF exactly as a dead runtime's
    # closed pipe would.
    monkeypatch.setattr(
        shim,
        "stdio_server",
        functools.partial(
            mcp_stdio.stdio_server,
            stdin=anyio.wrap_file(io.StringIO(stdin_text)),
            stdout=anyio.wrap_file(io.StringIO()),
        ),
    )

    async def _run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_handler), base_url="http://mock"
        ) as client:
            # No stdio_streams: take the real stdio_server branch, which is the
            # only path where the task-group join can hang.
            with anyio.fail_after(HANG_BOUND_SECONDS):
                await shim.serve("cortex", _endpoint(), http_client=client)

    anyio.run(_run)
