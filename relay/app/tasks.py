"""Task queue backed by Redis hashes and sorted sets.

Provides structured task assignment and tracking for multi-agent workflows.
Tasks are stored as individual hashes with a sorted set index for ordering.

Redis keys:
    nr:task:{id}     — Hash with task fields
    nr:tasks         — Sorted set of task IDs scored by creation time
"""

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

TASK_INDEX_KEY = "nr:tasks"
TASK_PREFIX = "nr:task:"
TASK_TTL_SECONDS = 86400 * 7  # 7 days

VALID_STATUSES = frozenset({
    "pending", "in-progress", "completed", "failed", "cancelled",
    # A2A-compatible states
    "working",          # alias for in-progress
    "input-required",   # agent needs clarification
    "rejected",         # invalid/unauthorized task
})


async def create_task(
    redis,
    title: str,
    assignee: str | None = None,
    assigner: str = "unknown",
    description: str = "",
    priority: str = "normal",
    files: list[str] | None = None,
    context: str = "",
) -> dict:
    """Create a new task and add to the index."""
    task_id = "task-" + str(uuid.uuid4())[:8]
    now = time.time()

    initial_history = [{"state": "pending", "timestamp": now}]
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

    key = f"{TASK_PREFIX}{task_id}"
    await redis.hset(key, mapping=task)
    await redis.expire(key, TASK_TTL_SECONDS)
    await redis.zadd(TASK_INDEX_KEY, {task_id: now})
    await redis.expire(TASK_INDEX_KEY, TASK_TTL_SECONDS)

    # Return with parsed JSON fields
    task["files"] = files or []
    task["history"] = initial_history
    return task


async def list_tasks(
    redis,
    assignee: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List tasks, optionally filtered by assignee and/or status."""
    # Get all task IDs ordered by creation time (newest first)
    task_ids = await redis.zrevrange(TASK_INDEX_KEY, 0, limit * 3)

    results = []
    for tid in task_ids:
        key = f"{TASK_PREFIX}{tid}"
        raw = await redis.hgetall(key)
        if not raw:
            # Task expired, clean up index
            await redis.zrem(TASK_INDEX_KEY, tid)
            continue

        # Apply filters
        if assignee and raw.get("assignee", "") != assignee:
            continue
        if status and raw.get("status", "") != status:
            continue

        task = _parse_task(raw)
        results.append(task)
        if len(results) >= limit:
            break

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
) -> dict | None:
    """Update task fields. Returns updated task or None if not found."""
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
        history.append({"state": status, "timestamp": time.time()})
        updates["history"] = json.dumps(history)
    if result is not None:
        updates["result"] = result
    if assignee is not None:
        updates["assignee"] = assignee

    await redis.hset(key, mapping=updates)

    # Return full task
    raw = await redis.hgetall(key)
    return _parse_task(raw)
