import json
import pytest
from unittest.mock import AsyncMock

from app.agent_gateway.fastpath import (
    check_fastpath,
    record_outcome_for_fastpath,
    fastpath_key,
)


def test_fastpath_key_format():
    key = fastpath_key("a1", "edit_file", "src/foo.py")
    # Hashed target — bounded 16-char hex suffix
    assert key.startswith("ag:fastpath:a1:edit_file:")
    suffix = key[len("ag:fastpath:a1:edit_file:"):]
    assert len(suffix) == 16
    assert all(c in "0123456789abcdef" for c in suffix)


def test_fastpath_key_stable_for_same_target():
    """Same inputs always produce the same key."""
    k1 = fastpath_key("a1", "edit_file", "src/foo.py")
    k2 = fastpath_key("a1", "edit_file", "src/foo.py")
    assert k1 == k2


def test_fastpath_key_differs_for_different_targets():
    """Different targets produce different keys."""
    k1 = fastpath_key("a1", "edit_file", "src/foo.py")
    k2 = fastpath_key("a1", "edit_file", "src/bar.py")
    assert k1 != k2


def test_fastpath_key_handles_target_with_colons():
    """Target with colons doesn't corrupt the key structure."""
    key = fastpath_key("a1", "call_api", "https://api.example.com:443/v1/x")
    assert key.startswith("ag:fastpath:a1:call_api:")
    # The suffix is still bounded 16 hex chars
    suffix = key[len("ag:fastpath:a1:call_api:"):]
    assert len(suffix) == 16


def test_fastpath_key_handles_long_target():
    """Target that's a 2KB command line still produces a bounded key."""
    long_target = "black " + ("x" * 2048)
    key = fastpath_key("a1", "run_command", long_target)
    assert len(key) < 80  # well under any concern threshold


@pytest.mark.asyncio
async def test_check_fastpath_returns_true_when_above_threshold():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps({"success_rate": 0.95, "samples": 25}))
    assert await check_fastpath(redis, "a1", "edit_file", "src/foo.py", min_rate=0.9, min_samples=20) is True


@pytest.mark.asyncio
async def test_check_fastpath_false_when_below_samples():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps({"success_rate": 0.99, "samples": 5}))
    assert await check_fastpath(redis, "a1", "edit_file", "src/foo.py", min_rate=0.9, min_samples=20) is False


@pytest.mark.asyncio
async def test_check_fastpath_false_when_below_rate():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=json.dumps({"success_rate": 0.5, "samples": 100}))
    assert await check_fastpath(redis, "a1", "edit_file", "src/foo.py", min_rate=0.9, min_samples=20) is False


@pytest.mark.asyncio
async def test_check_fastpath_false_when_missing():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    assert await check_fastpath(redis, "a1", "edit_file", "src/foo.py", min_rate=0.9, min_samples=20) is False


@pytest.mark.asyncio
async def test_check_fastpath_false_on_redis_error():
    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
    assert await check_fastpath(redis, "a1", "edit_file", "src/foo.py") is False


@pytest.mark.asyncio
async def test_record_outcome_calls_eval_with_success_arg():
    redis = AsyncMock()
    redis.eval = AsyncMock()
    await record_outcome_for_fastpath(redis, "a1", "edit_file", "src/foo.py", success=True, ttl_seconds=86400)
    redis.eval.assert_called_once()
    args = redis.eval.call_args
    # signature: redis.eval(script, num_keys, key, success_str, ttl_str)
    positional = args[0]
    assert positional[0].strip().startswith("local key")  # the Lua script
    assert positional[1] == 1  # numkeys
    # KEY is positional[2], success arg is positional[3], TTL is positional[4]
    assert positional[3] == "1"  # success=True


@pytest.mark.asyncio
async def test_record_outcome_calls_eval_with_failure_arg():
    redis = AsyncMock()
    redis.eval = AsyncMock()
    await record_outcome_for_fastpath(redis, "a1", "edit_file", "src/foo.py", success=False)
    args = redis.eval.call_args
    assert args[0][3] == "0"  # success=False


@pytest.mark.asyncio
async def test_record_outcome_swallows_eval_error():
    redis = AsyncMock()
    redis.eval = AsyncMock(side_effect=RuntimeError("eval failed"))
    # Should not raise — best-effort recording
    await record_outcome_for_fastpath(redis, "a1", "edit_file", "src/foo.py", success=True)
