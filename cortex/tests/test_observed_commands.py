"""Observed commands: POST /agent/action/observed, the command metrics built
from it, and the outcome-based Brier score (2026-11-09)."""

from unittest.mock import AsyncMock

import pytest

from app.agent_gateway.models import (
    OBSERVED_EVENT_TYPE,
    OBSERVED_TARGET_MAX,
    Action,
    ActionObservedRequest,
    Outcome,
)
from app.agent_gateway.service import AgentGatewayService
from app.evals.scorers import (
    brier_score,
    compute_tier1_metrics,
    outcome_brier_score,
)


def _observed(outcome, **extra):
    return {"event_type": OBSERVED_EVENT_TYPE, "outcome": outcome, **extra}


def _service(emitter):
    service = AgentGatewayService.__new__(AgentGatewayService)
    service.replay_emitter = emitter
    return service


def _request(session_id="s-1", success=True, target="pytest", exit_status=0):
    req = ActionObservedRequest(
        session_id=session_id, agent_id="agent-a", adapter="shell-hook",
        action=Action(type="run_command", target=target),
        outcome=Outcome(success=success), exit_status=exit_status,
    )
    req._verified_workspace = "ws-1"
    req._verified_member = "member-1"
    return req


class TestObserve:
    @pytest.mark.asyncio
    async def test_emits_one_outcome_bearing_event(self):
        emit = AsyncMock()
        resp = await _service(emit).observe(_request(success=False, exit_status=1))

        assert resp.recorded is True
        kwargs = emit.await_args.kwargs
        assert kwargs["event_type"] == OBSERVED_EVENT_TYPE
        assert kwargs["session_id"] == "s-1"
        assert kwargs["outcome"] == "failure"
        assert kwargs["workspace_id"] == "ws-1"
        assert kwargs["member_id"] == "member-1"
        assert kwargs["payload"] == {
            "action": {"type": "run_command", "target": "pytest"},
            "outcome": {"success": False},
            "exit_status": 1,
            "adapter": "shell-hook",
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_id", ["", "unknown", "  "])
    async def test_unattributable_session_is_not_emitted(self, session_id):
        emit = AsyncMock()
        resp = await _service(emit).observe(_request(session_id=session_id))
        assert resp.recorded is False
        emit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_target_is_bounded_server_side(self):
        emit = AsyncMock()
        await _service(emit).observe(_request(target="x" * 5000))
        stored = emit.await_args.kwargs["payload"]["action"]["target"]
        assert len(stored) == OBSERVED_TARGET_MAX

    @pytest.mark.asyncio
    async def test_emit_failure_never_raises(self):
        emit = AsyncMock(side_effect=RuntimeError("redis down"))
        resp = await _service(emit).observe(_request())
        assert resp.recorded is False


class TestCommandMetrics:
    def test_absent_when_no_commands_were_observed(self):
        metrics = compute_tier1_metrics([{"event_type": "memory_read"}])
        assert metrics["command_count"] == 0.0
        assert "command_failure_rate" not in metrics
        assert "final_command_failed" not in metrics

    def test_rate_and_final_state(self):
        events = [_observed("failure"), _observed("failure"),
                  _observed("success"), _observed("success")]
        metrics = compute_tier1_metrics(events)
        assert metrics["command_count"] == 4.0
        assert metrics["command_failure_rate"] == 0.5
        assert metrics["final_command_failed"] == 0.0

    def test_session_left_red(self):
        metrics = compute_tier1_metrics([_observed("success"), _observed("failure")])
        assert metrics["final_command_failed"] == 1.0

    def test_commands_do_not_enter_the_legacy_outcome_population(self):
        """tool_success_rate / failure_rate / outcome_event_count feed frozen
        consumers (compliance.py optimism-skew): one failing command must not
        read there as a contradiction of a self-reported success."""
        events = [
            {"event_type": "session_end", "outcome": "success"},
            {"event_type": "memory_write", "outcome": "success"},
            _observed("failure"), _observed("failure"), _observed("success"),
        ]
        metrics = compute_tier1_metrics(events)
        assert metrics["outcome_event_count"] == 2.0
        assert metrics["tool_success_rate"] == 1.0
        assert metrics["failure_rate"] == 0.0
        assert metrics["command_count"] == 3.0

    def test_only_commands_leaves_legacy_rates_unmeasured(self):
        metrics = compute_tier1_metrics([_observed("failure")])
        assert "tool_success_rate" not in metrics
        assert "failure_rate" not in metrics
        assert metrics["outcome_event_count"] == 0.0


class TestOutcomeBrier:
    def test_confident_and_right_scores_near_zero(self):
        actions = [{"prediction_confidence": 0.9, "success": True}] * 4
        assert outcome_brier_score(actions) == pytest.approx(0.01)

    def test_confident_and_wrong_scores_high(self):
        actions = [{"prediction_confidence": 0.9, "success": False}]
        assert outcome_brier_score(actions) == pytest.approx(0.81)

    def test_missing_fields_are_excluded(self):
        actions = [
            {"prediction_confidence": None, "success": True},
            {"prediction_confidence": 0.8, "success": None},
            {"prediction_confidence": 0.8},
            {"prediction_confidence": 0.8, "success": "yes"},
        ]
        assert outcome_brier_score(actions) is None

    def test_the_two_scores_disagree_on_a_correct_confident_prediction(self):
        """The reason this metric exists: an action that succeeded as
        predicted, whose free-text criteria were never echoed back, has a
        match score of 0.5 — so the frozen score punishes good calibration."""
        action = {"prediction_confidence": 0.9, "prediction_match_score": 0.5,
                  "success": True}
        assert brier_score([action]) == pytest.approx(0.16)
        assert outcome_brier_score([action]) == pytest.approx(0.01)
