"""Persistent bulletin board backed by Redis sorted sets."""

import json
import logging
import time
import uuid

logger = logging.getLogger(__name__)


async def post_bulletin(redis, content: str, author: str, tags: list[str], ttl_hours: int = 24) -> dict:
    """Post to the bulletin board."""
    post_id = str(uuid.uuid4())[:8]
    now = time.time()
    post_data = {"id": post_id, "content": content, "author": author, "tags": tags, "timestamp": now}

    # Store post data with TTL
    post_key = f"nr:post:{post_id}"
    await redis.set(post_key, json.dumps(post_data), ex=ttl_hours * 3600)

    # Add to sorted set scored by timestamp
    await redis.zadd("nr:bulletin", {post_id: now})
    await redis.expire("nr:bulletin", ttl_hours * 3600)

    return post_data


async def read_bulletin(redis, tags: list[str] | None = None, author: str | None = None, limit: int = 20) -> list[dict]:
    """Read bulletin board entries, optionally filtered."""
    # Get recent post IDs (overfetch to account for filtering and expired posts)
    post_ids = await redis.zrevrange("nr:bulletin", 0, limit * 2)

    results = []
    for pid in post_ids:
        raw = await redis.get(f"nr:post:{pid}")
        if raw is None:
            # Post expired, clean up sorted set
            await redis.zrem("nr:bulletin", pid)
            continue
        try:
            post = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Corrupt data in Redis key nr:post:%s", pid)
            await redis.zrem("nr:bulletin", pid)
            continue
        # Apply filters
        if tags and not any(t in post.get("tags", []) for t in tags):
            continue
        if author and post.get("author") != author:
            continue
        results.append(post)
        if len(results) >= limit:
            break

    return results


async def get_bulletin_count(redis) -> int:
    """Approximate count of bulletin posts (may include recently expired)."""
    return await redis.zcard("nr:bulletin")
