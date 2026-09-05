"""The broker's loopback HTTP API and the process that runs it.

Four routes, all bearer-authenticated, and deliberately none of them grants
a permit: a caller can ask for one (`POST /permits`), watch it
(`GET /permits/<challenge>`) and spend it once it has been granted
(`POST /permits/<challenge>/consume`). Granting happens only in the input
listener thread or the phone bridge thread. If you are adding a route here
and it writes `approved`, stop — that is the boundary this whole wheel
exists to hold.

The socket binds 127.0.0.1 only and the bearer token is minted per run and
written 0600 alongside the port, so a process on the machine needs read
access to the user's own `~/.firekeep` to talk to the broker at all. That is
a real limit and not a strong one — a process running as this user has it —
which is exactly why possession of the token buys the ability to *ask*, not
the ability to answer.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from firekeep_client import hooklog

from .. import paths
from ..config import _write_json_atomic, load_config
from ..ids import machine_id
from ..keep import KeepLink
from .permits import PermitStore
from .phone import PhoneBridge

log = logging.getLogger(__name__)

# A permit request is a handful of short strings. Anything larger is either a
# bug or someone probing; refuse it before it is parsed, let alone stored.
_MAX_BODY = 16 * 1024
_DRAIN_CAP = 1024 * 1024

_CONSUME_ROUTE = re.compile(r"^/permits/([^/]{1,256})/consume$")
_PERMIT_ROUTE = re.compile(r"^/permits/([^/]{1,256})$")


class _BadRequest(Exception):
    pass


class _TooLarge(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    """One request. `broker` is filled in by the subclass `BrokerServer`
    builds per instance, so several brokers in one process (the test suite)
    never share state through a class attribute."""

    broker: "BrokerServer" = None  # type: ignore[assignment]
    # HTTP/1.0: every response closes the connection, so a route that
    # refuses a request never has to reason about leftover body bytes on a
    # kept-alive socket.
    protocol_version = "HTTP/1.0"
    server_version = "FirekeepHandsBroker"
    sys_version = ""

    def log_message(self, fmt, *args):  # noqa: A002 - stdlib signature
        """Silence. The broker runs detached with its output at DEVNULL, and
        an access log of a loopback approval channel is a record of when a
        human approved what — not something to write by default."""
        log.debug("broker http: " + fmt, *args)

    # -- plumbing ---------------------------------------------------------

    def _authorised(self) -> bool:
        presented = self.headers.get("Authorization", "") or ""
        expected = f"Bearer {self.broker.token}"
        return secrets.compare_digest(
            presented.encode("utf-8", "replace"), expected.encode("utf-8")
        )

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _content_length(self) -> int:
        raw = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw)
        except ValueError:
            raise _BadRequest("bad Content-Length") from None
        if length < 0:
            raise _BadRequest("bad Content-Length")
        return length

    def _drain(self) -> None:
        """Read and discard a body we are about to refuse. Closing the socket
        with unread bytes in the receive buffer makes some clients see a
        connection reset instead of the status code that explains why."""
        try:
            remaining = min(self._content_length(), _DRAIN_CAP)
        except _BadRequest:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                return
            remaining -= len(chunk)

    def _read_json_body(self) -> dict:
        length = self._content_length()
        if length > _MAX_BODY:
            raise _TooLarge(f"body over {_MAX_BODY} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _BadRequest("body is not JSON") from None
        if not isinstance(data, dict):
            raise _BadRequest("body is not a JSON object")
        return data

    # -- routes -----------------------------------------------------------

    def do_GET(self):  # noqa: N802 - stdlib naming
        if not self._authorised():
            return self._json(401, {"error": "unauthorised"})
        path = urlsplit(self.path).path
        if path == "/health":
            return self._json(200, self.broker.health())
        match = _PERMIT_ROUTE.match(path)
        if match:
            permit = self.broker.store.get(unquote(match.group(1)))
            if permit is None:
                return self._json(404, {"error": "no such permit"})
            return self._json(200, self.broker.permit_json(permit))
        return self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib naming
        if not self._authorised():
            self._drain()
            return self._json(401, {"error": "unauthorised"})
        path = urlsplit(self.path).path
        try:
            if path == "/permits":
                return self._create_permit(self._read_json_body())
            match = _CONSUME_ROUTE.match(path)
            if match:
                self._drain()
                return self._consume_permit(unquote(match.group(1)))
        except _TooLarge as exc:
            self._drain()
            return self._json(413, {"error": str(exc)})
        except _BadRequest as exc:
            return self._json(400, {"error": str(exc)})
        self._drain()
        return self._json(404, {"error": "not found"})

    def _unsupported(self):
        """Authenticate first, then refuse. A caller without the token learns
        nothing about which methods or paths exist."""
        if not self._authorised():
            self._drain()
            return self._json(401, {"error": "unauthorised"})
        self._drain()
        return self._json(405, {"error": "method not allowed"})

    do_PUT = _unsupported
    do_PATCH = _unsupported
    do_DELETE = _unsupported

    def _create_permit(self, data: dict):
        challenge = data.get("challenge")
        title = data.get("title", "")
        classes = data.get("classes", [])
        task_id = data.get("task_id", "")
        step_index = data.get("step_index", 0)
        if not isinstance(challenge, str) or not (0 < len(challenge) <= 256):
            raise _BadRequest("challenge must be a non-empty string")
        if not isinstance(title, str) or len(title) > 500:
            raise _BadRequest("title must be a string of at most 500 characters")
        if not isinstance(classes, list) or not all(isinstance(c, str) for c in classes):
            raise _BadRequest("classes must be a list of strings")
        if not isinstance(task_id, str) or len(task_id) > 256:
            raise _BadRequest("task_id must be a string")
        if not isinstance(step_index, int) or isinstance(step_index, bool) or step_index < 0:
            raise _BadRequest("step_index must be a non-negative integer")
        permit = self.broker.store.request(
            challenge=challenge,
            title=title,
            classes=tuple(classes),
            task_id=task_id,
            step_index=step_index,
        )
        return self._json(201, self.broker.permit_json(permit))

    def _consume_permit(self, challenge: str):
        store = self.broker.store
        if store.get(challenge) is None:
            return self._json(404, {"error": "no such permit"})
        if store.consume(challenge):
            return self._json(200, {"state": "consumed"})
        current = store.get(challenge)
        return self._json(409, {"state": current.state if current else "expired"})


class BrokerServer:
    """Owns the socket, the token and `broker.json`.

    `listeners` is held by reference, not copied: the chord thread flips its
    own entry to "unavailable" if the OS hook dies, and `/health` (and so the
    kit's doctor row) tells the truth about it without a restart."""

    def __init__(self, store: PermitStore, *, chord: str, listeners: dict[str, str]):
        self.store = store
        self.chord = chord
        self.listeners = listeners
        self.token: str | None = None
        self.port: int | None = None
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def health(self) -> dict:
        return {
            "ok": True,
            "chord": self.chord,
            "listeners": dict(self.listeners),
            "pending": len(self.store.pending()),
        }

    def permit_json(self, permit) -> dict:
        """What a permit looks like on the wire. `expires_in_s` rather than
        the raw deadline because the store's clock is monotonic and means
        nothing in another process."""
        return {
            "challenge": permit.challenge,
            "title": permit.title,
            "classes": list(permit.classes),
            "task_id": permit.task_id,
            "step_index": permit.step_index,
            "state": permit.state,
            "via": permit.via,
            "expires_in_s": max(0.0, round(permit.expires_at - self.store.now(), 3)),
        }

    def start(self) -> tuple[int, str]:
        if self._httpd is not None:
            raise RuntimeError("broker server already started")
        self.token = secrets.token_urlsafe(32)
        handler = type("_BoundHandler", (_Handler,), {"broker": self})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self.port = int(httpd.server_address[1])
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="hands-broker-http", daemon=True
        )
        self._thread.start()
        self._write_info()
        return self.port, self.token

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd, self._thread = None, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        self._remove_info()

    def _write_info(self) -> None:
        _write_json_atomic(
            paths.broker_info_path(),
            {
                "port": self.port,
                "token": self.token,
                "pid": os.getpid(),
                "started_at": time.time(),
                "chord": self.chord,
            },
        )

    def _remove_info(self) -> None:
        """Only remove the file if it still describes THIS broker — another
        broker that started meanwhile owns it now, and deleting its file
        would leave a running broker nothing can find."""
        path = paths.broker_info_path()
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if info.get("pid") == os.getpid() and info.get("port") == self.port:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _configure_logging() -> None:
    """Off unless asked for. `FIREKEEP_HANDS_LOG=DEBUG` is what makes the
    macOS tap print `(keycode, flags, userData, sourceStateID)` for the
    source-state measurement that has not been done on real hardware yet."""
    level = (os.environ.get("FIREKEEP_HANDS_LOG") or "").strip().upper()
    if not level:
        return
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _chord_listener(cfg, store: PermitStore, listeners: dict[str, str]) -> threading.Thread | None:
    """Start the platform listener, or report why there is none.

    A listener that cannot be installed (no Input Monitoring permission on
    macOS, an unsupported platform) must leave `listeners["chord"]` saying
    so rather than pretend: with no chord and no phone, every protected step
    is refused, and the human needs to be told which of the two is missing."""
    if sys.platform == "win32":
        from .listeners.win import ChordTracker, run_listener
    elif sys.platform == "darwin":
        from .listeners.mac import ChordTracker, run_listener
    else:
        log.info("no chord listener on %s", sys.platform)
        return None

    try:
        tracker = ChordTracker(cfg.chord, cfg.deny_chord)
    except ValueError as exc:
        hooklog.log_failure("hands", f"unusable chord in config ({exc}); using defaults", exc)
        from ..config import HandsConfig

        defaults = HandsConfig()
        cfg.chord, cfg.deny_chord = defaults.chord, defaults.deny_chord
        tracker = ChordTracker(cfg.chord, cfg.deny_chord)

    def on_decision(decision: str) -> None:
        permit = store.decide_oldest(decision, via="chord")
        log.info("chord %s -> %s", decision, permit.challenge if permit else "nothing pending")

    def pump() -> None:
        try:
            run_listener(tracker, on_decision)
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            listeners["chord"] = "unavailable"
            hooklog.log_failure("hands", f"chord listener stopped: {exc}", exc)

    thread = threading.Thread(target=pump, daemon=True, name="hands-chord")
    thread.start()
    listeners["chord"] = "active"
    return thread


def run(argv=None) -> int:
    """The broker process. Blocks until SIGINT/SIGTERM, then stops cleanly.

    The wait loop has a timeout because a bare `Event().wait()` on Windows is
    not interruptible by Ctrl+C — the console handler cannot run while the
    main thread sits in an uninterruptible wait."""
    _configure_logging()
    cfg = load_config()
    store = PermitStore(ttl_s=cfg.permit_ttl_s)
    listeners = {"chord": "unavailable", "phone": "offline"}

    _chord_listener(cfg, store, listeners)

    bridge = None
    link = KeepLink(
        agent_id=os.environ.get("NEXUS_AGENT_ID") or f"hands-{machine_id()[:8]}",
        machine_id=machine_id(),
    )
    if not link.offline:
        bridge = PhoneBridge(store, link)
        bridge.start()
        listeners["phone"] = "active"

    server = BrokerServer(store, chord=cfg.chord, listeners=listeners)
    port, _token = server.start()
    log.info("firekeep-hands-broker listening on 127.0.0.1:%s", port)

    stop = threading.Event()

    def _on_signal(signum, _frame):
        log.info("broker stopping on signal %s", signum)
        stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass  # not the main thread, or the platform has no such signal

    try:
        while not stop.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if bridge is not None:
            bridge.stop()
        server.stop()
    return 0
