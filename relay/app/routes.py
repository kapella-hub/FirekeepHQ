"""Starlette REST routes for dashboard access to presence, DMs, and status."""

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.presence import who_is_online, deregister

logger = logging.getLogger(__name__)


# -- Handler functions (testable without Starlette) ---------------------

async def handle_get_presence(redis, include_idle: bool = True) -> dict:
    agents = await who_is_online(redis, include_idle)
    return {"agents": agents, "count": len(agents)}


async def handle_get_single_presence(redis, agent_id: str) -> dict | None:
    from app.presence import PRESENCE_PREFIX, ACTIVE_THRESHOLD
    import time
    key = f"{PRESENCE_PREFIX}{agent_id}"
    data = await redis.hgetall(key)
    if not data:
        return None
    now = time.time()
    try:
        last_hb = float(data.get("last_heartbeat", 0))
    except (ValueError, TypeError):
        last_hb = 0
    data["status"] = "active" if (now - last_hb) < ACTIVE_THRESHOLD else "idle"
    return data


async def handle_delete_presence(redis, agent_id: str) -> dict:
    return await deregister(redis, agent_id)


# -- DM handler functions -----------------------------------------------

async def handle_get_dm(redis, agent_id: str, unread_only: bool = False) -> dict:
    from app.dm import get_dms
    messages = await get_dms(redis, agent_id, unread_only)
    return {"agent_id": agent_id, "messages": messages, "count": len(messages)}


async def handle_post_dm(redis, agent_id: str, content: str, from_id: str) -> dict:
    from app.dm import send_dm
    msg = await send_dm(redis, agent_id, content, from_id)
    return {"status": "sent", "message": msg}


async def handle_mark_dm_read(redis, agent_id: str) -> dict:
    from app.dm import mark_read
    count = await mark_read(redis, agent_id)
    return {"agent_id": agent_id, "marked_read": count}


async def handle_get_status(redis) -> dict:
    """Return relay status: channels, bulletin count, active claims/leases.

    Mirrors the relay_status MCP tool output; consumed by the SP1b-server GET /briefing aggregator.
    """
    from app.pubsub import get_active_channels
    from app.bulletin import get_bulletin_count

    channels = await get_active_channels(redis)
    bulletin_count = await get_bulletin_count(redis)

    # Collect claim keys
    claims = []
    keys = [k async for k in redis.scan_iter("nr:claim:*", count=100)]
    if keys:
        async with redis.pipeline() as pipe:
            for k in keys:
                pipe.get(k)
                pipe.ttl(k)
            results = await pipe.execute()
        for key, data, ttl in zip(keys, results[0::2], results[1::2]):
            if data is None:
                continue
            try:
                claim = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            resource = key.replace("nr:claim:", "")
            claims.append({
                "resource": resource,
                "agent_id": claim.get("agent_id", "unknown"),
                "expires_in": max(ttl, 0),
            })

    # Collect lease keys
    lease_keys = [k async for k in redis.scan_iter("nr:lease:*", count=100)]
    if lease_keys:
        async with redis.pipeline() as pipe:
            for k in lease_keys:
                pipe.get(k)
                pipe.ttl(k)
            lease_results = await pipe.execute()
        for key, data, ttl in zip(lease_keys, lease_results[0::2], lease_results[1::2]):
            if data is None:
                continue
            try:
                lease = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            resource = key.replace("nr:lease:", "")
            claims.append({
                "resource": resource,
                "agent_id": lease.get("holder_id", "unknown"),
                "fencing_token": lease.get("fencing_token"),
                "expires_in": max(ttl, 0),
                "type": "lease",
            })

    return {
        "channels": channels,
        "channel_count": len(channels),
        "bulletin_count": bulletin_count,
        "active_claims": len(claims),
        "claims": claims,
    }


# -- Briefing REST handlers (SP1b — server-side GET /briefing substrate) ------

async def handle_get_tasks(
    redis, assignee: str | None = None, status: str | None = None, limit: int = 20,
    oldest_first: bool = False, title: str | None = None,
) -> dict:
    """Wrap relay_task_list logic for the Cortex briefing aggregator.

    Same body as the relay_task_list MCP tool (mcp_server.py:488) minus the
    tool decorator, so the briefing router reaches it over REST behind SP1a auth.
    """
    from app.tasks import list_tasks
    tasks = await list_tasks(
        redis, assignee, status, min(limit, 200),
        oldest_first=oldest_first, title=title,
    )
    return {"tasks": tasks, "count": len(tasks)}


async def handle_post_task(
    redis, *, title: str, assignee: str | None = None, assigner: str = "unknown",
    description: str = "", priority: str = "normal", files: list[str] | None = None,
    context: str = "",
) -> dict:
    """Create a task WITH the two side effects the MCP tool has always had.

    One helper for both the tool and the REST route: parity is three effects
    (store, tasks-channel broadcast, coordination/task_created replay emit),
    and a route that did only the first would create tasks nobody is told
    about. Lazy imports keep routes.py free of the mcp_server import cycle
    (`_get_redis` below does the same).
    """
    from app.tasks import create_task
    from app.pubsub import broadcast
    from app.config import get_settings
    from app.mcp_server import _replay_emit

    task = await create_task(redis, title, assignee, assigner, description, priority, files, context)
    msg = f"Task for {assignee}: {title}" if assignee else f"New task: {title}"
    await broadcast(
        redis, "tasks", msg, assigner, ["task-assigned"],
        backlog_size=get_settings().CHANNEL_BACKLOG_SIZE,
        backlog_ttl_seconds=get_settings().BULLETIN_TTL_HOURS * 3600,
    )
    await _replay_emit(
        "coordination",
        {"action": "task_created", "task_id": task["id"], "assignee": assignee or ""},
        agent_id=assigner,
    )
    return task


async def handle_get_bulletin(redis, limit: int = 20) -> dict:
    """Wrap relay_read logic (unfiltered) for the Cortex briefing aggregator."""
    from app.bulletin import read_bulletin
    posts = await read_bulletin(redis, None, None, min(limit, 200))
    return {"posts": posts, "count": len(posts)}


# -- Scope (FirekeepScope) handler functions --------------------------------

async def handle_post_scope_session(redis, *, agent_id: str, goal: str, origin: str, project=None, bridge_session_id=None, scope_id=None) -> dict:
    from app.scope import create_session
    return await create_session(
        redis, agent_id=agent_id, goal=goal, origin=origin,
        project=project, bridge_session_id=bridge_session_id, scope_id=scope_id,
    )


async def handle_post_scope_screen(redis, scope_id: str, screen: dict) -> dict:
    from app.scope import mirror_screen, get_session
    session = await get_session(redis, scope_id)
    if session is None:
        raise ValueError(f"Session {scope_id} not found")
    return await mirror_screen(redis, scope_id, screen)


async def handle_get_scope_sessions(redis, status: str = "active", limit: int = 50) -> dict:
    from app.scope import list_sessions
    sessions = await list_sessions(redis, status=status, limit=limit)
    return {"sessions": sessions, "count": len(sessions)}


async def handle_get_scope_session(redis, scope_id: str) -> dict | None:
    from app.scope import get_session, get_screens
    session = await get_session(redis, scope_id)
    if session is None:
        return None
    session["screens"] = await get_screens(redis, scope_id)
    return session


async def handle_post_scope_answer(redis, scope_id: str, screen_id: str, *, answers: dict, source: str) -> dict:
    from app.scope import post_answer
    from app.config import get_settings
    settings = get_settings()
    return await post_answer(
        redis, scope_id, screen_id, answers=answers, source=source,
        bridge_url=settings.BRIDGE_URL, api_key=settings.FIREKEEP_API_KEY,
    )


async def handle_get_scope_events(redis, scope_id: str, since: int = 0) -> dict:
    from app.scope import get_events
    events = await get_events(redis, scope_id, since=since)
    return {"events": events, "count": len(events)}


# -- Starlette route wrappers ------------------------------------------

async def _get_redis():
    from app.mcp_server import get_redis
    return await get_redis()


async def route_get_presence(request: Request) -> JSONResponse:
    try:
        include_idle = request.query_params.get("include_idle", "true").lower() != "false"
        r = await _get_redis()
        result = await handle_get_presence(r, include_idle)
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /presence failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_single_presence(request: Request) -> JSONResponse:
    try:
        agent_id = request.path_params["agent_id"]
        r = await _get_redis()
        result = await handle_get_single_presence(r, agent_id)
        if result is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /presence/{agent_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_delete_presence(request: Request) -> JSONResponse:
    try:
        agent_id = request.path_params["agent_id"]
        r = await _get_redis()
        result = await handle_delete_presence(r, agent_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error("DELETE /presence/{agent_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_dm(request: Request) -> JSONResponse:
    try:
        agent_id = request.path_params["agent_id"]
        unread_only = request.query_params.get("unread_only", "false").lower() == "true"
        r = await _get_redis()
        result = await handle_get_dm(r, agent_id, unread_only)
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /dm/{agent_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_post_dm(request: Request) -> JSONResponse:
    try:
        agent_id = request.path_params["agent_id"]
        body = await request.json()
        content = body.get("content", "")
        from_id = body.get("from_id", "dashboard")
        if not content:
            return JSONResponse({"error": "content is required"}, status_code=400)
        r = await _get_redis()
        result = await handle_post_dm(r, agent_id, content, from_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error("POST /dm/{agent_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_mark_dm_read(request: Request) -> JSONResponse:
    try:
        agent_id = request.path_params["agent_id"]
        r = await _get_redis()
        result = await handle_mark_dm_read(r, agent_id)
        return JSONResponse(result)
    except Exception as e:
        logger.error("POST /dm/{agent_id}/read failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_delete_task(request: Request) -> JSONResponse:
    try:
        task_id = request.path_params["task_id"]
        r = await _get_redis()
        from app.tasks import delete_task
        deleted = await delete_task(r, task_id)
        if not deleted:
            return JSONResponse({"error": f"Task {task_id} not found"}, status_code=404)
        return JSONResponse({"status": "deleted", "task_id": task_id})
    except Exception as e:
        logger.error("DELETE /tasks/{task_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_status(request: Request) -> JSONResponse:
    try:
        r = await _get_redis()
        result = await handle_get_status(r)
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /status failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_tasks(request: Request) -> JSONResponse:
    try:
        assignee = request.query_params.get("assignee")
        status = request.query_params.get("status")
        title = request.query_params.get("title")
        oldest_first = request.query_params.get("oldest_first", "").lower() in {
            "1", "true", "yes",
        }
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        r = await _get_redis()
        result = await handle_get_tasks(
            r, assignee, status, limit, oldest_first=oldest_first, title=title,
        )
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /tasks failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_post_task(request: Request) -> JSONResponse:
    """POST /tasks — REST twin of relay_task_post, for server-side enqueue.

    Auth is the blanket key middleware and deliberately NO per-route scope
    (same as GET/DELETE /tasks, /dm/*, /presence/*): cortex's internal key
    carries no relay scope and deployed keys cannot be re-scoped in place
    (spec 2026-09-02 fleet-as-gpu, decision 4).
    """
    try:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed body is the caller's fault
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        title = str(body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        if len(title) > 500:
            return JSONResponse({"error": "Title too long (max 500 chars)"}, status_code=400)
        files = body.get("files")
        if files is not None and not (
            isinstance(files, list) and all(isinstance(f, str) for f in files)
        ):
            return JSONResponse({"error": "files must be a list of strings"}, status_code=400)
        r = await _get_redis()
        task = await handle_post_task(
            r, title=title, assignee=(body.get("assignee") or None),
            assigner=str(body.get("assigner") or "unknown"),
            description=str(body.get("description") or ""),
            priority=str(body.get("priority") or "normal"),
            files=files, context=str(body.get("context") or ""),
        )
        return JSONResponse({"status": "created", "task": task}, status_code=201)
    except Exception as e:
        logger.error("POST /tasks failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_bulletin(request: Request) -> JSONResponse:
    try:
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (ValueError, TypeError):
            limit = 20
        r = await _get_redis()
        result = await handle_get_bulletin(r, limit)
        return JSONResponse(result)
    except Exception as e:
        logger.error("GET /bulletin failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# -- Scope (FirekeepScope) route wrappers ------------------------------------

async def route_post_scope_session(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:write")
        body = await request.json()
        r = await _get_redis()
        result = await handle_post_scope_session(
            r,
            agent_id=body.get("agent_id", "unknown"),
            goal=body.get("goal", ""),
            origin=body.get("origin", "cli"),
            project=body.get("project"),
            bridge_session_id=body.get("bridge_session_id"),
            scope_id=body.get("scope_id"),
        )
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logger.error("POST /scope/sessions failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_post_scope_screen(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:write")
        scope_id = request.path_params["scope_id"]
        screen = await request.json()
        r = await _get_redis()
        result = await handle_post_scope_screen(r, scope_id, screen)
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        logger.error("POST /scope/sessions/{scope_id}/screens failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_scope_sessions(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:read")
        status = request.query_params.get("status", "active")
        r = await _get_redis()
        result = await handle_get_scope_sessions(r, status=status)
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("GET /scope/sessions failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_scope_session(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:read")
        scope_id = request.path_params["scope_id"]
        r = await _get_redis()
        result = await handle_get_scope_session(r, scope_id)
        if result is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("GET /scope/sessions/{scope_id} failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_post_scope_answer(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:write")
        scope_id = request.path_params["scope_id"]
        screen_id = request.path_params["screen_id"]
        body = await request.json()
        r = await _get_redis()
        result = await handle_post_scope_answer(
            r, scope_id, screen_id,
            answers=body.get("answers", {}), source=body.get("source", "dashboard"),
        )
        if not result["resolved"]:
            return JSONResponse(result, status_code=409)
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except ValueError as e:
        status_code = 404 if "not found" in str(e) else 400
        return JSONResponse({"error": str(e)}, status_code=status_code)
    except Exception as e:
        logger.error("POST /scope/sessions/{scope_id}/screens/{screen_id}/answer failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


async def route_get_scope_events(request: Request) -> JSONResponse:
    from auth.asgi import require_scope_asgi, ScopeError
    try:
        require_scope_asgi(request, "relay:read")
        scope_id = request.path_params["scope_id"]
        try:
            since = int(request.query_params.get("since", "0"))
        except (ValueError, TypeError):
            since = 0
        r = await _get_redis()
        result = await handle_get_scope_events(r, scope_id, since=since)
        return JSONResponse(result)
    except ScopeError as e:
        return JSONResponse({"error": e.detail}, status_code=e.status_code)
    except Exception as e:
        logger.error("GET /scope/sessions/{scope_id}/events failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
