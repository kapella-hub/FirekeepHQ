"""Agent presence registry — tracks which agents are currently online.

No TTL — presence persists until explicitly deregistered (via debrief hook
on clean exit) or manually removed from the dashboard. The heartbeat updates
last_heartbeat so the dashboard can show activity status:
  - "active"  = heartbeat within last 10 minutes
  - "idle"    = registered but no recent heartbeat

Ownership (THREAT-MODEL §5.14, 2026-10-04). A row records the verified
``owner_workspace`` / ``owner_member`` that wrote it. Only that member may
re-register, heartbeat or deregister the label; the dashboard's admin key may
also remove it. The row is what binds a LABEL to a member: DMs sent to the
label are addressed to that member (``app.dm``), and another member may not
post DMs, bulletins or broadcasts under it.

Legacy rows (written before owners existed) are UNBOUND, and — unlike every
other legacy record in Relay — the next verified writer adopts them. Strict
deployment-owner-only would freeze every teammate's presence on upgrade:
rows never expire, and a member could neither refresh nor remove its own. The
residual is a squatting window at upgrade time (a teammate can adopt another
member's label before its agent next heartbeats); it exposes no stored data,
because DMs written before the upgrade carry no recipient member and stay
readable by the deployment owner alone.
"""

import time
import logging

from app.principal import Caller, RelayAccessError, administers, is_bound, owns

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
    caller: Caller | None = None,
) -> dict:
    """Register an agent as online. Idempotent — overwrites existing. No TTL.

    With ``caller``: refuses a label bound to another member (RelayAccessError)
    and binds the row to the caller."""
    now = time.time()
    key = f"{PRESENCE_PREFIX}{agent_id}"
    if caller is not None:
        existing = await redis.hgetall(key)
        if existing and is_bound(existing) and not owns(existing, caller):
            raise RelayAccessError(
                f"agent_id '{agent_id}' is registered by another member; choose another label"
            )

    data = {
        "agent_id": agent_id,
        "session_id": session_id or "",
        "goal": goal,
        "hostname": hostname,
        "started_at": str(now),
        "last_heartbeat": str(now),
        "status": "active",
    }
    if caller is not None:
        data.update(caller.owner_fields())

    await redis.hset(key, mapping=data)
    # No TTL — persists until deregistered
    await redis.zadd(PRESENCE_INDEX, {agent_id: now})

    return data


async def heartbeat_presence(
    redis,
    agent_id: str,
    session_id: str | None = None,
    goal: str | None = None,
    caller: Caller | None = None,
) -> dict:
    """Update last_heartbeat timestamp. Optionally backfill session_id and goal.

    With ``caller``: a row bound to another member is left untouched
    (``reason: not_owner``); an unbound legacy row is adopted."""
    key = f"{PRESENCE_PREFIX}{agent_id}"

    existing = await redis.hgetall(key)
    if not existing:
        return {"refreshed": False, "reason": "not_registered"}

    now = str(time.time())
    updates = {"last_heartbeat": now, "status": "active"}
    if caller is not None:
        if is_bound(existing):
            if not owns(existing, caller):
                return {"refreshed": False, "reason": "not_owner"}
        else:
            updates.update(caller.owner_fields())
    if session_id:
        updates["session_id"] = session_id
    if goal:
        updates["goal"] = goal

    await redis.hset(key, mapping=updates)
    # No TTL — persists until deregistered
    await redis.zadd(PRESENCE_INDEX, {agent_id: float(now)})

    return {"refreshed": True, "agent_id": agent_id}


async def deregister(redis, agent_id: str, caller: Caller | None = None) -> dict:
    """Remove an agent's presence immediately.

    With ``caller``: only the owning member — or an admin key in the row's
    workspace (the dashboard) — may remove a bound row."""
    key = f"{PRESENCE_PREFIX}{agent_id}"
    if caller is not None:
        existing = await redis.hgetall(key)
        if existing and is_bound(existing) and not administers(existing, caller):
            return {"removed": False, "agent_id": agent_id, "reason": "not_owner"}
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


async def label_held_by_other(redis, label: str, caller: Caller | None) -> bool:
    """Is ``label`` bound (by a presence row) to a member other than ``caller``?

    Used to refuse DMs, bulletins and broadcasts posted under another member's
    label. An admin key (the dashboard) may use any label; an unbound label
    is free for anyone."""
    if caller is None:
        return True
    row = await redis.hgetall(f"{PRESENCE_PREFIX}{label}")
    if not row or not is_bound(row):
        return False
    return not administers(row, caller)


async def label_owner(redis, label: str) -> tuple[str, str] | None:
    """(workspace, member) a label is bound to, or None when unbound."""
    row = await redis.hgetall(f"{PRESENCE_PREFIX}{label}")
    if not row or not is_bound(row):
        return None
    from app.principal import OWNER_MEMBER, record_workspace
    return record_workspace(row), row[OWNER_MEMBER]
