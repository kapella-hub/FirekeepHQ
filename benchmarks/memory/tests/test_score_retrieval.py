import json
import math

import pytest

from bench import score_retrieval as sr
from bench.ingest import Ledger


def _hits(*session_ids):
    return [{"session_id": s, "score": 1.0, "content": ""} for s in session_ids]


def test_perfect_first_hit():
    s = sr.score_question(_hits("e1", "x", "x"), {"e1"}, k=3, n_relevant_available=5)
    assert s["recall_at_k"] == 1
    assert s["mrr"] == 1.0
    assert s["coverage_at_k"] == 1.0
    # DCG = 1/log2(2) = 1.0; IDCG (3 ideal relevant slots) = 1 + 1/log2(3) + 1/log2(4)
    idcg = 1 + 1 / math.log2(3) + 1 / math.log2(4)
    assert s["ndcg_at_k"] == pytest.approx(1.0 / idcg)


def test_no_relevant_hits():
    s = sr.score_question(_hits("x", None, "y"), {"e1"}, k=3, n_relevant_available=4)
    assert s == {"recall_at_k": 0, "coverage_at_k": 0.0, "mrr": 0.0, "ndcg_at_k": 0.0}


def test_second_rank_hit_mrr_and_ndcg():
    s = sr.score_question(_hits("x", "e1"), {"e1", "e2"}, k=2, n_relevant_available=6)
    assert s["mrr"] == 0.5
    assert s["coverage_at_k"] == 0.5  # one of two evidence sessions found
    dcg = 1 / math.log2(3)          # relevant at rank 2
    idcg = 1 + 1 / math.log2(3)      # 2 ideal slots (k=2 < available)
    assert s["ndcg_at_k"] == pytest.approx(dcg / idcg)


def test_ideal_capped_by_available_relevant():
    # Only 1 relevant memory exists in the namespace: IDCG must use 1 slot, not k.
    s = sr.score_question(_hits("e1", "x", "x"), {"e1"}, k=3, n_relevant_available=1)
    assert s["ndcg_at_k"] == pytest.approx(1.0)


def test_duplicate_session_counts_once_for_coverage():
    s = sr.score_question(_hits("e1", "e1"), {"e1", "e2"}, k=2, n_relevant_available=4)
    assert s["coverage_at_k"] == 0.5


def test_aggregate_means():
    agg = sr.aggregate([
        {"recall_at_k": 1, "coverage_at_k": 1.0, "mrr": 1.0, "ndcg_at_k": 1.0},
        {"recall_at_k": 0, "coverage_at_k": 0.0, "mrr": 0.0, "ndcg_at_k": 0.0},
    ])
    assert agg["n"] == 2
    assert agg["recall_at_k"] == 0.5
    assert agg["mrr"] == 0.5


def test_score_run_flags_ledger_gap_questions(tmp_path):
    # Evidence is nonempty but the ledger has no record of any memories
    # ingested for that evidence session — a missing/incomplete ledger,
    # not a genuine zero-relevant-available case. The question is still
    # scored as before; it's additionally flagged for visibility.
    row = {
        "question_id": "q1",
        "question_type": "multi-session",
        "answer_session_ids": ["s_a"],
    }
    recall_path = tmp_path / "recall_bench.jsonl"
    recall_path.write_text(
        json.dumps({"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None}) + "\n",
        encoding="utf-8",
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")  # empty: no memories recorded for s_a
    result = sr.score_run([row], recall_path, ledger, k=10)
    assert result["ledger_gap_questions"] == ["q1"]
    # Still scored normally — the gap is additive, not a substitute.
    assert result["overall"]["n"] == 1


def test_score_run_no_gap_flagged_when_ledger_has_records(tmp_path):
    row = {
        "question_id": "q1",
        "question_type": "multi-session",
        "answer_session_ids": ["s_a"],
    }
    recall_path = tmp_path / "recall_bench.jsonl"
    recall_path.write_text(
        json.dumps({"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None}) + "\n",
        encoding="utf-8",
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    ledger.mark("lm_q1/s_a", n_memories=3)
    result = sr.score_run([row], recall_path, ledger, k=10)
    assert result["ledger_gap_questions"] == []


def test_score_run_no_gap_flagged_when_evidence_is_empty(tmp_path):
    # Abstention-style rows with no evidence sessions must not be flagged —
    # a gap only means something when evidence exists but the ledger can't
    # account for it.
    row = {
        "question_id": "q1",
        "question_type": "single-session-user",
        "answer_session_ids": [],
    }
    recall_path = tmp_path / "recall_bench.jsonl"
    recall_path.write_text(
        json.dumps({"question_id": "q1", "hits": [], "error": None}) + "\n",
        encoding="utf-8",
    )
    ledger = Ledger(tmp_path / "ledger.jsonl")
    result = sr.score_run([row], recall_path, ledger, k=10)
    assert result["ledger_gap_questions"] == []
