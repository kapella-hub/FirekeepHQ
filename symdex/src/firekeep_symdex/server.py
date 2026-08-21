"""MCP server for FirekeepSymdex."""

import argparse
import asyncio
import inspect
import json
import logging
import os
from typing import Optional

from mcp.server import Server
from mcp.types import Tool, TextContent

logger = logging.getLogger(__name__)

from .tools import discover_tools

# Every tool result is re-sent on every remaining turn of the session, so
# serialization whitespace is not paid once — it is paid once per remaining
# turn. Measured on the live index (2026-08-21), `indent=2` inflated results
# 19.6-23.5% for zero information gain: JSON parses identically either way and
# no model reads an indented object more accurately. The only thing the
# whitespace bought was readability of a raw wire log, and the debug log
# already gives that. Guarded by tests/test_wire_economy.py.
_COMPACT = (",", ":")

# Hard backstop against a result no context window can hold. Measured on the
# live 938-file index (2026-08-21), `export_index` returns 525,879 tokens —
# 262.9% of a 200k window — so that call cannot succeed; it ends the session
# that made it. This is NOT a token budget: `find_dead_code` (49,454 tok) and
# `get_import_graph` (32,006 tok) are expensive but usable and must keep
# working. Whether they deserve narrowing of their own is a separate judgment.
#
# The ceiling REFUSES rather than truncates. Cutting the payload and sending the
# prefix would be shorter and is deliberately not done: a truncated index or
# file tree makes the agent believe a symbol does not exist, and a false
# negative on "does this already exist" defeats the entire point of a code
# index. A refusal cannot cause that. Guarded by tests/test_result_ceiling.py.
_DEFAULT_MAX_RESULT_TOKENS = 120_000

# Narrowing levers, in the order worth trying. Only those a tool actually
# accepts are ever suggested — advice to pass `path_prefix` to a tool with no
# such parameter sends the agent into a retry loop, which is worse than the
# problem it was meant to solve.
_NARROWING_PARAMS = (
    "path_prefix", "file_path", "focus", "kind", "limit", "max_results",
    "budget_tokens", "include_summaries", "include_signatures", "include_tests",
    "format",
)


def _max_result_chars() -> int:
    """Ceiling in characters. `FIREKEEP_SYMDEX_MAX_RESULT_TOKENS` overrides."""
    raw = os.environ.get("FIREKEEP_SYMDEX_MAX_RESULT_TOKENS", "")
    try:
        tokens = int(raw)
    except (TypeError, ValueError):
        tokens = _DEFAULT_MAX_RESULT_TOKENS
    if tokens <= 0:
        tokens = _DEFAULT_MAX_RESULT_TOKENS
    return tokens * 4


def _narrowing_for(tool_name: str | None) -> list[str]:
    """The narrowing parameters this specific tool accepts."""
    tool = _TOOLS.get(tool_name or "")
    if not tool:
        return []
    try:
        params = inspect.signature(tool["handler"]).parameters
    except (TypeError, ValueError):
        return []
    return [p for p in _NARROWING_PARAMS if p in params]


def _wire(payload: dict, tool_name: str | None = None) -> str:
    """Serialize a tool result for the wire. The single funnel for all tools."""
    text = json.dumps(payload, separators=_COMPACT, default=str)
    ceiling = _max_result_chars()
    if len(text) <= ceiling:
        return text

    narrow = _narrowing_for(tool_name)
    refusal = {
        "error": (
            "Result too large to return. Nothing was truncated — a partial "
            "index would read as 'this does not exist', so the call is refused "
            "instead. Re-run it narrowed."
        ),
        "tool": tool_name,
        "result_tokens": len(text) // 4,
        "max_result_tokens": ceiling // 4,
    }
    if narrow:
        refusal["narrow_with"] = narrow
    return json.dumps(refusal, separators=_COMPACT, default=str)

# Build the tool registry once at import time.
_TOOLS = discover_tools()

# Create server
server = Server("FirekeepSymdex")


@server.list_tools()
async def list_tools() -> list[Tool]:
    """List all available tools."""
    return [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in _TOOLS.values()
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle tool calls."""
    storage_path = os.environ.get("CODE_INDEX_PATH")

    tool = _TOOLS.get(name)
    if not tool:
        return [TextContent(type="text", text=_wire({"error": f"Unknown tool: {name}"}))]

    try:
        handler = tool["handler"]
        sig = inspect.signature(handler)
        if "storage_path" in sig.parameters and "storage_path" not in arguments:
            arguments["storage_path"] = storage_path

        if tool.get("is_async"):
            result = await handler(**arguments)
        else:
            result = handler(**arguments)

        return [TextContent(type="text", text=_wire(result, tool_name=name))]

    except Exception as e:
        logger.exception("Tool %s failed: %s", name, e)
        return [TextContent(type="text", text=_wire({"error": str(e)}, tool_name=name))]


async def run_server():
    """Run the MCP server in stdio mode."""
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


async def run_http_server(host: str, port: int):
    """Run the MCP server in HTTP mode using Streamable HTTP (stateless)."""
    from mcp.server.streamable_http import StreamableHTTPServerTransport
    from starlette.responses import JSONResponse
    import uvicorn
    import anyio

    # Task group for managing per-request server tasks
    _task_group: anyio.abc.TaskGroup | None = None

    async def handle_mcp(scope, receive, send):
        """Stateless MCP handler — each request gets a fresh transport."""
        # Strip any session ID; stateless mode doesn't use sessions
        headers = [(k, v) for k, v in scope.get("headers", [])
                   if k.lower() != b"mcp-session-id"]
        scope["headers"] = headers

        transport = StreamableHTTPServerTransport(
            mcp_session_id=None,
            is_json_response_enabled=True,
        )

        async def run_server(*, task_status=anyio.TASK_STATUS_IGNORED):
            async with transport.connect() as (read_stream, write_stream):
                task_status.started()
                await server.run(
                    read_stream, write_stream,
                    server.create_initialization_options(),
                    stateless=True,
                )

        await _task_group.start(run_server)
        await transport.handle_request(scope, receive, send)

    async def app(scope, receive, send):
        """Raw ASGI app with path routing."""
        nonlocal _task_group

        if scope["type"] == "lifespan":
            # Manage the task group lifecycle
            async with anyio.create_task_group() as tg:
                _task_group = tg
                await receive()  # startup
                await send({"type": "lifespan.startup.complete"})
                await receive()  # shutdown
                _task_group = None
                tg.cancel_scope.cancel()
                await send({"type": "lifespan.shutdown.complete"})
            return

        if scope["type"] != "http":
            return

        path = scope["path"]

        if path == "/health":
            response = JSONResponse({"status": "ok"})
            await response(scope, receive, send)
        elif path == "/mcp":
            await handle_mcp(scope, receive, send)
        else:
            response = JSONResponse({"error": "Not Found"}, status_code=404)
            await response(scope, receive, send)

    config = uvicorn.Config(app, host=host, port=port)
    srv = uvicorn.Server(config)
    await srv.serve()


def main(argv: Optional[list[str]] = None):
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="firekeep-symdex",
        description="Run the FirekeepSymdex MCP server.",
    )
    parser.parse_args(argv)
    mode = os.environ.get("FIREKEEP_SYMDEX_MODE", "stdio")
    if mode == "http":
        host = os.environ.get("FIREKEEP_SYMDEX_HOST", "0.0.0.0")
        port = int(os.environ.get("FIREKEEP_SYMDEX_PORT", "8090"))
        asyncio.run(run_http_server(host, port))
    else:
        asyncio.run(run_server())


if __name__ == "__main__":
    main()
