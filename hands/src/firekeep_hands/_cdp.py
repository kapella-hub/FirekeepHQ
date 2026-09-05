"""The Chrome DevTools Protocol transport: find a browser binary, launch it
with a dedicated profile, and speak JSON-RPC over its DevTools websocket.

This is transport only — no Page/Runtime/Input semantics live here, those
belong to `browser.py`. What this module owns is the plumbing a JSON-RPC
websocket protocol needs that a single request/response call does not: a
background thread reading the socket (`websocket-client`'s `recv()` is
blocking, and requests/events interleave in arbitrary order), matching
replies to requests by id, and buffering unsolicited events per session so
`wait_event` can be called either before or after the event it is waiting
for actually arrives.

PLATFORM NOTE: browser discovery below covers Windows, macOS and Linux, but
this file has only been exercised in the live check (Task 9's report) on
Windows, against a real Chrome. The macOS/Linux paths are written to
documented install locations, unverified on hardware.
"""
from __future__ import annotations

import itertools
import json
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

import websocket

from .backends.base import HandsError

# How long `launch` waits for the browser to write `DevToolsActivePort`
# (first line: the port; second line: the browser-level websocket path)
# under the profile directory before giving up.
_LAUNCH_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.1

# The socket's post-connect read timeout: short enough that `_recv_loop`
# notices `close()` promptly, long enough to be a rare, harmless wakeup
# rather than a busy loop. This is NOT a signal that anything is wrong — an
# idle gap this long (an LLM thinking between actions) is the normal rhythm
# of a Hands session, which is exactly what the recv-loop bug below was
# missing.
_RECV_POLL_TIMEOUT_S = 1.0

# A ceiling per (session, method) event bucket: nothing in this module ever
# calls `wait_event` for a method no one is polling for, so an unbounded
# buffer would only grow if a caller genuinely stopped listening — a leak,
# not a feature. Oldest entries are dropped first, matching `wait_event`'s
# FIFO consumption.
_MAX_BUFFERED_EVENTS_PER_KEY = 200


def _windows_candidates(name: str) -> list[Path]:
    import os

    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LocalAppData", "")
    if name == "chrome":
        candidates = [
            Path(program_files) / "Google/Chrome/Application/chrome.exe",
            Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
        ]
        if local_app_data:
            candidates.append(Path(local_app_data) / "Google/Chrome/Application/chrome.exe")
        return candidates
    return [
        Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Microsoft/Edge/Application/msedge.exe",
    ]


def _macos_candidates(name: str) -> list[Path]:
    if name == "chrome":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    return [Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]


def _linux_candidates(name: str) -> list[str]:
    if name == "chrome":
        return ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    return ["microsoft-edge", "microsoft-edge-stable"]


def _candidates_for(name: str) -> list[Path | str]:
    """Every place `name` ("chrome" or "edge") might be installed, most
    likely first, for the current platform."""
    system = platform.system()
    if system == "Windows":
        return _windows_candidates(name)
    if system == "Darwin":
        return _macos_candidates(name)
    return _linux_candidates(name)


def _terminate_and_wait(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """`terminate()`, escalating to `kill()` if the process outlives
    `timeout`, waiting after each step. Every failure path that gives up on
    a launched browser routes through this — a `terminate()`/`kill()` with
    no matching `wait()` leaves the process unreaped and, worse, gives no
    guarantee the browser (and its lock on Hands' profile directory) is
    actually gone before this function returns."""
    process.terminate()
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass  # nothing more this process can do; the OS reaps it eventually


def _resolve_binary(kind: str) -> Path:
    """`kind` is "chrome", "edge", or "auto" (chrome, then edge). Raises
    `HandsError("backend", ...)` — not FileNotFoundError — so callers see the
    same closed error-code set as every other Hands failure."""
    if kind not in ("chrome", "edge", "auto"):
        raise HandsError("invalid_action", f"unknown browser kind: {kind!r}")
    names = ["chrome", "edge"] if kind == "auto" else [kind]
    for name in names:
        for candidate in _candidates_for(name):
            if isinstance(candidate, Path):
                if candidate.is_file():
                    return candidate
            else:
                found = shutil.which(candidate)
                if found:
                    return Path(found)
    raise HandsError("backend", "no Chrome or Edge found")


class CdpTransport:
    """One instance per running browser process. `send` is a blocking
    request/response call; `wait_event` blocks for an unsolicited event that
    may arrive before or after the call. Both are safe to call from any
    thread — the receive loop runs on its own."""

    def __init__(self, ws: "websocket.WebSocket", process: subprocess.Popen | None = None):
        self._ws = ws
        self._process = process
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._events: dict[tuple[str | None, str], list[dict]] = {}
        self._cond = threading.Condition()
        self._closed = False
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    # -- launching a browser -------------------------------------------

    @classmethod
    def launch(cls, kind: str, profile_dir: Path) -> "CdpTransport":
        """Starts a fresh Chrome/Edge against `profile_dir` (Hands' own
        profile — see `paths.chrome_profile_dir`, never the user's everyday
        one) with a random debugging port, and connects to the browser-level
        websocket once the browser has written it out.

        No `--headless`: Hands drives a visible window on purpose, so a
        person can see (and physically stop) whatever it's doing."""
        binary = _resolve_binary(kind)
        profile_dir.mkdir(parents=True, exist_ok=True)
        active_port_file = profile_dir / "DevToolsActivePort"
        active_port_file.unlink(missing_ok=True)  # stale from a prior, uncleanly-ended run

        argv = [
            str(binary),
            "--remote-debugging-port=0",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--password-store=basic",
            # Chrome >=111 rejects the DevTools websocket handshake outright
            # unless the connecting origin is allow-listed (a hardening
            # response to DNS-rebinding attacks on remote debugging).
            # websocket-client sends an Origin header derived from a random
            # local port, which cannot be predicted ahead of the handshake,
            # so this is the only viable value short of disabling the check
            # per-origin after the fact. Found on real hardware, not in the
            # documented flag list this method started from.
            "--remote-allow-origins=*",
            "--new-window",
            "about:blank",
        ]
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise HandsError("backend", f"could not start {binary}: {exc}") from exc

        try:
            port, ws_path = cls._wait_for_devtools_port(active_port_file, process)
            ws_url = f"ws://127.0.0.1:{port}{ws_path}"
            ws = websocket.create_connection(ws_url, timeout=10)
            # The `timeout=10` above governs the connect/handshake only; once
            # connected, shorten it to `_RECV_POLL_TIMEOUT_S` so `_recv_loop`
            # wakes up regularly to notice `close()` rather than blocking for
            # up to 10s on shutdown.
            ws.settimeout(_RECV_POLL_TIMEOUT_S)
        except Exception:
            _terminate_and_wait(process)
            raise
        return cls(ws, process)

    @staticmethod
    def _wait_for_devtools_port(path: Path, process: subprocess.Popen,
                                 timeout: float = _LAUNCH_TIMEOUT_S) -> tuple[int, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise HandsError(
                    "backend", f"browser exited before DevTools was ready (code {process.returncode})"
                )
            if path.exists():
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    lines = []
                if len(lines) >= 2 and lines[0].strip().isdigit():
                    return int(lines[0].strip()), lines[1].strip()
            time.sleep(_POLL_INTERVAL_S)
        _terminate_and_wait(process)
        raise HandsError("backend", "timed out waiting for the browser's DevTools port")

    # -- JSON-RPC ---------------------------------------------------------

    def send(self, method: str, params: dict | None = None, *, session: str | None = None,
              timeout: float = 10.0) -> dict:
        if self._closed:
            raise HandsError("backend", "the CDP connection is closed")
        message_id = next(self._ids)
        payload: dict = {"id": message_id, "method": method, "params": params or {}}
        if session is not None:
            payload["sessionId"] = session
        event = threading.Event()
        holder: dict = {}
        with self._lock:
            self._pending[message_id] = (event, holder)
        try:
            self._ws.send(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - any transport failure is fatal to this call
            with self._lock:
                self._pending.pop(message_id, None)
            raise HandsError("backend", f"{method} could not be sent: {exc}") from exc
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(message_id, None)
            raise HandsError("backend", f"{method} timed out after {timeout}s")
        if "error" in holder:
            raise HandsError("backend", f"{method} failed: {holder['error']}")
        return holder.get("result", {})

    def wait_event(self, name: str, *, session: str | None, timeout: float) -> dict | None:
        """The next occurrence of `name` on `session` — including one that
        already arrived and was buffered before this call started waiting.
        `None` on timeout, never a raise: a slow page is not a Hands error,
        it is information the caller (`Browser.navigate`) acts on."""
        key = (session, name)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                bucket = self._events.get(key)
                if bucket:
                    return bucket.pop(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def attach(self, target_id: str) -> str:
        """`Target.attachToTarget(flatten=True)` — flattened so every
        subsequent per-target command rides the top-level connection tagged
        with `sessionId`, rather than needing a second, nested websocket."""
        result = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return result["sessionId"]

    def close(self) -> None:
        if self._closed:
            return
        try:
            # Sent BEFORE `_closed` flips: `send` refuses once closed, so
            # doing it the other way around (a bug caught live — see the
            # task report) meant this call always raised, silently, and
            # closing fell all the way through to the 5s process-wait below
            # on every call rather than a graceful CDP shutdown.
            self.send("Browser.close", timeout=2.0)
        except HandsError:
            pass  # best-effort: the process is killed below regardless
        self._closed = True
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - already tearing down
            pass
        # `_recv_loop` is blocked in `recv()` for at most
        # `_RECV_POLL_TIMEOUT_S`, and `_ws.close()` above typically wakes it
        # sooner by making that `recv()` raise — either way, join it so the
        # thread is actually gone before `close()` returns rather than left
        # racing the process teardown below.
        self._recv_thread.join(timeout=_RECV_POLL_TIMEOUT_S + 2.0)
        if self._process is not None:
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _terminate_and_wait(self._process)

    # -- internals: the receive loop ---------------------------------------

    def _recv_loop(self) -> None:
        while True:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                # An idle socket, not a dead one — `ws.settimeout` above
                # means every `recv()` times out this often by design.
                # Treating this as a disconnect was a real bug (found in
                # review): any ~10s gap with no CDP traffic — an LLM
                # thinking between actions, the normal rhythm of a Hands
                # session — silently killed this thread for good, after
                # which every later `send()` timed out against a perfectly
                # healthy browser.
                continue
            except (websocket.WebSocketConnectionClosedException, OSError):
                break
            except Exception:  # noqa: BLE001 - any other read failure ends the loop too
                break
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if "id" in message:
                with self._lock:
                    waiter = self._pending.pop(message["id"], None)
                if waiter is not None:
                    event, holder = waiter
                    holder.update(message)
                    event.set()
                continue
            method = message.get("method")
            if not method:
                continue
            key = (message.get("sessionId"), method)
            with self._cond:
                bucket = self._events.setdefault(key, [])
                bucket.append(message.get("params", {}))
                if len(bucket) > _MAX_BUFFERED_EVENTS_PER_KEY:
                    del bucket[: len(bucket) - _MAX_BUFFERED_EVENTS_PER_KEY]
                self._cond.notify_all()
        # The socket is gone: wake every waiter so nothing blocks forever on
        # a browser that just died.
        with self._lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for event, holder in waiters:
            holder.setdefault("error", "CDP connection closed")
            event.set()
        with self._cond:
            self._cond.notify_all()
