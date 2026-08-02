"""A transient upstream outage must not poison the runtime's stdio MCP session.

Codex does not respawn a stdio MCP child after its transport closes.  The HTTP
client therefore converts failures *after* a successful initialize exchange into
ordinary JSON-RPC errors.  The failed call is never replayed; a later call may
reach the recovered server over the same stdio session.
"""

import json
import ssl

import anyio
import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

from firekeep_client import shim
from firekeep_client.resolver import Endpoint


def _response_for(request: httpx.Request, *, text: str = "recovered") -> httpx.Response:
    body = json.loads(request.content)
    method = body.get("method")
    if method == "initialize":
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
            headers={"content-type": "application/json"},
        )
    if "id" not in body:  # notifications/initialized
        return httpx.Response(202)
    if method == "tools/list":
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "test tool",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            },
            headers={"content-type": "application/json"},
        )
    return httpx.Response(
        200,
        json={
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {"content": [{"type": "text", "text": text}], "isError": False},
        },
        headers={"content-type": "application/json"},
    )


def _client(handler):
    return shim.RecoveringMCPClient(
        transport=httpx.MockTransport(handler),
        base_url="http://upstream",
    )


class _FailingSSEStream(httpx.AsyncByteStream):
    """Yield complete progress, then simulate a reset after response headers."""

    def __init__(self, request_id: int, terminal: str, wire_format: str) -> None:
        self.request_id = request_id
        self.terminal = terminal
        self.wire_format = wire_format

    async def __aiter__(self):
        progress = {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {
                "progressToken": self.request_id,
                "progress": 1,
                "total": 2,
                "message": "halfway",
            },
        }
        payload = json.dumps(progress, separators=(",", ":"))
        if self.wire_format == "lf":
            yield f"event: message\ndata: {payload}\n\n".encode()
            yield b'event: message\ndata: {"jsonrpc":"2.0","id":'
        else:
            # Deliberately split CRLF pairs across chunks. A boundary matcher
            # must treat each pair as one newline, never as the blank line that
            # terminates an SSE event.
            yield b"event: message\r"
            yield f"\ndata: {payload}\r".encode()
            yield b"\n\r"
            yield b"\n"
            # End after a complete data line but before the blank separator.
            yield b'event: message\r\ndata: {"jsonrpc":"2.0","id":\r'
            yield b"\n"
        # This happens after AsyncClient.send() has already returned the 200.
        if self.terminal == "read-error":
            raise httpx.ReadError("upstream reset mid-stream")

    async def aclose(self) -> None:
        pass


class _InterruptedInitializeSSEStream(httpx.AsyncByteStream):
    """Return valid initialize JSON without its terminating SSE blank line."""

    def __init__(self, request_id: int, terminal: str) -> None:
        self.request_id = request_id
        self.terminal = terminal

    async def __aiter__(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "interrupted", "version": "0"},
                },
            },
            separators=(",", ":"),
        )
        yield f"event: message\r\ndata: {payload}\r\n".encode()
        if self.terminal == "read-error":
            raise httpx.ReadError("initialize reset after headers")

    async def aclose(self) -> None:
        pass


class _SplitTerminalSSEStream(httpx.AsyncByteStream):
    """Split the final CRLF, then keep the legal long-lived SSE body open."""

    def __init__(self, request_id: int) -> None:
        self.request_id = request_id

    async def __aiter__(self):
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "result": {
                    "content": [{"type": "text", "text": "stream-complete"}],
                    "isError": False,
                },
            },
            separators=(",", ":"),
        )
        yield f"event: message\r\ndata: {payload}\r\n\r".encode()
        yield b"\n"
        await anyio.sleep_forever()

    async def aclose(self) -> None:
        pass


def test_failed_tool_call_does_not_close_session_and_next_call_recovers():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            calls += 1
            if calls == 1:
                raise httpx.ConnectError(
                    "server being replaced (sensitive diagnostic)", request=request
                )
        return _response_for(request)

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with pytest.raises(McpError, match="temporarily unavailable") as exc_info:
                        await session.call_tool("echo", {"text": "first"})
                    assert "sensitive diagnostic" not in str(exc_info.value)
                    result = await session.call_tool("echo", {"text": "second"})
                    assert result.content[0].text == "recovered"

    anyio.run(scenario)
    assert calls == 2  # the ambiguous failed call was not replayed


@pytest.mark.parametrize("terminal", ["read-error", "early-eof"])
@pytest.mark.parametrize("wire_format", ["lf", "fragmented-crlf"])
def test_sse_body_failure_returns_error_preserves_progress_and_then_recovers(
    terminal, wire_format
):
    calls = 0
    progress_updates = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            calls += 1
            if calls == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_FailingSSEStream(body["id"], terminal, wire_format),
                )
        return _response_for(request)

    async def on_progress(progress, total, message):
        progress_updates.append((progress, total, message))

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # The stock MCP SDK swallows this read error and waits forever.
                    with anyio.fail_after(1):
                        with pytest.raises(McpError, match="temporarily unavailable"):
                            await session.call_tool(
                                "echo", {"text": "first"}, progress_callback=on_progress
                            )
                    result = await session.call_tool("echo", {"text": "second"})
                    assert result.content[0].text == "recovered"

    anyio.run(scenario)
    assert calls == 2
    assert progress_updates == [(1.0, 2.0, "halfway")]


def test_complete_sse_event_split_before_final_lf_does_not_wait_for_eof():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_SplitTerminalSSEStream(body["id"]),
            )
        return _response_for(request)

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with anyio.fail_after(1):
                        result = await session.call_tool("echo", {"text": "first"})
                    assert result.content[0].text == "stream-complete"

    anyio.run(scenario)


@pytest.mark.parametrize("failure", ["notification", "http-503"])
def test_notification_and_http_status_failures_also_leave_session_usable(failure):
    failed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        body = json.loads(request.content)
        method = body.get("method")
        should_fail = (
            (failure == "notification" and method == "notifications/initialized")
            or (failure == "http-503" and method == "tools/call")
        )
        if should_fail and not failed:
            failed = True
            if failure == "notification":
                raise httpx.ConnectError("server being replaced", request=request)
            return httpx.Response(503, text="deploying")
        return _response_for(request)

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if failure == "http-503":
                        with pytest.raises(McpError, match="temporarily unavailable"):
                            await session.call_tool("echo", {"text": "during deploy"})
                    result = await session.call_tool("echo", {"text": "after deploy"})
                    assert result.content[0].text == "recovered"

    anyio.run(scenario)
    assert failed is True


def test_initialize_failure_remains_fail_loud():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server is down", request=request)

    async def scenario():
        async with shim.RecoveringMCPClient(
            transport=httpx.MockTransport(handler), base_url="http://upstream"
        ) as client:
            with pytest.raises(httpx.ConnectError, match="server is down"):
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                )

    anyio.run(scenario)


@pytest.mark.parametrize("terminal", ["read-error", "early-eof"])
def test_initialize_sse_body_failure_is_bounded_and_does_not_mark_ready(terminal):
    initialize_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialize_calls
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            initialize_calls += 1
            if initialize_calls == 1:
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_InterruptedInitializeSSEStream(body["id"], terminal),
                )
        return _response_for(request)

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    with anyio.fail_after(1):
                        with pytest.raises(McpError, match="initialization was interrupted"):
                            await session.initialize()
                    assert client._mcp_initialized is False

                    initialized = await session.initialize()
                    assert initialized.serverInfo.name == "stub-upstream"
                    # The notification and this request are consumed in order;
                    # by the time list_tools returns, readiness must be recorded.
                    await session.list_tools()
                    assert client._mcp_initialized is True

    anyio.run(scenario)
    assert initialize_calls == 2


def test_failed_jsonrpc_response_post_is_treated_as_no_reply_message():
    """Sampling/elicitation responses have an id but no method of their own."""
    fail_response_post = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fail_response_post
        body = json.loads(request.content)
        if "method" not in body and ("result" in body or "error" in body):
            fail_response_post = True
            raise httpx.ConnectError("server replaced", request=request)
        return _response_for(request)

    async def scenario():
        async with _client(handler) as client:
            initialized = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
            assert initialized.status_code == 200
            notification = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notification.status_code == 202
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 9, "result": {"model": "ok"}},
            )
            assert response.status_code == 202
            assert response.content == b""

    anyio.run(scenario)
    assert fail_response_post is True


@pytest.mark.parametrize(
    ("failure", "message"),
    [("auth", "authentication was rejected"), ("tls", "TLS verification failed")],
)
def test_actionable_failures_keep_session_usable(failure, message):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        body = json.loads(request.content)
        if body.get("method") == "tools/call":
            calls += 1
            if calls == 1 and failure == "auth":
                return httpx.Response(401, text="secret credential diagnostic")
            if calls == 1:
                error = httpx.ConnectError("TLS internals", request=request)
                raise error from ssl.SSLCertVerificationError(
                    1, "certificate diagnostic"
                )
        return _response_for(request)

    async def scenario():
        client = _client(handler)
        async with client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (read, write, _get_session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    with pytest.raises(McpError, match=message) as exc_info:
                        await session.call_tool("echo", {"text": "first"})
                    assert "secret credential diagnostic" not in str(exc_info.value)
                    assert "certificate diagnostic" not in str(exc_info.value)
                    result = await session.call_tool("echo", {"text": "second"})
                    assert result.content[0].text == "recovered"

    anyio.run(scenario)
    assert calls == 2


def test_production_build_client_enables_recovery():
    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "test-agent"},
        verify=False,
    )

    async def scenario():
        client = shim.build_client(endpoint)
        try:
            assert isinstance(client, shim.RecoveringMCPClient)
        finally:
            await client.aclose()

    anyio.run(scenario)
