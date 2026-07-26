import json
import pytest
from unittest.mock import AsyncMock

from app.workers.agent_gateway_sweep import sweep_overdue_actions


@pytest.fixture
def fake_redis():
    """In-memory fake async Redis client for sweep tests."""
    redis = AsyncMock()
    redis._data = {}
    redis._ttl = {}

    async def fake_scan(cursor, match=None, count=100):
        prefix = match.rstrip("*") if match else ""
        keys = [k for k in redis._data if k.startswith(prefix)]
        return (0, keys)

    async def fake_ttl(k):
        return redis._ttl.get(k, -2)

    async def fake_get(k):
        return redis._data.get(k)

    async def fake_delete(k):
        redis._data.pop(k, None)
        redis._ttl.pop(k, None)
        return 1

    redis.scan = fake_scan
    redis.ttl = fake_ttl
    redis.get = fake_get
    redis.delete = fake_delete
    return redis


@pytest.mark.asyncio
async def test_sweep_marks_overdue_actions(fake_redis):
    fake_redis._data["ag:predict:act_old"] = json.dumps({
        "agent_id": "a", "session_id": "s",
        "prediction": {"intent": "x", "confidence": 0.9},
    })
    fake_redis._ttl["ag:predict:act_old"] = 10  # below grace=30, should sweep

    fake_redis._data["ag:predict:act_fresh"] = json.dumps({
        "agent_id": "a", "session_id": "s",
        "prediction": {"intent": "x", "confidence": 0.9},
    })
    fake_redis._ttl["ag:predict:act_fresh"] = 200  # above grace=30, should NOT sweep

    emitter = AsyncMock()

    swept = await sweep_overdue_actions(fake_redis, emitter, grace_seconds=30)
    assert swept == 1
    assert "ag:predict:act_old" not in fake_redis._data
    assert "ag:predict:act_fresh" in fake_redis._data
    assert emitter.call_args.kwargs["payload"]["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_sweep_handles_empty_store(fake_redis):
    emitter = AsyncMock()
    swept = await sweep_overdue_actions(fake_redis, emitter)
    assert swept == 0
    emitter.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_swallows_emit_errors(fake_redis):
    fake_redis._data["ag:predict:act_old"] = json.dumps({
        "agent_id": "a", "session_id": "s",
        "prediction": {"intent": "x", "confidence": 0.9},
    })
    fake_redis._ttl["ag:predict:act_old"] = 10  # below grace, should attempt sweep

    emitter = AsyncMock(side_effect=RuntimeError("emitter down"))

    # The emit error is caught inside the per-key try/except.
    # The key is NOT deleted and swept count stays 0.
    swept = await sweep_overdue_actions(fake_redis, emitter, grace_seconds=30)
    assert isinstance(swept, int)
    # No exception should propagate
    assert swept == 0


@pytest.mark.asyncio
async def test_sweep_skips_none_redis():
    emitter = AsyncMock()
    swept = await sweep_overdue_actions(None, emitter)
    assert swept == 0
    emitter.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_action_id_extracted_from_key(fake_redis):
    """action_id in the emitted payload matches the key suffix."""
    fake_redis._data["ag:predict:myaction123"] = json.dumps({
        "agent_id": "agent1", "session_id": "sess1",
        "prediction": {"intent": "do something", "confidence": 0.8},
    })
    fake_redis._ttl["ag:predict:myaction123"] = 5  # near expiry

    emitter = AsyncMock()
    await sweep_overdue_actions(fake_redis, emitter, grace_seconds=30)

    assert emitter.call_args.kwargs["payload"]["action_id"] == "myaction123"
    assert emitter.call_args.kwargs["session_id"] == "sess1"
    assert emitter.call_args.kwargs["agent_id"] == "agent1"
