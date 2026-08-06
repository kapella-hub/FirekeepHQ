"""Reader-independent retrieval metrics from recall JSONL dumps."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from bench.common import (
    DATA_DIR,
    WORK_DIR,
    is_abstention,
    load_dataset,
    run_work_dir,
    sanitize_namespace,
)
from bench.ingest import Ledger

_METRICS = ("recall_at_k", "coverage_at_k", "mrr", "ndcg_at_k")


def score_question(hits, evidence_ids, k, n_relevant_available) -> dict:
    top = hits[:k]
    rels = [1 if h["session_id"] in evidence_ids else 0 for h in top]

    recall = 1 if any(rels) else 0
    found = {h["session_id"] for h, r in zip(top, rels) if r}
    coverage = len(found) / len(evidence_ids) if evidence_ids else 0.0
    mrr = 0.0
    for i, r in enumerate(rels):
        if r:
            mrr = 1.0 / (i + 1)
            break
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels))
    ideal_slots = min(k, max(n_relevant_available, 0))
    idcg = sum(1 / math.log2(i + 2) for i in range(ideal_slots))
    ndcg = (dcg / idcg) if idcg > 0 else 0.0
    return {"recall_at_k": recall, "coverage_at_k": coverage,
            "mrr": mrr, "ndcg_at_k": ndcg}


def aggregate(question_scores: list[dict]) -> dict:
    n = len(question_scores)
    out = {"n": n}
    for m in _METRICS:
        out[m] = (sum(q[m] for q in question_scores) / n) if n else 0.0
    return out


def displacement_facts(dataset_rows, recalls: dict, k: int) -> dict:
    """The displacement block stamped onto every score record.

    Two jobs, and the second is the reason it lives here rather than in
    `bench.displacement`:

    1. **Single-run counts.** `evidence_hits_at_k` and `untagged_slots_at_k`
       are the two headline numbers a displacement comparison is built from, so
       carrying them per run makes "untagged slots went 0 -> 3" readable
       straight off two published records with no tooling at all.
    2. **The dataset join.** A hit row records only `session_id` and rank;
       deciding whether that slot held EVIDENCE needs the question's
       `answer_session_ids`, which exist only in the 265 MB dataset — and
       `data/` is gitignored while `results/` is committed. Stamping the map
       here (the scorer is the one stage that holds the dataset) is what keeps
       a published record analysable for displacement after the fact, on a
       machine that never downloaded LongMemEval-S.

    Its scope is deliberately WIDER than the metrics beside it: `score_run`
    excludes abstention (`*_abs`) questions from every aggregate, but a dream
    can displace evidence in an abstention question's top-k just as easily —
    and in the first measured A/B that is exactly where the one lost evidence
    hit landed. Scoping this block to the scored subset would rebuild the blind
    spot the displacement analysis exists to remove, so every dataset row is
    stamped and the abstention share is reported separately.

    Errored recalls are counted in neither total: they returned no hits, and a
    zero from a failed call is not a measurement of retrieval.
    """
    answer_session_ids: dict[str, list] = {}
    totals = {"questions": 0, "evidence_hits_at_k": 0, "untagged_slots_at_k": 0,
              "questions_with_untagged_slots": 0}
    abstention = dict(totals)

    for row in dataset_rows:
        qid = row["question_id"]
        evidence = set(row["answer_session_ids"])
        answer_session_ids[qid] = list(row["answer_session_ids"])
        rec = recalls.get(qid)
        if rec is None or rec.get("error"):
            continue
        top = (rec.get("hits") or [])[:k]
        ev_hits = sum(1 for h in top if h.get("session_id") in evidence)
        untagged = sum(1 for h in top if h.get("session_id") is None)
        for bucket in (totals, abstention) if is_abstention(qid) else (totals,):
            bucket["questions"] += 1
            bucket["evidence_hits_at_k"] += ev_hits
            bucket["untagged_slots_at_k"] += untagged
            bucket["questions_with_untagged_slots"] += 1 if untagged else 0

    return {"k": k, **totals, "abstention": abstention,
            "answer_session_ids": answer_session_ids}


def score_run(dataset_rows, recall_path: Path, ledger: Ledger, k: int) -> dict:
    recalls = {}
    for line in recall_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            recalls[rec["question_id"]] = rec

    per_question, by_type = [], defaultdict(list)
    errored, abstention_excluded, missing = [], 0, []
    ledger_gap_questions = []
    for row in dataset_rows:
        qid = row["question_id"]
        if is_abstention(qid):
            abstention_excluded += 1
            continue
        rec = recalls.get(qid)
        if rec is None:
            missing.append(qid)
            continue
        if rec.get("error"):
            errored.append(qid)
            continue
        ns = sanitize_namespace(qid)
        evidence = set(row["answer_session_ids"])
        per_sess = ledger.memories_per_session(ns)
        n_avail = sum(per_sess.get(sid, 0) for sid in evidence)
        if evidence and n_avail == 0:
            # Evidence exists but the ledger has no record of it — a
            # missing/incomplete ledger, not a genuine zero-relevant-
            # available question. Still scored as-is; flagged for visibility.
            ledger_gap_questions.append(qid)
        s = score_question(rec["hits"], evidence, k, n_avail)
        per_question.append(s)
        by_type[row["question_type"]].append(s)

    return {
        "k": k,
        "overall": aggregate(per_question),
        "by_question_type": {t: aggregate(v) for t, v in sorted(by_type.items())},
        "errored_questions": errored,
        "missing_questions": missing,
        "abstention_excluded": abstention_excluded,
        "ledger_gap_questions": ledger_gap_questions,
        # Everything `bench.displacement` needs that only the dataset can
        # supply, plus the two single-run counts a reader can compare by eye.
        # Deliberately spans every question, abstention included — see
        # `displacement_facts`.
        "displacement": displacement_facts(dataset_rows, recalls, k),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--k", type=int, required=True)
    # Scores are per run label, like the recall rows they are computed from.
    # The ingest ledger is NOT — the store is a shared fixture (see bench.run).
    ap.add_argument("--run-label", default="bench")
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    ledger = Ledger(WORK_DIR / "ingest_ledger.jsonl")
    work = run_work_dir(args.run_label, work_dir=WORK_DIR)
    result = score_run(rows, work / f"recall_{args.config}.jsonl", ledger, args.k)
    out = work / f"scores_{args.config}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["overall"], indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
