"""Direct message system for agent-to-agent and dashboard-to-agent communication.

Messages are stored per-recipient in Redis lists with key pattern nr:dm:{agent_id}.
Each message is a JSON object with sender, content, timestamp, and read status.
Messages expire after a configurable TTL (default 24h).
"""

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)

DM_PREFIX = "nr:dm:"
DM_TTL_SECONDS = 86400  # 24 hours


async def send_dm(
    redis,
    to_agent_id: str,
    content: str,
    from_id: str,
) -> dict:
    """Send a direct message to an agent. Stored in recipient's inbox."""
    msg_id = f"dm-{uuid.uuid4().hex[:8]}"
    now = time.time()

    message = {
        "id": msg_id,
        "from": from_id,
        "to": to_agent_id,
        "content": content,
        "timestamp": now,
        "read": False,
    }

    key = f"{DM_PREFIX}{to_agent_id}"
    await redis.lpush(key, json.dumps(message))
    # Set/refresh TTL on the inbox
    await redis.expire(key, DM_TTL_SECONDS)

    return message


async def get_dms(
    redis,
    agent_id: str,
    unread_only: bool = False,
    limit: int = 50,
) -> list[dict]:
    """Get direct messages for an agent, newest first.

    If unread_only is True, only returns messages where read is False.
    """
    key = f"{DM_PREFIX}{agent_id}"
    raw_messages = await redis.lrange(key, 0, -1)

    messages = []
    for raw in raw_messages:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if unread_only and msg.get("read", False):
            continue
        messages.append(msg)
        if len(messages) >= limit:
            break

    return messages


async def mark_read(
    redis,
    agent_id: str,
) -> int:
    """Mark all messages in an agent's inbox as read. Returns count marked."""
    key = f"{DM_PREFIX}{agent_id}"
    raw_messages = await redis.lrange(key, 0, -1)

    if not raw_messages:
        return 0

    count = 0
    updated = []
    for raw in raw_messages:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            updated.append(raw)
            continue
        if not msg.get("read", False):
            msg["read"] = True
            count += 1
        updated.append(json.dumps(msg))

    if count > 0:
        # Atomically replace the list
        pipe = redis.pipeline()
        pipe.delete(key)
        for item in reversed(updated):  # reversed because lpush prepends
            pipe.lpush(key, item)
        pipe.expire(key, DM_TTL_SECONDS)
        await pipe.execute()

    return count
