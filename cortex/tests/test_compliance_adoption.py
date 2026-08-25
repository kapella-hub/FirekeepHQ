"""Living Instructions round 3 — grading ADOPTION (outcome truth PR4 D2).

Adds `grade_self_reported` = graded / completed to the compliance table,
sliced by `experiment_group` (PR4 D1). FREEZE-SAFE: the six round-1
predicates read `e["metrics"]` and nothing else; `task_result` /
`task_result_source` / `experiment_group` are TOP-LEVEL fields on the eval
record (`EvalResult`, evals/models.py), not metrics. The call site enriches
the dict handed to every predicate with those three fields under names no
metric uses, so the six frozen predicates keep reading their metric keys
unchanged and this row is the only one that reads the new keys, via
`recognized_grade_pair` (the one grade-validity check in cortex).

The freeze guard here is `test_enrich_dict_does_not_perturb_the_six_frozen_predicates`:
it reproduces the founding-measurement fixture (same shape as
test_autopilot_api.py's `test_compliance_scores_the_founding_predicates` and
test_compliance_attribution.py's round-2 fixtures) with no grade fields at
all, and pins every existing row's hits/total/rate to what they were before
this change. Those two existing test modules staying green (with only the
mechanical fixture/key-set updates a NEW row necessarily requires — never a
change to a frozen predicate's hits/rate on an unchanged fixture) is the
other half of the guard.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr
import pytest

from app.autopilot import compliance as comp

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)

FROZEN_KEYS = (
    "recall_before_work", "write_as_you_go", "recall_visibly_used",
    "ctx_working_state", "declared_predictions", "outcome_bearing",
)


def rec(sid, *, task_result=None, task_result_source=None, experiment_group=None,
        failure_event_ids=None, days_ago=1.0, **metrics):
    r: dict = {
        "session_id": sid,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "trigger": "session_complete",
        "metrics": metrics,
    }
    if task_result is not None:
        r["task_result"] = task_result
    if task_result_source is not None:
        r["task_result_source"] = task_result_source
    if experiment_group is not None:
        r["experiment_group"] = experiment_group
    if failure_event_ids is not None:
        r["failure_event_ids"] = failure_event_ids
    return r


def rows_by_key(evals):
    return {r["key"]: r for r in comp.build_rows(evals)}


# ------------------------------------------------------------- freeze guard --

def test_enrich_dict_does_not_perturb_the_six_frozen_predicates():
    """The load-bearing guard: a fixture with NO grade fields at all — every
    frozen row's hits/total/rate must be exactly what it was before
    task_result/task_result_source/experiment_group existed."""
    evals = [
        rec("s1", memory_read_count=2, memory_write_count=1, recall_used_rate=0.5,
            context_snapshot_count=3, brier_score=0.11, outcome_event_count=2),
        rec("s2", memory_read_count=0, memory_write_count=0, recall_used_rate=0.0,
            context_snapshot_count=0, outcome_event_count=1),
    ]
    rows = rows_by_key(evals)
    for key in FROZEN_KEYS:
        assert rows[key]["hits"] == 1, key
        assert rows[key]["total"] == 2, key
        assert rows[key]["rate"] == 0.5, key


def test_grade_fields_present_do_not_perturb_the_six_frozen_predicates():
    """Same fixture, but now WITH grade/arm fields on the records — a
    graded, arm-attributed session must score the six frozen predicates
    identically to an ungraded one with the same metrics."""
    evals = [
        rec("s1", memory_read_count=2, memory_write_count=1, recall_used_rate=0.5,
            context_snapshot_count=3, brier_score=0.11, outcome_event_count=2,
            task_result="success", task_result_source="self_reported",
            experiment_group="A"),
        rec("s2", memory_read_count=0, memory_write_count=0, recall_used_rate=0.0,
            context_snapshot_count=0, outcome_event_count=1,
            task_result="failure", task_result_source="self_reported",
            experiment_group="B"),
    ]
    rows = rows_by_key(evals)
    for key in FROZEN_KEYS:
        assert rows[key]["hits"] == 1, key
        assert rows[key]["total"] == 2, key
        assert rows[key]["rate"] == 0.5, key


def test_new_row_present_alongside_the_frozen_six():
    rows = rows_by_key([rec("s1", memory_read_count=1)])
    assert set(rows) == set(FROZEN_KEYS) | {"grade_self_reported"}


# ---------------------------------------------------------- grade_self_reported --

def test_recognized_grade_pair_counts_as_a_hit():
    evals = [rec("s1", task_result="success", task_result_source="self_reported")]
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["hits"] == 1
    assert row["total"] == 1
    assert row["rate"] == 1.0


def test_ungraded_session_is_a_miss():
    evals = [rec("s1")]  # completed, never graded
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["hits"] == 0
    assert row["total"] == 1
    assert row["rate"] == 0.0


def test_grade_without_recognized_source_is_a_miss():
    """A 'partial' (or any) grade with no recognized task_result_source is
    NOT a recognized pair — recognized_grade_pair is atomic (spec D2)."""
    evals = [
        rec("no-source", task_result="success"),  # source missing entirely
        rec("partial-no-source", task_result="partial"),
        rec("unrecognized-source", task_result="success",
            task_result_source="human_reviewed"),
    ]
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["hits"] == 0
    assert row["total"] == 3


def test_partial_grade_with_recognized_source_still_counts_as_adoption():
    """Adoption measures whether the agent self-reported ANY recognized
    grade, not specifically success — recognized_grade_pair treats
    partial/self_reported as a valid, atomic pair."""
    evals = [rec("s1", task_result="partial", task_result_source="self_reported")]
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["hits"] == 1


def test_non_numeric_grade_string_never_raises():
    evals = [rec("weird", task_result="banana", task_result_source="self_reported")]
    row = rows_by_key(evals)["grade_self_reported"]  # must not raise TypeError
    assert row["hits"] == 0
    assert row["total"] == 1


# ------------------------------------------------------------ per-arm split --

def test_rate_reported_per_experiment_group():
    evals = [
        rec("a1", task_result="success", task_result_source="self_reported",
            experiment_group="A"),
        rec("a2", experiment_group="A"),  # ungraded
        rec("b1", task_result="success", task_result_source="self_reported",
            experiment_group="B"),
        rec("b2", task_result="success", task_result_source="self_reported",
            experiment_group="B"),
        rec("none1"),  # no experiment_group at all — pre-PR4 or unattributed
    ]
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["hits"] == 3
    assert row["total"] == 5
    assert row["by_experiment_group"] == {
        "A": {"hits": 1, "total": 2},
        "B": {"hits": 2, "total": 2},
    }


def test_unattributed_experiment_group_excluded_from_the_arm_split():
    """None (unverified/unattributed OR pre-D1) is not a measured arm — it is
    EXCLUDED from by_experiment_group, not given its own bucket, while still
    counting toward the row's overall hits/total."""
    evals = [rec("s1", task_result="success", task_result_source="self_reported")]
    row = rows_by_key(evals)["grade_self_reported"]
    assert row["by_experiment_group"] == {}
    assert row["hits"] == 1
    assert row["total"] == 1


def test_other_rows_carry_no_experiment_group_split():
    """The per-arm split is additive to the new row only — it must not
    appear on the existing frozen rows."""
    evals = [rec("s1", memory_read_count=1, experiment_group="A")]
    rows = rows_by_key(evals)
    for key in FROZEN_KEYS:
        assert "by_experiment_group" not in rows[key], key


def test_empty_evals_yields_empty_arm_split_not_error():
    row = rows_by_key([])["grade_self_reported"]
    assert row["by_experiment_group"] == {}
    assert row["rate"] is None


# ---------------------------------------------------- optimism-skew (D3) --
#
# optimism_skew = (self-reported-success sessions carrying an INDEPENDENT
# failure contradiction) / (all self-reported-success sessions). Visibility
# only — no gating, no mutation, reuses the same scan_evals population as
# build_rows above (no second scan). Two independent contradictions:
# has_failures (failure_event_ids non-empty) and a GUARDED tool_success_rate
# (< 1.0, only counted when outcome_event_count >= 2 — below that the
# outcome population is ~= the self-report itself and not independent).

def test_self_success_with_failure_event_ids_is_a_skew_hit():
    evals = [rec("s1", task_result="success", task_result_source="self_reported",
                  failure_event_ids=["evt-1"])]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["hits"] == 1
    assert row["self_success_total"] == 1


def test_tool_success_rate_below_min_outcome_events_is_not_independent():
    """outcome_event_count=1: the guard is non-negotiable — NOT a hit even
    though tool_success_rate < 1.0."""
    evals = [rec("s1", task_result="success", task_result_source="self_reported",
                  tool_success_rate=0.5, outcome_event_count=1)]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["hits"] == 0
    assert row["self_success_total"] == 1


def test_tool_success_rate_at_min_outcome_events_is_a_skew_hit():
    """Same tool_success_rate, but outcome_event_count=2 clears the guard —
    IS a hit."""
    evals = [rec("s1", task_result="success", task_result_source="self_reported",
                  tool_success_rate=0.5, outcome_event_count=2)]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["hits"] == 1


def test_clean_tool_success_rate_is_not_a_hit():
    evals = [rec("s1", task_result="success", task_result_source="self_reported",
                  tool_success_rate=1.0, outcome_event_count=5)]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["hits"] == 0


def test_self_failure_is_never_a_hit_regardless_of_contradictions():
    evals = [rec("s1", task_result="failure", task_result_source="self_reported",
                  failure_event_ids=["evt-1"], tool_success_rate=0.0,
                  outcome_event_count=5)]
    skew = comp.build_optimism_skew(evals)["overall"]
    assert skew["hits"] == 0
    assert skew["self_success_total"] == 0


def test_ungraded_session_is_not_counted_in_the_denominator():
    evals = [rec("s1", failure_event_ids=["evt-1"], tool_success_rate=0.0,
                  outcome_event_count=5)]
    skew = comp.build_optimism_skew(evals)["overall"]
    assert skew["hits"] == 0
    assert skew["self_success_total"] == 0


def test_below_min_self_success_n_yields_null_rate_not_zero():
    """The min-N gate: below MIN_SELF_SUCCESS_N self-success sessions, rate
    must be null/insufficient_n — NEVER a bare 0.0 masquerading as a clean
    measurement on almost no data."""
    n = comp.MIN_SELF_SUCCESS_N - 1
    evals = [rec(f"s{i}", task_result="success", task_result_source="self_reported")
             for i in range(n)]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["self_success_total"] == n
    assert row["hits"] == 0
    assert row["rate"] is None
    assert row["insufficient_n"] is True


def test_at_min_self_success_n_reports_a_real_rate():
    n = comp.MIN_SELF_SUCCESS_N
    evals = [
        rec(f"s{i}", task_result="success", task_result_source="self_reported",
            failure_event_ids=["evt-1"] if i < 5 else None)
        for i in range(n)
    ]
    row = comp.build_optimism_skew(evals)["overall"]
    assert row["self_success_total"] == n
    assert row["hits"] == 5
    assert row["rate"] == round(5 / n, 4)
    assert row["insufficient_n"] is False


def test_empty_evals_yields_a_gated_overall_not_an_error():
    row = comp.build_optimism_skew([])["overall"]
    assert row == {
        "hits": 0, "self_success_total": 0, "rate": None, "insufficient_n": True,
    }


def test_reported_per_experiment_group():
    n = comp.MIN_SELF_SUCCESS_N
    evals = [
        rec(f"a{i}", task_result="success", task_result_source="self_reported",
            experiment_group="A", failure_event_ids=["evt-1"] if i < 3 else None)
        for i in range(n)
    ] + [
        rec(f"b{i}", task_result="success", task_result_source="self_reported",
            experiment_group="B")
        for i in range(5)  # below MIN_SELF_SUCCESS_N for the B arm
    ]
    by_group = comp.build_optimism_skew(evals)["by_experiment_group"]
    assert by_group["A"] == {
        "hits": 3, "self_success_total": n, "rate": round(3 / n, 4),
        "insufficient_n": False,
    }
    assert by_group["B"] == {
        "hits": 0, "self_success_total": 5, "rate": None, "insufficient_n": True,
    }


def test_none_experiment_group_excluded_from_skew_split_but_counted_overall():
    evals = [rec("s1", task_result="success", task_result_source="self_reported")]
    skew = comp.build_optimism_skew(evals)
    assert skew["by_experiment_group"] == {}
    assert skew["overall"]["self_success_total"] == 1


# ------------------------------------------ endpoint-level via fakeredis --

@pytest.mark.asyncio
async def test_optimism_skew_surfaced_alongside_the_compliance_table():
    r = fr.FakeRedis(decode_responses=True)
    await r.set("rp:eval:hit", json.dumps(rec(
        "hit", task_result="success", task_result_source="self_reported",
        failure_event_ids=["evt-1"])))
    await r.set("rp:eval:clean", json.dumps(rec(
        "clean", task_result="success", task_result_source="self_reported")))
    await r.set("rp:eval:failed", json.dumps(rec(
        "failed", task_result="failure", task_result_source="self_reported",
        failure_event_ids=["evt-2"])))

    body = await comp.build_compliance(r)

    assert body["optimism_skew"]["overall"] == {
        "hits": 1, "self_success_total": 2, "rate": None, "insufficient_n": True,
    }
    assert body["optimism_skew"]["by_experiment_group"] == {}
