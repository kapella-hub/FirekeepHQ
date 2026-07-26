"""FirekeepBridge MCP Server — shadow context tools for AI agents."""

from __future__ import annotations

import atexit
import asyncio
import logging

from fastmcp import FastMCP

try:
    from fastmcp.server.dependencies import get_http_headers
except ImportError as exc:
    logging.getLogger(__name__).error(
        "fastmcp get_http_headers unavailable — header-based identity DISABLED; "
        "all MCP calls will default to unknown/default identity: %s",
        exc,
    )

    def get_http_headers(*_args, **_kwargs) -> dict[str, str]:
        """Fallback used when fastmcp does not provide get_http_headers."""
        return {}

from app.config import get_settings
from app.proactive_recall import fetch_relevant_memories
from app.redis_client import get_redis, close_redis
from app.session import SessionManager
from app.shadow import assemble_shadow

logger = logging.getLogger(__name__)

settings = get_settings()

from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(server):
    """Start the distillation worker for the lifetime of the server (SP0 D1)."""
    from app.distill_worker import close_distiller, distill_worker_loop

    worker_task = asyncio.create_task(distill_worker_loop())
    try:
        yield {}
    finally:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        await close_distiller()


mcp = FastMCP("FirekeepBridge", lifespan=_lifespan)


def _default_agent_id(agent_id: str | None = None) -> str:
    """Resolve agent identity: explicit param > X-Agent-Id header > "default".

    "default" is the sentinel tool default, so a connection header overrides
    it; any other explicit value wins (SP0 D3). get_http_headers() never
    raises and returns {} outside a request context.
    """
    if agent_id and agent_id != "default":
        return agent_id
    headers = get_http_headers()
    return headers.get("x-agent-id") or agent_id or "default"

# ---------------------------------------------------------------------------
# Replay emitter (fire-and-forget trace events)
# ---------------------------------------------------------------------------

_replay_initialized = False


async def _ensure_replay() -> None:
    """Lazy-init the replay emitter on first use."""
    global _replay_initialized
    if _replay_initialized:
        return
    _replay_initialized = True
    try:
        from replay.emitter import init_emitter
        await init_emitter()
        logger.info("Replay emitter initialized for Bridge")
    except Exception as exc:
        logger.debug("Replay emitter init failed (non-critical): %s", exc)


async def _replay_emit(event_type: str, session_id: str, agent_id: str, payload: dict, **kwargs) -> None:
    """Emit a replay event. Never raises, never blocks."""
    try:
        await _ensure_replay()
        from replay.emitter import emit
        await emit(event_type, session_id, agent_id, payload, **kwargs)
    except Exception as exc:
        logger.debug("Replay emit failed (non-critical): %s", exc)

# ---------------------------------------------------------------------------
# Detached background tasks (SP0 D5)
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Run a coroutine as a detached task, holding a strong reference so the
    event loop cannot garbage-collect it mid-flight."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task

async def _get_manager() -> SessionManager:
    r = await get_redis()
    return SessionManager(r, get_settings())


async def _shutdown() -> None:
    """Cleanup Redis and HTTP connections on shutdown."""
    await close_redis()
    logger.info("FirekeepBridge shutdown complete")


def _atexit_cleanup() -> None:
    """Best-effort cleanup at interpreter exit (fix #6)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_shutdown())
        else:
            loop.run_until_complete(_shutdown())
    except RuntimeError:
        pass


atexit.register(_atexit_cleanup)


async def _trigger_eval(api_url: str, session_id: str, max_retries: int = 3):
    """Trigger eval computation on Cortex with retry. Fire-and-forget."""
    import httpx
    headers: dict[str, str] = {}
    if settings.FIREKEEP_API_KEY:
        headers["X-API-Key"] = settings.FIREKEEP_API_KEY
    last_error: str | None = None
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{api_url}/evals/sessions/{session_id}/compute",
                    headers=headers,
                )
                if resp.status_code < 400:  # 2xx success
                    return True
                if resp.status_code < 500:  # 4xx permanent failure — don't retry
                    logger.warning(
                        "Eval trigger permanent failure for session %s: HTTP %d",
                        session_id, resp.status_code,
                    )
                    return False
                # 5xx transient — retry
                last_error = f"HTTP {resp.status_code}"
                logger.debug(
                    "Eval trigger transient failure for session %s (attempt %d): %s",
                    session_id, attempt + 1, last_error,
                )
        except Exception as exc:
            last_error = f"connection error: {exc}"
            logger.debug(
                "Eval trigger transient failure for session %s (attempt %d): %s",
                session_id, attempt + 1, last_error,
            )
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
    logger.warning(
        "Eval trigger failed for session %s after %d retries: %s",
        session_id, max_retries, last_error,
    )
    return False


async def _trigger_skill_evaluate(api_url: str, session_id: str, skill_worthy: bool = False) -> bool:
    """Fire-and-forget POST /skill/evaluate on Cortex."""
    import httpx
    headers: dict[str, str] = {}
    if settings.FIREKEEP_API_KEY:
        headers["X-API-Key"] = settings.FIREKEEP_API_KEY
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{api_url}/skill/evaluate",
                json={"session_id": session_id, "skill_worthy": skill_worthy},
                headers=headers,
            )
            return resp.status_code < 400
    except Exception as exc:
        logger.debug("Skill evaluate trigger failed for session %s: %s", session_id, exc)
        return False


@mcp.tool()
async def ctx_start_session(
    goal: str,
    agent_id: str = "default",
    tags: list[str] | None = None,
    project: str | None = None,
    briefing_id: str | None = None,
) -> dict:
    """Start a new working session with a goal description.

    Call this when beginning a new task. If you already have an active session,
    it will be automatically paused.

    Args:
        goal: What you are trying to accomplish (e.g., "implement namespace normalization").
        agent_id: Your agent identifier (use different IDs for parallel terminals).
        tags: Optional categorization tags.
        project: Optional project name for team memory attribution. Stored with
            the session and forwarded to Cortex when the session is distilled.
        briefing_id: Optional id minted by the server-side GET /briefing endpoint.
            Stored on the session so the pre-flight A/B tip-shown recording can be
            attributed to this session (closes the strategy-pattern feedback loop).
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    result = await mgr.start_session(goal, agent_id=agent_id, tags=tags, project=project, briefing_id=briefing_id)

    # Replay: trace session start
    sid = result.get("session_id", "")
    if sid:
        await _replay_emit(
            "session_start", sid, agent_id,
            {"goal": goal, "tags": tags or []},
        )

    return result


@mcp.tool()
async def ctx_update(
    category: str, content: str, key: str | None = None, agent_id: str = "default"
) -> dict:
    """Update your working context with new information.

    Call this as you work to record your plan, decisions, file knowledge, and progress.
    This data persists across context compressions and session restarts.

    Args:
        category: What to update — "plan", "decision", "file", "progress", or "scratch".
        content: The content to store. For "plan", send the full current plan (Markdown checklist).
        key: Required for "file" (file path) and "scratch" (entry name). Ignored for others.
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    try:
        result = await mgr.update(category, content, key=key, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    # Replay: trace context update
    session_id = await mgr.get_active_session_id(agent_id)
    if session_id:
        # Context snapshots only at decision points (plan/decision categories)
        ctx_ref = None
        if category in ("decision", "plan"):
            try:
                from replay.emitter import store_context_snapshot
                from app.shadow import assemble_shadow
                data = await mgr.get_session_data(session_id)
                if data:
                    ctx_ref = await store_context_snapshot(assemble_shadow(data))
            except Exception:
                pass
        await _replay_emit(
            "ctx_update", session_id, agent_id,
            {"category": category, "content_length": len(content), "key": key},
            context_ref=ctx_ref,
        )

    # Proactive recall: fetch relevant memories for qualifying categories
    if (
        settings.PROACTIVE_RECALL_ENABLED
        and category in settings.PROACTIVE_RECALL_CATEGORIES.split(",")
    ):
        try:
            session_id = await mgr.get_active_session_id(agent_id)
            if session_id:
                memories = await fetch_relevant_memories(
                    content,
                    api_url=settings.FIREKEEP_API_URL,
                    api_key=settings.FIREKEEP_API_KEY,
                    namespace=settings.FIREKEEP_NAMESPACE,
                    top_k=settings.PROACTIVE_RECALL_TOP_K,
                    min_score=settings.PROACTIVE_RECALL_MIN_SCORE,
                )
                if memories:
                    await mgr.set_proactive_memories(session_id, memories)
                    result["proactive_memories"] = len(memories)
        except Exception as exc:
            logger.debug("Proactive recall trigger failed (non-fatal): %s", exc)

    return result


@mcp.tool()
async def ctx_get_shadow(session_id: str | None = None, agent_id: str = "default") -> dict:
    """Retrieve your full working context as a Markdown document.

    Call this after context compression or when starting a new conversation to restore
    your working state. Returns everything: your plan, decisions, file knowledge,
    progress, and scratchpad.

    Args:
        session_id: Specific session to retrieve (defaults to your active session).
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    if session_id is None:
        session_id = await mgr.get_active_session_id(agent_id)
    if not session_id:
        return {"error": "No active session. Start one with ctx_start_session."}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found."}

    shadow = assemble_shadow(data)
    return {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": data.get("status", ""),
        "shadow": shadow,
    }


@mcp.tool()
async def ctx_complete_session(
    session_id: str | None = None, outcome: str | None = None,
    agent_id: str = "default", skill_worthy: bool = False,
) -> dict:
    """Mark the current session as completed and save learnings to long-term memory.

    Call this when your task is done. The session is enqueued for distillation into a
    FirekeepCortex memory (a background worker drains the queue with retry/backoff) so
    future sessions can benefit from what you learned.

    Args:
        session_id: Session to complete (defaults to active session).
        outcome: Summary of what was accomplished.
        agent_id: Your agent identifier.
        skill_worthy: Set True if this session involved a hard-won fix worth saving as a skill.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    try:
        result = await mgr.complete_session(session_id=session_id, outcome=outcome, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    sid = result["session_id"]

    # Replay: trace session end
    await _replay_emit(
        "session_end", sid, agent_id,
        {"outcome": outcome or "", "distilled": False},
        outcome="success",
    )

    # D1: distillation is enqueued by SessionManager.complete_session and
    # drained by the distill worker with retry/backoff. No inline distillation.
    result["distillation"] = "queued"

    # Auto-eval: detached fire-and-forget (SP0 D5) — completion must return
    # as soon as the session state + distill enqueue are committed.
    _spawn_background(_trigger_eval(settings.FIREKEEP_API_URL, sid))
    result["eval_triggered"] = "scheduled"

    # Skill synthesis: trigger async (fire-and-forget)
    skill_ok = await _trigger_skill_evaluate(settings.FIREKEEP_API_URL, sid, skill_worthy)
    result["skill_synthesis_triggered"] = skill_ok

    return result


@mcp.tool()
async def ctx_abandon_session(session_id: str | None = None, agent_id: str = "default") -> dict:
    """Abandon a session without saving to long-term memory.

    Use this for sessions started by mistake or no longer relevant.

    Args:
        session_id: Session to abandon (defaults to active session).
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    try:
        result = await mgr.abandon_session(session_id=session_id, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    # Replay: trace session abandoned
    sid = result.get("session_id", session_id or "")
    if sid:
        await _replay_emit(
            "session_end", sid, agent_id,
            {"outcome": "abandoned", "distilled": False},
            outcome="partial",
        )

        # Auto-eval on abandon: detached fire-and-forget (SP0 D5)
        _spawn_background(_trigger_eval(settings.FIREKEEP_API_URL, sid))

    return result


@mcp.tool()
async def ctx_list_sessions(
    status: str | None = None, agent_id: str | None = None, limit: int = 10
) -> dict:
    """List recent sessions.

    Args:
        status: Filter by status — "active", "paused", "completed", "abandoned".
        agent_id: Filter by agent ID.
        limit: Maximum number of sessions to return.
    """
    mgr = await _get_manager()
    sessions = await mgr.list_sessions(status=status, agent_id=agent_id, limit=limit)
    return {"sessions": sessions}


@mcp.tool()
async def ctx_resume_session(session_id: str, agent_id: str = "default") -> dict:
    """Resume a paused session and get its working context.

    Args:
        session_id: The session ID to resume.
        agent_id: Your agent identifier.
    """
    agent_id = _default_agent_id(agent_id)
    mgr = await _get_manager()
    try:
        await mgr.resume_session(session_id, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    data = await mgr.get_session_data(session_id)
    if not data:
        return {"error": f"Session {session_id} not found after resume."}

    shadow = assemble_shadow(data)
    return {
        "session_id": session_id,
        "goal": data.get("goal", ""),
        "status": "active",
        "shadow": shadow,
    }


from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse
from auth.asgi import require_scope_asgi, ScopeError
from app.distill_worker import requeue_dlq as requeue_distill_dlq_records


@mcp.custom_route("/ops/distill-dlq/requeue", methods=["POST"], name="requeue_distill_dlq")
async def _requeue_distill_dlq(request: StarletteRequest) -> StarletteJSONResponse:
    """Requeue dead-lettered distillation jobs (admin) — Bridge's one
    state-changing ops route, mirroring cortex POST /ops/dlq/requeue.
    Expired sessions (post-DLQ 7d TTL) are dropped-with-log, counted
    expired_dropped; see distill_worker.requeue_dlq."""
    try:
        require_scope_asgi(request, "admin")
        try:
            limit = int(request.query_params.get("limit", "1000"))
        except (ValueError, TypeError):
            limit = 1000
        limit = min(max(limit, 1), 10_000)
        redis = await get_redis()
        result = await requeue_distill_dlq_records(redis, get_settings(), limit=limit)
        return StarletteJSONResponse({"queue": "distill_dlq", **result})
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("POST /ops/distill-dlq/requeue failed: %s", e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/sessions", methods=["GET"], name="list_sessions")
async def _list_sessions(request: StarletteRequest) -> StarletteJSONResponse:
    """List sessions with optional file data. Consumed by the SP1b-server
    GET /briefing aggregator (resumable-sessions source) and the
    session-resumption flow."""
    try:
        status_filter = request.query_params.get("status")
        agent_filter = request.query_params.get("agent_id")
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        limit = min(max(limit, 1), 200)

        mgr = await _get_manager()
        sessions = await mgr.list_sessions(
            status=status_filter, agent_id=agent_filter, limit=limit,
        )

        # Enrich with files for the briefing's resumables view (active sessions only, to limit cost)
        for sess in sessions:
            if sess.get("status") == "active":
                data = await mgr.get_session_data(sess["session_id"])
                if data:
                    sess["files"] = data.get("files", {})

        return StarletteJSONResponse({"sessions": sessions})
    except Exception as e:
        logger.error("GET /sessions failed: %s", e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/sessions/{session_id}", methods=["GET"], name="get_session")
async def _get_session(request: StarletteRequest) -> StarletteJSONResponse:
    """REST endpoint: get a single session by ID including its shadow data."""
    try:
        session_id = request.path_params["session_id"]
        mgr = await _get_manager()
        data = await mgr.get_session_data(session_id)
        if data is None:
            return StarletteJSONResponse({"error": "Session not found"}, status_code=404)
        shadow = assemble_shadow(data)
        return StarletteJSONResponse({
            "session_id": session_id,
            "goal": data.get("goal", ""),
            "outcome": data.get("outcome", ""),
            "duration_seconds": data.get("duration_seconds"),
            "shadow": shadow,
        })
    except Exception as e:
        logger.error("GET /sessions/%s failed: %s", request.path_params.get("session_id", ""), e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


async def handle_post_session_context(mgr: SessionManager, *, agent_id: str, category: str, content: str, key: str | None = None) -> dict:
    """Testable handler — REST equivalent of the ctx_update MCP tool.

    Lets Relay persist Bridge decisions for origin:"mcp" scope sessions
    (SP2 D-S18), which have no local companion to call ctx_update over MCP.
    Raises ValueError on bad input or no active Bridge session for agent_id,
    same as ctx_update.
    """
    return await mgr.update(category, content, key=key, agent_id=agent_id)


@mcp.custom_route("/sessions/{agent_id}/context", methods=["POST"], name="post_session_context")
async def _post_session_context(request: StarletteRequest) -> StarletteJSONResponse:
    try:
        require_scope_asgi(request, "session:write")
        agent_id = request.path_params["agent_id"]
        body = await request.json()
        category = body.get("category")
        content = body.get("content")
        key = body.get("key")
        if not category or content is None:
            return StarletteJSONResponse({"error": "category and content are required"}, status_code=400)

        mgr = await _get_manager()
        result = await handle_post_session_context(mgr, agent_id=agent_id, category=category, content=content, key=key)
        return StarletteJSONResponse(result)
    except ScopeError as e:
        return StarletteJSONResponse({"error": e.detail}, status_code=e.status_code)
    except ValueError as e:
        return StarletteJSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("POST /sessions/%s/context failed: %s", request.path_params.get("agent_id", ""), e)
        return StarletteJSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/health", methods=["GET"], name="health")
async def _health(request: StarletteRequest) -> StarletteJSONResponse:
    return StarletteJSONResponse({"status": "ok", "service": "bridge"})


@mcp.custom_route("/version", methods=["GET"], name="version")
async def _version(request: StarletteRequest) -> StarletteJSONResponse:
    """Build provenance. Unauthenticated and probes no backends — answers
    'what code is running here?' without introspection, so it works even when
    the service's dependencies are down."""
    from provenance import get_version_info

    return StarletteJSONResponse(get_version_info("bridge"))


if __name__ == "__main__":
    from auth.asgi import build_auth_middleware
    from auth.config import get_auth_settings

    mcp.run(
        transport="http",
        host=settings.MCP_HOST,
        port=settings.MCP_PORT,
        stateless_http=True,
        middleware=build_auth_middleware(get_auth_settings(), skip_paths=("/health", "/version")),
    )
