import json


from bench import dream_ab


def _scores(recall, cov, ndcg, *, completed=None, completed_last=None):
    """A run record as the CURRENT harness writes it: when it records counts at
    all it records both the cumulative total and the last invocation's, and
    `completed_last` defaults to the single-invocation case (they agree)."""
    out = {"k": 10, "overall": {"n": 470, "recall_at_k": recall,
                                "coverage_at_k": cov, "mrr": 0.7, "ndcg_at_k": ndcg}}
    if completed is not None:
        out["recall_counts"] = {
            "completed": completed, "skipped": 0, "errored": 0, "invocations": 1,
            "completed_last_invocation":
                completed if completed_last is None else completed_last,
        }
    return out


def _legacy_scores(recall, cov, ndcg, *, completed):
    """A record from BEFORE `completed_last_invocation` was written — cumulative
    count only. The comparator must still read it (published history), while
    saying out loud that it cannot rule out a same-label no-op re-run."""
    out = _scores(recall, cov, ndcg, completed=completed)
    del out["recall_counts"]["completed_last_invocation"]
    return out


def test_improvement_is_not_a_regression():
    out = dream_ab.compare_runs(_scores(0.80, 0.70, 0.60), _scores(0.85, 0.75, 0.65))
    assert out["regressed"] is False
    assert out["deltas"]["recall_at_k"] > 0


def test_drop_beyond_tolerance_is_a_regression():
    out = dream_ab.compare_runs(_scores(0.85, 0.75, 0.65), _scores(0.80, 0.75, 0.65))
    assert out["regressed"] is True
    assert "recall_at_k" in out["verdict"]


def test_noise_within_tolerance_is_not_a_regression():
    out = dream_ab.compare_runs(_scores(0.8000, 0.70, 0.60), _scores(0.7990, 0.70, 0.60))
    assert out["regressed"] is False


# --- defensive cases beyond the brief's three -------------------------------

def test_custom_tolerance_is_honoured():
    # 0.003 drop passes the default 0.005 tolerance but fails a tighter one.
    out = dream_ab.compare_runs(
        _scores(0.800, 0.70, 0.60), _scores(0.797, 0.70, 0.60), tolerance=0.001,
    )
    assert out["regressed"] is True
    assert "recall_at_k" in out["verdict"]


def test_multiple_offending_metrics_are_all_named_in_verdict():
    out = dream_ab.compare_runs(
        _scores(0.85, 0.75, 0.65), _scores(0.80, 0.70, 0.60),
    )
    assert out["regressed"] is True
    assert "recall_at_k" in out["verdict"]
    assert "coverage_at_k" in out["verdict"]
    assert "ndcg_at_k" in out["verdict"]


def test_mrr_drop_alone_is_not_a_regression():
    # mrr is reported for visibility but is NOT one of the gated metrics.
    before = _scores(0.80, 0.70, 0.60)
    after = _scores(0.80, 0.70, 0.60)
    after["overall"]["mrr"] = 0.1
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is False
    assert out["deltas"]["mrr"] < 0


def test_missing_overall_key_is_a_loud_error_not_a_crash():
    out = dream_ab.compare_runs({"k": 10}, _scores(0.80, 0.70, 0.60))
    assert out["regressed"] is True
    assert out["deltas"] == {}
    assert "overall" in out["verdict"].lower()


def test_mismatched_k_is_a_loud_comparison_error():
    before = _scores(0.80, 0.70, 0.60)
    before["k"] = 3
    after = _scores(0.80, 0.70, 0.60)
    after["k"] = 10
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is True
    assert out["deltas"] == {}
    assert "k" in out["verdict"].lower()


def test_missing_single_metric_is_skipped_not_fatal():
    before = _scores(0.80, 0.70, 0.60)
    after = _scores(0.85, 0.75, 0.65)
    del after["overall"]["ndcg_at_k"]
    out = dream_ab.compare_runs(before, after)
    assert "ndcg_at_k" not in out["deltas"]
    assert out["regressed"] is False
    assert out["deltas"]["recall_at_k"] > 0


def test_all_gate_metrics_missing_is_a_loud_error():
    before = {"k": 10, "overall": {"n": 5, "mrr": 0.5}}
    after = {"k": 10, "overall": {"n": 5, "mrr": 0.6}}
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is True
    assert "cannot verify" in out["verdict"].lower() or "gate metric" in out["verdict"].lower()


def _full_run_record(config_scores: dict) -> dict:
    return {"generated_at": "2026-08-04T00:00:00Z", "meta": {}, "retrieval": config_scores}


def test_compare_result_files_extracts_common_configs_from_full_run_records():
    before = _full_run_record({"bench": _scores(0.80, 0.70, 0.60), "defaults": _scores(0.70, 0.60, 0.50)})
    after = _full_run_record({"bench": _scores(0.85, 0.75, 0.65), "defaults": _scores(0.60, 0.60, 0.50)})
    out = dream_ab.compare_result_files(before, after)
    assert set(out) == {"bench", "defaults"}
    assert out["bench"]["regressed"] is False
    assert out["defaults"]["regressed"] is True


def test_compare_result_files_accepts_bare_score_run_shape():
    before = _scores(0.80, 0.70, 0.60)
    after = _scores(0.85, 0.75, 0.65)
    out = dream_ab.compare_result_files(before, after)
    assert len(out) == 1
    (only,) = out.values()
    assert only["regressed"] is False


def test_compare_result_files_no_common_config_is_a_loud_error():
    before = _full_run_record({"bench": _scores(0.80, 0.70, 0.60)})
    after = _full_run_record({"defaults": _scores(0.80, 0.70, 0.60)})
    out = dream_ab.compare_result_files(before, after)
    assert len(out) == 1
    (only,) = out.values()
    assert only["regressed"] is True
    assert "no recall config" in only["verdict"].lower()


def test_render_markdown_contains_table_and_verdict():
    comparisons = dream_ab.compare_result_files(
        _full_run_record({"bench": _scores(0.85, 0.75, 0.65)}),
        _full_run_record({"bench": _scores(0.80, 0.75, 0.65)}),
    )
    md = dream_ab.render_markdown(comparisons)
    assert "|" in md  # a markdown table was rendered
    assert "recall_at_k" in md
    assert "REGRESSION" in md


def test_main_exits_nonzero_and_prints_verdict_on_regression(tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.85, 0.75, 0.65)})), encoding="utf-8")
    after_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.80, 0.75, 0.65)})), encoding="utf-8")

    rc = dream_ab.main(["--before", str(before_path), "--after", str(after_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "REGRESSION" in out


# --- the no-op comparison the harness could not previously detect -----------

def test_after_run_that_completed_zero_recalls_is_not_evidence():
    """The observed defect: a second label skipped all 500 recalls, re-scored
    the first label's rows, and produced +0.0000 on every metric — which read
    as a perfect no-regression and would have passed the ship gate."""
    before = _scores(0.7809, 0.5647, 0.5696, completed=470)
    after = _scores(0.7809, 0.5647, 0.5696, completed=0)
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is True
    assert out["deltas"] == {}
    assert "completed=0" in out["verdict"]


def test_zero_completed_fails_even_when_metrics_improved():
    # A no-op leg is not evidence in EITHER direction — an apparent
    # improvement from a run that recalled nothing is equally meaningless.
    out = dream_ab.compare_runs(
        _scores(0.70, 0.60, 0.50, completed=470),
        _scores(0.99, 0.99, 0.99, completed=0),
    )
    assert out["regressed"] is True
    assert "completed=0" in out["verdict"]


def test_before_run_with_zero_completed_does_not_fail_the_gate():
    # Only the AFTER leg must have executed: `compare_runs` asks whether the
    # after run measured this store, not how the baseline's rows were produced.
    out = dream_ab.compare_runs(
        _scores(0.80, 0.70, 0.60, completed=0),
        _scores(0.85, 0.75, 0.65, completed=470),
    )
    assert out["regressed"] is False


def test_executed_after_run_passes_normally():
    out = dream_ab.compare_runs(
        _scores(0.80, 0.70, 0.60, completed=470),
        _scores(0.85, 0.75, 0.65, completed=470),
    )
    assert out["regressed"] is False
    assert out["warnings"] == []


def test_identical_metrics_are_not_flagged_when_the_after_run_executed():
    # Retrieval scoring is deterministic — an unchanged store legitimately
    # produces bit-identical metrics. A run that provably recalled must not be
    # second-guessed for reporting no change.
    out = dream_ab.compare_runs(
        _scores(0.80, 0.70, 0.60, completed=470),
        _scores(0.80, 0.70, 0.60, completed=470),
    )
    assert out["regressed"] is False
    assert out["warnings"] == []
    assert all(d == 0 for d in out["deltas"].values())


def test_identical_metrics_without_counts_warn_but_do_not_gate():
    out = dream_ab.compare_runs(_scores(0.80, 0.70, 0.60), _scores(0.80, 0.70, 0.60))
    assert out["regressed"] is False           # identity is possible, not proof
    assert any("SUSPECT" in w for w in out["warnings"])
    assert any("UNVERIFIED" in w for w in out["warnings"])


def test_missing_counts_alone_warn_unverified_but_not_suspect():
    out = dream_ab.compare_runs(_scores(0.80, 0.70, 0.60), _scores(0.85, 0.75, 0.65))
    assert out["regressed"] is False
    assert any("UNVERIFIED" in w for w in out["warnings"])
    assert not any("SUSPECT" in w for w in out["warnings"])


# --- the gate must read the LAST invocation, not the label's running total ---

def test_a_same_label_no_op_rerun_is_refused_despite_a_large_cumulative_total():
    """Finding 1. `completed` accumulates over a label's invocations, so the
    reachable operator sequence — run the leg, enable dreaming, re-run the
    identical command (the label defaults to `bench`) — leaves a record whose
    cumulative count is large and whose final invocation did nothing at all.
    Gating on the cumulative figure certifies that no-op."""
    before = _scores(0.7809, 0.5647, 0.5696, completed=470)
    after = _scores(0.7809, 0.5647, 0.5696, completed=470, completed_last=0)
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is True
    assert out["deltas"] == {}
    assert "completed=0" in out["verdict"]
    assert "final invocation" in out["verdict"]


def test_a_genuine_resume_still_passes_the_gate():
    """The other half of finding 1: an interrupted 4-hour leg finishes its
    remaining questions in a later invocation. Its final invocation completed
    > 0, so it is real evidence and must NOT be gated — that is why `skipped==0`
    or `invocations==1` are not the test."""
    before = _scores(0.78, 0.56, 0.56, completed=470)
    after = _scores(0.80, 0.58, 0.58, completed=470, completed_last=112)
    out = dream_ab.compare_runs(before, after)
    assert out["regressed"] is False
    assert out["warnings"] == []


def test_a_zero_last_invocation_fails_even_when_metrics_improved():
    out = dream_ab.compare_runs(
        _scores(0.70, 0.60, 0.50, completed=470),
        _scores(0.99, 0.99, 0.99, completed=470, completed_last=0),
    )
    assert out["regressed"] is True
    assert "completed=0" in out["verdict"]


def test_legacy_cumulative_only_record_is_read_but_flagged_unverified():
    # Published history must stay readable — but a record that cannot say what
    # its final invocation did cannot certify anything either.
    out = dream_ab.compare_runs(
        _legacy_scores(0.80, 0.70, 0.60, completed=470),
        _legacy_scores(0.85, 0.75, 0.65, completed=470),
    )
    assert out["regressed"] is False
    assert any("CUMULATIVE" in w for w in out["warnings"])


def test_legacy_cumulative_zero_still_fails_loud():
    out = dream_ab.compare_runs(
        _scores(0.80, 0.70, 0.60, completed=470),
        _legacy_scores(0.80, 0.70, 0.60, completed=0),
    )
    assert out["regressed"] is True
    assert "completed=0" in out["verdict"]


def test_malformed_last_invocation_count_falls_back_to_the_cumulative_figure():
    after = _scores(0.85, 0.75, 0.65, completed=470)
    after["recall_counts"]["completed_last_invocation"] = "many"
    out = dream_ab.compare_runs(_scores(0.80, 0.70, 0.60, completed=470), after)
    assert out["regressed"] is False
    assert any("CUMULATIVE" in w for w in out["warnings"])


def test_malformed_counts_are_treated_as_absent_not_as_zero():
    # `None` (nothing recorded) and `0` (recorded nothing) must never collapse
    # into each other — the latter is a hard failure, the former a warning.
    for bad in ({"completed": "470"}, {"completed": None}, {"completed": True}, "nope"):
        after = _scores(0.85, 0.75, 0.65)
        after["recall_counts"] = bad
        out = dream_ab.compare_runs(_scores(0.80, 0.70, 0.60), after)
        assert out["regressed"] is False
        assert any("UNVERIFIED" in w for w in out["warnings"])


def test_render_markdown_surfaces_warnings():
    comparisons = dream_ab.compare_result_files(
        _full_run_record({"bench": _scores(0.80, 0.70, 0.60)}),
        _full_run_record({"bench": _scores(0.80, 0.70, 0.60)}),
    )
    md = dream_ab.render_markdown(comparisons)
    assert "WARNING:" in md and "SUSPECT" in md


def test_main_exits_nonzero_when_the_after_leg_recalled_nothing(tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_full_run_record(
        {"bench": _scores(0.82, 0.72, 0.62, completed=470)})), encoding="utf-8")
    after_path.write_text(json.dumps(_full_run_record(
        {"bench": _scores(0.82, 0.72, 0.62, completed=0)})), encoding="utf-8")

    rc = dream_ab.main(["--before", str(before_path), "--after", str(after_path)])
    out = capsys.readouterr().out
    assert rc != 0
    assert "completed=0" in out


def test_main_exits_zero_with_warnings_when_unverified(tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.80, 0.70, 0.60)})), encoding="utf-8")
    after_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.80, 0.70, 0.60)})), encoding="utf-8")

    rc = dream_ab.main(["--before", str(before_path), "--after", str(after_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK (WITH WARNINGS)" in out


def test_every_loud_failure_shape_still_carries_a_warnings_key():
    # Callers (render_markdown) read `warnings` unconditionally; no early
    # return may omit it.
    shapes = [
        dream_ab.compare_runs({"k": 10}, _scores(0.8, 0.7, 0.6)),
        dream_ab.compare_runs(_scores(0.8, 0.7, 0.6), _scores(0.8, 0.7, 0.6, completed=0)),
        dream_ab.compare_runs({"k": 3, "overall": {"recall_at_k": 0.1}},
                              {"k": 10, "overall": {"recall_at_k": 0.1}}),
        dream_ab.compare_runs({"k": 10, "overall": {"n": 5, "mrr": 0.5}},
                              {"k": 10, "overall": {"n": 5, "mrr": 0.6}}),
    ]
    assert all(s["warnings"] == [] for s in shapes)
    (only,) = dream_ab.compare_result_files(
        _full_run_record({"bench": _scores(0.8, 0.7, 0.6)}),
        _full_run_record({"defaults": _scores(0.8, 0.7, 0.6)}),
    ).values()
    assert only["warnings"] == []


def test_main_exits_zero_on_non_regression(tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.80, 0.70, 0.60)})), encoding="utf-8")
    after_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.85, 0.75, 0.65)})), encoding="utf-8")

    rc = dream_ab.main(["--before", str(before_path), "--after", str(after_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out


# ---------------------------------------------------------------------------
# The completed=0 verdict must name the SHAPE of the zero it found. Two causes
# with opposite remedies land here, and the counts block already distinguishes
# them — asserting the wrong one sends an operator whose backend was down to go
# and audit run labels instead.
# ---------------------------------------------------------------------------

def _run_with_counts(**counts):
    return {
        "k": 10,
        "overall": {"recall_at_k": 0.8, "coverage_at_k": 0.7, "ndcg_at_k": 0.6, "mrr": 0.7},
        "recall_counts": counts,
    }


def test_an_all_errored_leg_is_not_blamed_on_skipping():
    """The recalls RAN and FAILED — nothing was skipped and nothing was
    re-scored, so a verdict claiming otherwise is actively misleading."""
    before = _run_with_counts(completed=500, completed_last_invocation=500, skipped=0, errored=0)
    after = _run_with_counts(completed=0, completed_last_invocation=0, skipped=0, errored=500)
    got = dream_ab.compare_runs(before, after)
    assert got["regressed"] is True
    assert "ERRORED" in got["verdict"]
    assert "backend" in got["verdict"] or "connectivity" in got["verdict"]
    assert "skipped" not in got["verdict"].lower().replace("skipped=0", "")


def test_an_all_skipped_leg_still_names_the_resume_cause():
    """The original defect keeps its original, correct diagnosis."""
    before = _run_with_counts(completed=500, completed_last_invocation=500, skipped=0, errored=0)
    after = _run_with_counts(completed=500, completed_last_invocation=0, skipped=500, errored=0)
    got = dream_ab.compare_runs(before, after)
    assert got["regressed"] is True
    assert "skipped" in got["verdict"]
    assert "re-score" in got["verdict"]


def test_a_zero_with_no_shape_recorded_says_so_rather_than_guessing():
    """Absence of the breakdown is not evidence for either cause."""
    before = _run_with_counts(completed=500, completed_last_invocation=500)
    after = _run_with_counts(completed=0, completed_last_invocation=0)
    got = dream_ab.compare_runs(before, after)
    assert got["regressed"] is True
    assert "does not say" in got["verdict"]
