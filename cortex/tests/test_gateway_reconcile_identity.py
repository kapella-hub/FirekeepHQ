"""Guards for three pre-existing agent-gateway defects (plan Task 1).

Each test must go RED if its fix is reverted — that is the only proof these
assert the behaviour rather than the shape.
"""
import pytest

from app.agent_gateway.models import (
    Action, ActionAfterRequest, ActionBeforeRequest, Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter


class _Decision:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _WarnDecision(_Decision):
    action = "warn"
    reasons = ["file_risk: hot file"]


class _Engine:
    def __init__(self, decision):
        self._d = decision

    async def evaluate(self, ctx):
        return self._d


def _service(fakeredis_client, engine, emitted, recorded):
    async def _emit(**kwargs):
        emitted.append(kwargs)

    async def _no(*a, **k):
        return False

    svc = AgentGatewayService(
        policy_engine=engine,
        recent_failure_check=_no,
        fastpath_check=_no,
        session_touched_check=_no,
        replay_emitter=_emit,
        rethink_counter=RethinkCounter(fakeredis_client),
        prediction_redis=fakeredis_client,
        fastpath_redis=fakeredis_client,
        policy_decision_redis=fakeredis_client,
    )
    return svc


@pytest.mark.asyncio
async def test_reconcile_carries_the_session_even_without_a_prediction(monkeypatch):
    """P1a: the shell hook sends no prediction; the reconcile must still be
    filed under the real session, not the empty string."""
    import fakeredis.aioredis as fr

    r = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(r, _Engine(_Decision()), emitted, [])

    before = await svc.decide(ActionBeforeRequest(
        session_id="sess-real", agent_id="ag-1", adapter="shell-hook",
        action=Action(type="edit_file", target="requirements.txt"),
    ))
    await svc.record(ActionAfterRequest(
        action_id=before.action_id,
        outcome=Outcome(success=True, actual_changes=["requirements.txt"]),
    ))

    reconcile = [e for e in emitted if e["event_type"] == "agent.action.reconcile"]
    assert len(reconcile) == 1
    assert reconcile[0]["session_id"] == "sess-real"
    assert reconcile[0]["agent_id"] == "ag-1"


@pytest.mark.asyncio
async def test_reconcile_emits_a_real_outcome():
    """P1b: without outcome=, _failure_rate never sees this event and every
    session evaluates as a success."""
    import fakeredis.aioredis as fr

    r = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(r, _Engine(_Decision()), emitted, [])

    before = await svc.decide(ActionBeforeRequest(
        session_id="s", agent_id="a", adapter="shell-hook",
        action=Action(type="edit_file", target="x.py"),
    ))
    await svc.record(ActionAfterRequest(
        action_id=before.action_id, outcome=Outcome(success=False),
    ))

    reconcile = [e for e in emitted if e["event_type"] == "agent.action.reconcile"]
    assert reconcile[0]["outcome"] == "failure"


@pytest.mark.asyncio
async def test_warn_decisions_are_recorded():
    """P2: warn is remapped to allow before the audit gate, so no warn has ever
    reached policy:decisions."""
    import fakeredis.aioredis as fr

    # The plan named this reader `list_policy_decisions`; the real function in
    # app/policy/store.py is `get_policy_decisions`. Using the real one.
    from app.policy.store import get_policy_decisions

    r = fr.FakeRedis(decode_responses=True)
    svc = _service(r, _Engine(_WarnDecision()), [], [])

    resp = await svc.decide(ActionBeforeRequest(
        session_id="s", agent_id="a", adapter="shell-hook",
        action=Action(type="edit_file", target="hot.py"),
    ))
    assert resp.decision == "allow"  # the wire contract is unchanged

    rows = await get_policy_decisions(r, limit=10)
    assert any(d.get("action") == "warn" for d in rows), rows
