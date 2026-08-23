"""FirekeepSentinel MCP Server — environment observation tools for AI agents."""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP
from pydantic import ValidationError

from app.config import get_settings
from app.models import EventIngest
from app.redis_client import get_redis
from app.store import get_events, get_event_count, push_event, trim_by_age
from app.collectors.docker import run_docker_collector, get_collector as get_docker_collector
from app.collectors.git import run_git_collector, get_collector as get_git_collector
from app.collectors.files import run_file_collector, get_collector as get_file_collector

logger = logging.getLogger(__name__)

# Served in the MCP `initialize` handshake. This is the ONLY instruction channel
# that needs no client-side adapter, so it is the only one that reaches Codex (which
# has no hook surface and no instruction file) and a user who has deleted the
# rendered block from their own instruction file.
#
# It exists because of a real failure: a user asked "deploy to my vps" and the agent
# said it did not know, while the answer sat in memory as a 100%-confidence first
# result. Storage and retrieval worked; nothing triggered them. Tool descriptions do
# not fix this -- memory_recall's description already states its trigger and still
# does not fire (same lesson as decision_board in client 0.1.11).
#
# Keep it SHORT. It is sent once per session, not per request, but it competes for
# attention with everything else in the handshake.
# REACHABILITY (measured 2026-08-21): the shipped client kit does NOT receive
# this string. Every runtime mounts exactly one MCP entry -- the local gateway
# (FIREKEEP_MCP_KEYS = ("firekeep",), client/firekeep_client/adapters/base.py) --
# and the gateway discards each backend's `initialize` result and reads only
# tools/list (client/firekeep_client/gateway.py, Backend.start / Backend.discover).
# What a kit agent actually receives in its system prompt is the gateway's own
# GATEWAY_INSTRUCTIONS.
#
# So this text reaches ONLY a hand-configured client connected straight to this
# service's port (docs/INTEGRATIONS.md). That is a real audience and the reason
# the string stays -- but it means editing it changes nothing for any kit user.
# Adding behaviour here is the same trap adapters/base.py records having cost a
# release: a paragraph added where no runtime could see it. If you need an agent
# to do something, put it in GATEWAY_INSTRUCTIONS.
_INSTRUCTIONS = """Firekeep -- persistent team memory for agents.

Recall BEFORE answering, and treat not knowing as the trigger: if the user names a
host, IP, path, service, credential or convention you cannot name from the current
conversation ("my VPS", "our server"), or uses history words ("again", "still",
"last time", "how did we"), call memory_recall(task=<their request>) first. Never
claim you don't know about the user's own systems before calling it once. If a
result names a vault key, follow up with vault_retrieve.

Write as you go: ctx_update after each meaningful step, memory_learn the moment a
fix works (including what failed first), skill_create after a hard-won fix,
ctx_complete_session when done. Secrets go to vault_store, never memory_learn.
"""

mcp = FastMCP("FirekeepSentinel", instructions=_INSTRUCTIONS)

# ---------------------------------------------------------------------------
# Collector lifecycle — started once on first tool invocation
# ---------------------------------------------------------------------------

_collectors_started = False
_stop_event = asyncio.Event()


async def _retention_loop(redis, settings):
    """Periodically trim events older than the configured retention window."""
    while not _stop_event.is_set():
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
        if _stop_event.is_set():
            break
        try:
            await trim_by_age(redis, max_age_hours=settings.EVENT_RETENTION_HOURS)
        except Exception as e:
            logger.warning("Retention trim failed: %s", e)


async def _ensure_collectors():
    """Start collector background tasks on first call (idempotent)."""
    global _collectors_started
    if _collectors_started:
        return
    _collectors_started = True

    settings = get_settings()
    redis = await get_redis()

    # Opt-in: talking to the Docker socket is host-root-equivalent (see the note
    # on DOCKER_COLLECTOR_ENABLED in config.py). With it off, nothing in this
    # process ever opens the socket, and docker-compose.yml does not mount it.
    if settings.DOCKER_COLLECTOR_ENABLED:
        asyncio.create_task(run_docker_collector(redis, settings, _stop_event))
    else:
        logger.info(
            "Docker collector disabled (NS_DOCKER_COLLECTOR_ENABLED=false). "
            "Container states will be absent from /environment."
        )
    asyncio.create_task(run_git_collector(redis, settings, _stop_event))
    asyncio.create_task(run_file_collector(redis, settings, _stop_event))
    asyncio.create_task(_retention_loop(redis, settings))
    logger.info("Collectors started")

    # Initialize replay emitter so env_change events are recorded
    try:
        from replay.emitter import init_emitter
        await init_emitter()
        logger.info("Replay emitter initialized for Sentinel")
    except Exception as exc:
        logger.debug("Replay emitter init failed (non-critical): %s", exc)


# ---------------------------------------------------------------------------
# Shared handlers (used by MCP tools AND the SP1b REST routes)
# ---------------------------------------------------------------------------


async def handle_get_environment(redis) -> dict:
    """Full environment health detail — Redis, collectors, Docker container states.

    Shared by the sentinel_get_health MCP tool and the GET /environment REST
    route (Cortex briefing 'environment' section source). Returns the FULL detail
    (containers / healthy / container_count), not the leaner /health body.
    """
    try:
        await redis.ping()
        redis_status = "connected"
    except Exception:
        redis_status = "error"

    # A disabled collector is OMITTED, not reported False. The briefing's
    # _environment_summary renders any falsey entry as "Collector(s) degraded",
    # so reporting the opt-out as False would put a permanent fake fault in
    # every agent's session briefing -- the failure mode where a warning that is
    # always on stops being read.
    collectors = {
        "git": get_git_collector().healthy,
        "files": get_file_collector().healthy,
    }
    if get_settings().DOCKER_COLLECTOR_ENABLED:
        collectors["docker"] = get_docker_collector().healthy

    event_count = await get_event_count(redis) if redis_status == "connected" else 0

    container_states: dict[str, dict] = {}
    if redis_status == "connected":
        events = await get_events(redis, source="docker", limit=200)
        for ev in events:
            name = ev.get("details", {}).get("container", "")
            if name and name not in container_states:
                container_states[name] = {
                    "state": ev["details"].get("state", "unknown"),
                    "status": ev["details"].get("status", ""),
                    "last_event": ev["event_type"],
                    "last_seen": ev["timestamp"],
                }

    all_ok = redis_status == "connected"
    all_containers_healthy = all(c.get("state") == "running" for c in container_states.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "redis": redis_status,
        "collectors": collectors,
        "event_count": event_count,
        "healthy": all_containers_healthy if container_states else None,
        "containers": container_states,
        "container_count": len(container_states),
    }


async def handle_get_events(
    redis,
    source: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    limit: int = 20,
) -> dict:
    """Shared body for sentinel_get_events + the GET /events REST route."""
    events = await get_events(redis, source=source, event_type=event_type, severity=severity, limit=limit)
    total = await get_event_count(redis)
    return {"events": events, "total_in_stream": total, "returned": len(events)}


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def sentinel_get_events(
    source: str | None = None,
    event_type: str | None = None,
    severity: str | None = None,
    limit: int = 20,
) -> dict:
    """Get recent environment events observed by FirekeepSentinel.

    Call this to check what has changed in your environment — container state changes,
    new git commits, file modifications, or custom events pushed by other agents.

    Args:
        source: Filter by source — "docker", "git", "files", or a custom source name.
        event_type: Filter by event type — e.g. "container.running", "commit.new", "file.modified".
        severity: Filter by severity — "info", "warning", "error", "critical".
        limit: Maximum number of events to return (default 20).
    """
    await _ensure_collectors()
    redis = await get_redis()
    return await handle_get_events(redis, source=source, event_type=event_type, severity=severity, limit=limit)


@mcp.tool()
async def sentinel_get_health() -> dict:
    """Get health status of FirekeepSentinel — Redis connectivity, collector status,
    and Docker container states.

    Call this to quickly check if Sentinel is operational and all collectors are
    running, or to diagnose which component is down.
    """
    await _ensure_collectors()
    redis = await get_redis()
    return await handle_get_environment(redis)


@mcp.tool()
async def sentinel_push_event(
    source: str,
    event_type: str,
    summary: str,
    severity: str = "info",
    tags: list[str] | None = None,
) -> dict:
    """Push a custom observation event into the Sentinel event stream.

    Call this when your agent notices something noteworthy — a test failure,
    a deployment completing, an anomaly in logs, etc. Other agents can then
    query these events via sentinel_get_events.

    Args:
        source: Who is reporting — e.g. your agent name or "ci", "deploy".
        event_type: Dot-separated event category — e.g. "test.failed", "deploy.complete".
        summary: Human-readable one-line description of what happened.
        severity: Event severity — "info", "warning", "error", or "critical".
        tags: Optional tags for filtering.
    """
    await _ensure_collectors()

    if len(source) > 500:
        return {"error": "source must be 500 characters or fewer"}
    if len(event_type) > 200:
        return {"error": "event_type must be 200 characters or fewer"}
    if len(summary) > 10000:
        return {"error": "summary must be 10000 characters or fewer"}

    if severity not in ("info", "warning", "error", "critical"):
        return {"error": f"Invalid severity '{severity}'. Use info/warning/error/critical."}

    redis = await get_redis()
    settings = get_settings()
    entry_id = await push_event(
        redis, source, event_type, summary, {}, severity, tags or [],
        maxlen=settings.EVENT_MAXLEN,
    )
    return {"status": "accepted", "event_id": entry_id}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse


@mcp.custom_route("/health", methods=["GET"], name="health")
async def _health(request: StarletteRequest) -> StarletteJSONResponse:
    """Full health check including collector status — consumed by the SP1b-server GET /briefing environment section."""
    try:
        await _ensure_collectors()
        redis = await get_redis()

        try:
            await redis.ping()
            redis_status = "connected"
        except Exception:
            redis_status = "error"

        docker_collector = get_docker_collector()
        git_collector = get_git_collector()
        file_collector = get_file_collector()

        collectors = {
            "docker": docker_collector.healthy,
            "git": git_collector.healthy,
            "files": file_collector.healthy,
        }

        event_count = await get_event_count(redis) if redis_status == "connected" else 0

        all_ok = redis_status == "connected"
        return StarletteJSONResponse({
            "status": "ok" if all_ok else "degraded",
            "service": "sentinel",
            "redis": redis_status,
            "collectors": collectors,
            "event_count": event_count,
        })
    except Exception as e:
        logger.error("/health failed: %s", e)
        return StarletteJSONResponse(
            {"status": "error", "service": "sentinel", "collectors": {}},
            status_code=500,
        )


@mcp.custom_route("/environment", methods=["GET"], name="environment")
async def _environment(request: StarletteRequest) -> StarletteJSONResponse:
    """Full environment health detail for the Cortex briefing aggregator.

    NOT named /health/full: the auth middleware skip_paths=("/health",) is a
    prefix match, so /health/full would be silently auth-exempt. /environment
    sits under the auth gate (SP1b D5).
    """
    try:
        await _ensure_collectors()
        redis = await get_redis()
        return StarletteJSONResponse(await handle_get_environment(redis))
    except Exception as e:
        logger.error("GET /environment failed: %s", e)
        return StarletteJSONResponse(
            {"status": "error", "service": "sentinel"}, status_code=500,
        )


@mcp.custom_route("/events", methods=["GET"], name="events")
async def _events(request: StarletteRequest) -> StarletteJSONResponse:
    try:
        await _ensure_collectors()
        redis = await get_redis()
        source = request.query_params.get("source")
        event_type = request.query_params.get("event_type")
        severity = request.query_params.get("severity")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        return StarletteJSONResponse(
            await handle_get_events(redis, source, event_type, severity, limit)
        )
    except Exception as e:
        logger.error("GET /events failed: %s", e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/events", methods=["POST"], name="events_post")
async def _events_post(request: StarletteRequest) -> StarletteJSONResponse:
    """Authenticated ingest for the VPS failure puller (field-failure spec,
    'VPS ingest'). Accepts one EventIngest object or a list of them.
    Validation errors return 4xx -- NEVER swallowed into a default (the
    Literal-degrade gotcha this codebase has been bitten by before)."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body, not a server error
        return StarletteJSONResponse({"error": "invalid JSON body"}, status_code=400)

    items = body if isinstance(body, list) else [body]
    if len(items) > 1000:
        return StarletteJSONResponse({"error": "too many events (max 1000)"}, status_code=400)

    parsed: list[EventIngest] = []
    for item in items:
        if not isinstance(item, dict):
            return StarletteJSONResponse({"error": "each event must be an object"}, status_code=422)
        try:
            parsed.append(EventIngest(**item))
        except ValidationError as exc:
            return StarletteJSONResponse({"error": str(exc)[:2000]}, status_code=422)

    await _ensure_collectors()
    redis = await get_redis()
    settings = get_settings()
    for ev in parsed:
        await push_event(
            redis, ev.source, ev.event_type, ev.summary,
            ev.details, ev.severity, ev.tags, maxlen=settings.EVENT_MAXLEN,
        )
    return StarletteJSONResponse({"stored": len(parsed)}, status_code=202)


@mcp.custom_route("/version", methods=["GET"], name="version")
async def _version(request: StarletteRequest) -> StarletteJSONResponse:
    """Build provenance. Unauthenticated and probes no backends — answers
    'what code is running here?' without introspection, so it works even when
    the service's dependencies are down."""
    from provenance import get_version_info

    return StarletteJSONResponse(get_version_info("sentinel"))


if __name__ == "__main__":
    import os

    from auth.asgi import build_auth_middleware
    from auth.config import get_auth_settings

    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    host = os.getenv("NS_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("NS_MCP_PORT", "8060"))

    mcp.run(
        transport="http",
        host=host,
        port=port,
        stateless_http=True,
        middleware=build_auth_middleware(get_auth_settings(), skip_paths=("/health", "/version")),
    )
