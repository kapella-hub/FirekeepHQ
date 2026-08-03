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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from firekeep_client.adapters.base import GATEWAY_INSTRUCTIONS


REMOTE_SERVICES = ("cortex", "bridge", "sentinel", "relay")
LOCAL_SERVERS = ("symdex", "decision")
STATUS_TOOL = {
    "name": "firekeep_gateway_status",
    "description": (
        "Report which Firekeep backends are reachable and why any backend's "
        "tools are absent. This is diagnostic only; plans never filter tools."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _console_script(name: str) -> str:
    suffix = ".exe" if os.name == "nt" else ""
    beside_python = Path(sys.executable).resolve().parent / f"{name}{suffix}"
    if beside_python.exists():
        return str(beside_python)
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
        shim = _console_script("firekeep-shim")
        self.backends = [
            *(Backend(name, [shim, "--service", name]) for name in REMOTE_SERVICES),
            *(Backend(name, [_console_script(f"firekeep-{name}")]) for name in LOCAL_SERVERS),
        ]
        self.protocol_version = "2025-03-26"
        self.routes: dict[str, Backend] = {}

    def discover(self) -> list[dict[str, Any]]:
        with ThreadPoolExecutor(max_workers=len(self.backends)) as pool:
            list(pool.map(lambda backend: backend.discover(self.protocol_version), self.backends))
        self.routes = {}
        tools = [STATUS_TOOL]
        for backend in self.backends:
            for tool in backend.tools:
                name = str(tool["name"])
                if name in self.routes or name == STATUS_TOOL["name"]:
                    backend.state = f"degraded: duplicate tool name {name}"
                    continue
                self.routes[name] = backend
                tools.append(tool)
        return tools

    def status(self) -> dict[str, Any]:
        return {
            "backends": {backend.name: backend.state for backend in self.backends},
            "tool_count": len(self.routes),
            "plan_filtering": False,
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            self.protocol_version = params.get("protocolVersion") or self.protocol_version
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "firekeep", "version": "1"},
                    "instructions": GATEWAY_INSTRUCTIONS,
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


def run() -> int:
    gateway = Gateway()
    try:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                response = gateway.handle(message)
            except Exception as exc:
                response = Gateway._error(None, -32603, f"gateway error: {exc}")
            if response is not None:
                sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
                sys.stdout.flush()
    finally:
        gateway.close()
    return 0
