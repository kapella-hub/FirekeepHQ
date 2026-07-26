"""Pattern-cached fast-path: demotes proven safe actions to auto tier."""

from __future__ import annotations

import hashlib
import json
import logging

logger = logging.getLogger(__name__)

_INCREMENTAL_UPDATE_LUA = """
local key = KEYS[1]
local success = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])

local current = redis.call('GET', key)
local samples
local success_rate

if current then
    local data = cjson.decode(current)
    local n = tonumber(data.samples) or 0
    local r = tonumber(data.success_rate) or 0
    samples = n + 1
    success_rate = (r * n + success) / samples
else
    samples = 1
    success_rate = success
end

local payload = cjson.encode({samples = samples, success_rate = success_rate})
redis.call('SET', key, payload, 'EX', ttl)
return payload
"""


def fastpath_key(agent_id: str, action_type: str, target: str) -> str:
    """Build a bounded-length Redis key for fastpath stats.

    The target is hashed so URLs, Windows paths, or long command strings
    don't produce keys with embedded colons or unbounded length.
    """
    target_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    return f"ag:fastpath:{agent_id}:{action_type}:{target_hash}"


async def check_fastpath(
    redis,
    agent_id: str,
    action_type: str,
    target: str,
    min_rate: float = 0.9,
    min_samples: int = 20,
) -> bool:
    try:
        raw = await redis.get(fastpath_key(agent_id, action_type, target))
        if not raw:
            return False
        data = json.loads(raw)
        return data.get("success_rate", 0.0) >= min_rate and data.get("samples", 0) >= min_samples
    except Exception as exc:
        logger.debug("fastpath check error: %s", exc)
        return False


async def record_outcome_for_fastpath(
    redis,
    agent_id: str,
    action_type: str,
    target: str,
    success: bool,
    ttl_seconds: int = 86400,
) -> None:
    """Atomically update fastpath stats. Concurrent calls do not lose samples."""
    key = fastpath_key(agent_id, action_type, target)
    try:
        await redis.eval(
            _INCREMENTAL_UPDATE_LUA,
            1,  # number of KEYS
            key,
            "1" if success else "0",
            str(ttl_seconds),
        )
    except Exception as exc:
        logger.warning("fastpath record error: %s", exc)
