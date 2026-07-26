"""Tests for the auto-eval system — models and Tier 1 scorers."""

import pytest

from app.evals.models import EvalResult, EvalSummary
from app.evals.scorers import compute_tier1_metrics


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

def _make_events(specs: list[dict]) -> list[dict]:
    """Helper to create mock replay events from specs."""
    events = []
    for i, spec in enumerate(specs):
        events.append({
            "id": f"event-{i}",
            "event_type": spec.get("type", "ctx_update"),
            "outcome": spec.get("outcome"),
            "timestamp": f"2026-03-18T10:{i:02d}:00+00:00",
            "payload": spec.get("payload", {}),
            "context_ref": spec.get("context_ref"),
            "session_id": "test-session",
            "agent_id": "default",
        })
    return events


# ---------------------------------------------------------------------------
# Scorer tests
# ---------------------------------------------------------------------------


class TestComputeTier1Metrics:
    def test_empty_events(self):
        assert compute_tier1_metrics([]) == {}

    def test_event_count(self):
        events = _make_events([{"type": "ctx_update"}, {"type": "memory_read"}])
        metrics = compute_tier1_metrics(events)
        assert metrics["event_count"] == 2.0

    def test_tool_success_rate_all_success(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_write", "outcome": "success"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["tool_success_rate"] == 1.0

    def test_tool_success_rate_mixed(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_read", "outcome": "failure"},
            {"type": "ctx_update", "outcome": "success"},
            {"type": "claim", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["tool_success_rate"] == 0.5

    def test_tool_success_rate_no_outcomes(self):
        events = _make_events([
            {"type": "ctx_update"},
            {"type": "session_start"},
        ])
        metrics = compute_tier1_metrics(events)
        assert "tool_success_rate" not in metrics  # None excluded

    def test_memory_read_count(self):
        events = _make_events([
            {"type": "memory_read"},
            {"type": "memory_read"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_read_count"] == 2.0

    def test_memory_write_count(self):
        events = _make_events([
            {"type": "memory_write"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_write_count"] == 1.0

    def test_memory_freshness_at_recall(self):
        events = _make_events([
            {"type": "memory_read", "payload": {"top_score": 0.9}},
            {"type": "memory_read", "payload": {"top_score": 0.7}},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["memory_freshness_at_recall"] == 0.8

    def test_memory_freshness_no_reads(self):
        events = _make_events([{"type": "ctx_update"}])
        metrics = compute_tier1_metrics(events)
        assert "memory_freshness_at_recall" not in metrics

    def test_claim_contention_rate(self):
        events = _make_events([
            {"type": "claim", "outcome": "success"},
            {"type": "claim", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["claim_contention_rate"] == 0.5

    def test_failure_rate(self):
        events = _make_events([
            {"type": "memory_read", "outcome": "success"},
            {"type": "memory_read", "outcome": "success"},
            {"type": "ctx_update", "outcome": "failure"},
        ])
        metrics = compute_tier1_metrics(events)
        assert abs(metrics["failure_rate"] - 0.3333) < 0.01

    def test_session_duration(self):
        events = _make_events([
            {"type": "session_start"},
            {"type": "ctx_update"},
            {"type": "ctx_update"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["session_duration_ms"] == 120000.0  # 2 minutes

    def test_unique_event_types(self):
        events = _make_events([
            {"type": "session_start"},
            {"type": "memory_read"},
            {"type": "memory_read"},
            {"type": "claim"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["unique_event_types"] == 3.0

    def test_context_snapshot_count(self):
        events = _make_events([
            {"type": "ctx_update", "context_ref": "abc123"},
            {"type": "ctx_update"},
            {"type": "ctx_update", "context_ref": "def456"},
        ])
        metrics = compute_tier1_metrics(events)
        assert metrics["context_snapshot_count"] == 2.0


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestEvalResult:
    def test_minimal(self):
        result = EvalResult(
            session_id="test-1",
            trigger="session_complete",
        )
        assert result.event_count == 0
        assert result.metrics == {}
        assert result.has_failures is False

    def test_with_metrics(self):
        result = EvalResult(
            session_id="test-2",
            trigger="session_complete",
            metrics={"tool_success_rate": 0.85, "event_count": 42.0},
            event_count=42,
            has_failures=False,
        )
        assert result.metrics["tool_success_rate"] == 0.85

    def test_with_failures(self):
        result = EvalResult(
            session_id="test-3",
            trigger="session_abandon",
            failure_event_ids=["ev-1", "ev-2"],
            has_failures=True,
        )
        assert len(result.failure_event_ids) == 2


class TestEvalSummary:
    def test_empty(self):
        summary = EvalSummary()
        assert summary.total_sessions_evaluated == 0

    def test_with_data(self):
        summary = EvalSummary(
            total_sessions_evaluated=10,
            sessions_with_failures=2,
            avg_metrics={"tool_success_rate": 0.9},
        )
        assert summary.avg_metrics["tool_success_rate"] == 0.9


# ---------------------------------------------------------------------------
# compute_session_eval integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_includes_brier_score_when_predict_events_present(monkeypatch):
    """If replay has predict + reconcile events, brier_score appears in metrics."""
    import replay.reader as reader_mod
    from app.evals import compute as compute_mod
    from app.evals.store import store_eval  # noqa: F401 — imported to allow patching

    fake_events = [
        {
            "event_type": "agent.action.predict",
            "payload": {"action_id": "a1", "prediction": {"confidence": 0.9}},
        },
        {
            "event_type": "agent.action.reconcile",
            "payload": {"action_id": "a1", "prediction_match_score": 1.0},
        },
        {
            "event_type": "agent.action.predict",
            "payload": {"action_id": "a2", "prediction": {"confidence": 0.3}},
        },
        {
            "event_type": "agent.action.reconcile",
            "payload": {"action_id": "a2", "prediction_match_score": 0.0},
        },
    ]

    async def fake_get_session_summary(*args, **kwargs):
        return {"event_count": len(fake_events), "duration_ms": 1000}

    async def fake_get_session_timeline(*args, **kwargs):
        return {"events": fake_events}

    async def fake_store_eval(*args, **kwargs):
        return None

    # Patch replay reader functions used inside compute_session_eval
    monkeypatch.setattr(reader_mod, "get_session_summary", fake_get_session_summary)
    monkeypatch.setattr(reader_mod, "get_session_timeline", fake_get_session_timeline)

    # Patch store_eval so we don't need Redis
    import app.evals.store as store_mod
    monkeypatch.setattr(store_mod, "store_eval", fake_store_eval)

    # Also disable pattern extraction and webhooks side effects
    monkeypatch.setenv("EVAL_LLM_ENABLED", "false")

    result = await compute_mod.compute_session_eval(
        replay_redis=None,  # type: ignore[arg-type]
        session_id="s1",
    )

    assert result is not None, "compute_session_eval returned None unexpectedly"
    # (0.9 - 1.0)^2 + (0.3 - 0.0)^2 = 0.01 + 0.09 = 0.10 / 2 = 0.05
    assert result.metrics.get("brier_score") == pytest.approx(0.05, abs=1e-4)
