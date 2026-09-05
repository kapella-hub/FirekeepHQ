"""The MCP adapter: eight tools over stdio, and the one thread they run on.

Two things live here and nothing else does. The first is the envelope: a tool
call goes in as a name and an argument dict, and comes back as a JSON text
block (plus an image block when a screenshot was taken). Every failure —
including one the session raised — comes back inside that normal result
rather than as an MCP protocol error, because a model that receives an
exception can only give up, while one that receives `{"ok": false, "error":
"stale_ref: ..."}` knows to look again.

The second is `Worker`. On Windows, `uiautomation` binds COM to the first
thread that uses it, and every later call from another thread either fails or
returns nonsense. So the backend is constructed on one dedicated thread and
every subsequent backend, browser and session call is submitted to that same
thread for the life of the process. The asyncio handler awaits the result, so
the event loop stays free to answer pings and cancellations while a step runs.

The session itself holds no lock; the single worker is what makes that safe.
`hands_request_permit` runs there too — a wait of up to 55 seconds serialises
behind everything else, which is correct for a client that issues one tool
call at a time and is waiting for a human anyway.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import mcp.types as t
from mcp.server import Server

from firekeep_client import hooklog

from . import __version__
from .backends import load_backend
from .backends.base import HandsError
from .broker.client import BrokerClient
from .browser import Browser
from .config import load_config, load_policy
from .ids import machine_id
from .keep import KeepLink
from .session import HandsSession

SERVER_NAME = "firekeep-hands"

TOOLS = [
    t.Tool(
        name="hands_status",
        description=(
            "What Hands can do on this machine right now: platform, permissions, "
            "approval broker, current task."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    t.Tool(
        name="hands_task_start",
        description=(
            "Begin an operator task. Declares the goal and the apps you expect to touch; "
            "anything outside them is a boundary step that needs approval."
        ),
        inputSchema={
            "type": "object",
            "required": ["goal"],
            "properties": {
                "goal": {"type": "string"},
                "apps": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    t.Tool(
        name="hands_observe",
        description=(
            "Look at the screen: the active window's interactive controls with refs you "
            "can act on. detail=summary|controls|screenshot."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "detail": {"type": "string", "enum": ["summary", "controls", "screenshot"]},
                "app": {"type": "string"},
                "region": {"type": "array", "items": {"type": "integer"},
                           "minItems": 4, "maxItems": 4},
                "max_nodes": {"type": "integer"},
            },
        },
    ),
    t.Tool(
        name="hands_find",
        description=(
            "Find controls by name/value text in the active window (or a named app). "
            "Refs from hands_find are actable until the next act."
        ),
        inputSchema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "role": {"type": "string"},
                "app": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    ),
    t.Tool(
        name="hands_act",
        description=(
            "Do one thing: {kind: invoke|set_value|click|type|key|scroll|focus_app|open_app|"
            "open_url|clipboard_set|wait, ...}. Refs come from hands_observe/hands_find; raw "
            "coordinates are refused. A protected step returns needs_permit — call "
            "hands_request_permit, then repeat the same action with permit=<challenge>."
        ),
        inputSchema={
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {"type": "object"},
                "permit": {"type": "string"},
            },
        },
    ),
    t.Tool(
        name="hands_request_permit",
        description=(
            "Wait for the human to approve a protected step (chord on the keyboard or a tap "
            "on the dashboard). Returns the permit state."
        ),
        inputSchema={
            "type": "object",
            "required": ["challenge"],
            "properties": {
                "challenge": {"type": "string"},
                "wait_s": {"type": "integer"},
            },
        },
    ),
    t.Tool(
        name="hands_browser",
        description=(
            "Operate the Hands-managed browser: op=open|tabs|navigate|read|find|click|fill|"
            "screenshot. Navigating to a host outside the allowlist is a boundary step."
        ),
        inputSchema={
            "type": "object",
            "required": ["op"],
            "properties": {
                "op": {"type": "string"},
                "url": {"type": "string"},
                "ref": {"type": "string"},
                "text": {"type": "string"},
                "query": {"type": "string"},
                "tab": {"type": "string"},
                "permit": {"type": "string"},
            },
        },
    ),
    t.Tool(
        name="hands_task_end",
        description=(
            "Finish the task: outcome=done|failed|abandoned with a one-line summary. "
            "Releases the machine lease and closes the evidence ledger."
        ),
        inputSchema={
            "type": "object",
            "required": ["outcome"],
            "properties": {
                "outcome": {"type": "string", "enum": ["done", "failed", "abandoned"]},
                "summary": {"type": "string"},
            },
        },
    ),
]


class Worker:
    """One thread, for the life of the process, for every backend call.

    A `ThreadPoolExecutor` with `max_workers=1` spawns its thread on the
    first submission and keeps it — so whatever is submitted first is what
    binds COM, which is why `build_session` (and the `load_backend()` inside
    it) has to be the very first thing to go through here."""

    def __init__(self):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hands-backend")

    def run(self, fn, *args, **kwargs):
        """Synchronously, on the worker thread. Exceptions propagate to the
        caller unchanged."""
        return self.pool.submit(fn, *args, **kwargs).result()

    async def call(self, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, lambda: fn(*args, **kwargs))

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False)


def build_session() -> HandsSession:
    """Everything the server needs, built on the worker thread.

    The broker may legitimately be absent here — it is a separate process
    started by a logon task — so `from_disk()` returning None is not an
    error; the session re-probes when a protected step actually arrives."""
    config = load_config()
    session_id = os.environ.get("FIREKEEP_SESSION_ID") or uuid.uuid4().hex[:12]
    return HandsSession(
        backend=load_backend(),
        broker=BrokerClient.from_disk(),
        link=KeepLink(
            agent_id=os.environ.get("NEXUS_AGENT_ID") or "hands",
            machine_id=machine_id(),
            session_id=session_id,
        ),
        browser=Browser(kind=config.browser),
        config=config,
        policy=load_policy(),
        session_id=session_id,
    )


def _invoke(session: HandsSession, name: str, arguments: dict):
    if name == "hands_status":
        return session.status()
    if name == "hands_task_start":
        return session.task_start(arguments["goal"], arguments.get("apps"))
    if name == "hands_observe":
        return session.observe(
            detail=arguments.get("detail", "controls"),
            app=arguments.get("app"),
            region=arguments.get("region"),
            max_nodes=arguments.get("max_nodes"),
        )
    if name == "hands_find":
        return session.find(
            arguments["query"], role=arguments.get("role"), app=arguments.get("app"),
            limit=arguments.get("limit", 10),
        )
    if name == "hands_act":
        return session.act(arguments["action"], permit=arguments.get("permit"))
    if name == "hands_request_permit":
        return session.request_permit(arguments["challenge"], wait_s=arguments.get("wait_s", 45))
    if name == "hands_browser":
        return session.browser_op(
            arguments["op"],
            **{k: v for k, v in arguments.items() if k != "op"},
        )
    if name == "hands_task_end":
        return session.task_end(arguments["outcome"], arguments.get("summary", ""))
    raise HandsError("invalid_action", f"unknown tool {name!r}")


def dispatch(session: HandsSession, name: str, arguments: dict) -> list[t.ContentBlock]:
    """One tool call, start to finish, with nothing able to escape.

    Screenshot bytes never go into the JSON: they ride as a separate image
    block, so the text result stays readable and the same PNG is not carried
    twice."""
    try:
        result = _invoke(session, name, dict(arguments or {}))
    except HandsError as exc:
        result = {"ok": False, "error": f"{exc.code}: {exc}"}
    except KeyError as exc:  # a required argument the client did not send
        result = {"ok": False, "error": f"invalid_action: missing argument {exc}"}
    except Exception as exc:  # noqa: BLE001 — a crash here would kill the connection
        hooklog.log_failure("hands", f"{name} failed: {exc}", exc)
        result = {"ok": False, "error": f"backend: {exc}"}

    png = result.pop("screenshot_png", None) if isinstance(result, dict) else None
    blocks: list[t.ContentBlock] = [
        t.TextContent(type="text", text=json.dumps(result, default=str))
    ]
    if png:
        blocks.append(t.ImageContent(
            type="image",
            data=base64.b64encode(png).decode("ascii"),
            mimeType="image/png",
        ))
    return blocks


def build_server(session: HandsSession, worker: Worker) -> Server:
    """The MCP server bound to one session and one worker thread.

    The version is the wheel's own: left unset, the SDK advertises its own
    version as the server's, and a client asking "which Hands is this?" would
    be told which `mcp` it was built against."""
    server = Server(SERVER_NAME, version=__version__)

    @server.list_tools()
    async def list_tools() -> list[t.Tool]:
        return list(TOOLS)

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[t.ContentBlock]:
        return await worker.call(dispatch, session, name, arguments)

    return server


async def serve() -> None:
    from mcp.server.stdio import stdio_server

    worker = Worker()
    try:
        # First submission, so this thread is the one COM binds to.
        session = await worker.call(build_session)
        server = build_server(session, worker)
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        worker.shutdown()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog=SERVER_NAME,
        description="Run the Firekeep Hands MCP server on stdio.",
    )
    parser.parse_args(argv)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
