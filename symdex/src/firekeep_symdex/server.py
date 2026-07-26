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
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}, indent=2))]

    try:
        handler = tool["handler"]
        sig = inspect.signature(handler)
        if "storage_path" in sig.parameters and "storage_path" not in arguments:
            arguments["storage_path"] = storage_path

        if tool.get("is_async"):
            result = await handler(**arguments)
        else:
            result = handler(**arguments)

        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    except Exception as e:
        logger.exception("Tool %s failed: %s", name, e)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


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
