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


# ---------------------------------------------------------------------------
# The displacement block. Its job is to make a published run record analysable
# for evidence displacement AFTER the fact, on a machine that never downloaded
# the dataset — `data/` is gitignored, `results/` is committed.
# ---------------------------------------------------------------------------

def _dataset_row(qid, *evidence):
    return {"question_id": qid, "question_type": "multi-session",
            "answer_session_ids": list(evidence)}


def _recall_file(tmp_path, *records):
    path = tmp_path / "recall_bench.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_displacement_block_counts_evidence_and_untagged_slots(tmp_path):
    rows = [_dataset_row("q1", "s_a")]
    recall_path = _recall_file(tmp_path, {
        "question_id": "q1",
        "hits": [{"session_id": "s_a"}, {"session_id": None}, {"session_id": "x"}],
        "error": None,
    })
    result = sr.score_run(rows, recall_path, Ledger(tmp_path / "l.jsonl"), k=10)
    block = result["displacement"]
    assert block["k"] == 10
    assert block["questions"] == 1
    assert block["evidence_hits_at_k"] == 1
    assert block["untagged_slots_at_k"] == 1
    assert block["questions_with_untagged_slots"] == 1


def test_displacement_block_respects_top_k(tmp_path):
    rows = [_dataset_row("q1", "s_a")]
    recall_path = _recall_file(tmp_path, {
        "question_id": "q1",
        "hits": [{"session_id": "x"}, {"session_id": "y"}, {"session_id": "s_a"}],
        "error": None,
    })
    result = sr.score_run(rows, recall_path, Ledger(tmp_path / "l.jsonl"), k=2)
    assert result["displacement"]["evidence_hits_at_k"] == 0


def test_displacement_block_covers_abstention_questions_the_metrics_exclude(tmp_path):
    """Load-bearing. `score_run` drops `*_abs` questions from every aggregate,
    and in the first measured Dreaming A/B the ONE question that lost evidence
    to a dream was an abstention question. A block scoped to the scored subset
    would rebuild exactly that blind spot."""
    rows = [_dataset_row("q1", "s_a"), _dataset_row("q2_abs", "s_b")]
    recall_path = _recall_file(
        tmp_path,
        {"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None},
        {"question_id": "q2_abs",
         "hits": [{"session_id": "s_b"}, {"session_id": None}], "error": None},
    )
    result = sr.score_run(rows, recall_path, Ledger(tmp_path / "l.jsonl"), k=10)
    assert result["abstention_excluded"] == 1          # excluded from the metrics
    block = result["displacement"]
    assert set(block["answer_session_ids"]) == {"q1", "q2_abs"}
    assert block["questions"] == 2
    assert block["evidence_hits_at_k"] == 2
    assert block["untagged_slots_at_k"] == 1
    # ...and the abstention share is reported separately, not merged away.
    assert block["abstention"]["questions"] == 1
    assert block["abstention"]["evidence_hits_at_k"] == 1
    assert block["abstention"]["untagged_slots_at_k"] == 1


def test_displacement_block_ignores_errored_and_missing_recalls(tmp_path):
    """A failed recall returned no hits; counting its zero would report a
    retrieval fact about a call that never retrieved."""
    rows = [_dataset_row("q1", "s_a"), _dataset_row("q2", "s_b"),
            _dataset_row("q3", "s_c")]
    recall_path = _recall_file(
        tmp_path,
        {"question_id": "q1", "hits": [{"session_id": "s_a"}], "error": None},
        {"question_id": "q2", "hits": [], "error": "boom"},
    )
    result = sr.score_run(rows, recall_path, Ledger(tmp_path / "l.jsonl"), k=10)
    block = result["displacement"]
    assert block["questions"] == 1                    # q2 errored, q3 missing
    assert block["evidence_hits_at_k"] == 1
    # The evidence map still covers every dataset row — it is the join key, and
    # a question absent from it cannot be compared at all.
    assert set(block["answer_session_ids"]) == {"q1", "q2", "q3"}


def test_the_stamped_block_is_what_bench_displacement_reads_back(tmp_path):
    """End to end on the seam: score a run, wrap it as a report would, and let
    `bench.displacement` resolve its evidence with no dataset in sight."""
    from bench import displacement

    rows = [_dataset_row("q1", "s_a")]
    recall_path = _recall_file(tmp_path, {
        "question_id": "q1",
        "hits": [{"session_id": "s_a"}, {"session_id": "s_a"}], "error": None,
    })
    scored = sr.score_run(rows, recall_path, Ledger(tmp_path / "l.jsonl"), k=10)
    before = {"retrieval": {"bench": scored},
              "per_question": {"bench": [{"question_id": "q1", "error": None, "hits": [
                  {"session_id": "s_a", "rank": 1}, {"session_id": "s_a", "rank": 2}]}]}}
    after = {"retrieval": {"bench": {"k": 10}},
             "per_question": {"bench": [{"question_id": "q1", "error": None, "hits": [
                 {"session_id": "s_a", "rank": 1}, {"session_id": None, "rank": 2}]}]}}

    resolved = displacement.resolve_evidence(
        before, after, default_dataset=tmp_path / "no-dataset-here.json")
    assert resolved["error"] is None
    assert resolved["evidence"] == {"q1": ["s_a"]}
    out = displacement.compare_displacement_files(before, after, resolved["evidence"])
    assert out["bench"]["metrics"]["net_evidence_delta"] == -1
    assert out["bench"]["metrics"]["untagged_slots_after"] == 1
