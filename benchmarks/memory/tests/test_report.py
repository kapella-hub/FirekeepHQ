import json

import pytest

from bench import dream_ab, recall, report
from bench.common import run_work_dir, sanitize_label


def test_qa_accuracy_excludes_judge_errors_from_denominator():
    rows = [
        {"question_id": "a", "verdict": True, "judge_error": None},
        {"question_id": "b", "verdict": False, "judge_error": None},
        {"question_id": "c", "verdict": None, "judge_error": "unparseable"},
    ]
    acc = report.qa_accuracy(rows)
    assert acc == {"n": 2, "correct": 1, "accuracy": 0.5, "judge_errors": 1}


def _fake_scores():
    agg = {"n": 2, "recall_at_k": 0.5, "coverage_at_k": 0.5, "mrr": 0.5,
           "ndcg_at_k": 0.5}
    return {"k": 10, "overall": agg, "by_question_type": {"multi-session": agg},
            "errored_questions": [], "missing_questions": [],
            "abstention_excluded": 1}


def test_render_methodology_carries_mandatory_caveats():
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()},
        {"n": 2, "correct": 1, "accuracy": 0.5, "judge_errors": 0},
        {"dataset": {"sha256": "abc"}, "cortex_version": {"git_sha": "deadbeef"},
         "models": {"reader": "qwen3:14b", "embed": "mxbai-embed-large"}},
    )
    text = report.render_methodology(result)
    assert "NOT comparable to published GPT-4o-reader numbers" in text
    assert "Evidence Recall@k" in text
    assert "deadbeef" in text
    assert "floor, not the ceiling" in text  # known-limitations block present


def test_render_markdown_has_both_config_rows():
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()}, None,
        {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    md = report.render_markdown(result)
    assert "defaults" in md and "bench" in md and "0.500" in md


def test_render_markdown_surfaces_ledger_gap_count_when_nonzero():
    scores = _fake_scores()
    scores["ledger_gap_questions"] = ["q1", "q2"]
    result = report.build_result(
        {"bench": scores}, None, {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    md = report.render_markdown(result)
    assert "2" in md and "ledger gap" in md.lower()


def test_render_markdown_omits_ledger_gap_line_when_absent():
    result = report.build_result(
        {"bench": _fake_scores()}, None,
        {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    md = report.render_markdown(result)
    assert "ledger gap" not in md.lower()


def test_load_meta_sets_explicit_reader_and_embed_and_configs(monkeypatch):
    monkeypatch.setattr(report, "_fetch_json", lambda url, timeout=5.0: "unavailable")
    meta = report._load_meta("http://c", "http://o", "qwen3:14b")
    assert meta["models"]["reader"] == "qwen3:14b"
    assert meta["models"]["embed"] == report.EMBED_MODEL == "mxbai-embed-large"
    assert meta["configs"] == recall.CONFIGS


def test_render_methodology_config_rows_are_actual_configs_not_score_aggregates():
    # Regression: this section used to render the SCORE aggregates dict
    # under a "Config rows (verbatim)" label — a fabricated config
    # description that happened to share some key names (e.g. no top_k/
    # token_budget/format at all). It must render the real recall.CONFIGS.
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()}, None,
        {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    text = report.render_methodology(result)
    assert json.dumps(recall.CONFIGS) in text
    assert '"top_k": 3' in text and '"token_budget": 600' in text
    assert '"top_k": 10' in text and '"format": "raw"' in text


def test_render_methodology_discloses_truncation_bounds():
    result = report.build_result(
        {"defaults": _fake_scores(), "bench": _fake_scores()}, None,
        {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    text = report.render_methodology(result)
    assert "5000" in text and "2000" in text


def test_load_ingest_errors_reads_fixture_file(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    (tmp_path / "ingest_errors.json").write_text(
        json.dumps(["lm_q1/s_a: boom", "lm_q2/s_b: boom2"]), encoding="utf-8")
    assert report._load_ingest_errors() == {
        "ingest_errors": 2,
        "ingest_error_keys": ["lm_q1/s_a: boom", "lm_q2/s_b: boom2"],
    }


def test_load_ingest_errors_absent_file_returns_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    assert report._load_ingest_errors() == {"ingest_errors": 0, "ingest_error_keys": []}


def test_load_ingest_errors_malformed_file_degrades_to_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    (tmp_path / "ingest_errors.json").write_text("not json", encoding="utf-8")
    assert report._load_ingest_errors() == {"ingest_errors": 0, "ingest_error_keys": []}


def test_build_result_includes_ingest_errors():
    result = report.build_result(
        {}, None, {"dataset": {}, "cortex_version": {}, "models": {}},
        ingest_errors={"ingest_errors": 3, "ingest_error_keys": ["a", "b", "c"]},
    )
    assert result["ingest_errors"] == 3
    assert result["ingest_error_keys"] == ["a", "b", "c"]


def test_build_result_defaults_ingest_errors_when_not_passed():
    result = report.build_result(
        {}, None, {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    assert result["ingest_errors"] == 0
    assert result["ingest_error_keys"] == []


def test_load_recall_per_question_reads_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    (tmp_path / "recall_bench.jsonl").write_text(
        "\n".join([
            json.dumps({"question_id": "q1", "hits": [
                {"session_id": "s_a"}, {"session_id": "s_b"}], "error": None}),
            json.dumps({"question_id": "q2", "hits": [], "error": "boom"}),
        ]),
        encoding="utf-8",
    )
    rows = report._load_recall_per_question("bench")
    assert rows[0] == {
        "question_id": "q1",
        "hits": [{"session_id": "s_a", "rank": 1}, {"session_id": "s_b", "rank": 2}],
        "error": None,
    }
    assert rows[1]["question_id"] == "q2"
    assert rows[1]["error"] == "boom"


def test_load_recall_per_question_absent_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    assert report._load_recall_per_question("bench") == []


def test_load_qa_per_question_truncates_answer_to_200_chars(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    long_answer = "x" * 500
    (tmp_path / "qa_bench.jsonl").write_text(
        json.dumps({"question_id": "q1", "answer": long_answer,
                    "verdict": True, "judge_error": None}) + "\n",
        encoding="utf-8",
    )
    rows = report._load_qa_per_question()
    assert len(rows[0]["answer"]) == 200
    assert rows[0]["verdict"] is True


def test_load_qa_per_question_absent_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "WORK_DIR", tmp_path)
    assert report._load_qa_per_question() == []


def test_build_result_includes_per_question():
    result = report.build_result(
        {}, None, {"dataset": {}, "cortex_version": {}, "models": {}},
        per_question={"bench": [{"question_id": "q1"}], "qa": []},
    )
    assert result["per_question"] == {"bench": [{"question_id": "q1"}], "qa": []}


def test_build_result_defaults_per_question_when_not_passed():
    result = report.build_result(
        {}, None, {"dataset": {}, "cortex_version": {}, "models": {}},
    )
    assert result["per_question"] == {}


# --- finding 4: the standalone report path must stamp the positive control --

_COUNTS = {"completed": 470, "errored": 0, "skipped": 470, "invocations": 2,
           "completed_last_invocation": 0}


@pytest.fixture
def standalone_report(monkeypatch, tmp_path):
    """`python -m bench.report --run-label X` over a work dir holding one
    config's scores plus the counts a no-op re-run would have left."""
    monkeypatch.setattr(report, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(report, "_fetch_json", lambda url, timeout=5.0: "unavailable")

    def build(label: str, counts: dict | None = _COUNTS):
        work = run_work_dir(label, work_dir=tmp_path / "work")
        work.mkdir(parents=True, exist_ok=True)
        (work / "scores_bench.json").write_text(json.dumps(_fake_scores()), encoding="utf-8")
        if counts is not None:
            (work / "recall_counts.json").write_text(
                json.dumps({"bench": counts}), encoding="utf-8")
        report.main(["--run-label", label])
        paths = sorted((tmp_path / "results").glob("*.json"))
        assert paths, "no run-record written"
        return json.loads(paths[-1].read_text(encoding="utf-8")), paths[-1]

    return build


def test_report_main_stamps_recall_counts_onto_the_record(standalone_report):
    """Only `bench.run._assemble_report` merged recall_counts, so a record
    regenerated by hand carried none — and `bench.dream_ab` silently dropped
    from a hard gate to a non-gating warning for exactly those records."""
    result, _ = standalone_report("post-dream")
    assert result["retrieval"]["bench"]["recall_counts"] == _COUNTS

    out = dream_ab.compare_runs(
        {"k": 10, "overall": _fake_scores()["overall"]},
        result["retrieval"]["bench"],
    )
    assert out["regressed"] is True
    assert "completed=0" in out["verdict"]


def test_report_main_omits_counts_when_the_work_dir_has_none(standalone_report):
    # Absence stays absence — `None` and `0` must not collapse.
    result, _ = standalone_report("post-dream", counts=None)
    assert "recall_counts" not in result["retrieval"]["bench"]


def test_report_main_sanitizes_the_results_filename(standalone_report):
    # Same failure as bench.run's: a raw label goes straight into the path.
    _, path = standalone_report("a/b")
    assert path.name.endswith(f"-{sanitize_label('a/b')}.json")
    assert path.parent.name == "results"
