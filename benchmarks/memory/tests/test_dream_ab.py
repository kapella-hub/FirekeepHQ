import json

import pytest

from bench import dream_ab


def _scores(recall, cov, ndcg):
    return {"k": 10, "overall": {"n": 470, "recall_at_k": recall,
                                 "coverage_at_k": cov, "mrr": 0.7, "ndcg_at_k": ndcg}}


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


def test_main_exits_zero_on_non_regression(tmp_path, capsys):
    before_path = tmp_path / "before.json"
    after_path = tmp_path / "after.json"
    before_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.80, 0.70, 0.60)})), encoding="utf-8")
    after_path.write_text(json.dumps(_full_run_record({"bench": _scores(0.85, 0.75, 0.65)})), encoding="utf-8")

    rc = dream_ab.main(["--before", str(before_path), "--after", str(after_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK" in out
