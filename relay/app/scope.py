"""FirekeepScope session/screen/answer storage (SP2 Phase A).

Redis keys:
    nr:scope:session:{scope_id}          — Hash with session fields
    nr:scope:screens:{scope_id}          — Hash keyed by screen_id -> JSON screen blob
    nr:scope:screens_order:{scope_id}    — List of screen_id in creation order
    nr:scope:screen_seq:{scope_id}       — INCR counter for minting screen_id suffixes
    nr:scope:answer:{scope_id}:{screen_id} — SET NX arbiter, JSON answer blob
    nr:scope:events:{scope_id}           — List of JSON event dicts (append-only)
    nr:scope:__index                     — Sorted set of scope_id scored by created_at
"""

import json
import logging
import re
import time
import uuid

import httpx

logger = logging.getLogger(__name__)

SESSION_PREFIX = "nr:scope:session:"
SCREENS_PREFIX = "nr:scope:screens:"
SCREENS_ORDER_PREFIX = "nr:scope:screens_order:"
SCREEN_SEQ_PREFIX = "nr:scope:screen_seq:"
ANSWER_PREFIX = "nr:scope:answer:"
EVENTS_PREFIX = "nr:scope:events:"
SCOPE_INDEX = "nr:scope:__index"

TTL_SECONDS = 86400 * 7           # 7 days from abandoned/completed
ABANDON_AFTER_SECONDS = 86400 * 3  # 72h no screen activity while active

VALID_ORIGINS = frozenset({"cli", "mcp"})
VALID_SOURCES = frozenset({"local", "dashboard"})

_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def create_session(
    redis, *, agent_id: str, goal: str, origin: str,
    project: str | None = None, bridge_session_id: str | None = None,
    scope_id: str | None = None,
) -> dict:
    """Create (or, if scope_id already exists, no-op-return) a scope session.

    "Upsert" for a scope session means idempotent-create: session fields are
    immutable after creation, so a retried create just returns the existing
    session rather than overwriting created_at / re-indexing it.
    """
    if origin not in VALID_ORIGINS:
        raise ValueError(f"Invalid origin: {origin!r}. Must be one of {sorted(VALID_ORIGINS)}")

    if scope_id and not _ID_PATTERN.match(scope_id):
        raise ValueError(f"Invalid scope_id: {scope_id!r}")

    if scope_id:
        existing = await get_session(redis, scope_id)
        if existing is not None:
            return existing
    else:
        scope_id = "sc_" + uuid.uuid4().hex[:8]

    now = time.time()
    session = {
        "v": 1, "scope_id": scope_id, "agent_id": agent_id,
        "bridge_session_id": bridge_session_id or "", "project": project or "",
        "goal": goal, "origin": origin, "status": "active",
        "created_at": now, "last_activity_at": now,
    }
    key = f"{SESSION_PREFIX}{scope_id}"
    await redis.hset(key, mapping=session)
    await redis.zadd(SCOPE_INDEX, {scope_id: now})
    return session


async def get_session(redis, scope_id: str) -> dict | None:
    raw = await redis.hgetall(f"{SESSION_PREFIX}{scope_id}")
    if not raw:
        return None
    return _parse_session(raw)


def _parse_session(raw: dict) -> dict:
    session = dict(raw)
    try:
        session["v"] = int(session["v"])
    except (ValueError, KeyError):
        pass
    for k in ("created_at", "last_activity_at", "closed_at"):
        if k in session:
            try:
                session[k] = float(session[k])
            except (ValueError, TypeError):
                pass
    return session


async def list_sessions(redis, *, status: str = "active", limit: int = 50) -> list[dict]:
    if status == "active":
        await abandon_stale_sessions(redis)

    scope_ids = await redis.zrevrange(SCOPE_INDEX, 0, limit * 3)
    results = []
    for sid in scope_ids:
        session = await get_session(redis, sid)
        if session is None:
            await redis.zrem(SCOPE_INDEX, sid)  # orphaned index entry — clean up
            continue
        if status and session["status"] != status:
            continue
        session["pending_screens"] = await _has_pending_gating_screens(redis, sid)
        results.append(session)
        if len(results) >= limit:
            break
    return results


async def _has_pending_gating_screens(redis, scope_id: str) -> bool:
    raw_values = await redis.hvals(f"{SCREENS_PREFIX}{scope_id}")
    for raw in raw_values:
        screen = json.loads(raw)
        if screen.get("status") == "pending" and screen.get("mode") == "gating":
            return True
    return False


async def complete_session(redis, scope_id: str) -> dict | None:
    session = await get_session(redis, scope_id)
    if session is None:
        return None
    if session["status"] != "active":
        return session
    return await _close_session(redis, scope_id, "completed")


async def abandon_stale_sessions(redis) -> int:
    now = time.time()
    scope_ids = await redis.zrange(SCOPE_INDEX, 0, -1)
    count = 0
    for sid in scope_ids:
        session = await get_session(redis, sid)
        if session is None:
            await redis.zrem(SCOPE_INDEX, sid)
            continue
        if session["status"] != "active":
            continue
        if now - session["last_activity_at"] < ABANDON_AFTER_SECONDS:
            continue
        await _close_session(redis, sid, "abandoned")
        count += 1
    return count


async def _close_session(redis, scope_id: str, status: str) -> dict:
    key = f"{SESSION_PREFIX}{scope_id}"
    await redis.hset(key, mapping={"status": status, "closed_at": time.time()})
    screens = await get_screens(redis, scope_id)
    keys_to_expire = [
        key,
        f"{SCREENS_PREFIX}{scope_id}",
        f"{SCREENS_ORDER_PREFIX}{scope_id}",
        f"{EVENTS_PREFIX}{scope_id}",
        f"{SCREEN_SEQ_PREFIX}{scope_id}",
    ] + [f"{ANSWER_PREFIX}{scope_id}:{s['screen_id']}" for s in screens]
    for k in keys_to_expire:
        await redis.expire(k, TTL_SECONDS)
    return await get_session(redis, scope_id)


async def _touch_activity(redis, scope_id: str) -> None:
    await redis.hset(f"{SESSION_PREFIX}{scope_id}", "last_activity_at", time.time())


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------


async def _next_screen_id(redis, scope_id: str) -> str:
    n = await redis.incr(f"{SCREEN_SEQ_PREFIX}{scope_id}")
    return f"{scope_id}-{n}"


async def mirror_screen(redis, scope_id: str, screen: dict) -> dict:
    """Upsert a screen by screen_id — idempotent, so retried mirror POSTs
    never duplicate an entry in the display-order list.

    A retry against an already-resolved (or otherwise non-pending) screen is
    a no-op: the caller's retry payload is the original screen definition,
    with no way to know the screen has since been answered, so overwriting
    would revert status back to "pending" and drop the attached answer.
    """
    screens_key = f"{SCREENS_PREFIX}{scope_id}"
    order_key = f"{SCREENS_ORDER_PREFIX}{scope_id}"

    screen_id = screen.get("screen_id")
    if not screen_id:
        screen_id = await _next_screen_id(redis, scope_id)
        is_new = True
    else:
        if not _ID_PATTERN.match(screen_id):
            raise ValueError(f"Invalid screen_id: {screen_id!r}")
        existing_raw = await redis.hget(screens_key, screen_id)
        is_new = existing_raw is None
        if not is_new:
            existing = json.loads(existing_raw)
            if existing.get("status") != "pending":
                return existing

    screen = {**screen, "screen_id": screen_id, "v": 1, "status": "pending"}
    await redis.hset(screens_key, screen_id, json.dumps(screen))

    if is_new:
        await redis.rpush(order_key, screen_id)
        await _touch_activity(redis, scope_id)
        await _append_event(redis, scope_id, {"type": "screen.posted", "screen_id": screen_id})

    return screen


async def get_screens(redis, scope_id: str) -> list[dict]:
    order_key = f"{SCREENS_ORDER_PREFIX}{scope_id}"
    screens_key = f"{SCREENS_PREFIX}{scope_id}"
    screen_ids = await redis.lrange(order_key, 0, -1)
    screens = []
    for sid in screen_ids:
        raw = await redis.hget(screens_key, sid)
        if raw:
            screens.append(json.loads(raw))
    return screens


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


async def post_answer(
    redis, scope_id: str, screen_id: str, *, answers: dict, source: str,
    bridge_url: str | None = None, api_key: str | None = None,
) -> dict:
    if source not in VALID_SOURCES:
        raise ValueError(f"Invalid source: {source!r}. Must be one of {sorted(VALID_SOURCES)}")

    screens_key = f"{SCREENS_PREFIX}{scope_id}"
    raw = await redis.hget(screens_key, screen_id)
    if raw is None:
        raise ValueError(f"Screen {screen_id} not found in session {scope_id}")

    answer_key = f"{ANSWER_PREFIX}{scope_id}:{screen_id}"
    payload = {"answers": answers, "source": source, "answered_at": time.time()}
    won = await redis.set(answer_key, json.dumps(payload), nx=True)
    if not won:
        existing = json.loads(await redis.get(answer_key))
        return {"resolved": False, "answer": existing}

    screen = json.loads(raw)
    screen["status"] = "resolved"
    screen["answer"] = payload
    await redis.hset(screens_key, screen_id, json.dumps(screen))
    await _touch_activity(redis, scope_id)
    await _append_event(redis, scope_id, {"type": "screen.answered", "screen_id": screen_id, "source": source})

    if bridge_url:
        session = await get_session(redis, scope_id)
        if session and session.get("origin") == "mcp":
            await _persist_to_bridge(
                bridge_url, session["agent_id"], "decision",
                f"FirekeepScope screen {screen_id} resolved: {json.dumps(answers)}",
                key=scope_id,
                api_key=api_key,
            )

    return {"resolved": True, "answer": payload}


async def _persist_to_bridge(
    bridge_url: str, agent_id: str, category: str, content: str,
    key: str | None = None, api_key: str | None = None,
) -> None:
    """Best-effort Bridge decision write for origin:"mcp" sessions (D-S18). Never raises."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{bridge_url}/sessions/{agent_id}/context",
                json={"category": category, "content": content, "key": key},
                headers=headers,
            )
    except Exception as exc:
        logger.warning("Bridge decision write failed (non-fatal, origin=mcp): %s", exc)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def _append_event(redis, scope_id: str, event: dict) -> int:
    event = {**event, "ts": time.time()}
    return await redis.rpush(f"{EVENTS_PREFIX}{scope_id}", json.dumps(event))


async def get_events(redis, scope_id: str, since: int = 0) -> list[dict]:
    raw = await redis.lrange(f"{EVENTS_PREFIX}{scope_id}", since, -1)
    return [json.loads(r) for r in raw]
