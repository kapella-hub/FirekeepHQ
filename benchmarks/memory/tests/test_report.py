from bench import report


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
