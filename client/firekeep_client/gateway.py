"""One local stdio MCP gateway for every Firekeep backend.

Each upstream remains an ordinary MCP server. The gateway owns only discovery,
tool-name routing, and failure isolation: one unavailable backend removes only
its tools, and ``firekeep_gateway_status`` explains the degraded inventory.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import sysconfig
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from firekeep_client import dexes, report
from firekeep_client.adapters.base import (
    CHAT_INSTRUCTIONS,
    CHAT_INSTRUCTIONS_HASH,
    GATEWAY_INSTRUCTIONS,
    GATEWAY_INSTRUCTIONS_HASH,
)
from firekeep_client.stdio import force_utf8_stdio, pin_import_paths


REMOTE_SERVICES = ("cortex", "bridge", "sentinel", "relay")
# What used to be LOCAL_SERVERS = ("symdex", "decision"). Decision is CORE
# infrastructure, not a dex — it indexes nothing, so nobody would ever want it
# off, and it stays unconditional. Symdex moved behind the dex registry
# (firekeep_client.dexes), which is what makes a second dex a data change rather
# than an edit to this file.
CORE_LOCAL_SERVERS = ("decision",)
STATUS_TOOL = {
    "name": "firekeep_gateway_status",
    "description": (
        "Report which Firekeep backends are reachable and why any backend's "
        "tools are absent. This is diagnostic only; plans never filter tools."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


# Named toolsets (FIREKEEP_TOOLSET). A toolset narrows the gateway to a curated
# surface for hosts where the full ~90 tools are wrong — the ChatGPT tunnel is
# the founding case: a consumer chat surface, prompt-injection-rich, that still
# deserves recall, sessions (prior art rides ctx_start_session) and the one
# write that makes chat valuable (memory_learn — the poisoning risk is carried
# by replay attribution, runtime: chatgpt, not prevented). Excluded outright:
# vault, corpus ingest, relay, backup, dex/code tools.
TOOLSET_PRESETS: dict[str, frozenset[str]] = {
    "chat": frozenset({
        "memory_recall", "memory_learn", "memory_feedback",
        "skill_recall", "skill_list",
        "ctx_start_session", "ctx_update", "ctx_complete_session",
        "ctx_abandon_session", "ctx_list_sessions", "ctx_resume_session",
        "ctx_get_shadow",
    }),
}

# A preset that narrows the tool surface also narrows the handshake text: the
# default GATEWAY_INSTRUCTIONS instructs agents to call vault_retrieve and
# decision_board, which the chat preset does not serve. An explicit
# FIREKEEP_TOOLS_ALLOW keeps the default text — the operator overrode the
# preset and owns the mismatch.
_PRESET_INSTRUCTIONS: dict[str, tuple[str, str]] = {
    "chat": (CHAT_INSTRUCTIONS, CHAT_INSTRUCTIONS_HASH),
}


def _active_toolset() -> tuple[str | None, frozenset[str] | None]:
    """(label, allowlist) from the environment.

    FIREKEEP_TOOLS_ALLOW (explicit comma-list) wins over FIREKEEP_TOOLSET.
    An UNKNOWN preset refuses to start rather than falling back to the full
    surface: this gateway may sit behind a tunnel reachable from a consumer
    chat host, and a typo must fail closed, not open ~90 tools."""
    allow = os.environ.get("FIREKEEP_TOOLS_ALLOW", "").strip()
    if allow:
        names = frozenset(n.strip() for n in allow.split(",") if n.strip())
        return "allowlist", names
    preset = os.environ.get("FIREKEEP_TOOLSET", "").strip()
    if not preset:
        return None, None
    if preset not in TOOLSET_PRESETS:
        raise SystemExit(
            f"firekeep gateway: unknown FIREKEEP_TOOLSET {preset!r} "
            f"(valid: {', '.join(sorted(TOOLSET_PRESETS))}); refusing to start "
            "rather than serve the full tool surface"
        )
    return preset, TOOLSET_PRESETS[preset]


def _slim_schema(schema: Any) -> Any:
    """Collapse Pydantic's ``X | None`` rendering into JSON Schema's type array.

    Pydantic emits an optional field as
    ``{"anyOf": [{"type": "X"}, {"type": "null"}]}`` — 43 characters where
    ``{"type": ["X", "null"]}`` is 24, for the same meaning. Measured on the
    live gateway (2026-08-21): 66 such fields across 98 tools. Tool definitions
    sit in the cached prompt prefix, so on a runtime WITHOUT tool deferral that
    is bytes re-sent every turn for the life of the session.

    Only the exact two-branch ``[T, null]`` shape collapses, and only into the
    type-array form. Dropping the null branch outright would be shorter and is
    deliberately NOT done: it would make an explicit ``null`` invalid where the
    server accepts it, which is a behaviour change wearing a token saving's
    clothes. ``tests/test_gateway_schema_slimming.py`` proves the equivalence
    against a real validator rather than asserting it.

    Never raises. A backend may serve any shape it likes, and a schema this
    cannot parse must reach the model unmodified rather than take the surface
    down — the same failure isolation the rest of the gateway is built on.
    """
    try:
        return _slim(schema)
    except Exception:  # noqa: BLE001 - pass-through beats a broken tool surface
        return schema


def _slim(node: Any) -> Any:
    if isinstance(node, list):
        return [_slim(item) for item in node]
    if not isinstance(node, dict):
        return node

    branches = node.get("anyOf")
    if isinstance(branches, list) and len(branches) == 2:
        nulls = [b for b in branches if b == {"type": "null"}]
        others = [b for b in branches if isinstance(b, dict) and b != {"type": "null"}]
        # Exactly one null branch and one non-null branch whose own `type` is a
        # plain string. Anything richer (a nested anyOf, a branch with no type,
        # a $ref) is left alone: this collapse is only provably lossless for
        # the simple shape, and "left alone" costs bytes, not correctness.
        if len(nulls) == 1 and len(others) == 1 and isinstance(others[0].get("type"), str):
            collapsed = {k: v for k, v in node.items() if k != "anyOf"}
            # The branch's own keywords (items, additionalProperties, ...) come
            # with it; the field's siblings (default, description) stay put.
            for key, value in others[0].items():
                if key != "type":
                    collapsed.setdefault(key, value)
            collapsed["type"] = [others[0]["type"], "null"]
            return {k: _slim(v) for k, v in collapsed.items()}

    return {k: _slim(v) for k, v in node.items()}


def _console_script(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    filename = f"{name}{suffix}"
    # The UNRESOLVED parent of sys.executable comes first. On POSIX the venv's
    # bin/python is a SYMLINK to the standalone CPython, so resolving it walks
    # out of the venv into a directory that holds no console scripts — which is
    # exactly how client 0.1.37 shipped a Linux gateway whose six backends all
    # reported executable-not-found while `firekeep doctor` stayed green
    # (Windows never hit it: Scripts\python.exe is a real file). The resolved
    # parent is kept as a later candidate, and sysconfig's scripts path covers
    # any layout where sys.executable is itself relocated.
    for directory in (
        Path(sys.executable).parent,
        Path(sysconfig.get_path("scripts")),
        Path(sys.executable).resolve().parent,
    ):
        candidate = directory / filename
        if candidate.exists():
            return str(candidate)
    found = shutil.which(name)
    return found or name


@dataclass
class Backend:
    name: str
    command: list[str]
    process: subprocess.Popen | None = None
    messages: queue.Queue = field(default_factory=queue.Queue)
    request_id: int = 0
    tools: list[dict[str, Any]] = field(default_factory=list)
    state: str = "not checked"
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            self.messages.put({"_eof": True})

    def start(self, protocol_version: str) -> None:
        if self.process and self.process.poll() is None:
            return
        self.messages = queue.Queue()
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        threading.Thread(target=self._reader, daemon=True).start()
        self.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "firekeep-gateway", "version": "1"},
            },
            timeout=8,
            initialize=False,
        )
        self.notify("notifications/initialized", {})

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise RuntimeError("backend process is not running")
        self.process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        )
        self.process.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        initialize: bool = True,
        protocol_version: str = "2025-03-26",
    ) -> dict[str, Any]:
        with self._lock:
            if initialize:
                self.start(protocol_version)
            assert self.process and self.process.stdin
            self.request_id += 1
            request_id = f"gateway:{self.name}:{self.request_id}"
            self.process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
                + "\n"
            )
            self.process.stdin.flush()
            deadline = time.monotonic() + timeout if timeout else None
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    raise TimeoutError(f"{self.name} timed out during {method}")
                try:
                    message = self.messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"{self.name} timed out during {method}") from exc
                if message.get("_eof"):
                    raise RuntimeError(f"{self.name} process closed")
                if message.get("id") == request_id:
                    return message

    def discover(self, protocol_version: str) -> None:
        try:
            response = self.request(
                "tools/list", timeout=8, protocol_version=protocol_version
            )
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "tools/list failed"))
            tools = (response.get("result") or {}).get("tools")
            if not isinstance(tools, list):
                raise RuntimeError("tools/list returned no tool array")
            self.tools = [tool for tool in tools if isinstance(tool, dict) and tool.get("name")]
            self.state = f"ready ({len(self.tools)} tools)"
        except Exception as exc:
            self.tools = []
            self.state = f"unavailable: {exc}"

    def close(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=2)
        except Exception:
            self.process.kill()


class Gateway:
    def __init__(self) -> None:
        # Seed the registry before reading it. This is the load fallback that
        # covers an update which never re-ran `firekeep install`: without it, an
        # existing install's first post-update session would find no dexes.json
        # and silently lose symdex. Never raises; a machine whose registry
        # cannot be seeded simply mounts no dexes.
        dexes.ensure_migrated()
        shim = _console_script("firekeep-shim")
        self.backends = [
            *(Backend(name, [shim, "--service", name]) for name in REMOTE_SERVICES),
            *(Backend(name, [_console_script(f"firekeep-{name}")])
              for name in CORE_LOCAL_SERVERS),
            # Only dexes that HAVE an MCP server to mount. An ingest-client dex
            # (docdex) is registered for lifecycle, doctor and its sync trigger,
            # and contributes no backend here.
            *(Backend(m.name, [_console_script(m.console_script)])
              for m in dexes.registered() if m.kind == "mcp-stdio"),
        ]
        self.protocol_version = "2025-03-26"
        self.routes: dict[str, Backend] = {}
        # Fail-closed at construction: an unknown preset never serves a request.
        self.toolset_label, self.toolset = _active_toolset()
        self.tools_filtered = 0

    def discover(self) -> list[dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=len(self.backends)) as pool:
            list(pool.map(lambda backend: backend.discover(self.protocol_version), self.backends))
        self.routes = {}
        self.tools_filtered = 0
        tools = [STATUS_TOOL]
        for backend in self.backends:
            for tool in backend.tools:
                name = str(tool["name"])
                if name in self.routes or name == STATUS_TOOL["name"]:
                    backend.state = f"degraded: duplicate tool name {name}"
                    continue
                # Toolset filter at the ROUTING layer: an excluded tool never
                # enters self.routes, so it is invisible in tools/list AND a
                # tools/call for it returns -32601 — enforcement, not decoration.
                if self.toolset is not None and name not in self.toolset:
                    self.tools_filtered += 1
                    continue
                self.routes[name] = backend
                # Slim the ADVERTISED schema only. Routing, calls and the
                # upstream's own validation are untouched: _slim_schema returns
                # new objects and the backend's tool dict is left as served.
                if isinstance(tool.get("inputSchema"), dict):
                    tool = {**tool, "inputSchema": _slim_schema(tool["inputSchema"])}
                tools.append(tool)
        return tools

    def status(self) -> dict[str, Any]:
        return {
            "backends": {backend.name: backend.state for backend in self.backends},
            "tool_count": len(self.routes),
            "plan_filtering": False,
            # Disclosure: a narrowed surface must say so. null when unfiltered.
            "toolset": self.toolset_label,
            "tools_filtered": self.tools_filtered,
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            self.protocol_version = params.get("protocolVersion") or self.protocol_version
            instructions, instructions_hash = _PRESET_INSTRUCTIONS.get(
                self.toolset_label or "", (GATEWAY_INSTRUCTIONS, GATEWAY_INSTRUCTIONS_HASH)
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    # version = the handshake text's content hash, so a session's
                    # recorded serverInfo names exactly which instruction text it
                    # received (round-2 measurement contract) — the hardcoded "1"
                    # said nothing. A preset that swaps the text swaps the hash.
                    "serverInfo": {"name": "firekeep", "version": instructions_hash},
                    "instructions": instructions,
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": self.discover()},
            }
        if method == "tools/call":
            name = params.get("name")
            if name == STATUS_TOOL["name"]:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(self.status(), indent=2)}
                        ]
                    },
                }
            backend = self.routes.get(str(name))
            if backend is None:
                self.discover()
                backend = self.routes.get(str(name))
            if backend is None:
                return self._error(request_id, -32601, f"unknown or unavailable tool: {name}")
            try:
                response = backend.request(
                    "tools/call", params, protocol_version=self.protocol_version
                )
            except Exception as exc:
                backend.state = f"unavailable: {exc}"
                report.emit("runtime", "gateway-call", exc=exc, backend=backend.name)
                return self._error(request_id, -32000, f"{backend.name} unavailable: {exc}")
            response["id"] = request_id
            return response
        return self._error(request_id, -32601, f"method not found: {method}")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def close(self) -> None:
        for backend in self.backends:
            backend.close()


def run(runtime: str | None = None) -> int:
    # The gateway's own stdio is the hop that was never configured: the BACKEND
    # pipes below are pinned to utf-8 (`Popen(..., encoding="utf-8")`), but this
    # process read `sys.stdin` and wrote `sys.stdout` at the platform default,
    # which on Windows is cp1252. Every non-ASCII character an agent wrote
    # through this gateway reached the server as mojibake. See stdio.py.
    force_utf8_stdio()
    # Long-running process launched through the `current` alias: freeze imports
    # to the venv we started under, so an update's flip mid-session can never
    # mix two client versions into this process. See stdio.pin_import_paths.
    pin_import_paths()
    # Flush point 2 (spec): the gateway mounts on EVERY runtime — including
    # codex/claude-desktop/generic, which have no hooks — making spool
    # delivery coverage uniform.
    report.flush()
    # Runtime identity (each adapter renders `firekeep gateway --runtime <name>`):
    # exported so the shim children this process spawns — the processes that make
    # the actual HTTP calls — attach the X-Firekeep-* attribution headers
    # (resolver._runtime_attribution). Absent on old rendered configs: no
    # export, no headers, everything else unchanged.
    if runtime:
        os.environ["FIREKEEP_RUNTIME"] = runtime
    gateway = Gateway()
    try:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                response = gateway.handle(message)
            except Exception as exc:
                report.emit("runtime", "gateway-dispatch", exc=exc)
                response = Gateway._error(None, -32603, f"gateway error: {exc}")
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
    finally:
        gateway.close()
    return 0
