# cortex/tests/test_trust_rows.py
"""The frozen aggregation (spec §3). The cohort is in-window PREDICTS;
reconciles pair by action_id to the declaring agent; rate never > 100%;
calibration is prediction-match Brier; truncation nulls biased metrics."""
from datetime import datetime, timedelta, timezone

from app.autopilot import trust
from app.evals.scorers import brier_score


def _predict(agent, action_id, ts, confidence=None):
    payload = {"action_id": action_id}
    if confidence is not None:
        payload["prediction"] = {"confidence": confidence}
    else:
        payload["prediction"] = None
    return {"event_type": "agent.action.predict", "agent_id": agent,
            "session_id": "s", "action_id": action_id,
            "ts": ts, "payload": payload}


def _reconcile(agent, action_id, ts, *, success=True, match=None):
    return {"event_type": "agent.action.reconcile", "agent_id": agent,
            "session_id": "s", "action_id": action_id, "ts": ts,
            "payload": {"action_id": action_id,
                        "outcome": {"success": success},
                        "prediction_match_score": match}}


T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_rate_never_exceeds_100_percent():
    # A reconcile whose declaration is OUTSIDE the cohort must not count.
    events = [
        _predict("a", "p1", T0, confidence=0.9),
        _reconcile("a", "p1", T0, match=0.9),
        _reconcile("a", "ORPHAN", T0, match=1.0),  # no in-window predict
    ]
    rows = trust.build_rows(events, truncated=False)
    row = next(r for r in rows if r["agent_id"] == "a")
    assert row["declared"] == 1
    assert row["reconciled"] == 1
    assert row["reconciliation_rate"] == 1.0  # not 2.0


def test_declared_includes_null_prediction_but_scored_does_not():
    events = [
        _predict("a", "p1", T0, confidence=None),   # declaration, no prediction
        _reconcile("a", "p1", T0, match=None),
        _predict("a", "p2", T0, confidence=0.8),
        _reconcile("a", "p2", T0, match=0.7),
    ]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["declared"] == 2
    assert row["scored_predictions"] == 1  # only p2 has confidence+score


def test_calibration_matches_brier_score():
    events = [_predict("a", f"p{i}", T0, confidence=0.8) for i in range(6)] + \
             [_reconcile("a", f"p{i}", T0, match=0.6) for i in range(6)]
    row = trust.build_rows(events, truncated=False)[0]
    expected = brier_score([{"prediction_confidence": 0.8,
                             "prediction_match_score": 0.6}] * 6)
    assert row["calibration"] == expected


def test_calibration_null_below_min_points():
    events = [_predict("a", "p1", T0, confidence=0.8),
              _reconcile("a", "p1", T0, match=0.6)]  # 1 < MIN(5)
    row = trust.build_rows(events, truncated=False)[0]
    assert row["scored_predictions"] == 1
    assert row["calibration"] is None


def test_reversals_count_success_false_only():
    events = [_predict("a", "p1", T0, confidence=0.5),
              _reconcile("a", "p1", T0, success=False, match=0.5),
              _predict("a", "p2", T0, confidence=0.5),
              _reconcile("a", "p2", T0, success=True, match=0.5)]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["reversals"] == 1


def test_truncation_nulls_biased_metrics_keeps_counts():
    events = [_predict("a", f"p{i}", T0, confidence=0.8) for i in range(6)] + \
             [_reconcile("a", f"p{i}", T0, match=0.6) for i in range(6)]
    row = trust.build_rows(events, truncated=True)[0]
    assert row["declared"] == 6 and row["reconciled"] == 6  # lower bounds kept
    assert row["reconciliation_rate"] is None
    assert row["calibration"] is None
    assert row["calibration_trend"] is None
    assert row["first_seen_in_window"] is None
    assert row["last_seen_in_window"] is not None  # newest survives


def test_no_declaration_no_row():
    events = [_reconcile("ghost", "x", T0, match=1.0)]  # reconcile only
    rows = trust.build_rows(events, truncated=False)
    assert all(r["agent_id"] != "ghost" for r in rows)


def test_duplicate_reconcile_counted_once():
    """The <=100% guarantee is LOCAL: a second reconcile for one action_id
    (which the gateway cannot emit today, but a future one might) is deduped,
    not double-counted."""
    events = [_predict("a", "p1", T0, confidence=0.9),
              _reconcile("a", "p1", T0, match=0.9),
              _reconcile("a", "p1", T0, match=0.1)]  # duplicate action_id
    row = trust.build_rows(events, truncated=False)[0]
    assert row["declared"] == 1
    assert row["reconciled"] == 1  # not 2
    assert row["reconciliation_rate"] == 1.0


def test_reconcile_attributed_to_the_declaring_agent():
    """A reconcile whose own agent_id differs from the declarer's is credited
    to the DECLARER (spec §3) — the declaration owns the action."""
    events = [_predict("declarer", "p1", T0, confidence=0.8),
              # same action_id, but the reconcile event names a different agent
              {"event_type": "agent.action.reconcile", "agent_id": "someone-else",
               "session_id": "s", "action_id": "p1", "ts": T0,
               "payload": {"action_id": "p1", "outcome": {"success": True},
                           "prediction_match_score": 0.8}}]
    rows = trust.build_rows(events, truncated=False)
    assert [r["agent_id"] for r in rows] == ["declarer"]
    assert rows[0]["reconciled"] == 1


def test_calibration_trend_sign_improving():
    """Distinct timestamps, both halves >= MIN, newer half better (lower Brier)
    -> negative trend = improving. Index-split gives balanced halves."""
    older = [(_predict("a", f"o{i}", T0 + timedelta(minutes=i), confidence=0.9),
              _reconcile("a", f"o{i}", T0 + timedelta(minutes=i), match=0.4))
             for i in range(5)]  # confident but poorly matched -> high Brier
    newer = [(_predict("a", f"n{i}", T0 + timedelta(hours=1, minutes=i), confidence=0.9),
              _reconcile("a", f"n{i}", T0 + timedelta(hours=1, minutes=i), match=0.9))
             for i in range(5)]  # confident and well matched -> low Brier
    events = [ev for pair in older + newer for ev in pair]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["calibration_trend"] is not None
    assert row["calibration_trend"] < 0  # newer Brier lower => improving


def test_trend_survives_tied_timestamps():
    """All scored points share one timestamp: index-split still yields balanced
    halves (a value-split would collapse to empty-vs-all and null the trend)."""
    events = [_predict("a", f"p{i}", T0, confidence=0.8) for i in range(10)] + \
             [_reconcile("a", f"p{i}", T0, match=0.6) for i in range(10)]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["calibration_trend"] is not None  # not None despite the tie
