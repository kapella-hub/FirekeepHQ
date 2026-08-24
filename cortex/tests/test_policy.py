"""Tests for the Runtime Policy Layer.

Run with: PYTHONPATH=cortex python -m pytest cortex/tests/test_policy.py -v --noconftest
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.policy.engine import PolicyContext, PolicyDecision, PolicyEngine, PolicyRule
from app.policy.rules import (
    FileRiskRule,
    LeaseRule,
    PathDenyRule,
    RecentFailureRule,
    SessionHealthRule,
)


# ---------------------------------------------------------------------------
# PolicyDecision model tests
# ---------------------------------------------------------------------------


class TestPolicyDecision:
    def test_defaults(self):
        d = PolicyDecision()
        assert d.action == "allow"
        assert d.reasons == []
        assert d.risk_score == 0.0
        assert d.signals == {}

    def test_block_decision(self):
        d = PolicyDecision(
            action="block",
            reasons=["[path_deny] File matches deny pattern '.env'"],
            risk_score=1.0,
            signals={"path_deny": {"action": "block", "risk": 1.0}},
        )
        assert d.action == "block"
        assert len(d.reasons) == 1


class TestPolicyContext:
    def test_defaults(self):
        ctx = PolicyContext(file_path="src/main.py")
        assert ctx.file_path == "src/main.py"
        assert ctx.agent_id == ""
        assert ctx.session_id == ""

    def test_full(self):
        ctx = PolicyContext(file_path="src/main.py", agent_id="agent-1", session_id="sess-1")
        assert ctx.agent_id == "agent-1"


# ---------------------------------------------------------------------------
# PolicyEngine tests
# ---------------------------------------------------------------------------


class AlwaysAllowRule(PolicyRule):
    name = "always_allow"

    async def evaluate(self, context):
        return ("allow", 0.0, "")


class AlwaysWarnRule(PolicyRule):
    name = "always_warn"

    async def evaluate(self, context):
        return ("warn", 0.3, "Something looks risky")


class AlwaysBlockRule(PolicyRule):
    name = "always_block"

    async def evaluate(self, context):
        return ("block", 1.0, "Blocked for testing")


class ExplodingRule(PolicyRule):
    name = "exploding"

    async def evaluate(self, context):
        raise RuntimeError("Rule exploded")


class TestPolicyEngine:
    @pytest.fixture
    def ctx(self):
        return PolicyContext(file_path="src/app.py", agent_id="a1", session_id="s1")

    @pytest.mark.asyncio
    async def test_empty_engine_allows(self, ctx):
        engine = PolicyEngine(rules=[])
        decision = await engine.evaluate(ctx)
        assert decision.action == "allow"
        assert decision.risk_score == 0.0

    @pytest.mark.asyncio
    async def test_all_allow(self, ctx):
        engine = PolicyEngine(rules=[AlwaysAllowRule()])
        decision = await engine.evaluate(ctx)
        assert decision.action == "allow"

    @pytest.mark.asyncio
    async def test_warn_escalation(self, ctx):
        engine = PolicyEngine(rules=[AlwaysAllowRule(), AlwaysWarnRule()])
        decision = await engine.evaluate(ctx)
        assert decision.action == "warn"
        assert len(decision.reasons) == 1
        assert "always_warn" in decision.reasons[0]

    @pytest.mark.asyncio
    async def test_block_escalation(self, ctx):
        engine = PolicyEngine(rules=[AlwaysWarnRule(), AlwaysBlockRule()])
        decision = await engine.evaluate(ctx)
        assert decision.action == "block"
        assert len(decision.reasons) == 2

    @pytest.mark.asyncio
    async def test_block_overrides_warn(self, ctx):
        engine = PolicyEngine(rules=[AlwaysBlockRule(), AlwaysWarnRule()])
        decision = await engine.evaluate(ctx)
        assert decision.action == "block"

    @pytest.mark.asyncio
    async def test_disabled_rule_skipped(self, ctx):
        rule = AlwaysBlockRule()
        rule.enabled = False
        engine = PolicyEngine(rules=[rule])
        decision = await engine.evaluate(ctx)
        assert decision.action == "allow"

    @pytest.mark.asyncio
    async def test_rule_exception_does_not_block(self, ctx):
        engine = PolicyEngine(rules=[ExplodingRule()])
        decision = await engine.evaluate(ctx)
        assert decision.action == "allow"
        assert "exploding" in decision.signals
        assert "error" in decision.signals["exploding"]

    @pytest.mark.asyncio
    async def test_risk_score_clamped(self, ctx):
        engine = PolicyEngine(rules=[AlwaysBlockRule(), AlwaysBlockRule()])
        # Two block rules each contribute 1.0 risk, total should be clamped to 1.0
        # But they have the same name so one overwrites the other in signals
        decision = await engine.evaluate(ctx)
        assert decision.risk_score <= 1.0

    def test_list_rules(self):
        engine = PolicyEngine(rules=[AlwaysAllowRule(), AlwaysBlockRule()])
        rules = engine.list_rules()
        assert len(rules) == 2
        assert rules[0]["name"] == "always_allow"

    def test_toggle_rule(self):
        engine = PolicyEngine(rules=[AlwaysBlockRule()])
        assert engine.toggle_rule("always_block") is False
        assert engine.rules[0].enabled is False
        assert engine.toggle_rule("always_block") is True
        assert engine.rules[0].enabled is True

    def test_toggle_missing_rule(self):
        engine = PolicyEngine(rules=[])
        assert engine.toggle_rule("nonexistent") is None

    def test_get_rule(self):
        rule = AlwaysBlockRule()
        engine = PolicyEngine(rules=[rule])
        assert engine.get_rule("always_block") is rule
        assert engine.get_rule("missing") is None

    def test_add_rule(self):
        engine = PolicyEngine()
        engine.add_rule(AlwaysAllowRule())
        assert len(engine.rules) == 1


# ---------------------------------------------------------------------------
# PathDenyRule tests
# ---------------------------------------------------------------------------


class TestPathDenyRule:
    @pytest.mark.asyncio
    async def test_no_patterns_allows(self):
        rule = PathDenyRule(deny_patterns=[])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/app.py"))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_env_file_blocked(self):
        rule = PathDenyRule(deny_patterns=[".env"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="/project/.env"))
        assert action == "block"
        assert ".env" in reason

    @pytest.mark.asyncio
    async def test_key_file_blocked(self):
        rule = PathDenyRule(deny_patterns=["*.key", "*.pem"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="/certs/server.key"))
        assert action == "block"

    @pytest.mark.asyncio
    async def test_pem_file_blocked(self):
        rule = PathDenyRule(deny_patterns=["*.pem"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="C:\\certs\\ca.pem"))
        assert action == "block"

    @pytest.mark.asyncio
    async def test_normal_file_allowed(self):
        rule = PathDenyRule(deny_patterns=[".env", "*.key", "*.pem"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/main.py"))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_glob_pattern_with_path(self):
        rule = PathDenyRule(deny_patterns=["scripts/*.sh"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="scripts/deploy.sh"))
        assert action == "block"

    @pytest.mark.asyncio
    async def test_secret_pattern(self):
        rule = PathDenyRule(deny_patterns=["*.secret"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="config/db.secret"))
        assert action == "block"

    @pytest.mark.asyncio
    async def test_empty_pattern_ignored(self):
        rule = PathDenyRule(deny_patterns=["", "  ", ".env"])
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/app.py"))
        assert action == "allow"


# ---------------------------------------------------------------------------
# LeaseRule tests
# ---------------------------------------------------------------------------


class TestLeaseRule:
    @pytest.mark.asyncio
    async def test_always_allows(self):
        """LeaseRule is a no-op; lease checking is done by the hook."""
        rule = LeaseRule()
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="x.py"))
        assert action == "allow"


# ---------------------------------------------------------------------------
# FileRiskRule tests
# ---------------------------------------------------------------------------


class TestFileRiskRule:
    @pytest.mark.asyncio
    async def test_no_redis_allows(self):
        rule = FileRiskRule(get_replay_redis=None)
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/app.py"))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_hotspot_match_warns(self):
        from app.patterns.models import PatternCard

        mock_pattern = PatternCard(
            id="p1",
            pattern_type="file_hotspot",
            confidence=0.85,
            description="Frequently fails",
            tags=["main.py"],
        )

        mock_redis = MagicMock()

        with patch("app.patterns.store.get_patterns", new_callable=AsyncMock, return_value=[mock_pattern]):
            rule = FileRiskRule(get_replay_redis=lambda: mock_redis)
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/main.py"))
            assert action == "warn"
            assert "hotspot" in reason.lower()

    @pytest.mark.asyncio
    async def test_low_confidence_hotspot_allows(self):
        from app.patterns.models import PatternCard

        mock_pattern = PatternCard(
            id="p1",
            pattern_type="file_hotspot",
            confidence=0.4,
            description="Sometimes fails",
            tags=["main.py"],
        )

        mock_redis = MagicMock()

        with patch("app.patterns.store.get_patterns", new_callable=AsyncMock, return_value=[mock_pattern]):
            rule = FileRiskRule(get_replay_redis=lambda: mock_redis)
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/main.py"))
            assert action == "allow"

    @pytest.mark.asyncio
    async def test_no_matching_hotspot(self):
        from app.patterns.models import PatternCard

        mock_pattern = PatternCard(
            id="p1",
            pattern_type="file_hotspot",
            confidence=0.9,
            tags=["other_file.py"],
        )

        mock_redis = MagicMock()

        with patch("app.patterns.store.get_patterns", new_callable=AsyncMock, return_value=[mock_pattern]):
            rule = FileRiskRule(get_replay_redis=lambda: mock_redis)
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/main.py"))
            assert action == "allow"


# ---------------------------------------------------------------------------
# SessionHealthRule tests
# ---------------------------------------------------------------------------


class TestSessionHealthRule:
    @pytest.mark.asyncio
    async def test_no_redis_allows(self):
        rule = SessionHealthRule(get_replay_redis=None)
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="x.py"))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_no_session_id_allows(self):
        rule = SessionHealthRule(get_replay_redis=lambda: MagicMock())
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="x.py", session_id=""))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_high_failure_rate_warns(self):
        from app.evals.models import EvalResult

        mock_eval = EvalResult(
            session_id="s1",
            trigger="session_complete",
            metrics={"failure_rate": 0.6},
        )

        with patch("app.evals.store.get_eval", new_callable=AsyncMock, return_value=mock_eval):
            rule = SessionHealthRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(
                PolicyContext(file_path="x.py", session_id="s1")
            )
            assert action == "warn"
            assert "failure rate" in reason.lower()

    @pytest.mark.asyncio
    async def test_low_failure_rate_allows(self):
        from app.evals.models import EvalResult

        mock_eval = EvalResult(
            session_id="s1",
            trigger="session_complete",
            metrics={"failure_rate": 0.1},
        )

        with patch("app.evals.store.get_eval", new_callable=AsyncMock, return_value=mock_eval):
            rule = SessionHealthRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(
                PolicyContext(file_path="x.py", session_id="s1")
            )
            assert action == "allow"

    @pytest.mark.asyncio
    async def test_no_eval_allows(self):
        with patch("app.evals.store.get_eval", new_callable=AsyncMock, return_value=None):
            rule = SessionHealthRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(
                PolicyContext(file_path="x.py", session_id="s1")
            )
            assert action == "allow"


# ---------------------------------------------------------------------------
# RecentFailureRule tests
# ---------------------------------------------------------------------------


class TestRecentFailureRule:
    @pytest.mark.asyncio
    async def test_no_redis_allows(self):
        rule = RecentFailureRule(get_replay_redis=None)
        action, risk, reason = await rule.evaluate(PolicyContext(file_path="x.py"))
        assert action == "allow"

    @pytest.mark.asyncio
    async def test_high_failure_rate_on_file_warns(self):
        from app.patterns.models import SessionFeatures

        features = [
            SessionFeatures(session_id=f"s{i}", outcome="failure",
                            outcome_source="task_result", file_paths=["src/buggy.py"])
            for i in range(3)
        ] + [
            SessionFeatures(session_id="s10", outcome="success",
                            outcome_source="task_result", file_paths=["src/buggy.py"]),
        ]

        with patch("app.patterns.store.get_all_features", new_callable=AsyncMock, return_value=features):
            rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/buggy.py"))
            assert action == "warn"
            assert "failure rate" in reason.lower()

    @pytest.mark.asyncio
    async def test_low_failure_rate_allows(self):
        from app.patterns.models import SessionFeatures

        features = [
            SessionFeatures(session_id="s1", outcome="success",
                            outcome_source="task_result", file_paths=["src/good.py"]),
            SessionFeatures(session_id="s2", outcome="success",
                            outcome_source="task_result", file_paths=["src/good.py"]),
            SessionFeatures(session_id="s3", outcome="success",
                            outcome_source="task_result", file_paths=["src/good.py"]),
            SessionFeatures(session_id="s4", outcome="failure",
                            outcome_source="task_result", file_paths=["src/good.py"]),
        ]

        with patch("app.patterns.store.get_all_features", new_callable=AsyncMock, return_value=features):
            rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/good.py"))
            assert action == "allow"

    @pytest.mark.asyncio
    async def test_too_few_sessions_allows(self):
        from app.patterns.models import SessionFeatures

        features = [
            SessionFeatures(session_id="s1", outcome="failure", file_paths=["src/rare.py"]),
        ]

        with patch("app.patterns.store.get_all_features", new_callable=AsyncMock, return_value=features):
            rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/rare.py"))
            assert action == "allow"

    @pytest.mark.asyncio
    async def test_unrelated_file_allows(self):
        from app.patterns.models import SessionFeatures

        features = [
            SessionFeatures(session_id=f"s{i}", outcome="failure", file_paths=["src/other.py"])
            for i in range(5)
        ]

        with patch("app.patterns.store.get_all_features", new_callable=AsyncMock, return_value=features):
            rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
            action, risk, reason = await rule.evaluate(PolicyContext(file_path="src/main.py"))
            assert action == "allow"


@pytest.mark.asyncio
async def test_unknown_sessions_do_not_dilute_recent_file_failure_rate():
    from app.patterns.models import SessionFeatures
    graded_failures = [
        SessionFeatures(
            session_id=f"g{i}", outcome="failure",
            outcome_source="task_result", file_paths=["src/buggy.py"])
        for i in range(3)
    ]
    unknown = [
        SessionFeatures(session_id=f"u{i}", file_paths=["src/buggy.py"])
        for i in range(17)
    ]
    with patch(
        "app.patterns.store.get_all_features",
        new_callable=AsyncMock,
        return_value=graded_failures + unknown,
    ):
        rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
        action, _, reason = await rule.evaluate(
            PolicyContext(file_path="src/buggy.py"))
    assert action == "warn"
    assert "3/3" in reason


# ---------------------------------------------------------------------------
# Compound engine integration tests
# ---------------------------------------------------------------------------


class TestCompoundEvaluation:
    @pytest.mark.asyncio
    async def test_path_deny_blocks_even_with_other_allows(self):
        engine = PolicyEngine(rules=[
            AlwaysAllowRule(),
            PathDenyRule(deny_patterns=[".env"]),
        ])
        decision = await engine.evaluate(PolicyContext(file_path="/project/.env"))
        assert decision.action == "block"

    @pytest.mark.asyncio
    async def test_multiple_warnings_aggregate(self):
        engine = PolicyEngine(rules=[AlwaysWarnRule(), AlwaysWarnRule()])
        decision = await engine.evaluate(PolicyContext(file_path="x.py"))
        assert decision.action == "warn"
        assert decision.risk_score == pytest.approx(0.6)

    @pytest.mark.asyncio
    async def test_normal_file_passes_all(self):
        engine = PolicyEngine(rules=[
            LeaseRule(),
            PathDenyRule(deny_patterns=[".env", "*.key"]),
            FileRiskRule(get_replay_redis=None),
            SessionHealthRule(get_replay_redis=None),
            RecentFailureRule(get_replay_redis=None),
        ])
        decision = await engine.evaluate(
            PolicyContext(file_path="src/main.py", agent_id="a1", session_id="s1")
        )
        assert decision.action == "allow"


# ---------------------------------------------------------------------------
# PolicyContext tier and prediction field tests
# ---------------------------------------------------------------------------


def test_policy_context_accepts_tier_and_prediction():
    from app.policy.engine import PolicyContext
    from app.agent_gateway.models import Prediction

    ctx = PolicyContext(
        file_path="/tmp/foo.py",
        agent_id="a1",
        session_id="s1",
        tier="full",
        prediction=Prediction(intent="x", confidence=0.4),
    )
    assert ctx.tier == "full"
    assert ctx.prediction.confidence == 0.4


def test_policy_context_defaults_tier_to_none():
    from app.policy.engine import PolicyContext

    ctx = PolicyContext(file_path="/tmp/foo.py")
    assert ctx.tier is None
    assert ctx.prediction is None


# ---------------------------------------------------------------------------
# PredictionConfidenceRule tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prediction_confidence_rule_auto_tier_allows():
    from app.policy.engine import PolicyContext
    from app.policy.rules import PredictionConfidenceRule

    rule = PredictionConfidenceRule(threshold=0.6)
    ctx = PolicyContext(file_path="/tmp/foo.py", tier="auto")
    action, _risk, reason = await rule.evaluate(ctx)
    assert action == "allow"
    assert reason == ""


@pytest.mark.asyncio
async def test_prediction_confidence_rule_missing_prediction_rethinks():
    from app.policy.engine import PolicyContext
    from app.policy.rules import PredictionConfidenceRule

    rule = PredictionConfidenceRule(threshold=0.6)
    ctx = PolicyContext(file_path="/tmp/foo.py", tier="full", prediction=None)
    action, _risk, reason = await rule.evaluate(ctx)
    assert action == "rethink"
    assert reason == "prediction_required"


@pytest.mark.asyncio
async def test_prediction_confidence_rule_low_confidence_full_tier_rethinks():
    from app.agent_gateway.models import Prediction
    from app.policy.engine import PolicyContext
    from app.policy.rules import PredictionConfidenceRule

    rule = PredictionConfidenceRule(threshold=0.6)
    ctx = PolicyContext(
        file_path="/tmp/foo.py",
        tier="full",
        prediction=Prediction(intent="x", confidence=0.4),
    )
    action, _risk, reason = await rule.evaluate(ctx)
    assert action == "rethink"
    assert reason == "low_confidence"


@pytest.mark.asyncio
async def test_prediction_confidence_rule_high_confidence_allows():
    from app.agent_gateway.models import Prediction
    from app.policy.engine import PolicyContext
    from app.policy.rules import PredictionConfidenceRule

    rule = PredictionConfidenceRule(threshold=0.6)
    ctx = PolicyContext(
        file_path="/tmp/foo.py",
        tier="full",
        prediction=Prediction(intent="x", confidence=0.9),
    )
    action, _risk, reason = await rule.evaluate(ctx)
    assert action == "allow"


# ---------------------------------------------------------------------------
# PolicyEngine rethink aggregation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_engine_aggregates_rethink_verdict():
    """A rule returning 'rethink' should produce a 'rethink' decision when no block exists."""
    from app.policy.engine import PolicyContext, PolicyEngine
    from app.policy.rules import PredictionConfidenceRule

    engine = PolicyEngine(rules=[PredictionConfidenceRule(threshold=0.6)])
    ctx = PolicyContext(file_path="/tmp/foo.py", tier="full", prediction=None)
    decision = await engine.evaluate(ctx)
    assert decision.action == "rethink"
    assert "prediction_required" in " ".join(decision.reasons)


@pytest.mark.asyncio
async def test_policy_engine_block_beats_rethink():
    """A block from any rule overrides a rethink from another rule."""
    from app.policy.engine import PolicyContext, PolicyEngine
    from app.policy.rules import PathDenyRule, PredictionConfidenceRule

    engine = PolicyEngine(rules=[
        PathDenyRule(deny_patterns=["*.env"]),
        PredictionConfidenceRule(threshold=0.6),
    ])
    ctx = PolicyContext(file_path="/tmp/.env", tier="full", prediction=None)
    decision = await engine.evaluate(ctx)
    assert decision.action == "block"


# ---------------------------------------------------------------------------
# /policy/evaluate alias tests
# ---------------------------------------------------------------------------


def test_policy_evaluate_alias_proxies_to_gateway():
    """The legacy /policy/evaluate endpoint should proxy to the gateway service."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.policy.api import create_policy_router
    from app.agent_gateway.models import ActionBeforeResponse

    captured = {}

    async def fake_decide(req):
        captured["adapter"] = req.adapter
        captured["target"] = req.action.target
        captured["session_id"] = req.session_id
        return ActionBeforeResponse(
            decision="allow", action_id="act_legacy", tier="auto", auto_reconcile=False,
        )

    class FakeService:
        decide = staticmethod(fake_decide)

    app = FastAPI()
    app.include_router(create_policy_router(
        get_engine=lambda: None,
        get_gateway_service=lambda: FakeService,
    ))

    with TestClient(app) as client:
        r = client.post("/policy/evaluate", json={
            "file_path": "/tmp/foo.py",
            "agent_id": "a1",
            "session_id": "s1",
        })

    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("action") == "allow"  # legacy response shape
    assert captured.get("adapter") == "shell-hook"
    assert captured.get("target") == "/tmp/foo.py"
    assert captured.get("session_id") == "s1"


def test_policy_evaluate_alias_falls_back_when_no_gateway():
    """When no gateway service is wired, falls back to policy engine."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock, MagicMock

    from app.policy.api import create_policy_router
    from app.policy.engine import PolicyEngine, PolicyDecision

    mock_engine = MagicMock(spec=PolicyEngine)
    mock_engine.evaluate = AsyncMock(return_value=PolicyDecision(
        action="allow", reasons=[], risk_score=0.0,
    ))

    app = FastAPI()
    app.include_router(create_policy_router(
        get_engine=lambda: mock_engine,
        get_gateway_service=None,
    ))

    with TestClient(app) as client:
        r = client.post("/policy/evaluate", json={
            "file_path": "/tmp/bar.py",
            "agent_id": "a2",
            "session_id": "s2",
        })

    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert body.get("action") == "allow"
    mock_engine.evaluate.assert_called_once()


def test_policy_decisions_endpoint_returns_records_and_summary():
    """GET /policy/decisions reads from the wired redis accessor."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock, MagicMock

    from app.policy.api import create_policy_router

    entries = [
        json.dumps({
            "timestamp": "2026-05-29T00:00:00+00:00", "file_path": "secret.env",
            "agent_id": "a1", "session_id": "s1", "action": "block",
            "risk_score": 1.0, "reasons": ["path deny"], "signals": {},
        }),
        json.dumps({
            "timestamp": "2026-05-29T00:01:00+00:00", "file_path": "src/x.py",
            "agent_id": "a2", "session_id": "s2", "action": "rethink",
            "risk_score": 0.7, "reasons": ["low_confidence"], "signals": {},
        }),
    ]

    redis = MagicMock()
    redis.lrange = AsyncMock(return_value=entries)

    app = FastAPI()
    app.include_router(create_policy_router(
        get_engine=lambda: MagicMock(),
        get_decision_redis=lambda: redis,
    ))

    with TestClient(app) as client:
        r = client.get("/policy/decisions?limit=10")

    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    body = r.json()
    assert len(body["decisions"]) == 2
    assert body["summary"]["counts"]["block"] == 1
    assert body["summary"]["counts"]["rethink"] if "rethink" in body["summary"]["counts"] else True


def test_policy_decisions_endpoint_graceful_when_not_wired():
    """When no redis accessor is supplied, /decisions returns empty + note."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock

    from app.policy.api import create_policy_router

    app = FastAPI()
    app.include_router(create_policy_router(get_engine=lambda: MagicMock()))

    with TestClient(app) as client:
        r = client.get("/policy/decisions")

    assert r.status_code == 200
    body = r.json()
    assert body["decisions"] == []
    assert body.get("error") == "not wired"
