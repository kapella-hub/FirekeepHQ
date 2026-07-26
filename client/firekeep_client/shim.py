"""firekeep-shim: stdio <-> Streamable-HTTP MCP transport bridge for one Firekeep service.

This is the ONLY module in firekeep_client permitted to import `mcp` and `httpx`.
The runtime spawns one `firekeep-shim --service <svc>` process per HTTP MCP server.
At spawn it resolves the ACTIVE profile, terminates internal-CA TLS itself, and
injects X-API-Key / X-Agent-Id on every request. Fail-loud on every error path;
NEVER logs the api_key.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import ssl
import sys

import anyio
import httpx
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server

from .resolver import (
    CONFIG_PATH,
    ConfigError,
    Endpoint,
    SERVICES,
    active_profile,
    is_bypassed,
    load_config,
    resolve,
)
from . import state, transport

PROG = "firekeep-shim"

# Bounded timeouts — a dead upstream must surface, never wedge the runtime (spec §5.2).
CONNECT_TIMEOUT = 10.0
SSE_READ_TIMEOUT = 300.0


def _stderr(message: str) -> None:
    """Emit one fail-loud line to stderr. Never contains the api_key (invariant)."""
    print(message, file=sys.stderr, flush=True)


def _config_location() -> str:
    return os.environ.get("FIREKEEP_CONFIG") or str(CONFIG_PATH)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="stdio<->Streamable-HTTP MCP bridge for one Firekeep service.",
    )
    parser.add_argument(
        "--service",
        required=True,
        help="one of: " + ", ".join(SERVICES),
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="resolve against this profile instead of [active] "
             "(adapters render this for pinned runtimes)",
    )
    return parser.parse_args(argv)


class _StashSessionAuth(httpx.Auth):
    """Attach X-Session-Id from the session stash onto every proxied request.

    Per-request (not a static default header) because ctx_start_session runs
    AFTER the shim spawns and each JSON-RPC message is its own POST — a static
    default would be permanently absent/stale for the whole connection. The
    server (_resolve_identity) treats an explicit session_id="unknown" as
    absent and falls through to this header, so a no-arg memory_recall gets
    attributed. Reads the stash fresh per request; skips injection while
    /personal bypass is on. Never raises — a stash hiccup must not break a
    proxied call.
    """

    def __init__(self, agent: str, profile: str) -> None:
        self._agent = agent
        self._profile = profile

    def auth_flow(self, request):
        try:
            if not is_bypassed():
                stash = state.read_session_stash(self._agent, self._profile)
                sid = (stash or {}).get("session_id")
                if sid:
                    request.headers["X-Session-Id"] = sid
        except Exception:
            pass  # attribution is best-effort; never block the request
        yield request


def build_client(
    endpoint: Endpoint, *, agent: str | None = None, profile: str | None = None
) -> httpx.AsyncClient:
    """httpx client with resolver-supplied auth headers + CA/verify policy.

    endpoint.verify is False (personal http) or a ca_path string (office https).
    httpx>=0.28 deprecates passing `verify=<str>` directly (DeprecationWarning
    from httpx._config.create_ssl_context); we build the ssl.SSLContext
    ourselves via ssl.create_default_context(cafile=...) so httpx never sees
    the raw path. verify=False passes through unchanged — building a context
    for it would be wrong (plain http, no TLS handshake ever occurs).

    follow_redirects=False: X-API-Key is a custom header, and httpx does NOT
    strip custom headers on cross-origin redirects. The internal API has no
    legitimate redirects, so we never follow one rather than risk replaying
    the api_key to an unexpected Location.
    """
    verify: bool | ssl.SSLContext
    if isinstance(endpoint.verify, str):
        # Handles both a ca_path file and the OS_TRUST sentinel ("os") — one
        # policy, defined once in transport.
        verify = transport._build_ssl_context(endpoint.verify)
    else:
        verify = endpoint.verify
    # Per-request X-Session-Id injection only when the caller identity is known
    # (agent + resolved profile). Static X-Agent-Id/X-API-Key stay as header
    # defaults; the session id is dynamic (set post-spawn) so it rides httpx.Auth.
    auth = _StashSessionAuth(agent, profile) if (agent and profile) else None
    return httpx.AsyncClient(
        headers=endpoint.headers,
        verify=verify,
        auth=auth,
        timeout=httpx.Timeout(
            CONNECT_TIMEOUT,
            read=SSE_READ_TIMEOUT,
            write=CONNECT_TIMEOUT,
            pool=CONNECT_TIMEOUT,
        ),
        follow_redirects=False,
    )


def _extract_session_id(result) -> str | None:
    """Pull session_id out of a tools/call result (structuredContent or the
    text content[0] JSON blob). Returns None on any shape mismatch. Never raises."""
    try:
        if not isinstance(result, dict):
            return None
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and structured.get("session_id"):
            return str(structured["session_id"])
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict) and parsed.get("session_id"):
                    return str(parsed["session_id"])
    except Exception:
        pass
    return None


class _BridgeSessionTap:
    """Bridge-shim frame interceptor (bridge service only): inject the stashed
    briefing_id into a ctx_start_session/resume the agent sends without one, and
    capture the returned session_id into the stash so the per-request Auth can
    attribute subsequent memory calls. Both hooks NEVER raise and ALWAYS return
    the (possibly-mutated-in-place) frame, so the pump forwards byte-identical on
    any error. The `_pending` request-id map is read/written synchronously (no
    await between check and set), so it is GIL-safe across the two pump directions.
    """

    # briefing_id is injected ONLY into ctx_start_session — bridge's
    # ctx_resume_session(session_id, agent_id) has no briefing_id param and
    # FastMCP rejects unexpected kwargs, so injecting it would break every
    # resume. Both start AND resume are tracked for session_id capture.
    _INJECT_TOOLS = frozenset({"ctx_start_session"})
    _CAPTURE_TOOLS = frozenset({"ctx_start_session", "ctx_resume_session"})
    _END_TOOLS = frozenset({"ctx_complete_session", "ctx_abandon_session"})

    def __init__(self, agent: str, profile: str) -> None:
        self._agent = agent
        self._profile = profile
        self._pending: dict = {}

    def on_request(self, item):
        """runtime->upstream: inject briefing_id (start only), remember id->tool."""
        try:
            root = item.message.root
            if getattr(root, "method", None) != "tools/call":
                return item
            params = getattr(root, "params", None)
            if not isinstance(params, dict):
                return item
            name = params.get("name")
            rid = getattr(root, "id", None)
            if rid is not None and (name in self._CAPTURE_TOOLS or name in self._END_TOOLS):
                self._pending[rid] = name
            if name in self._INJECT_TOOLS:
                args = params.get("arguments")
                if isinstance(args, dict) and not args.get("briefing_id"):
                    stash = state.read_session_stash(self._agent, self._profile) or {}
                    bid = stash.get("briefing_id")
                    if bid:
                        args["briefing_id"] = bid
        except Exception:
            pass
        return item

    def on_response(self, item):
        """upstream->runtime: capture session_id (start/resume) or clear (end)."""
        try:
            root = item.message.root
            rid = getattr(root, "id", None)
            if rid is None or rid not in self._pending:
                return item
            name = self._pending.pop(rid)
            if name in self._END_TOOLS:
                state.clear_session_stash(self._agent, self._profile)
                return item
            sid = _extract_session_id(getattr(root, "result", None))
            if sid:
                state.write_session_stash(self._agent, self._profile, session_id=sid)
        except Exception:
            pass
        return item


@contextlib.asynccontextmanager
async def _open_stdio(stdio_streams):
    """Yield (read_stream, write_stream): injected pair for tests, else real stdio."""
    if stdio_streams is not None:
        yield stdio_streams
    else:
        async with stdio_server() as (read_stream, write_stream):
            yield (read_stream, write_stream)


class UpstreamDisconnected(ConnectionError):
    """The upstream MCP connection closed while the runtime was still attached.

    Raised by `_bridge` when it detects the mcp SDK's notification-POST-failure
    swallow (see `_bridge`'s docstring for the mechanism). Subclasses the builtin
    `ConnectionError` so `_classify` routes it through the existing "unreachable"
    bucket with zero extra classification code — a connection that died mid-session
    and one that never connected both boil down to the same operator action (is the
    server up / VPN connected?).
    """


async def _pump(src, dst, tg, name: str, finishes: dict, transform=None) -> None:
    """Forward SessionMessages src->dst. Surface transport/parse errors (fail-loud);
    when either side closes, tear the whole bridge down so the sibling can't hang.

    Records *why* this pump ended in `finishes` so `_bridge` can tell a legitimate
    shutdown apart from a dead upstream connection:
    - `f"{name}_src_eof"`: the source stream drained/closed cleanly. For the stdio
      pump this means the runtime closed stdin (legitimate). For the http pump it
      means the upstream read side closed (suspicious — see `_bridge`).
    - `f"{name}_dst_broken"`: sending to the destination failed because it was
      already closed. For the stdio pump this means the upstream write side died
      (suspicious). For the http pump it means the runtime's stdio-write side died
      (a runtime-side problem, not an upstream one).
    Only the pump that finishes *first* ever records a reason: once either pump
    hits one of the two branches below it cancels the task group, and the sibling
    — however far through `src`/`dst` it had gotten — unwinds via cancellation
    (not caught here), so it never reaches its own `finishes.setdefault`.
    """
    try:
        async for item in src:
            if isinstance(item, Exception):
                # An error item on the upstream read stream — do NOT forward it as a
                # frame (it has no `.message`); raise so serve() classifies + exits.
                raise item
            if transform is not None:
                # Identity tap (bridge only). transform NEVER raises and always
                # returns a frame (byte-identical on any error), so this cannot
                # perturb the EOF/cancel-scope flow below. Kept synchronous — no
                # await inside — so its shared pending-map stays GIL-safe.
                item = transform(item)
            try:
                await dst.send(item)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                finishes.setdefault(f"{name}_dst_broken", True)
                return
        finishes.setdefault(f"{name}_src_eof", True)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream):
        finishes.setdefault(f"{name}_src_eof", True)
    finally:
        tg.cancel_scope.cancel()


async def _bridge(stdio_read, stdio_write, http_read, http_write,
                  *, req_transform=None, resp_transform=None) -> None:
    """Run both directions concurrently until one side closes.

    req_transform/resp_transform (bridge identity tap, optional) are applied to
    frames on the runtime->upstream and upstream->runtime directions
    respectively. They never raise and forward byte-identical on error, so the
    dead-connection detection below is unaffected.

    Dead-connection detection (Task 10 review carry-forward, CONFIRMED via repro):
    the mcp SDK's `post_writer` awaits any non-request POST (e.g. a notification —
    including `notifications/initialized`, sent right after every successful
    `initialize`) directly rather than via `tg.start_soon`. If that POST fails,
    `post_writer`'s own `except Exception: logger.exception(...)` logs-and-swallows
    it (never re-raises) and its `finally` closes both `read_stream_writer` (our
    `http_read`'s source) and `write_stream` (our `http_write` itself). Those
    closures are indistinguishable, at the anyio level, from an ordinary clean
    shutdown — `_pump` already treats EOF/closed-resource as "not an error" so the
    bridge never hangs — so left unchecked, `_bridge`/`serve`/`run()` return
    successfully (rc 0) on a dead upstream connection.

    We tell the two cases apart by *which side* finished *first*:
    - The runtime closing stdin (the `stdio` pump's source drains) is the only
      legitimate shutdown trigger. If that happened first, return normally even
      if the upstream side also looks closed by the time we check here — an
      ordinary shutdown can race the upstream tearing itself down too, and that
      race is not a failure worth reporting.
    - Otherwise, if the upstream side looks closed (the `http` pump's source
      drained, or the `stdio` pump's send into `http_write` failed because it was
      already closed) while the runtime was still attached, the connection died
      out from under us — raise `UpstreamDisconnected` so `run()` classifies it
      and exits non-zero instead of silently returning 0.
    """
    finishes: dict = {}
    async with anyio.create_task_group() as tg:
        tg.start_soon(_pump, stdio_read, http_write, tg, "stdio", finishes, req_transform)  # runtime -> upstream
        tg.start_soon(_pump, http_read, stdio_write, tg, "http", finishes, resp_transform)  # upstream -> runtime

    if finishes.get("stdio_src_eof"):
        return  # runtime closed stdin first: legitimate shutdown.

    if finishes.get("http_src_eof") or finishes.get("stdio_dst_broken"):
        raise UpstreamDisconnected(
            "upstream connection closed while the runtime was still attached "
            "(mcp SDK swallowed a failed notification POST and tore down its "
            "streams)"
        )


async def serve(service, endpoint, http_client=None, stdio_streams=None,
                *, agent=None, profile=None) -> None:
    """Open the stdio server and the Streamable-HTTP client, then pump between them.

    Transport errors (connection refused / 401 / TLS) surface out of the
    `streamable_http_client` context manager as an ExceptionGroup at __aexit__;
    they propagate to the caller (run()), which classifies and exits non-zero.

    agent/profile (when given) wire per-request X-Session-Id injection via
    build_client's httpx.Auth.
    """
    client = (http_client if http_client is not None
              else build_client(endpoint, agent=agent, profile=profile))
    owns_client = http_client is None
    # Bridge-only frame tap: inject briefing_id into ctx_start_session and
    # capture the returned session_id into the stash (the source of the
    # X-Session-Id header for every other shim). Other services get no tap.
    req_transform = resp_transform = None
    if service == "bridge" and agent and profile:
        tap = _BridgeSessionTap(agent, profile)
        req_transform, resp_transform = tap.on_request, tap.on_response
    try:
        async with _open_stdio(stdio_streams) as (stdio_read, stdio_write):
            async with streamable_http_client(
                endpoint.mcp_url, http_client=client
            ) as (http_read, http_write, _get_session_id):
                await _bridge(stdio_read, stdio_write, http_read, http_write,
                              req_transform=req_transform, resp_transform=resp_transform)
    finally:
        if owns_client:
            await client.aclose()


def _iter_causes(exc):
    """Yield exc and every nested exception: ExceptionGroup members + __cause__/__context__."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for sub in getattr(cur, "exceptions", ()) or ():  # ExceptionGroup / BaseExceptionGroup
            stack.append(sub)
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
        if cur.__context__ is not None:
            stack.append(cur.__context__)


def _classify(exc, *, service: str, url: str, profile: str) -> str:
    """Map a transport failure to a named fail-loud message (spec §5.2). Never includes the key."""
    causes = list(_iter_causes(exc))

    for c in causes:
        if (
            isinstance(c, httpx.HTTPStatusError)
            and c.response is not None
            and c.response.status_code == 401
        ):
            return (
                f"{PROG}: 401 from {service} at {url} (profile {profile}) — "
                f"check [{profile}] api_key"
            )

    for c in causes:
        if isinstance(c, ssl.SSLError):  # TLS must be checked before generic ConnectError
            return (
                f"{PROG}: TLS verify failed for {url} (profile {profile}) — "
                f"is ca_path installed/current?"
            )

    for c in causes:
        if isinstance(c, (httpx.ConnectError, httpx.ConnectTimeout, ConnectionError)):
            return (
                f"{PROG}: {service} unreachable at {url} (profile {profile}) — "
                f"is the server up / VPN connected?"
            )

    # Unknown transport error: name the type only — never str(exc) (may carry request/headers).
    return (
        f"{PROG}: {service} bridge failed via {url} (profile {profile}): "
        f"{type(exc).__name__}"
    )


def resolve_active(service: str, override: str | None = None) -> tuple[Endpoint, str]:
    """Resolve `service` against the ACTIVE profile (or the explicit pin override).
    Raises ConfigError on bad config."""
    cfg = load_config()
    profile = active_profile(cfg, override)
    endpoint = resolve(service, cfg=cfg, profile=profile)
    return endpoint, profile


def run(service: str, *, profile: str | None = None, http_client=None, stdio_streams=None) -> int:
    """Validate service, resolve the active (or pinned) profile, then run the bridge.

    Returns a process exit code. `profile` overrides [active]/FIREKEEP_PROFILE for this
    call (pinned runtimes). `http_client` / `stdio_streams` are injection seams for
    tests (Tasks 10-11); production passes neither.
    """
    if service == "symdex":
        _stderr(
            f"{PROG}: symdex is stdio-local (run firekeep-symdex directly) and is "
            f"never routed through the shim"
        )
        return 2
    if service not in SERVICES:
        _stderr(
            f"{PROG}: unknown service '{service}' — expected one of "
            f"{', '.join(SERVICES)}"
        )
        return 2

    # Hard bypass: under FIREKEEP_BYPASS (or a personal marker at spawn) serve an inert,
    # zero-tool MCP server — no config is resolved and NOTHING is proxied to the HTTP
    # service. This is startup-scoped (the running shim can't un-list tools mid-stream);
    # the live `/personal` marker is honored by the hooks, not here.
    if is_bypassed():
        return _serve_inert(service)

    try:
        endpoint, profile = resolve_active(service, profile)
    except ConfigError as exc:
        _stderr(
            f"{PROG}: config error for '{service}' — {exc}. "
            f"Check {_config_location()} for an [active] profile and its section."
        )
        return 1

    # Resolve the caller agent for X-Session-Id auto-injection. Best-effort: if
    # it fails, agent stays None and build_client simply attaches no Auth (the
    # header is absent, no regression) rather than failing the shim.
    agent: str | None = None
    try:
        from .resolver import agent_id, load_config
        agent = agent_id(load_config(), profile)
    except Exception:  # noqa: BLE001
        agent = None

    try:
        import functools
        anyio.run(functools.partial(
            serve, service, endpoint, http_client, stdio_streams,
            agent=agent, profile=profile,
        ))
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level fail-loud
        _stderr(_classify(exc, service=service, url=endpoint.mcp_url, profile=profile))
        return 1


def _serve_inert(service: str) -> int:
    """Serve an MCP server exposing ZERO tools — the FIREKEEP_BYPASS hard bypass.

    No HTTP client is built and nothing is proxied to the service; the agent simply
    sees no ``firekeep-<service>`` tools. Running a proper (empty) stdio server — rather
    than exiting immediately — gives a clean MCP handshake so the runtime logs no
    connection error. Returns when the client disconnects (session end)."""
    from mcp.server.fastmcp import FastMCP

    _stderr(f"{PROG}: {service} bypassed (personal mode / FIREKEEP_BYPASS) — 0 tools exposed")
    FastMCP(f"firekeep-{service}-bypassed").run()
    return 0


def _configure_logging() -> None:
    """Keep the SDK's internal logging off the operator surface.

    The client package configures no logging, so the mcp SDK's own
    `logger.exception(...)` calls (e.g. post_writer's swallow on a dead
    connection) would hit Python's lastResort handler and dump raw tracebacks
    to stderr next to our clean one-line fail-loud message. Our _classify
    message IS the operator surface; silence SDK internals unless the
    operator opts into debugging via FIREKEEP_SHIM_DEBUG=1.
    """
    if os.environ.get("FIREKEEP_SHIM_DEBUG"):
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
        return
    for name in ("mcp", "httpx", "httpcore", "anyio"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = parse_args(argv)
    return run(args.service, profile=args.profile)


if __name__ == "__main__":
    raise SystemExit(main())
