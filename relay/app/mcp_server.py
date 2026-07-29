"""FirekeepRelay MCP Server — agent-to-agent communication tools."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import PurePosixPath

from fastmcp import FastMCP

from app.config import get_settings
from app.pubsub import broadcast, get_backlog, get_active_channels
from app.bulletin import post_bulletin, read_bulletin, get_bulletin_count
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

mcp = FastMCP("FirekeepRelay")


# ---------------------------------------------------------------------------
# Replay emitter (fire-and-forget trace events)
# ---------------------------------------------------------------------------

_replay_initialized = False


async def _ensure_replay() -> None:
    global _replay_initialized
    if _replay_initialized:
        return
    _replay_initialized = True
    try:
        from replay.emitter import init_emitter
        await init_emitter()
    except Exception:
        pass


async def _replay_emit(event_type: str, payload: dict, agent_id: str = "relay", **kwargs) -> None:
    try:
        await _ensure_replay()
        from replay.emitter import emit as _emit, is_enabled
        if is_enabled():
            await _emit(event_type, session_id="relay", agent_id=agent_id, payload=payload, **kwargs)
    except Exception:
        pass

_VALID_NAME = re.compile(r'^[a-zA-Z0-9._-]{1,200}$')
_MAX_CONTENT_SIZE = 65536
_MAX_LIMIT = 200

RELEASE_LUA = """
local holder = redis.call('GET', KEYS[1])
if not holder then return 0 end
local data = cjson.decode(holder)
if data.agent_id == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
else
    return -1
end
"""

async def _run_release_script(r, key: str, agent_id: str) -> int:
    """Execute the atomic release Lua script. Returns 1 (released), 0 (not found), -1 (not owner)."""
    return await r.eval(RELEASE_LUA, 1, key, agent_id)


def _validate_name(name: str, field: str) -> str:
    if not _VALID_NAME.match(name):
        raise ValueError(f"Invalid {field}: must be 1-200 alphanumeric/._- chars")
    return name


def _normalize_resource_id(resource_id: str) -> str:
    resource_id = (resource_id or "").strip().replace("\\", "/")
    if not resource_id:
        raise ValueError("Invalid resource_id: must not be empty")
    path = PurePosixPath(resource_id)
    if ".." in path.parts:
        raise ValueError("Invalid resource_id: path traversal is not allowed")
    normalized = resource_id[1:] if resource_id.startswith("/") else resource_id
    normalized = normalized.replace("/", ".")
    return _validate_name(normalized, "resource_id")


@mcp.tool()
async def relay_broadcast(channel: str, content: str, sender: str = "anonymous", tags: list[str] | None = None) -> dict:
    """Send a message to a channel for real-time coordination.

    Messages are delivered to active listeners and stored in a backlog buffer
    so late-joining agents can catch up.

    Args:
        channel: Channel name (e.g. "build", "deploy", "debug", "general")
        content: Message content
        sender: Your agent identifier
        tags: Optional categorization tags
    """
    try:
        _validate_name(channel, "channel")
        _validate_name(sender, "sender")
        if len(content) > _MAX_CONTENT_SIZE:
            return {"error": "Content too large (max 64KB)"}
        r = await get_redis()
        settings = get_settings()
        await broadcast(
            r, channel, content, sender, tags or [],
            backlog_size=settings.CHANNEL_BACKLOG_SIZE,
            backlog_ttl_seconds=settings.BULLETIN_TTL_HOURS * 3600,
        )
        await _replay_emit("coordination", {"channel": channel, "message_summary": content[:200], "tags": tags or []}, agent_id=sender)
        return {"status": "sent", "channel": channel}
    except Exception as e:
        logger.error("relay_broadcast failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_get_messages(channel: str, limit: int = 20) -> dict:
    """Get recent messages from a channel backlog (poll-based, not real-time).

    Returns messages stored in the channel's backlog buffer, newest first.

    Args:
        channel: Channel name to read from
        limit: Maximum messages to return (default 20)
    """
    try:
        _validate_name(channel, "channel")
        limit = min(limit, _MAX_LIMIT)
        r = await get_redis()
        messages = await get_backlog(r, channel, limit)
        return {"channel": channel, "messages": messages, "count": len(messages)}
    except Exception as e:
        logger.error("relay_get_messages failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_post(content: str, author: str = "anonymous", tags: list[str] | None = None, ttl_hours: int | None = None) -> dict:
    """Post to the persistent bulletin board.

    Bulletin posts persist for a configurable TTL and are visible
    to all agents across all sessions. Use for cross-session context sharing.

    Args:
        content: Post content (e.g. "Deployed v2.3 at 14:00", "Auth refactor blocked on PR #42")
        author: Your identifier
        tags: Categorization tags for filtering
        ttl_hours: How long the post persists (default from config)
    """
    try:
        _validate_name(author, "author")
        if len(content) > _MAX_CONTENT_SIZE:
            return {"error": "Content too large (max 64KB)"}
        r = await get_redis()
        settings = get_settings()
        effective_ttl = ttl_hours if ttl_hours is not None else settings.BULLETIN_TTL_HOURS
        post = await post_bulletin(r, content, author, tags or [], effective_ttl)
        await _replay_emit("coordination", {"channel": "bulletin", "message_summary": content[:200], "tags": tags or []}, agent_id=author)
        return {"status": "posted", "post": post}
    except Exception as e:
        logger.error("relay_post failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_read(tags: list[str] | None = None, author: str | None = None, limit: int = 20) -> dict:
    """Read the bulletin board, optionally filtered by tags or author.

    Args:
        tags: Filter by any matching tag
        author: Filter by author
        limit: Maximum posts to return
    """
    try:
        if author is not None:
            _validate_name(author, "author")
        limit = min(limit, _MAX_LIMIT)
        r = await get_redis()
        posts = await read_bulletin(r, tags, author, limit)
        return {"posts": posts, "count": len(posts)}
    except Exception as e:
        logger.error("relay_read failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_claim(resource_id: str, agent_id: str = "default", ttl_minutes: int | None = None) -> dict:
    """Claim a resource (file/task) to prevent duplicate work.

    Uses atomic locking. If the resource is already claimed, returns info about
    who holds it and when it expires. Claims auto-expire after ttl_minutes.

    Args:
        resource_id: What to claim (e.g. file path, task ID)
        agent_id: Your agent identifier
        ttl_minutes: How long the claim lasts (default from config)
    """
    try:
        resource_id = _normalize_resource_id(resource_id)
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        settings = get_settings()
        effective_ttl = ttl_minutes if ttl_minutes is not None else settings.CLAIM_TTL_MINUTES
        key = f"nr:claim:{resource_id}"
        claim_data = json.dumps({"agent_id": agent_id, "timestamp": time.time()})
        acquired = await r.set(key, claim_data, nx=True, ex=effective_ttl * 60)
        if acquired:
            await _replay_emit("claim", {"resource_id": resource_id, "ttl_minutes": effective_ttl}, agent_id=agent_id)
            return {"claimed": True, "resource_id": resource_id, "agent_id": agent_id, "ttl_minutes": effective_ttl}
        # Already claimed
        holder_data = await r.get(key)
        ttl = await r.ttl(key)
        try:
            holder = json.loads(holder_data) if holder_data else {"agent_id": "unknown"}
        except json.JSONDecodeError:
            holder = {"agent_id": "unknown"}
        return {"claimed": False, "held_by": holder["agent_id"], "expires_in": max(ttl, 0), "resource_id": resource_id}
    except Exception as e:
        logger.error("relay_claim failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_release(resource_id: str, agent_id: str = "default", fencing_token: int = 0) -> dict:
    """Release a lease or legacy claim resource. Only the holder can release.

    Args:
        resource_id: The leased/claimed resource to release
        agent_id: Your agent identifier (must match the claim holder)
        fencing_token: Optional lease fencing token. Recommended for lease release.
    """
    try:
        resource_id = _normalize_resource_id(resource_id)
        _validate_name(agent_id, "agent_id")
        r = await get_redis()

        from app.leases import release_lease
        lease_result = await release_lease(r, resource_id, agent_id, fencing_token)
        if lease_result.get("released"):
            await _replay_emit("release", {"resource_id": resource_id}, agent_id=agent_id)
            return {"released": True, "resource_id": resource_id}
        if lease_result.get("reason") not in {"no_active_lease", "not_holder"}:
            return lease_result

        key = f"nr:claim:{resource_id}"
        result = await _run_release_script(r, key, agent_id)
        if result == 1:
            await _replay_emit("release", {"resource_id": resource_id}, agent_id=agent_id)
            return {"released": True, "resource_id": resource_id}
        elif result == 0:
            return {"released": False, "reason": "no active lease or claim"}
        else:
            return {"released": False, "reason": "not owner"}
    except Exception as e:
        logger.error("relay_release failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_status() -> dict:
    """Get relay system status: active channels, bulletin count, active claims.

    Use this to understand the current state of agent coordination.
    """
    try:
        r = await get_redis()
        channels = await get_active_channels(r)
        bulletin_count = await get_bulletin_count(r)
        # Collect claim keys first, then pipeline GET+TTL
        keys = [k async for k in r.scan_iter("nr:claim:*", count=100)]
        claims = []
        if keys:
            async with r.pipeline() as pipe:
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
                claims.append({"resource": resource, "agent_id": claim.get("agent_id", "unknown"), "expires_in": max(ttl, 0)})

        # Collect lease keys and merge into claims list
        lease_keys = [k async for k in r.scan_iter("nr:lease:*", count=100)]
        if lease_keys:
            async with r.pipeline() as pipe:
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
    except Exception as e:
        logger.error("relay_status failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# Lease MCP Tools (fencing tokens, heartbeat)
# ---------------------------------------------------------------------------


@mcp.tool()
async def relay_lease(resource_id: str, agent_id: str = "default", ttl_minutes: int | None = None) -> dict:
    """Acquire a lease on a resource with a fencing token.

    Leases are like claims but with stale-writer protection. The returned
    fencing_token must be passed to relay_release and relay_heartbeat.

    If the resource is already leased, returns info about the current holder
    and when the lease expires.

    Args:
        resource_id: What to lease (e.g. file path, task ID)
        agent_id: Your agent identifier
        ttl_minutes: How long the lease lasts (default from config)
    """
    try:
        resource_id = _normalize_resource_id(resource_id)
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        settings = get_settings()
        ttl_sec = (ttl_minutes if ttl_minutes else settings.CLAIM_TTL_MINUTES) * 60

        from app.leases import acquire_lease
        result = await acquire_lease(r, resource_id, agent_id, ttl_sec)

        if result.get("acquired"):
            await _replay_emit("claim", {
                "resource_id": resource_id,
                "fencing_token": result["fencing_token"],
                "ttl_seconds": ttl_sec,
            }, agent_id=agent_id)

        return result
    except Exception as e:
        logger.error("relay_lease failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_heartbeat(resource_id: str, fencing_token: int, agent_id: str = "default") -> dict:
    """Extend a lease's TTL. Only succeeds if fencing_token matches.

    Call this periodically to keep your lease alive. If you stop heartbeating,
    the lease expires automatically and the resource becomes available.

    Args:
        resource_id: The leased resource
        fencing_token: The token returned when you acquired the lease
        agent_id: Your agent identifier
    """
    try:
        resource_id = _normalize_resource_id(resource_id)
        r = await get_redis()
        settings = get_settings()

        from app.leases import heartbeat
        return await heartbeat(r, resource_id, agent_id, fencing_token, settings.CLAIM_TTL_MINUTES * 60)
    except Exception as e:
        logger.error("relay_heartbeat failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_lease_status(resource_id: str) -> dict:
    """Check the lease status of a resource.

    Returns who holds the lease, their fencing token, time remaining,
    and how many agents are waiting in the queue.

    Args:
        resource_id: The resource to check
    """
    try:
        resource_id = _normalize_resource_id(resource_id)
        r = await get_redis()

        from app.leases import get_lease_status
        return await get_lease_status(r, resource_id)
    except Exception as e:
        logger.error("relay_lease_status failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# Task Queue MCP Tools (multi-agent workflows)
# ---------------------------------------------------------------------------


@mcp.tool()
async def relay_task_post(
    title: str,
    assignee: str | None = None,
    assigner: str = "unknown",
    description: str = "",
    priority: str = "normal",
    files: list[str] | None = None,
    context: str = "",
) -> dict:
    """Create a task and assign it to an agent.

    Tasks appear in the assignee's inbox on their next turn. Use this to
    delegate work to another agent in a multi-agent workflow.

    Args:
        title: Short task description (e.g. "Write tests for auth middleware")
        assignee: Agent ID to assign to (e.g. "agent-beta"). Omit for unassigned.
        assigner: Your agent ID
        description: Detailed requirements
        priority: "low", "normal", "high", or "critical"
        files: List of files relevant to this task
        context: Additional context the assignee needs
    """
    try:
        if len(title) > 500:
            return {"error": "Title too long (max 500 chars)"}
        r = await get_redis()
        from app.tasks import create_task
        task = await create_task(r, title, assignee, assigner, description, priority, files, context)

        # Broadcast notification on the tasks channel
        msg = f"New task: {title}"
        if assignee:
            msg = f"Task for {assignee}: {title}"
        await broadcast(
            r, "tasks", msg, assigner, ["task-assigned"],
            backlog_size=get_settings().CHANNEL_BACKLOG_SIZE,
            backlog_ttl_seconds=get_settings().BULLETIN_TTL_HOURS * 3600,
        )

        await _replay_emit("coordination", {"action": "task_created", "task_id": task["id"], "assignee": assignee or ""}, agent_id=assigner)
        return {"status": "created", "task": task}
    except Exception as e:
        logger.error("relay_task_post failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_task_list(
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict:
    """List tasks, optionally filtered by assignee or status.

    Use this to check your inbox (filter by your agent ID) or see all
    pending work across agents.

    Args:
        assignee: Filter by assigned agent ID
        status: Filter by status: "pending", "in-progress", "completed", "failed"
        limit: Maximum tasks to return
    """
    try:
        r = await get_redis()
        from app.tasks import list_tasks
        tasks = await list_tasks(r, assignee, status, min(limit, _MAX_LIMIT))
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        logger.error("relay_task_list failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_task_update(
    task_id: str,
    status: str | None = None,
    result: str | None = None,
    assignee: str | None = None,
) -> dict:
    """Update a task's status or reassign it.

    Call this when you start working on a task (status="in-progress"),
    finish it (status="completed"), or need to hand it off.

    Args:
        task_id: The task ID (e.g. "task-abc12345")
        status: New status: "pending", "in-progress", "completed", "failed", "cancelled"
        result: Outcome description (when completing or failing)
        assignee: Reassign to a different agent
    """
    try:
        r = await get_redis()
        from app.tasks import update_task
        task = await update_task(r, task_id, status, result, assignee)
        if task is None:
            return {"error": f"Task {task_id} not found"}

        # Broadcast status change
        msg = f"Task {task_id}: {status or 'updated'}"
        if result:
            msg += f" — {result[:100]}"
        await broadcast(
            r, "tasks", msg, task.get("assignee", "unknown"), ["task-" + (status or "updated")],
            backlog_size=get_settings().CHANNEL_BACKLOG_SIZE,
            backlog_ttl_seconds=get_settings().BULLETIN_TTL_HOURS * 3600,
        )

        await _replay_emit("coordination", {"action": "task_updated", "task_id": task_id, "status": status or "updated"}, agent_id=task.get("assignee", "unknown"))
        return {"status": "updated", "task": task}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error("relay_task_update failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_task_delete(task_id: str) -> dict:
    """Delete a task permanently from the queue.

    Use this to clean up completed, cancelled, or stale tasks that are no
    longer needed.

    Args:
        task_id: The task ID to delete (e.g. "task-abc12345")
    """
    try:
        r = await get_redis()
        from app.tasks import delete_task
        deleted = await delete_task(r, task_id)
        if not deleted:
            return {"error": f"Task {task_id} not found"}
        await _replay_emit("coordination", {"action": "task_deleted", "task_id": task_id})
        return {"status": "deleted", "task_id": task_id}
    except Exception as e:
        logger.error("relay_task_delete failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# Presence Registry
# ---------------------------------------------------------------------------


@mcp.tool()
async def relay_register(
    agent_id: str,
    goal: str,
    hostname: str,
    session_id: str | None = None,
) -> dict:
    """Register this agent as online in the presence registry.

    Call this at session start (briefing hook). The presence entry
    Status is computed as 'active' (heartbeat within 10 minutes) or 'idle' (older).
    Presence persists until deregistered — there is no auto-expiry.

    Args:
        agent_id: Your agent identifier (e.g. "agent-alpha")
        goal: What you are working on
        hostname: Machine hostname
        session_id: Bridge session ID (optional, can be backfilled later via heartbeat)
    """
    try:
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        from app.presence import register
        result = await register(r, agent_id, goal, hostname, session_id)
        await _replay_emit("coordination", {
            "action": "presence_register",
            "hostname": hostname,
        }, agent_id=agent_id)
        return {"status": "registered", **result}
    except Exception as e:
        logger.error("relay_register failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_heartbeat_presence(
    agent_id: str,
    session_id: str | None = None,
    goal: str | None = None,
) -> dict:
    """Refresh your presence TTL. Call on every user turn.

    Also used to backfill session_id and goal after ctx_start_session.

    Args:
        agent_id: Your agent identifier
        session_id: Bridge session ID (optional, backfills if provided)
        goal: Current session goal (optional, updates displayed goal)
    """
    try:
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        from app.presence import heartbeat_presence
        return await heartbeat_presence(r, agent_id, session_id, goal)
    except Exception as e:
        logger.error("relay_heartbeat_presence failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_deregister(agent_id: str) -> dict:
    """Remove your presence entry. Call on session exit.

    Args:
        agent_id: Your agent identifier
    """
    try:
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        from app.presence import deregister
        result = await deregister(r, agent_id)
        await _replay_emit("coordination", {
            "action": "presence_deregister",
        }, agent_id=agent_id)
        return result
    except Exception as e:
        logger.error("relay_deregister failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_who_is_online(include_idle: bool = True) -> dict:
    """List all registered agents with computed status.

    Status is computed from heartbeat recency:
      - "active" = heartbeat within last 10 minutes
      - "idle" = registered but no recent heartbeat

    Args:
        include_idle: If true (default), include idle agents. If false, only active ones.
    """
    try:
        r = await get_redis()
        from app.presence import who_is_online
        agents = await who_is_online(r, include_idle)
        return {"agents": agents, "count": len(agents)}
    except Exception as e:
        logger.error("relay_who_is_online failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# FirekeepScope — default-on scope clarification (SP2 Phase A)
# ---------------------------------------------------------------------------

_SCOPE_ASK_POLL_INTERVAL_SECONDS = 2
_SCOPE_ASK_POLL_ITERATIONS = 12  # 12 * 2s = 24s — stays under MCP client timeout ceilings (D-S17)


@mcp.tool()
async def scope_start(
    goal: str,
    agent_id: str,
    project: str | None = None,
    bridge_session_id: str | None = None,
) -> dict:
    """Start a FirekeepScope scope-clarification session.

    Call this once per scoping conversation when your agent runtime has no
    local FirekeepScope companion (headless or MCP-only — kiro-cli, a VPS
    agent). Returns the scope_id every other scope_* tool needs.

    Args:
        goal: What you're trying to scope (becomes the session's display goal)
        agent_id: Your agent identifier
        project: Optional project scope
        bridge_session_id: Optional Bridge session ID to link decisions to
    """
    try:
        _validate_name(agent_id, "agent_id")
        r = await get_redis()
        from app.scope import create_session
        return await create_session(
            r, agent_id=agent_id, goal=goal, origin="mcp",
            project=project, bridge_session_id=bridge_session_id,
        )
    except Exception as e:
        logger.error("scope_start failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def scope_ask(
    screen: dict,
    scope_id: str | None = None,
    agent_id: str = "default",
    goal: str | None = None,
) -> dict:
    """Post a gating scoping screen and wait for a human to answer it.

    Internally long-polls in ~2s cycles for up to ~24s (D-S17) so this call
    never blocks past MCP client timeout ceilings. Returns {"status":
    "answered", "scope_id", "screen_id", "answers"} once resolved, or
    {"status": "pending", "scope_id", "screen_id"} if still unanswered after
    the poll budget. If pending, call scope_check(scope_id) to keep waiting
    — do NOT call scope_ask again with the same screen, that posts a
    duplicate screen.

    Args:
        screen: Screen object per client/contract/scope-v1.md (kind, title,
            questions — screen_id/mode may be omitted; this tool sets
            mode="gating" and mints screen_id if absent)
        scope_id: Session to post into; if omitted a new session is created
            from `goal`/`agent_id`
        agent_id: Your agent identifier (used only when scope_id is omitted)
        goal: Session goal (used only when scope_id is omitted)
    """
    try:
        r = await get_redis()
        from app.scope import create_session, mirror_screen, get_screens

        if not scope_id:
            _validate_name(agent_id, "agent_id")
            session = await create_session(r, agent_id=agent_id, goal=goal or screen.get("title", "Scoping"), origin="mcp")
            scope_id = session["scope_id"]

        posted = await mirror_screen(r, scope_id, {**screen, "mode": "gating"})
        screen_id = posted["screen_id"]

        for _ in range(_SCOPE_ASK_POLL_ITERATIONS):
            current = await get_screens(r, scope_id)
            match = next((s for s in current if s["screen_id"] == screen_id), None)
            if match and match.get("status") == "resolved":
                return {"status": "answered", "scope_id": scope_id, "screen_id": screen_id, "answers": match["answer"]["answers"]}
            await asyncio.sleep(_SCOPE_ASK_POLL_INTERVAL_SECONDS)

        return {"status": "pending", "scope_id": scope_id, "screen_id": screen_id}
    except Exception as e:
        logger.error("scope_ask failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def scope_post(
    screen: dict,
    scope_id: str | None = None,
    agent_id: str = "default",
    goal: str | None = None,
) -> dict:
    """Post a non-gating scoping screen and return immediately.

    Use during execution when a question shouldn't block progress. Poll for
    the answer later with scope_check(scope_id).

    Args: same as scope_ask, but this tool never blocks.
    """
    try:
        r = await get_redis()
        from app.scope import create_session, mirror_screen

        if not scope_id:
            _validate_name(agent_id, "agent_id")
            session = await create_session(r, agent_id=agent_id, goal=goal or screen.get("title", "Scoping"), origin="mcp")
            scope_id = session["scope_id"]

        posted = await mirror_screen(r, scope_id, {**screen, "mode": "async"})
        return {"status": "posted", "scope_id": scope_id, "screen_id": posted["screen_id"]}
    except Exception as e:
        logger.error("scope_post failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def scope_check(scope_id: str) -> dict:
    """Check a scope session for answered and still-pending screens.

    Args:
        scope_id: Session ID returned by scope_start/scope_ask/scope_post
    """
    try:
        r = await get_redis()
        from app.scope import get_screens
        screens = await get_screens(r, scope_id)
        answered = {s["screen_id"]: s["answer"]["answers"] for s in screens if s.get("status") == "resolved"}
        pending = [s["screen_id"] for s in screens if s.get("status") != "resolved"]
        return {"scope_id": scope_id, "answered": answered, "pending": pending}
    except Exception as e:
        logger.error("scope_check failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def scope_complete(scope_id: str) -> dict:
    """Mark a scope session completed (D-S22).

    Call this when you're done with a scoping session, so it doesn't sit
    active for up to 72h before the abandonment sweep closes it. Idempotent
    to call on an already-completed session.

    Args:
        scope_id: Session ID returned by scope_start/scope_ask/scope_post
    """
    try:
        r = await get_redis()
        from app.scope import complete_session
        result = await complete_session(r, scope_id)
        if result is None:
            return {"error": f"Session {scope_id} not found", "status": "not_found"}
        return result
    except Exception as e:
        logger.error("scope_complete failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# Direct Messages
# ---------------------------------------------------------------------------


@mcp.tool()
async def relay_send_dm(to_agent_id: str, content: str, from_id: str = "anonymous") -> dict:
    """Send a direct message to another agent or to the dashboard.

    Messages appear in the recipient's DM inbox and can be polled via
    relay_get_dm or the dashboard DM drawer.

    Args:
        to_agent_id: Recipient agent ID (e.g. "agent-alpha", "dashboard")
        content: Message content
        from_id: Your agent identifier
    """
    try:
        _validate_name(to_agent_id, "to_agent_id")
        _validate_name(from_id, "from_id")
        if len(content) > _MAX_CONTENT_SIZE:
            return {"error": "Content too large (max 64KB)"}
        r = await get_redis()
        from app.dm import send_dm
        msg = await send_dm(r, to_agent_id, content, from_id)
        await _replay_emit("coordination", {
            "action": "dm_sent",
            "to": to_agent_id,
            "message_preview": content[:200],
        }, agent_id=from_id)
        return {"status": "sent", "message": msg}
    except Exception as e:
        logger.error("relay_send_dm failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


@mcp.tool()
async def relay_get_dm(agent_id: str, unread_only: bool = False, limit: int = 20) -> dict:
    """Direct messages for an agent (newest first).

    Args:
        agent_id: Inbox owner (messages sent TO you).
        unread_only: If true, only unread.
        limit: Max messages (default 20).
    """
    try:
        _validate_name(agent_id, "agent_id")
        limit = min(limit, _MAX_LIMIT)
        r = await get_redis()
        from app.dm import get_dms
        messages = await get_dms(r, agent_id, unread_only, limit)
        return {"agent_id": agent_id, "messages": messages, "count": len(messages)}
    except Exception as e:
        logger.error("relay_get_dm failed: %s", e)
        return {"error": str(e), "status": "unavailable"}


# ---------------------------------------------------------------------------
# Agent Card discovery (/.well-known/agent.json)
# ---------------------------------------------------------------------------

from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse

_AGENT_CARD_PROTOCOL_VERSION = "0.2.0"


def _build_agent_card(base_url: str) -> dict:
    """Build the A2A Agent Card for discovery only."""
    return {
        "name": "Firekeep",
        "description": (
            "Self-hosted cognitive infrastructure for AI coding agents. "
            "Provides persistent memory, session continuity, environment monitoring, "
            "agent coordination, and replayable decision traces."
        ),
        "version": "1.0.0",
        "protocolVersion": _AGENT_CARD_PROTOCOL_VERSION,
        "provider": {
            "organization": "Firekeep",
            "url": "https://github.com/kapella-hub/FirekeepHQ",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [
            {
                "id": "memory-recall",
                "name": "Recall Memory",
                "description": "Search persistent agent memory for relevant past experience",
                "tags": ["memory", "knowledge"],
            },
            {
                "id": "memory-learn",
                "name": "Store Memory",
                "description": "Store a learning or decision in persistent agent memory",
                "tags": ["memory", "knowledge"],
            },
            {
                "id": "environment-status",
                "name": "Environment Status",
                "description": "Get current infrastructure health and recent events",
                "tags": ["monitoring", "environment"],
            },
            {
                "id": "coordinate",
                "name": "Agent Coordination",
                "description": "Broadcast messages, claim resources, or post to the bulletin board",
                "tags": ["coordination", "communication"],
            },
        ],
    }


@mcp.custom_route("/.well-known/agent.json", methods=["GET"], name="a2a_agent_card")
async def a2a_agent_card(request: StarletteRequest) -> StarletteJSONResponse:
    """A2A Agent Card discovery endpoint."""
    base_url = str(request.base_url).rstrip("/")
    return StarletteJSONResponse(_build_agent_card(base_url))


# ---------------------------------------------------------------------------
# REST routes for Dashboard
# ---------------------------------------------------------------------------


@mcp.custom_route("/presence", methods=["GET"], name="get_presence")
async def _route_presence(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_presence
    return await route_get_presence(request)


@mcp.custom_route("/presence/{agent_id}", methods=["GET"], name="get_single_presence")
async def _route_single_presence(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_single_presence
    return await route_get_single_presence(request)


@mcp.custom_route("/presence/{agent_id}", methods=["DELETE"], name="delete_presence")
async def _route_delete_presence(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_delete_presence
    return await route_delete_presence(request)


@mcp.custom_route("/dm/{agent_id}", methods=["GET"], name="get_dm")
async def _route_get_dm(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_dm
    return await route_get_dm(request)


@mcp.custom_route("/dm/{agent_id}", methods=["POST"], name="post_dm")
async def _route_post_dm(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_post_dm
    return await route_post_dm(request)


@mcp.custom_route("/dm/{agent_id}/read", methods=["POST"], name="mark_dm_read")
async def _route_mark_dm_read(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_mark_dm_read
    return await route_mark_dm_read(request)


@mcp.custom_route("/tasks/{task_id}", methods=["DELETE"], name="delete_task")
async def _route_delete_task(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_delete_task
    return await route_delete_task(request)


@mcp.custom_route("/tasks", methods=["GET"], name="get_tasks")
async def _route_get_tasks(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_tasks
    return await route_get_tasks(request)


@mcp.custom_route("/bulletin", methods=["GET"], name="get_bulletin")
async def _route_get_bulletin(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_bulletin
    return await route_get_bulletin(request)


@mcp.custom_route("/status", methods=["GET"], name="get_status")
async def _route_status(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_status
    return await route_get_status(request)


# ---------------------------------------------------------------------------
# FirekeepScope REST routes for Dashboard + companion (SP2 Phase A)
# ---------------------------------------------------------------------------


@mcp.custom_route("/scope/sessions", methods=["POST"], name="post_scope_session")
async def _route_post_scope_session(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_post_scope_session
    return await route_post_scope_session(request)


@mcp.custom_route("/scope/sessions", methods=["GET"], name="get_scope_sessions")
async def _route_get_scope_sessions(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_scope_sessions
    return await route_get_scope_sessions(request)


@mcp.custom_route("/scope/sessions/{scope_id}", methods=["GET"], name="get_scope_session")
async def _route_get_scope_session(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_scope_session
    return await route_get_scope_session(request)


@mcp.custom_route("/scope/sessions/{scope_id}/screens", methods=["POST"], name="post_scope_screen")
async def _route_post_scope_screen(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_post_scope_screen
    return await route_post_scope_screen(request)


@mcp.custom_route("/scope/sessions/{scope_id}/screens/{screen_id}/answer", methods=["POST"], name="post_scope_answer")
async def _route_post_scope_answer(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_post_scope_answer
    return await route_post_scope_answer(request)


@mcp.custom_route("/scope/sessions/{scope_id}/events", methods=["GET"], name="get_scope_events")
async def _route_get_scope_events(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_get_scope_events
    return await route_get_scope_events(request)


@mcp.custom_route("/health", methods=["GET"], name="health")
async def _health(request: StarletteRequest) -> StarletteJSONResponse:
    return StarletteJSONResponse({"status": "ok", "service": "relay"})


@mcp.custom_route("/version", methods=["GET"], name="version")
async def _version(request: StarletteRequest) -> StarletteJSONResponse:
    """Build provenance. Unauthenticated and probes no backends — answers
    'what code is running here?' without introspection, so it works even when
    the service's dependencies are down."""
    from provenance import get_version_info

    return StarletteJSONResponse(get_version_info("relay"))


if __name__ == "__main__":
    import os

    from auth.asgi import build_auth_middleware
    from auth.config import get_auth_settings

    host = os.getenv("NR_MCP_HOST", "0.0.0.0")
    port = int(os.getenv("NR_MCP_PORT", "8050"))
    mcp.run(
        transport="http",
        host=host,
        port=port,
        stateless_http=True,
        middleware=build_auth_middleware(
            get_auth_settings(),
            skip_paths=("/health", "/version", "/.well-known/agent.json"),
        ),
    )
