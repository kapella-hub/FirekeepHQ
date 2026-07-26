import pytest
from unittest.mock import AsyncMock, MagicMock

from app.agent_gateway.models import ActionBeforeRequest
from app.agent_gateway.service import AgentGatewayService


@pytest.fixture
def stub_policy_engine():
    engine = MagicMock()
    decision = MagicMock(action="allow", reasons=[], risk_score=0.0)
    engine.evaluate = AsyncMock(return_value=decision)
    return engine


@pytest.fixture
def stub_recent_failure_check():
    return AsyncMock(return_value=False)


@pytest.fixture
def stub_fastpath_check():
    return AsyncMock(return_value=False)


@pytest.fixture
def stub_session_touched_check():
    return AsyncMock(return_value=False)


@pytest.fixture
def stub_replay_emitter():
    return AsyncMock()


@pytest.fixture
def stub_rethink_counter():
    counter = MagicMock()
    counter.increment = AsyncMock(return_value=1)
    counter.reset = AsyncMock()
    return counter


@pytest.fixture
def stub_prediction_redis():
    """In-memory fake Redis for the prediction store."""
    redis = AsyncMock()
    redis._data = {}

    async def fake_get(k):
        return redis._data.get(k)

    async def fake_set(k, v, ex=None):
        redis._data[k] = v
        return True

    async def fake_delete(k):
        existed = k in redis._data
        redis._data.pop(k, None)
        return 1 if existed else 0

    redis.get = fake_get
    redis.set = fake_set
    redis.delete = fake_delete
    return redis


@pytest.fixture
def service(stub_policy_engine, stub_recent_failure_check, stub_fastpath_check,
            stub_session_touched_check, stub_replay_emitter, stub_rethink_counter,
            stub_prediction_redis):
    return AgentGatewayService(
        policy_engine=stub_policy_engine,
        recent_failure_check=stub_recent_failure_check,
        fastpath_check=stub_fastpath_check,
        session_touched_check=stub_session_touched_check,
        replay_emitter=stub_replay_emitter,
        rethink_counter=stub_rethink_counter,
        prediction_redis=stub_prediction_redis,
    )


@pytest.mark.asyncio
async def test_low_risk_mcp_action_returns_allow(service):
    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    resp = await service.decide(req)
    assert resp.decision == "allow"
    assert resp.action_id.startswith("act_")


@pytest.mark.asyncio
async def test_shell_hook_missing_prediction_does_not_rethink(service, stub_policy_engine):
    """Predict-incapable shell-hook adapter never gets rethink from prediction_required."""
    rethink = MagicMock(action="rethink", reasons=["prediction_required"], risk_score=0.2)
    stub_policy_engine.evaluate = AsyncMock(return_value=rethink)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="shell-hook",
        action={"type": "delete", "target": "/tmp/x"},
    )
    resp = await service.decide(req)
    assert resp.decision == "allow"
    # But advisory is recorded
    codes = [a.code for a in resp.advisories]
    assert "prediction_required" in codes


@pytest.mark.asyncio
async def test_mcp_missing_prediction_on_full_tier_rethinks(service, stub_policy_engine):
    """Predict-capable MCP adapter rethinks when prediction missing on elevated tier."""
    rethink = MagicMock(action="rethink", reasons=["prediction_required"], risk_score=0.2)
    stub_policy_engine.evaluate = AsyncMock(return_value=rethink)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "delete", "target": "/tmp/x"},
    )
    resp = await service.decide(req)
    assert resp.decision == "rethink"


@pytest.mark.asyncio
async def test_block_decision_passes_through(service, stub_policy_engine):
    block = MagicMock(action="block", reasons=["File matches deny pattern '*.env'"], risk_score=1.0)
    stub_policy_engine.evaluate = AsyncMock(return_value=block)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "/tmp/.env"},
    )
    resp = await service.decide(req)
    assert resp.decision == "block"


@pytest.mark.asyncio
async def test_auto_reconcile_defaults_by_adapter(service):
    req_shell = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="shell-hook",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    req_mcp = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    resp_shell = await service.decide(req_shell)
    resp_mcp = await service.decide(req_mcp)
    assert resp_shell.auto_reconcile is True
    assert resp_mcp.auto_reconcile is False


@pytest.mark.asyncio
async def test_rethink_increments_counter(service, stub_policy_engine, stub_rethink_counter):
    rethink = MagicMock(action="rethink", reasons=["low_confidence"], risk_score=0.3)
    stub_policy_engine.evaluate = AsyncMock(return_value=rethink)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
        prediction={"intent": "x", "confidence": 0.4},
    )
    await service.decide(req)
    stub_rethink_counter.increment.assert_called_once()


@pytest.mark.asyncio
async def test_rethink_limit_escalates_to_block(service, stub_policy_engine, stub_rethink_counter):
    rethink = MagicMock(action="rethink", reasons=["low_confidence"], risk_score=0.3)
    stub_policy_engine.evaluate = AsyncMock(return_value=rethink)
    stub_rethink_counter.increment = AsyncMock(return_value=3)  # at limit

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
        prediction={"intent": "x", "confidence": 0.4},
    )
    resp = await service.decide(req)
    assert resp.decision == "block"
    codes = [a.code for a in resp.advisories]
    assert "rethink_limit" in codes


@pytest.mark.asyncio
async def test_allow_resets_rethink_counter(service, stub_rethink_counter):
    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    await service.decide(req)
    stub_rethink_counter.reset.assert_called_once()


@pytest.mark.asyncio
async def test_session_health_reason_routed_to_correct_advisory(service, stub_policy_engine):
    """SessionHealthRule reasons contain 'failure rate' but should route to session_health, not recent_failure."""
    warn = MagicMock(
        action="warn",
        reasons=["Session has high failure rate (50%). Consider reviewing before more edits."],
        risk_score=0.3,
    )
    stub_policy_engine.evaluate = AsyncMock(return_value=warn)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    resp = await service.decide(req)
    codes = [a.code for a in resp.advisories]
    assert "session_health" in codes
    assert "recent_failure" not in codes


@pytest.mark.asyncio
async def test_recent_failure_reason_still_routes_correctly(service, stub_policy_engine):
    """RecentFailureRule reasons should still route to recent_failure code."""
    warn = MagicMock(
        action="warn",
        reasons=["Recent sessions editing this file have a high failure rate (3/5)."],
        risk_score=0.3,
    )
    stub_policy_engine.evaluate = AsyncMock(return_value=warn)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    resp = await service.decide(req)
    codes = [a.code for a in resp.advisories]
    assert "recent_failure" in codes
    assert "session_health" not in codes


@pytest.mark.asyncio
async def test_record_outcome_emits_reconcile_event(service, stub_replay_emitter, stub_prediction_redis):
    import json
    from app.agent_gateway.models import ActionAfterRequest, Outcome

    # Simulate that decide() was called previously and stored the prediction in Redis
    await stub_prediction_redis.set("ag:predict:act_test", json.dumps({
        "agent_id": "a1",
        "session_id": "s1",
        "prediction": {"intent": "x", "expected_changes": ["src/foo.py"],
                       "success_criteria": ["TESTS_PASS"], "confidence": 0.9},
        "adapter": "mcp",
        "action_type": "edit_file",
        "target": "src/foo.py",
    }))

    req = ActionAfterRequest(
        action_id="act_test",
        outcome=Outcome(success=True, actual_changes=["src/foo.py"], observed_criteria_met=["TESTS_PASS"]),
    )
    resp = await service.record(req)
    assert resp.recorded is True
    assert resp.prediction_match_score == 1.0
    # Verify reconcile event emitted
    calls = stub_replay_emitter.call_args_list
    assert any(c.kwargs.get("event_type") == "agent.action.reconcile" for c in calls)


@pytest.mark.asyncio
async def test_record_outcome_without_prior_prediction_still_recorded(service):
    from app.agent_gateway.models import ActionAfterRequest, Outcome

    req = ActionAfterRequest(
        action_id="act_unknown",
        outcome=Outcome(success=True),
    )
    resp = await service.record(req)
    assert resp.recorded is True
    assert resp.prediction_match_score is None


@pytest.mark.asyncio
async def test_record_calls_fastpath_update_when_redis_wired(
    stub_policy_engine, stub_recent_failure_check, stub_fastpath_check,
    stub_session_touched_check, stub_replay_emitter, stub_rethink_counter,
    stub_prediction_redis,
):
    """fastpath_redis.eval is called when fastpath_redis is wired and entry exists."""
    import json
    from unittest.mock import AsyncMock as AM
    from app.agent_gateway.models import ActionAfterRequest, Outcome

    # Separate mock for fastpath Redis (tracks .eval calls for atomic fastpath updates)
    fastpath_redis = AM()
    fastpath_redis.eval = AM()

    svc = AgentGatewayService(
        policy_engine=stub_policy_engine,
        recent_failure_check=stub_recent_failure_check,
        fastpath_check=stub_fastpath_check,
        session_touched_check=stub_session_touched_check,
        replay_emitter=stub_replay_emitter,
        rethink_counter=stub_rethink_counter,
        prediction_redis=stub_prediction_redis,
        fastpath_redis=fastpath_redis,
    )
    # Prime the prediction store in Redis
    await stub_prediction_redis.set("ag:predict:act_fp", json.dumps({
        "agent_id": "a1",
        "session_id": "s1",
        "prediction": {"intent": "x", "expected_changes": [], "success_criteria": [], "confidence": 0.9},
        "adapter": "mcp",
        "action_type": "edit_file",
        "target": "src/foo.py",
    }))

    req = ActionAfterRequest(
        action_id="act_fp",
        outcome=Outcome(success=True),
    )
    await svc.record(req)
    fastpath_redis.eval.assert_called_once()


@pytest.mark.asyncio
async def test_record_skips_fastpath_update_when_entry_missing(
    stub_policy_engine, stub_recent_failure_check, stub_fastpath_check,
    stub_session_touched_check, stub_replay_emitter, stub_rethink_counter,
    stub_prediction_redis,
):
    """fastpath_redis is NOT called when there is no matching prediction entry."""
    from unittest.mock import AsyncMock as AM
    from app.agent_gateway.models import ActionAfterRequest, Outcome

    fastpath_redis = AM()
    fastpath_redis.get = AM(return_value=None)
    fastpath_redis.set = AM()

    svc = AgentGatewayService(
        policy_engine=stub_policy_engine,
        recent_failure_check=stub_recent_failure_check,
        fastpath_check=stub_fastpath_check,
        session_touched_check=stub_session_touched_check,
        replay_emitter=stub_replay_emitter,
        rethink_counter=stub_rethink_counter,
        prediction_redis=stub_prediction_redis,  # empty store — no entry for act_not_in_store
        fastpath_redis=fastpath_redis,
    )

    req = ActionAfterRequest(
        action_id="act_not_in_store",
        outcome=Outcome(success=True),
    )
    await svc.record(req)
    fastpath_redis.set.assert_not_called()


class _FakeDecisionRedis:
    """Minimal Redis stand-in capturing record_policy_decision writes."""

    def __init__(self):
        self.items = []

    async def lpush(self, key, value):
        self.items.insert(0, value)
        return len(self.items)

    async def ltrim(self, key, start, end):
        self.items = self.items[start:end + 1]
        return True


def _service_with_decision_redis(stub_policy_engine, redis):
    return AgentGatewayService(
        policy_engine=stub_policy_engine,
        recent_failure_check=AsyncMock(return_value=False),
        fastpath_check=AsyncMock(return_value=False),
        session_touched_check=AsyncMock(return_value=False),
        replay_emitter=AsyncMock(),
        rethink_counter=_mk_counter(),
        policy_decision_redis=redis,
    )


def _mk_counter():
    counter = MagicMock()
    counter.increment = AsyncMock(return_value=1)
    counter.reset = AsyncMock()
    return counter


@pytest.mark.asyncio
async def test_block_decision_is_audit_recorded(stub_policy_engine):
    import json
    redis = _FakeDecisionRedis()
    block = MagicMock(action="block", reasons=["path deny pattern '.env'"], risk_score=1.0, signals={})
    stub_policy_engine.evaluate = AsyncMock(return_value=block)
    service = _service_with_decision_redis(stub_policy_engine, redis)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "secret.env"},
    )
    resp = await service.decide(req)
    assert resp.decision == "block"
    assert len(redis.items) == 1
    recorded = json.loads(redis.items[0])
    assert recorded["action"] == "block"
    assert recorded["file_path"] == "secret.env"
    assert recorded["agent_id"] == "a1"


@pytest.mark.asyncio
async def test_allow_decision_is_not_audit_recorded(stub_policy_engine):
    redis = _FakeDecisionRedis()
    allow = MagicMock(action="allow", reasons=[], risk_score=0.0, signals={})
    stub_policy_engine.evaluate = AsyncMock(return_value=allow)
    service = _service_with_decision_redis(stub_policy_engine, redis)

    req = ActionBeforeRequest(
        session_id="s1", agent_id="a1", adapter="mcp",
        action={"type": "edit_file", "target": "src/foo.py"},
    )
    resp = await service.decide(req)
    assert resp.decision == "allow"
    assert redis.items == []  # allows are intentionally not recorded
