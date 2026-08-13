"""Task 10 — bridge loop: initialize + tool-call round-trip, and a server->client notification."""
import contextlib

import anyio
import httpx

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server as LowLevelServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification, TextContent, Tool

from firekeep_client import shim
from firekeep_client.resolver import Endpoint


def _make_upstream():
    """A real in-process MCP server with one `echo` tool, in JSON-response mode."""
    server = LowLevelServer("stub-upstream")

    @server.list_tools()
    async def _list_tools():
        return [
            Tool(
                name="echo",
                description="echo back the text argument",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            )
        ]

    @server.call_tool()
    async def _call_tool(name, arguments):
        return [TextContent(type="text", text=str(arguments.get("text", "")))]

    manager = StreamableHTTPSessionManager(app=server, json_response=True)

    async def asgi(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    return manager, asgi


async def _roundtrip():
    manager, asgi = _make_upstream()
    async with manager.run():
        transport = httpx.ASGITransport(app=asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://upstream") as client:
            async with streamable_http_client(
                "http://upstream/mcp", http_client=client
            ) as (http_read, http_write, _get_sid):
                # In-memory stdio pair standing in for the runtime.
                to_bridge_send, to_bridge_recv = anyio.create_memory_object_stream(100)
                from_bridge_send, from_bridge_recv = anyio.create_memory_object_stream(100)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        shim._bridge, to_bridge_recv, from_bridge_send, http_read, http_write
                    )
                    async with ClientSession(from_bridge_recv, to_bridge_send) as session:
                        with anyio.fail_after(30):
                            init = await session.initialize()
                            assert init.serverInfo.name == "stub-upstream"
                            result = await session.call_tool("echo", {"text": "round-trip-ok"})
                            assert result.content[0].text == "round-trip-ok"
                    tg.cancel_scope.cancel()


def test_initialize_and_tool_call_roundtrip():
    anyio.run(_roundtrip)


async def _server_notification():
    stdio_read_send, stdio_read_recv = anyio.create_memory_object_stream(10)
    stdio_write_send, stdio_write_recv = anyio.create_memory_object_stream(10)
    http_read_send, http_read_recv = anyio.create_memory_object_stream(10)
    http_write_send, http_write_recv = anyio.create_memory_object_stream(10)

    notification = SessionMessage(
        JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/message",
                params={"level": "info", "data": "server-says-hi"},
            )
        )
    )
    await http_read_send.send(notification)  # upstream -> runtime direction

    async with anyio.create_task_group() as tg:
        tg.start_soon(
            shim._bridge, stdio_read_recv, stdio_write_send, http_read_recv, http_write_send
        )
        with anyio.fail_after(5):
            forwarded = await stdio_write_recv.receive()
        assert isinstance(forwarded, SessionMessage)
        assert forwarded.message.root.method == "notifications/message"
        assert forwarded.message.root.params["data"] == "server-says-hi"
        tg.cancel_scope.cancel()


def test_server_notification_reaches_stdio():
    anyio.run(_server_notification)


# --- Task 10 review carry-forward: serve()'s composition path ---------------
#
# The two tests above call `shim._bridge` directly, and `test_shim_skeleton.py`'s
# run()-success test replaces `shim.serve` with an AsyncMock — so `serve()`'s own
# body (the `stdio_streams is None -> real stdio_server()` branch of `_open_stdio`,
# and the `owns_client`/`client.aclose()` glue) had zero coverage. The real
# `stdio_server()` branch needs real stdin and is infeasible to unit test; what's
# actually testable — and what the review calls out as the load-bearing part of
# this specific gap — is the client-ownership lifecycle: serve() must build its
# own client via build_client() when none is injected, and must close exactly
# that client when the bridge ends. build_client and streamable_http_client are
# faked out here so this is a focused test of serve()'s own wiring, not a repeat
# of the real-upstream round trip above.


def test_serve_builds_and_closes_its_own_client_when_none_injected(monkeypatch):
    endpoint = Endpoint(
        mcp_url="http://198.51.100.7:8080/mcp",
        rest_base="http://198.51.100.7:8100",
        headers={"X-Agent-Id": "mogan"},
        verify=False,
    )

    built = []
    closed = []

    class _FakeClient:
        async def aclose(self):
            closed.append(True)

    def _fake_build_client(ep, *, agent=None, tap=None):
        # `tap` mirrors build_client's real signature: serve() passes it
        # unconditionally (None for non-bridge services like this cortex run).
        built.append(ep)
        return _FakeClient()

    monkeypatch.setattr(shim, "build_client", _fake_build_client)

    @contextlib.asynccontextmanager
    async def _fake_streamable_http_client(url, http_client=None):
        # An http side that never sends anything and is never closed until after
        # the bridge ends -- only the stdio side (closed below, before serve() is
        # even called) drives the shutdown, so this exercises the ordinary
        # "runtime closed stdin" path deterministically, not the dead-connection
        # detector added in Task 11.
        http_read_send, http_read_recv = anyio.create_memory_object_stream(10)
        http_write_send, http_write_recv = anyio.create_memory_object_stream(10)
        try:
            yield (http_read_recv, http_write_send, lambda: None)
        finally:
            await http_read_send.aclose()
            await http_write_recv.aclose()

    monkeypatch.setattr(shim, "streamable_http_client", _fake_streamable_http_client)

    async def _scenario():
        stdio_read_send, stdio_read_recv = anyio.create_memory_object_stream(10)
        stdio_write_send, stdio_write_recv = anyio.create_memory_object_stream(10)
        await stdio_read_send.aclose()  # runtime stdin EOF immediately -> clean shutdown
        await shim.serve(
            "cortex", endpoint, None, (stdio_read_recv, stdio_write_send)
        )
        await stdio_write_recv.aclose()

    anyio.run(_scenario)

    assert built == [endpoint]  # serve() built its own client (none was injected)
    assert closed == [True]  # ...and closed exactly that client, exactly once
