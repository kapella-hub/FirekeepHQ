"""Reader-independent retrieval metrics from recall JSONL dumps."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from bench.common import DATA_DIR, WORK_DIR, is_abstention, load_dataset, sanitize_namespace
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


def score_run(dataset_rows, recall_path: Path, ledger: Ledger, k: int) -> dict:
    recalls = {}
    for line in recall_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            recalls[rec["question_id"]] = rec

    per_question, by_type = [], defaultdict(list)
    errored, abstention_excluded, missing = [], 0, []
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
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--k", type=int, required=True)
    args = ap.parse_args()
    rows = load_dataset(DATA_DIR / "longmemeval_s.json")
    ledger = Ledger(WORK_DIR / "ingest_ledger.jsonl")
    result = score_run(rows, WORK_DIR / f"recall_{args.config}.jsonl", ledger, args.k)
    out = WORK_DIR / f"scores_{args.config}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["overall"], indent=2))
    print(f"-> {out}")


if __name__ == "__main__":
    main()
