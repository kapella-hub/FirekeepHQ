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

Ownership (THREAT-MODEL §5.14, 2026-10-04). A lease records the verified
``owner_workspace`` / ``owner_member`` that acquired it. Release and heartbeat
require that member AND the holder label (as before) AND the fencing token —
a token is a staleness guard, not a credential, and it is returned to anyone
who asks ``relay_lease_status``. A legacy lease with no owner is releasable
only by the deployment owner member until its TTL expires. With auth disabled
the owner check is skipped (``enforce`` = 0), so that mode is unchanged.
"""

from __future__ import annotations

import json
import logging
import time

from redis.asyncio import Redis

from app.principal import Caller, is_deployment_owner

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
local owner_workspace = ARGV[4] or ''
local owner_member = ARGV[5] or ''

-- Check if lease exists and is still held
local existing = redis.call('GET', lease_key)
if existing then
    return {0, existing}  -- Already held, return current holder
end

-- Increment fencing token (monotonic across all holders of this resource)
-- fence_key has NO TTL — persists across lease cycles to guarantee monotonicity
local token = redis.call('INCR', fence_key)

-- Create lease
local lease = {
    holder_id = agent_id,
    fencing_token = token,
    acquired_at = now,
    ttl_seconds = ttl
}
if owner_member ~= '' then
    lease.owner_workspace = owner_workspace
    lease.owner_member = owner_member
end
local data = cjson.encode(lease)
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
-- Verified-owner check: ARGV[3]=enforce, ARGV[4]=caller workspace,
-- ARGV[5]=caller member, ARGV[6]=caller is the deployment owner
if ARGV[3] == '1' then
    local om = data.owner_member
    if type(om) == 'string' and om ~= '' then
        if om ~= ARGV[5] or data.owner_workspace ~= ARGV[4] then
            return -3  -- Held by another member
        end
    elseif ARGV[6] ~= '1' then
        return -3  -- Legacy lease: deployment owner only
    end
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
-- Verified-owner check: ARGV[4]=enforce, ARGV[5]=caller workspace,
-- ARGV[6]=caller member, ARGV[7]=caller is the deployment owner
if ARGV[4] == '1' then
    local om = data.owner_member
    if type(om) == 'string' and om ~= '' then
        if om ~= ARGV[6] or data.owner_workspace ~= ARGV[5] then
            return -3  -- Held by another member
        end
    elseif ARGV[7] ~= '1' then
        return -3  -- Legacy lease: deployment owner only
    end
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


def owner_args(caller: Caller | None) -> list[str]:
    """ARGV tail for the owner check: enforce flag, workspace, member, and
    whether the caller is the deployment owner (who alone may act on a legacy
    lease or claim). No caller (internal use) or auth disabled: not enforced."""
    if caller is None or not caller.authenticated:
        return ["0", "", "", "0"]
    return [
        "1", caller.workspace_id, caller.member_id,
        "1" if is_deployment_owner(caller) else "0",
    ]


def _reason(result: int) -> str:
    return {0: "no_active_lease", -1: "not_holder", -3: "not_owner"}.get(result, "token_mismatch")


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def acquire_lease(
    redis: Redis,
    resource_id: str,
    agent_id: str,
    ttl_seconds: int = 1800,
    caller: Caller | None = None,
) -> dict:
    """Acquire a lease on a resource, owned by ``caller``'s member when given.

    Returns:
        {acquired: True, fencing_token: int, ...} on success
        {acquired: False, held_by: str, fencing_token: int, expires_in: int} if held
    """
    lease_key = f"{_LEASE_PREFIX}{resource_id}"
    fence_key = f"{_FENCE_PREFIX}{resource_id}"
    now = str(time.time())

    owner = caller.owner_fields() if caller is not None else {}
    result = await redis.eval(
        ACQUIRE_LEASE_LUA, 2, lease_key, fence_key,
        agent_id, str(ttl_seconds), now,
        owner.get("owner_workspace", ""), owner.get("owner_member", ""),
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
    caller: Caller | None = None,
) -> dict:
    """Release a lease. Requires matching agent_id and optionally fencing_token,
    and — with ``caller`` — the member that acquired it.

    If fencing_token is 0, only agent_id is checked (backward-compatible).
    """
    lease_key = f"{_LEASE_PREFIX}{resource_id}"

    result = await redis.eval(
        RELEASE_LEASE_LUA, 1, lease_key,
        agent_id, str(fencing_token), *owner_args(caller),
    )

    if result == 1:
        # Notify wait queue
        await _notify_waitqueue(redis, resource_id)
        return {"released": True, "resource_id": resource_id}
    return {"released": False, "reason": _reason(result)}


async def heartbeat(
    redis: Redis,
    resource_id: str,
    agent_id: str,
    fencing_token: int,
    ttl_seconds: int = 1800,
    caller: Caller | None = None,
) -> dict:
    """Extend a lease's TTL. Requires matching agent_id and fencing_token,
    and — with ``caller`` — the member that acquired it."""
    lease_key = f"{_LEASE_PREFIX}{resource_id}"

    result = await redis.eval(
        HEARTBEAT_LUA, 1, lease_key,
        agent_id, str(fencing_token), str(ttl_seconds), *owner_args(caller),
    )

    if result == 1:
        return {"extended": True, "resource_id": resource_id, "ttl_seconds": ttl_seconds}
    return {"extended": False, "reason": _reason(result)}


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
