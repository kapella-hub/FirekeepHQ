"""Tests for task queue operations including delete."""

import pytest
import pytest_asyncio
import fakeredis.aioredis

from app.tasks import create_task, delete_task, list_tasks, get_task


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestDeleteTask:
    @pytest.mark.asyncio
    async def test_delete_existing_task(self, redis):
        task = await create_task(redis, "Test task", assignee="agent-alpha")
        deleted = await delete_task(redis, task["id"])
        assert deleted is True

    @pytest.mark.asyncio
    async def test_deleted_task_not_in_list(self, redis):
        task = await create_task(redis, "Test task")
        await delete_task(redis, task["id"])
        tasks = await list_tasks(redis)
        assert len(tasks) == 0

    @pytest.mark.asyncio
    async def test_deleted_task_not_found_by_get(self, redis):
        task = await create_task(redis, "Test task")
        await delete_task(redis, task["id"])
        result = await get_task(redis, task["id"])
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, redis):
        deleted = await delete_task(redis, "task-doesnotexist")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_delete_one_preserves_others(self, redis):
        t1 = await create_task(redis, "Task 1")
        t2 = await create_task(redis, "Task 2")
        await delete_task(redis, t1["id"])
        tasks = await list_tasks(redis)
        assert len(tasks) == 1
        assert tasks[0]["id"] == t2["id"]
