"""Agent presence registry — tracks which agents are currently online.

No TTL — presence persists until explicitly deregistered (via debrief hook
on clean exit) or manually removed from the dashboard. The heartbeat updates
last_heartbeat so the dashboard can show activity status:
  - "active"  = heartbeat within last 10 minutes
  - "idle"    = registered but no recent heartbeat
"""

import time
import logging

logger = logging.getLogger(__name__)

PRESENCE_PREFIX = "nr:presence:"
PRESENCE_INDEX = "nr:presence:__index"
ACTIVE_THRESHOLD = 600  # 10 minutes — considered "active" if heartbeat within this


async def register(
    redis,
    agent_id: str,
    goal: str,
    hostname: str,
    session_id: str | None = None,
) -> dict:
    """Register an agent as online. Idempotent — overwrites existing. No TTL."""
    now = time.time()
    key = f"{PRESENCE_PREFIX}{agent_id}"

    data = {
        "agent_id": agent_id,
        "session_id": session_id or "",
        "goal": goal,
        "hostname": hostname,
        "started_at": str(now),
        "last_heartbeat": str(now),
        "status": "active",
    }

    await redis.hset(key, mapping=data)
    # No TTL — persists until deregistered
    await redis.zadd(PRESENCE_INDEX, {agent_id: now})

    return data


async def heartbeat_presence(
    redis,
    agent_id: str,
    session_id: str | None = None,
    goal: str | None = None,
) -> dict:
    """Update last_heartbeat timestamp. Optionally backfill session_id and goal."""
    key = f"{PRESENCE_PREFIX}{agent_id}"

    if not await redis.exists(key):
        return {"refreshed": False, "reason": "not_registered"}

    now = str(time.time())
    updates = {"last_heartbeat": now, "status": "active"}
    if session_id:
        updates["session_id"] = session_id
    if goal:
        updates["goal"] = goal

    await redis.hset(key, mapping=updates)
    # No TTL — persists until deregistered
    await redis.zadd(PRESENCE_INDEX, {agent_id: float(now)})

    return {"refreshed": True, "agent_id": agent_id}


async def deregister(redis, agent_id: str) -> dict:
    """Remove an agent's presence immediately."""
    key = f"{PRESENCE_PREFIX}{agent_id}"
    deleted = await redis.delete(key)
    await redis.zrem(PRESENCE_INDEX, agent_id)
    return {"removed": bool(deleted), "agent_id": agent_id}


async def who_is_online(redis, include_idle: bool = True) -> list[dict]:
    """List all registered agents. Computes status from last_heartbeat.

    Status is computed dynamically:
      - "active" if last_heartbeat within ACTIVE_THRESHOLD (10 min)
      - "idle" if registered but heartbeat is older
    """
    now = time.time()
    agent_ids = await redis.zrevrange(PRESENCE_INDEX, 0, -1, withscores=True)

    results = []
    for agent_id, score in agent_ids:
        key = f"{PRESENCE_PREFIX}{agent_id}"
        data = await redis.hgetall(key)

        if not data:
            # Orphaned index entry — clean up
            await redis.zrem(PRESENCE_INDEX, agent_id)
            continue

        # Compute status from heartbeat age
        try:
            last_hb = float(data.get("last_heartbeat", 0))
        except (ValueError, TypeError):
            last_hb = 0
        is_active = (now - last_hb) < ACTIVE_THRESHOLD
        data["status"] = "active" if is_active else "idle"

        if is_active or include_idle:
            results.append(data)

    return results
