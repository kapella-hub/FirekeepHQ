"""Task queue backed by Redis hashes and sorted sets.

Provides structured task assignment and tracking for multi-agent workflows.
Tasks are stored as individual hashes with a sorted set index for ordering.

Redis keys:
    nr:task:{id}     — Hash with task fields
    nr:tasks         — Sorted set of task IDs scored by creation time

Who wrote a task (THREAT-MODEL row 12, §5.8). Three optional fields hold the
VERIFIED principal of a write, as a JSON object
``{workspace_id, member_id, credential_id, authenticated}``:

    created_by    — the caller that created the task
    updated_by    — the caller of the most recent update
    completed_by  — the caller of the most recent write that left the task in a
                    terminal state (completed/failed/cancelled/rejected).
                    Cleared when a task is reopened, and cleared — never
                    inherited — when that write carried no verified principal.

Each history entry carries the same object under ``by``. The principal comes
from the auth layer (``auth.principal.principal_from_scope``), never from
``assigner``/``assignee``/``X-Agent-Id``, which are display labels. With auth
disabled every caller is the deployment's anonymous owner, stamped
``authenticated: false`` — every writer looks the same, and says so.
"""

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

TASK_INDEX_KEY = "nr:tasks"
TASK_PREFIX = "nr:task:"
TASK_TTL_SECONDS = 86400 * 7  # 7 days

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "rejected"})
_PRINCIPAL_FIELDS = ("created_by", "updated_by", "completed_by")

VALID_STATUSES = frozenset({
    "pending", "in-progress", "completed", "failed", "cancelled",
    # A2A-compatible states
    "working",          # alias for in-progress
    "input-required",   # agent needs clarification
    "rejected",         # invalid/unauthorized task
})


def _principal_stamp(identity: dict) -> dict:
    """The audit-relevant subset of a verified identity. Scopes are dropped:
    they describe what a key may do, not who holds it."""
    return {
        "workspace_id": str(identity.get("workspace_id") or ""),
        "member_id": str(identity.get("member_id") or ""),
        "credential_id": str(identity.get("credential_id") or ""),
        "authenticated": identity.get("authenticated") is True,
    }


def principal_stamp_from_scope(scope) -> dict | None:
    """The stamp for the request behind an ASGI ``scope``, or None when unknowable.

    An identity attached by FirekeepKeyAuthMiddleware is authenticated by
    construction (it carries no ``authenticated`` key of its own). With auth
    disabled, ``principal_from_scope`` returns the anonymous owner, which
    says ``authenticated: False`` itself. Auth enabled with no identity
    attached is a wiring fault: nothing is stamped, and a reader treats an
    absent stamp as "unknown writer", never as somebody in particular."""
    try:
        identity = (scope.get("state") or {}).get("identity")
        if identity is not None:
            return _principal_stamp({**identity, "authenticated": True})
        from auth.principal import principal_from_scope
        return _principal_stamp(principal_from_scope(scope))
    except Exception as exc:  # noqa: BLE001 — an unknowable writer is recorded as absent
        logger.debug("no verified principal for this task write: %s", exc)
        return None


async def create_task(
    redis,
    title: str,
    assignee: str | None = None,
    assigner: str = "unknown",
    description: str = "",
    priority: str = "normal",
    files: list[str] | None = None,
    context: str = "",
    created_by: dict | None = None,
) -> dict:
    """Create a new task and add to the index.

    ``created_by`` is the verified principal stamp of the caller (see the
    module docstring); omitted when the caller is unknowable."""
    task_id = "task-" + str(uuid.uuid4())[:8]
    now = time.time()

    initial_history = [{"state": "pending", "timestamp": now}]
    if created_by is not None:
        initial_history[0]["by"] = created_by
    task = {
        "id": task_id,
        "title": title,
        "description": description,
        "assignee": assignee or "",
        "assigner": assigner,
        "status": "pending",
        "priority": priority,
        "files": json.dumps(files or []),
        "context": context,
        "created_at": now,
        "updated_at": now,
        "history": json.dumps(initial_history),
    }
    if created_by is not None:
        task["created_by"] = json.dumps(created_by)

    key = f"{TASK_PREFIX}{task_id}"
    await redis.hset(key, mapping=task)
    await redis.expire(key, TASK_TTL_SECONDS)
    await redis.zadd(TASK_INDEX_KEY, {task_id: now})
    await redis.expire(TASK_INDEX_KEY, TASK_TTL_SECONDS)

    # Return with parsed JSON fields
    task["files"] = files or []
    task["history"] = initial_history
    if created_by is not None:
        task["created_by"] = created_by
    return task


async def list_tasks(
    redis,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 20,
    oldest_first: bool = False,
    title: str | None = None,
) -> list[dict]:
    """List tasks with explicit ordering and optional field filters."""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    read_range = redis.zrange if oldest_first else redis.zrevrange
    batch_size = max(20, limit * 3)
    results = []
    stale_ids = []
    offset = 0
    while len(results) < limit:
        task_ids = await read_range(TASK_INDEX_KEY, offset, offset + batch_size - 1)
        if not task_ids:
            break
        offset += len(task_ids)
        for tid in task_ids:
            key = f"{TASK_PREFIX}{tid}"
            raw = await redis.hgetall(key)
            if not raw:
                # Defer cleanup until paging ends so removals do not shift the
                # next rank window and silently skip a live task.
                stale_ids.append(tid)
                continue

            if assignee and raw.get("assignee", "") != assignee:
                continue
            if status and raw.get("status", "") != status:
                continue
            if title and raw.get("title", "") != title:
                continue

            results.append(_parse_task(raw))
            if len(results) >= limit:
                break
        if len(task_ids) < batch_size:
            break

    if stale_ids:
        await redis.zrem(TASK_INDEX_KEY, *stale_ids)

    return results


def _parse_task(raw: dict) -> dict:
    """Parse a raw Redis hash into a task dict with typed fields."""
    task = dict(raw)
    try:
        task["files"] = json.loads(task.get("files", "[]"))
    except (json.JSONDecodeError, TypeError):
        task["files"] = []
    try:
        task["history"] = json.loads(task.get("history", "[]"))
    except (json.JSONDecodeError, TypeError):
        task["history"] = []
    for k in ("created_at", "updated_at"):
        try:
            task[k] = float(task[k])
        except (ValueError, KeyError):
            pass
    for k in _PRINCIPAL_FIELDS:
        if k not in task:
            continue
        try:
            parsed = json.loads(task[k])
        except (json.JSONDecodeError, TypeError):
            parsed = None
        # A stamp that does not parse is not a stamp. Dropping it is the
        # fail-closed reading: a consumer gating on it sees "unknown writer".
        if isinstance(parsed, dict):
            task[k] = parsed
        else:
            del task[k]
    return task


async def get_task(redis, task_id: str) -> dict | None:
    """Get a single task by ID. Returns None if not found."""
    key = f"{TASK_PREFIX}{task_id}"
    raw = await redis.hgetall(key)
    if not raw:
        return None
    return _parse_task(raw)


async def delete_task(redis, task_id: str) -> bool:
    """Delete a task by ID. Returns True if it existed."""
    key = f"{TASK_PREFIX}{task_id}"
    deleted = await redis.delete(key)
    await redis.zrem(TASK_INDEX_KEY, task_id)
    if deleted:
        logger.info("Deleted task '%s'", task_id)
    return bool(deleted)


async def update_task(
    redis,
    task_id: str,
    status: str | None = None,
    result: str | None = None,
    assignee: str | None = None,
    principal: dict | None = None,
) -> dict | None:
    """Update task fields. Returns updated task or None if not found.

    ``principal`` is the verified stamp of the caller. It becomes
    ``updated_by``, and ``completed_by`` whenever the task is terminal after
    this write — including a write that changes only ``result`` on an
    already-completed task, because otherwise a second writer could rewrite a
    human's answer under the human's name. With no principal both fields are
    removed rather than left naming the previous writer."""
    key = f"{TASK_PREFIX}{task_id}"
    exists = await redis.exists(key)
    if not exists:
        return None

    updates = {"updated_at": str(time.time())}
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid status: {status}. Must be one of: {sorted(VALID_STATUSES)}")
        updates["status"] = status
        # Append to state transition history
        history_raw = await redis.hget(key, "history")
        try:
            history = json.loads(history_raw) if history_raw else []
        except (json.JSONDecodeError, TypeError):
            history = []
        entry = {"state": status, "timestamp": time.time()}
        if principal is not None:
            entry["by"] = principal
        history.append(entry)
        updates["history"] = json.dumps(history)
    if result is not None:
        updates["result"] = result
    if assignee is not None:
        updates["assignee"] = assignee

    resulting_status = status if status is not None else await redis.hget(key, "status")
    removals = []
    if principal is not None:
        updates["updated_by"] = json.dumps(principal)
    else:
        removals.append("updated_by")
    if principal is not None and resulting_status in TERMINAL_STATUSES:
        updates["completed_by"] = json.dumps(principal)
    else:
        removals.append("completed_by")

    # One transaction: the fields this write sets and the stale stamps it
    # clears land together, so no reader sees this write's result under the
    # previous writer's name.
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hset(key, mapping=updates)
        if removals:
            pipe.hdel(key, *removals)
        await pipe.execute()

    # Return full task
    raw = await redis.hgetall(key)
    return _parse_task(raw)
