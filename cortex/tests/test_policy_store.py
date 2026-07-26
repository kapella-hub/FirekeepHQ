from __future__ import annotations

import pytest

from app.policy.store import get_policy_decisions, record_policy_decision, summarize_policy_decisions


class _FakeRedis:
    def __init__(self):
        self.items: list[str] = []

    async def lpush(self, key, value):
        self.items.insert(0, value)
        return len(self.items)

    async def ltrim(self, key, start, end):
        self.items = self.items[start:end + 1]
        return True

    async def lrange(self, key, start, end):
        if end == -1:
            return self.items[start:]
        return self.items[start:end + 1]


@pytest.mark.asyncio
async def test_record_and_query_policy_decisions():
    redis = _FakeRedis()

    await record_policy_decision(
        redis,
        file_path="src/main.py",
        agent_id="agent-1",
        session_id="session-1",
        action="warn",
        risk_score=0.4,
        reasons=["warn reason"],
        signals={"file_risk": {"action": "warn"}},
    )
    await record_policy_decision(
        redis,
        file_path="src/secret.env",
        agent_id="agent-2",
        session_id="session-2",
        action="block",
        risk_score=1.0,
        reasons=["block reason"],
        signals={"path_deny": {"action": "block"}},
    )

    decisions = await get_policy_decisions(redis, limit=10)
    summary = summarize_policy_decisions(decisions)

    assert len(decisions) == 2
    assert decisions[0]["action"] == "block"
    assert summary["counts"]["warn"] == 1
    assert summary["counts"]["block"] == 1
    assert summary["unique_agents"] == 2


@pytest.mark.asyncio
async def test_policy_decision_filters():
    redis = _FakeRedis()
    await record_policy_decision(
        redis,
        file_path="src/a.py",
        agent_id="agent-1",
        session_id="session-1",
        action="allow",
        risk_score=0.0,
        reasons=[],
        signals={},
    )
    await record_policy_decision(
        redis,
        file_path="src/b.py",
        agent_id="agent-1",
        session_id="session-2",
        action="warn",
        risk_score=0.3,
        reasons=["warn"],
        signals={},
    )

    warn_only = await get_policy_decisions(redis, limit=10, action="warn")
    agent_only = await get_policy_decisions(redis, limit=10, agent_id="agent-1")

    assert len(warn_only) == 1
    assert warn_only[0]["file_path"] == "src/b.py"
    assert len(agent_only) == 2
