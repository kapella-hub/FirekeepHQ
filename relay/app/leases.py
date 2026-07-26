"""Lease system with fencing tokens for FirekeepRelay.

Upgrades the existing claim system with:
1. Monotonic fencing tokens — prevents stale writers after lease expiry
2. Server-side TTL — leases expire automatically, no heartbeat required
3. Optional heartbeat — extends TTL if the agent is still alive
4. Wait queue — agents can queue for contended resources

Fencing token flow:
    Agent A acquires lease → gets fencing_token=42
    Agent A's lease expires
    Agent B acquires lease → gets fencing_token=43
    Agent A (stale) tries to write with token=42
    System rejects: 42 < 43 → stale writer blocked
"""

from __future__ import annotations

import json
import logging
import time

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lua scripts for atomic operations
# ---------------------------------------------------------------------------

# Acquire a lease: check if free, increment fencing token, create lease
ACQUIRE_LEASE_LUA = """
local lease_key = KEYS[1]
local fence_key = KEYS[2]
local agent_id = ARGV[1]
local ttl = tonumber(ARGV[2])
local now = ARGV[3]

-- Check if lease exists and is still held
local existing = redis.call('GET', lease_key)
if existing then
    return {0, existing}  -- Already held, return current holder
end

-- Increment fencing token (monotonic across all holders of this resource)
-- fence_key has NO TTL — persists across lease cycles to guarantee monotonicity
local token = redis.call('INCR', fence_key)

-- Create lease
local data = cjson.encode({
    holder_id = agent_id,
    fencing_token = token,
    acquired_at = now,
    ttl_seconds = ttl
})
redis.call('SET', lease_key, data, 'EX', ttl)
return {1, data}
"""

# Release a lease: only if fencing token matches (prevents stale release)
RELEASE_LEASE_LUA = """
local lease_key = KEYS[1]
local agent_id = ARGV[1]
local expected_token = tonumber(ARGV[2])

local existing = redis.call('GET', lease_key)
if not existing then
    return 0  -- No active lease
end

local data = cjson.decode(existing)
if data.holder_id ~= agent_id then
    return -1  -- Not the holder
end
if expected_token > 0 and data.fencing_token ~= expected_token then
    return -2  -- Token mismatch (stale reference)
end

redis.call('DEL', lease_key)
return 1  -- Released
"""

# Heartbeat: extend TTL only if holder and token match
HEARTBEAT_LUA = """
local lease_key = KEYS[1]
local agent_id = ARGV[1]
local expected_token = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local existing = redis.call('GET', lease_key)
if not existing then
    return 0  -- No active lease
end

local data = cjson.decode(existing)
if data.holder_id ~= agent_id then
    return -1  -- Not the holder
end
if data.fencing_token ~= expected_token then
    return -2  -- Token mismatch
end

redis.call('EXPIRE', lease_key, ttl)
return 1  -- Extended
"""

# Key patterns
_LEASE_PREFIX = "nr:lease:"
_FENCE_PREFIX = "nr:fence:"
_WAITQ_PREFIX = "nr:waitq:"


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def acquire_lease(
    redis: Redis,
    resource_id: str,
    agent_id: str,
    ttl_seconds: int = 1800,
) -> dict:
    """Acquire a lease on a resource.

    Returns:
        {acquired: True, fencing_token: int, ...} on success
        {acquired: False, held_by: str, fencing_token: int, expires_in: int} if held
    """
    lease_key = f"{_LEASE_PREFIX}{resource_id}"
    fence_key = f"{_FENCE_PREFIX}{resource_id}"
    now = str(time.time())

    result = await redis.eval(
        ACQUIRE_LEASE_LUA, 2, lease_key, fence_key,
        agent_id, str(ttl_seconds), now,
    )

    acquired = result[0]
    data = json.loads(result[1])

    if acquired:
        return {
            "acquired": True,
            "resource_id": resource_id,
            "agent_id": agent_id,
            "fencing_token": data["fencing_token"],
            "ttl_seconds": ttl_seconds,
        }
    else:
        ttl = await redis.ttl(lease_key)
        return {
            "acquired": False,
            "resource_id": resource_id,
            "held_by": data.get("holder_id", "unknown"),
            "fencing_token": data.get("fencing_token", 0),
            "expires_in": max(ttl, 0),
        }


async def release_lease(
    redis: Redis,
    resource_id: str,
    agent_id: str,
    fencing_token: int = 0,
) -> dict:
    """Release a lease. Requires matching agent_id and optionally fencing_token.

    If fencing_token is 0, only agent_id is checked (backward-compatible).
    """
    lease_key = f"{_LEASE_PREFIX}{resource_id}"

    result = await redis.eval(
        RELEASE_LEASE_LUA, 1, lease_key,
        agent_id, str(fencing_token),
    )

    if result == 1:
        # Notify wait queue
        await _notify_waitqueue(redis, resource_id)
        return {"released": True, "resource_id": resource_id}
    elif result == 0:
        return {"released": False, "reason": "no_active_lease"}
    elif result == -1:
        return {"released": False, "reason": "not_holder"}
    else:
        return {"released": False, "reason": "token_mismatch"}


async def heartbeat(
    redis: Redis,
    resource_id: str,
    agent_id: str,
    fencing_token: int,
    ttl_seconds: int = 1800,
) -> dict:
    """Extend a lease's TTL. Requires matching agent_id and fencing_token."""
    lease_key = f"{_LEASE_PREFIX}{resource_id}"

    result = await redis.eval(
        HEARTBEAT_LUA, 1, lease_key,
        agent_id, str(fencing_token), str(ttl_seconds),
    )

    if result == 1:
        return {"extended": True, "resource_id": resource_id, "ttl_seconds": ttl_seconds}
    elif result == 0:
        return {"extended": False, "reason": "no_active_lease"}
    elif result == -1:
        return {"extended": False, "reason": "not_holder"}
    else:
        return {"extended": False, "reason": "token_mismatch"}


async def get_lease_status(redis: Redis, resource_id: str) -> dict:
    """Get current lease status for a resource."""
    lease_key = f"{_LEASE_PREFIX}{resource_id}"
    raw = await redis.get(lease_key)
    if not raw:
        return {"resource_id": resource_id, "held": False}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"resource_id": resource_id, "held": False}

    ttl = await redis.ttl(lease_key)
    waitq_key = f"{_WAITQ_PREFIX}{resource_id}"
    waitq_len = await redis.llen(waitq_key)

    return {
        "resource_id": resource_id,
        "held": True,
        "holder_id": data.get("holder_id", "unknown"),
        "fencing_token": data.get("fencing_token", 0),
        "acquired_at": data.get("acquired_at"),
        "ttl_seconds": data.get("ttl_seconds", 0),
        "expires_in": max(ttl, 0),
        "waitqueue_length": waitq_len,
    }


# ---------------------------------------------------------------------------
# Wait queue
# ---------------------------------------------------------------------------


async def join_waitqueue(redis: Redis, resource_id: str, agent_id: str) -> dict:
    """Join the wait queue for a contended resource.

    When the current lease expires or is released, the next agent in the
    queue is notified via a bulletin post.
    """
    waitq_key = f"{_WAITQ_PREFIX}{resource_id}"
    await redis.rpush(waitq_key, agent_id)
    await redis.expire(waitq_key, 86400)  # 24h TTL on queue
    position = await redis.llen(waitq_key)
    return {"queued": True, "resource_id": resource_id, "position": position}


async def _notify_waitqueue(redis: Redis, resource_id: str) -> None:
    """Pop the next agent from the wait queue and notify via bulletin."""
    waitq_key = f"{_WAITQ_PREFIX}{resource_id}"
    next_agent = await redis.lpop(waitq_key)
    if not next_agent:
        return

    # Post notification to bulletin board
    try:
        from app.bulletin import post_bulletin
        await post_bulletin(
            redis,
            content=f"Lease for '{resource_id}' is now available. You were next in queue.",
            author="relay-system",
            tags=["lease-notification", resource_id],
            ttl_hours=1,
        )
    except Exception as e:
        logger.debug("Wait queue notification failed: %s", e)
