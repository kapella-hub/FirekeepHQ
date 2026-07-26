"""firekeep_client.sidecar — session-lifecycle daemon (stdlib-only).

Owns the universal session-lifecycle concerns that must work on EVERY runtime,
including MCP-only ones (Codex): presence register on start, heartbeat loop,
periodic workspace snapshot, clean deregister on exit. Separate from the shim
(which the runtime spawns). Reads the active profile LIVE via the resolver on
each cycle, so a `firekeep profile use` flip is picked up without a restart.

All server calls are JSON-RPC `tools/call` POSTs to the resolved `mcp_url` via
`transport.post_json` — the same surface the retired bash hooks used
(relay_register / relay_heartbeat_presence / relay_deregister on Relay;
ctx_update on Bridge). `transport.post_json` extracts the single `data:` SSE
frame and returns the parsed JSON-RPC result.

Importing this module has NO side effects: no daemon, no signals, no
subprocess. `mark_registered` / `should_deregister` live in `firekeep_client.
state` -- the single keying authority for the shared registration-race guard
(SP1b Task 19 seam reconciliation). This module imports them by name; the
hook cores (`hooks/session_start.py`, `hooks/stop.py`) call the same
functions via `state.mark_registered(...)` / `state.should_deregister(...)`.
Neither path spawns a daemon. All daemon/signal work runs inside
`Sidecar.run()` / `main()`.

OWNERSHIP (controller decision, SP1b Task 19): presence lifecycle is owned by
the HOOKS (session_start.py/stop.py) for Claude Code, and by the SIDECAR for
MCP-only runtimes that have no hook lifecycle events (codex, kiro). Both may
be alive for the same agent_id in a mixed composition; the shared guard in
`firekeep_client.state` is what makes that safe -- whichever registered most
recently wins, and the other backs off instead of clobbering it.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading

from firekeep_client import hooklog, resolver, state, transport
from firekeep_client.hooks import _git
from firekeep_client.state import (
    REGISTRATION_RACE_WINDOW,  # noqa: F401 - deliberate re-export; tests monkeypatch sidecar.REGISTRATION_RACE_WINDOW
    mark_registered,
    should_deregister,
)

HOOK = "sidecar"
DEFAULT_INTERVAL = 60           # heartbeat cadence, seconds
DEFAULT_SNAPSHOT_EVERY = 5      # snapshot every Nth heartbeat (mirrors poll's %5)


def _pid_scratch(agent_id: str) -> str:
    return f"sidecar-{agent_id}-pid"


# --- singleton liveness ------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Best-effort, cross-platform liveness check for the single-instance lock."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
        except Exception:
            return False
        return (
            out.returncode == 0
            and "No tasks" not in out.stdout
            and str(pid) in out.stdout
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Sidecar:
    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        snapshot_every: int = DEFAULT_SNAPSHOT_EVERY,
        timeout: float = transport.DEFAULT_TIMEOUT,
        workdir: str | None = None,
        goal: str | None = None,
        post_json=transport.post_json,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.interval = interval
        self.snapshot_every = snapshot_every
        self.timeout = timeout
        self.workdir = workdir or os.getcwd()
        self.goal = goal or os.environ.get("FIREKEEP_AGENT_GOAL", "Session started")
        self.hostname = socket.gethostname()
        self._post_json = post_json
        self._stop = stop_event or threading.Event()

    # -- helpers --------------------------------------------------------------

    def _tool_call(self, endpoint, tool: str, arguments: dict):
        """POST a JSON-RPC tools/call to endpoint.mcp_url; return parsed result.

        transport.post_json strips the single `data:` SSE frame and returns the
        parsed JSON-RPC envelope; raises transport.TransportError on non-2xx.
        """
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        return self._post_json(
            endpoint.mcp_url, body,
            headers=endpoint.headers, timeout=self.timeout, verify=endpoint.verify,
        )

    @staticmethod
    def _errored(parsed) -> bool:
        """True if the JSON-RPC envelope or the wrapped tool payload signals an
        error. Relay/Bridge tools return {"error": ..., "status": "unavailable"}
        with HTTP 200 on internal failure, so non-2xx detection alone is not
        enough — mirror debrief's `grep '"error"'`."""
        try:
            return '"error"' in json.dumps(parsed)
        except (TypeError, ValueError):
            return True

    # -- lifecycle operations (profile read LIVE each call) -------------------

    def register(self) -> None:
        if resolver.is_bypassed():
            return  # personal mode: no presence reaches Relay
        try:
            cfg = resolver.load_config()
            profile = resolver.active_profile(cfg)
            aid = resolver.agent_id(cfg, profile)
            ep = resolver.resolve("relay", cfg=cfg, profile=profile)
            parsed = self._tool_call(ep, "relay_register", {
                "agent_id": aid, "goal": self.goal, "hostname": self.hostname,
            })
            if self._errored(parsed):
                hooklog.log_failure(
                    HOOK, f"relay_register in-band error: {json.dumps(parsed)[:300]}")
            else:
                mark_registered(aid, profile=profile)
        except (transport.TransportError, OSError) as e:  # OSError incl. ssl.SSLError: a malformed office ca_path raises RAW SSLError
        # from _build_ssl_context (outside transport's own try) — must degrade, not crash
            hooklog.log_failure(HOOK, f"relay_register failed: {e}")

    def heartbeat(self) -> None:
        if resolver.is_bypassed():
            return  # personal mode: no presence heartbeat reaches Relay
        try:
            cfg = resolver.load_config()
            profile = resolver.active_profile(cfg)
            aid = resolver.agent_id(cfg, profile)
            sid = state.resolve_session_id({}, cfg)
            ep = resolver.resolve(
                "relay", cfg=cfg, profile=profile,
                session_id=None if sid == "unknown" else sid,
            )
            args = {"agent_id": aid, "goal": self.goal}
            if sid != "unknown":
                args["session_id"] = sid
            parsed = self._tool_call(ep, "relay_heartbeat_presence", args)
            if self._errored(parsed):
                hooklog.log_failure(
                    HOOK,
                    f"relay_heartbeat_presence in-band error: {json.dumps(parsed)[:300]}")
        except (transport.TransportError, OSError) as e:  # OSError incl. ssl.SSLError: a malformed office ca_path raises RAW SSLError
        # from _build_ssl_context (outside transport's own try) — must degrade, not crash
            hooklog.log_failure(HOOK, f"relay_heartbeat_presence failed: {e}")

    def snapshot(self) -> None:
        if resolver.is_bypassed():
            return  # personal mode: no workspace snapshot reaches Bridge
        try:
            cfg = resolver.load_config()
            profile = resolver.active_profile(cfg)
            aid = resolver.agent_id(cfg, profile)
            sid = state.resolve_session_id({}, cfg)
            if sid == "unknown":
                return  # no active Bridge session to attach the snapshot to
            ep = resolver.resolve("bridge", cfg=cfg, profile=profile, session_id=sid)
            parsed = self._tool_call(ep, "ctx_update", {
                "category": "scratch",
                "key": "workspace_snapshot",
                "content": self._collect_snapshot(),
                "agent_id": aid,
            })
            if self._errored(parsed):
                hooklog.log_failure(
                    HOOK,
                    f"ctx_update(workspace_snapshot) in-band error: {json.dumps(parsed)[:300]}")
        except (transport.TransportError, OSError) as e:  # OSError incl. ssl.SSLError: a malformed office ca_path raises RAW SSLError
        # from _build_ssl_context (outside transport's own try) — must degrade, not crash
            hooklog.log_failure(HOOK, f"ctx_update(workspace_snapshot) failed: {e}")

    def _collect_snapshot(self) -> str:
        """SP1b Task 19 Part C: delegates to hooks/_git.py's workspace_snapshot
        (the ONE git-snapshot implementation, shared with stop.py/prompt.py) --
        was a near-duplicate inline implementation with drifted timeout (10 vs
        _git.py's 5, now unified to 10) and no cwd support upstream."""
        return _git.workspace_snapshot(cwd=self.workdir)

    def deregister(self) -> None:
        if resolver.is_bypassed():
            return  # personal mode: skip the deregister comm (mirrors stop.py)
        try:
            cfg = resolver.load_config()
            profile = resolver.active_profile(cfg)
            aid = resolver.agent_id(cfg, profile)
            if not should_deregister(aid, profile=profile):
                return  # newer session took over — race guard
            ep = resolver.resolve("relay", cfg=cfg, profile=profile)
            parsed = self._tool_call(ep, "relay_deregister", {"agent_id": aid})
            if self._errored(parsed):
                hooklog.log_failure(
                    HOOK, f"relay_deregister in-band error: {json.dumps(parsed)[:300]}")
            state.clear_registered(aid, profile=profile)
        except (transport.TransportError, OSError) as e:  # OSError incl. ssl.SSLError: a malformed office ca_path raises RAW SSLError
        # from _build_ssl_context (outside transport's own try) — must degrade, not crash
            hooklog.log_failure(HOOK, f"relay_deregister failed: {e}")

    # -- singleton + run loop -------------------------------------------------

    def _acquire_singleton(self) -> bool:
        """Best-effort single-instance-per-agent lock via state scratch PID.
        Returns False if a live sidecar already owns this agent identity."""
        cfg = resolver.load_config()
        aid = resolver.agent_id(cfg, resolver.active_profile(cfg))
        existing = state.read_scratch(_pid_scratch(aid))
        if existing is not None:
            try:
                pid = int(existing)
            except (TypeError, ValueError):
                pid = -1
            if pid != os.getpid() and _pid_alive(pid):
                hooklog.log_failure(
                    HOOK, f"sidecar already running for {aid} (pid {pid}); exiting")
                return False
        state.write_scratch(_pid_scratch(aid), str(os.getpid()))
        return True

    def _release_singleton(self) -> None:
        try:
            cfg = resolver.load_config()
            aid = resolver.agent_id(cfg, resolver.active_profile(cfg))
            if state.read_scratch(_pid_scratch(aid)) == str(os.getpid()):
                state.delete_scratch(_pid_scratch(aid))
        except Exception:
            pass

    def stop(self) -> None:
        self._stop.set()

    def run(self, *, max_iterations: int | None = None) -> None:
        if not self._acquire_singleton():
            return
        self.register()
        ticks = 0
        try:
            while not self._stop.is_set():
                if max_iterations is not None and ticks >= max_iterations:
                    break
                if self._stop.wait(self.interval):
                    break
                ticks += 1
                try:
                    self.heartbeat()
                    if self.snapshot_every > 0 and ticks % self.snapshot_every == 0:
                        self.snapshot()
                except Exception as e:  # failure isolation: one bad tick never kills the daemon
                    hooklog.log_failure(HOOK, f"heartbeat/snapshot cycle error: {e}")
        finally:
            self.deregister()
            self._release_singleton()


def _install_signal_handlers(sidecar: "Sidecar") -> None:
    """Wire SIGINT/SIGTERM to a clean stop.

    Guarantees: SIGINT (Ctrl-C) and normal exit deregister cleanly (via run()'s
    `finally`). On Windows `os.kill(pid, SIGTERM)` terminates the process WITHOUT
    delivering the handler, so SIGTERM is not a clean-shutdown path there — SIGINT
    and normal exit are. How `stop.run` signals a *running* sidecar to exit on
    Windows is a separate open seam (out of scope here). Registration is
    best-effort: a no-op off the main thread or where the signal is unsupported.
    """
    import signal

    def _handler(signum, frame):
        sidecar.stop()

    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    interval = float(os.environ.get("FIREKEEP_SIDECAR_INTERVAL", DEFAULT_INTERVAL))
    sc = Sidecar(interval=interval)
    _install_signal_handlers(sc)
    sc.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
