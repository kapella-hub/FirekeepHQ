"""Tests for FirekeepRelay bulletin module."""

import pytest

from app.bulletin import post_bulletin, read_bulletin, get_bulletin_count


@pytest.mark.asyncio
async def test_post_and_read_roundtrip(redis):
    """Posted bulletins can be read back."""
    await post_bulletin(redis, "deploy v1", "agent-1", ["deploy"], ttl_hours=1)
    await post_bulletin(redis, "test passed", "agent-2", ["ci"], ttl_hours=1)

    posts = await read_bulletin(redis, limit=10)
    assert len(posts) == 2
    # Newest first
    assert posts[0]["content"] == "test passed"
    assert posts[1]["content"] == "deploy v1"


@pytest.mark.asyncio
async def test_filter_by_tag(redis):
    """Reading with tag filter returns only matching posts."""
    await post_bulletin(redis, "deploy v1", "agent-1", ["deploy"], ttl_hours=1)
    await post_bulletin(redis, "test passed", "agent-2", ["ci"], ttl_hours=1)

    posts = await read_bulletin(redis, tags=["ci"], limit=10)
    assert len(posts) == 1
    assert posts[0]["content"] == "test passed"


@pytest.mark.asyncio
async def test_filter_by_author(redis):
    """Reading with author filter returns only matching posts."""
    await post_bulletin(redis, "deploy v1", "agent-1", ["deploy"], ttl_hours=1)
    await post_bulletin(redis, "test passed", "agent-2", ["ci"], ttl_hours=1)

    posts = await read_bulletin(redis, author="agent-1", limit=10)
    assert len(posts) == 1
    assert posts[0]["author"] == "agent-1"


@pytest.mark.asyncio
async def test_expired_post_cleanup(redis):
    """Expired posts are cleaned from the sorted set on read."""
    post = await post_bulletin(redis, "ephemeral", "agent", [], ttl_hours=1)
    # Manually delete the post key to simulate expiration
    await redis.delete(f"nr:post:{post['id']}")

    posts = await read_bulletin(redis, limit=10)
    assert len(posts) == 0
    # Sorted set should be cleaned
    count = await redis.zcard("nr:bulletin")
    assert count == 0


@pytest.mark.asyncio
async def test_get_bulletin_count(redis):
    """get_bulletin_count returns approximate count via ZCARD."""
    await post_bulletin(redis, "post1", "agent", [], ttl_hours=1)
    await post_bulletin(redis, "post2", "agent", [], ttl_hours=1)

    count = await get_bulletin_count(redis)
    assert count == 2
