"""Replay events emitted inside a request carry the WRITER's verified principal.

Replay reads are scoped per event (replay/authz.py): a non-admin member sees
only events stamped with its own member, and an unstamped event belongs to the
deployment owner. So every emit site that runs inside a request must stamp the
verified ``workspace_id`` / ``member_id`` — otherwise a member's own gateway
predictions, feedback and briefing receipts vanish from its own timeline. #45
stamped memory_read / memory_write; these are the remaining request-path
emitters. Background emitters with no principal (collectors, sentinel, relay's
shared bus) deliberately stay unattributed.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent_gateway.models import (
    Action, ActionAfterRequest, ActionBeforeRequest, Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter
from app.workers.agent_gateway_sweep import sweep_overdue_actions


class _Allow:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _Engine:
    async def evaluate(self, ctx):
        return _Allow()


def _service(redis, emitted):
    async def _emit(**kwargs):
        emitted.append(kwargs)

    async def _no(*a, **k):
        return False

    return AgentGatewayService(
        policy_engine=_Engine(),
        recent_failure_check=_no,
        fastpath_check=_no,
        session_touched_check=_no,
        replay_emitter=_emit,
        rethink_counter=RethinkCounter(redis),
        prediction_redis=redis,
        fastpath_redis=redis,
        policy_decision_redis=redis,
    )


def _verified_before(member: str = "member-alice") -> ActionBeforeRequest:
    req = ActionBeforeRequest(
        session_id="sess", agent_id="claude", adapter="shell-hook",
        action=Action(type="edit_file", target="x.py"),
    )
    # What agent_gateway/api.py stamps from require_scope's identity.
    req._verified_workspace = "workspace-a"
    req._verified_member = member
    return req


@pytest.mark.asyncio
async def test_gateway_prediction_and_reconcile_carry_the_predictors_principal():
    import fakeredis.aioredis as fr

    redis = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(redis, emitted)

    before = await svc.decide(_verified_before())
    after = ActionAfterRequest(action_id=before.action_id, outcome=Outcome(success=True))
    # action_after carries only the verified workspace; a different member's
    # key reconciling does not re-attribute the action.
    after._verified_workspace = "workspace-a"
    await svc.record(after)

    by_type = {e["event_type"]: e for e in emitted}
    for event_type in ("agent.action.predict", "agent.action.reconcile"):
        assert by_type[event_type]["workspace_id"] == "workspace-a", event_type
        assert by_type[event_type]["member_id"] == "member-alice", event_type


@pytest.mark.asyncio
async def test_unverified_direct_service_calls_stay_unattributed():
    """Direct service calls (no REST principal) must not invent an owner."""
    import fakeredis.aioredis as fr

    redis = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(redis, emitted)
    await svc.decide(ActionBeforeRequest(
        session_id="sess", agent_id="claude", adapter="shell-hook",
        action=Action(type="edit_file", target="x.py"),
    ))
    predict = next(e for e in emitted if e["event_type"] == "agent.action.predict")
    assert not predict.get("workspace_id")
    assert not predict.get("member_id")


@pytest.mark.asyncio
async def test_the_sweeper_files_an_expired_action_under_its_predictor():
    redis = AsyncMock()
    entry = json.dumps({
        "agent_id": "claude", "session_id": "sess",
        "workspace_id": "workspace-a", "member_id": "member-alice",
        "prediction": {"intent": "x", "confidence": 0.9},
    })
    redis.scan = AsyncMock(return_value=(0, ["ag:predict:act_old"]))
    redis.ttl = AsyncMock(return_value=5)
    redis.get = AsyncMock(return_value=entry)
    redis.delete = AsyncMock(return_value=1)
    emitter = AsyncMock()

    assert await sweep_overdue_actions(redis, emitter, grace_seconds=30) == 1
    assert emitter.call_args.kwargs["workspace_id"] == "workspace-a"
    assert emitter.call_args.kwargs["member_id"] == "member-alice"


@pytest.mark.asyncio
async def test_briefing_skills_receipt_carries_the_callers_principal(monkeypatch):
    from app.briefing import sections as S

    from types import SimpleNamespace

    async def fake_points(*args, **kwargs):
        point = SimpleNamespace(id="A1", payload={
            "trigger": "rotate keys", "symptoms": "s", "skill_status": "active"})
        return [point], True

    async def no_trial(*args, **kwargs):
        return []

    emitted = []

    async def fake_emit(event_type, **kw):
        emitted.append((event_type, kw))

    monkeypatch.setattr("app.main._replay_emit", fake_emit, raising=False)
    monkeypatch.setattr(S, "search_skill_points", fake_points)
    monkeypatch.setattr(S, "_trial_fallback", no_trial)

    vector = MagicMock()
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    await S.skills_section(
        vector, settings, goal="rotate", project=None,
        session_id="sess", agent_id="claude",
        workspace_id="workspace-a", member_id="member-alice",
    )

    assert emitted, "the receipt must still fire"
    assert emitted[0][1]["workspace_id"] == "workspace-a"
    assert emitted[0][1]["member_id"] == "member-alice"
