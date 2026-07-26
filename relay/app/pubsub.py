"""Redis pub/sub + backlog for FirekeepRelay channel messaging."""

import json
import logging
import time

logger = logging.getLogger(__name__)


async def broadcast(
    redis,
    channel: str,
    content: str,
    sender: str,
    tags: list[str],
    backlog_size: int = 100,
    backlog_ttl_seconds: int = 86400,
):
    """Publish message to channel + store in backlog."""
    msg = json.dumps({"content": content, "sender": sender, "tags": tags, "timestamp": time.time()})
    # Publish to real-time subscribers
    await redis.publish(f"nr:channel:{channel}", msg)
    # Store in backlog for late joiners
    backlog_key = f"nr:backlog:{channel}"
    await redis.lpush(backlog_key, msg)
    await redis.ltrim(backlog_key, 0, backlog_size - 1)
    await redis.expire(backlog_key, backlog_ttl_seconds)


async def get_backlog(redis, channel: str, limit: int = 50) -> list[dict]:
    """Get recent messages from channel backlog."""
    backlog_key = f"nr:backlog:{channel}"
    messages = await redis.lrange(backlog_key, 0, limit - 1)
    results = []
    for m in messages:
        try:
            results.append(json.loads(m))
        except json.JSONDecodeError:
            logger.warning("Corrupt data in backlog key %s", backlog_key)
            continue
    return results


async def get_active_channels(redis) -> list[str]:
    """List channels with backlog entries."""
    keys = []
    async for key in redis.scan_iter("nr:backlog:*"):
        channel = key.replace("nr:backlog:", "")
        keys.append(channel)
    return keys
