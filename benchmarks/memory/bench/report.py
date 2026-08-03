"""Assemble the publishable run-record JSON and generate METHODOLOGY.md.

Consumes Task 5's `work/scores_<config>.json` shape, Task 6's
`work/qa_bench.jsonl` shape, and Task 1's `data/dataset_meta.json`, plus a
best-effort cortex `/version` and host Ollama `/api/tags` snapshot. Produces
`results/<timestamp>-<run-label>.json` and `results/METHODOLOGY.md`.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

import httpx

from bench import recall
from bench.common import DATA_DIR, RESULTS_DIR, WORK_DIR

# Single source of truth for the embedding model name in the published
# meta — the bench compose file's EMBEDDING_MODEL must agree with this.
EMBED_MODEL = "mxbai-embed-large"

_NOT_COMPARABLE = (
    "The local-reader QA row is NOT comparable to published GPT-4o-reader "
    "numbers."
)

_METRIC_DEFINITIONS = """\
## Metric definitions

- A hit is *relevant* iff its `session_id` is in the question's `answer_session_ids`.
- **Evidence Recall@k** = 1 if any of the first k hits is relevant.
- **Evidence Coverage@k** = |distinct evidence sessions among first k hits| / |evidence sessions|.
- **MRR** = 1/rank of the first relevant hit (0 if none in top k).
- **NDCG@k**: binary gains, `DCG = Σ rel_i / log2(i+1)`; `IDCG` assumes the top `min(k, n_relevant_available)` slots are all relevant, where `n_relevant_available` = total memories ingested for that question's evidence sessions (from the ledger). Graph-only hits (`session_id=None`) count as non-relevant but occupy rank slots — that is deliberate: they consumed a top-k slot the product actually spent.
- Abstention questions (`*_abs`) and errored questions are excluded from aggregates and counted separately.
"""

_KNOWN_LIMITATIONS = """\
## Known limitations

1. **QA rows are not comparable to published GPT-4o numbers.** Retrieval rows are
   the head-to-head claim; QA rows show the full pipeline works locally.
2. **Judge = reader model** (self-judging bias); mitigated by the judge task being
   string-comparison-shaped, and disclosed.
3. LongMemEval haystacks are synthetic chat logs; Firekeep's production write path
   (agent-authored distilled memories) is plausibly *better* than raw turn-pairs —
   this benchmark measures a floor, not the ceiling.
4. Contradiction/auto-supersession stays on: repeated haystack content may
   supersede evidence memories. That is the shipped behavior; we measure it, not
   hide it. If it materially hurts, that's a product finding worth its own line.
5. Single run, no variance bars (deterministic retrieval; only QA has sampling
   noise — reader temperature pinned to 0).
6. Two truncation bounds affect what the QA reader ultimately sees: turn
   sides are truncated to 5000 characters at ingest (`bench.ingest._MAX_FIELD`),
   and per-hit recalled content is capped at 2000 characters in recall rows
   (`bench.recall.extract_hits`).
"""

_REPRODUCTION = """\
## Reproduction

```bash
cd benchmarks/memory
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m bench.download
docker compose -f docker-compose.bench.yml -p firekeep-bench up -d --build
.venv/Scripts/python -m bench.run --limit 2 --config both --run-label smoke
.venv/Scripts/python -m bench.run --config both --run-label full
```
"""


def qa_accuracy(qa_rows: list[dict]) -> dict:
    judge_errors = sum(1 for r in qa_rows if r.get("judge_error"))
    judged = [r for r in qa_rows if r.get("verdict") is not None]
    n = len(judged)
    correct = sum(1 for r in judged if r["verdict"])
    accuracy = (correct / n) if n else 0.0
    return {"n": n, "correct": correct, "accuracy": accuracy,
            "judge_errors": judge_errors}


def build_result(
    scores: dict[str, dict], qa: dict | None, meta: dict, *,
    ingest_errors: dict | None = None, per_question: dict | None = None,
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "retrieval": scores,
        "qa_local": qa,
        **(ingest_errors or {"ingest_errors": 0, "ingest_error_keys": []}),
        "per_question": per_question if per_question is not None else {},
    }


def render_markdown(result: dict) -> str:
    lines = [
        "| config | k | n | Recall@k | Coverage@k | MRR | NDCG@k |",
        "|---|---|---|---|---|---|---|",
    ]
    for config_name, scores in result["retrieval"].items():
        overall = scores["overall"]
        lines.append(
            f"| {config_name} | {scores['k']} | {overall['n']} | "
            f"{overall['recall_at_k']:.3f} | {overall['coverage_at_k']:.3f} | "
            f"{overall['mrr']:.3f} | {overall['ndcg_at_k']:.3f} |"
        )
    gap_lines = [
        f"{config_name}: {len(gaps)} question(s) with ledger gaps (evidence "
        "session record missing/incomplete — NDCG's ideal-slot count "
        "understates true availability for these)."
        for config_name, scores in result["retrieval"].items()
        if (gaps := scores.get("ledger_gap_questions") or [])
    ]
    if gap_lines:
        lines.append("")
        lines.extend(gap_lines)
    qa = result.get("qa_local")
    if qa:
        lines.append("")
        lines.append(
            f"Local-reader QA (bench config, NOT comparable to published "
            f"GPT-4o-reader numbers): {qa['correct']}/{qa['n']} = "
            f"{qa['accuracy']:.3f} accuracy ({qa['judge_errors']} judge errors)."
        )
    return "\n".join(lines)


def render_methodology(result: dict) -> str:
    meta = result["meta"]
    dataset = meta.get("dataset", {})
    cortex_version = meta.get("cortex_version", {})
    models = meta.get("models", {})
    # The actual recall config rows (top_k/token_budget/format per config
    # name) — NOT the score aggregates, which used to be mislabeled here
    # under the same "Config rows (verbatim)" heading.
    configs = meta.get("configs", recall.CONFIGS)

    sections = [
        "# LongMemEval Benchmark Methodology\n",
        f"Generated: {result['generated_at']}\n",
        "## What was run\n",
        f"- Dataset: `{json.dumps(dataset)}`",
        f"- Cortex version: `{json.dumps(cortex_version)}`",
        f"- Models: `{json.dumps(models)}`",
        f"- Config rows (verbatim): `{json.dumps(configs)}`\n",
        _METRIC_DEFINITIONS,
        "## The two rows explained\n",
        "- **defaults** — the product's stock-install recall settings "
        "(`top_k=3`, `token_budget=600`, `format=synthesized`). This is the "
        "honesty row: it's what a customer gets out of the box.",
        "- **bench** — `top_k=10`, `token_budget=10000`, `format=raw`. This is "
        "the comparable row; competitors also tune retrieval settings for "
        "their published benchmark numbers, and the defaults row's 600-token "
        "context measures the trim policy as much as retrieval.\n",
        "## Local-reader QA caveat\n",
        f"{_NOT_COMPARABLE} It is reported to show the full local pipeline "
        "works end to end, not as a head-to-head reader comparison. Reader "
        "context is date-prefixed from the ingest-time `lm_date` tags, so the "
        "reader sees the same temporal grounding a production agent would.\n",
        _KNOWN_LIMITATIONS,
        _REPRODUCTION,
    ]
    return "\n".join(sections)


def _fetch_json(url: str, timeout: float = 5.0) -> dict | str:
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return "unavailable"


def _load_meta(cortex_url: str, ollama_url: str, reader_model: str) -> dict:
    """Pin what was actually run: reader model (caller-supplied — it's a CLI
    flag, not discoverable from any service), embed model (one constant,
    EMBED_MODEL, so it can't say something different from what the bench
    compose file configures), and the recall config rows actually used
    (recall.CONFIGS) — so the published run record can't silently drift from
    the settings that produced it.
    """
    dataset_meta_path = DATA_DIR / "dataset_meta.json"
    dataset = (
        json.loads(dataset_meta_path.read_text(encoding="utf-8"))
        if dataset_meta_path.exists() else {}
    )
    cortex_version = _fetch_json(f"{cortex_url}/version")
    ollama_tags = _fetch_json(f"{ollama_url}/api/tags")
    return {
        "dataset": dataset,
        "cortex_version": cortex_version,
        "models": {"reader": reader_model, "embed": EMBED_MODEL},
        "ollama_tags": ollama_tags,
        "configs": recall.CONFIGS,
    }


def _load_ingest_errors() -> dict:
    """Best-effort read of work/ingest_errors.json (I3): absent or malformed
    file degrades to the zero/empty shape rather than failing the report."""
    path = WORK_DIR / "ingest_errors.json"
    if not path.exists():
        return {"ingest_errors": 0, "ingest_error_keys": []}
    try:
        errors = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"ingest_errors": 0, "ingest_error_keys": []}
    if not isinstance(errors, list):
        return {"ingest_errors": 0, "ingest_error_keys": []}
    return {"ingest_errors": len(errors), "ingest_error_keys": errors}


def _load_recall_per_question(config_name: str) -> list[dict]:
    """Per-question audit rows from work/recall_<config>.jsonl: question_id,
    hits' session_ids + ranks, and error. Best-effort — absent file or a
    malformed line degrades to skipping it, never raises."""
    path = WORK_DIR / f"recall_{config_name}.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        hits = rec.get("hits") or []
        rows.append({
            "question_id": rec.get("question_id"),
            "hits": [
                {"session_id": h.get("session_id"), "rank": i + 1}
                for i, h in enumerate(hits)
            ],
            "error": rec.get("error"),
        })
    return rows


def _load_qa_per_question() -> list[dict]:
    """Per-question audit rows from work/qa_bench.jsonl: question_id,
    verdict, judge_error, and the answer text (truncated to 200 chars to
    keep the artifact lean)."""
    path = WORK_DIR / "qa_bench.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        answer = rec.get("answer")
        rows.append({
            "question_id": rec.get("question_id"),
            "verdict": rec.get("verdict"),
            "judge_error": rec.get("judge_error"),
            "answer": answer[:200] if isinstance(answer, str) else answer,
        })
    return rows


def _load_per_question(config_names: list[str]) -> dict:
    per_question = {name: _load_recall_per_question(name) for name in config_names}
    per_question["qa"] = _load_qa_per_question()
    return per_question


def main() -> None:
    # Imported lazily to avoid a hard import-time dependency of report.py's
    # library functions on qa.py (only this CLI entry point needs the
    # default reader model name).
    from bench import qa as qa_module

    ap = argparse.ArgumentParser()
    ap.add_argument("--run-label", required=True)
    ap.add_argument("--cortex-url", default="http://127.0.0.1:18100")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    ap.add_argument("--reader-model", default=qa_module.READER_MODEL)
    args = ap.parse_args()

    scores = {}
    for name in recall.CONFIGS:
        path = WORK_DIR / f"scores_{name}.json"
        if path.exists():
            scores[name] = json.loads(path.read_text(encoding="utf-8"))

    qa = None
    qa_path = WORK_DIR / "qa_bench.jsonl"
    if qa_path.exists():
        rows = [json.loads(line) for line in
                qa_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if rows:
            qa = qa_accuracy(rows)

    meta = _load_meta(args.cortex_url, args.ollama_url, args.reader_model)
    ingest_errors = _load_ingest_errors()
    per_question = _load_per_question(list(scores))
    result = build_result(
        scores, qa, meta, ingest_errors=ingest_errors, per_question=per_question)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_json = RESULTS_DIR / f"{timestamp}-{args.run_label}.json"
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")

    out_md = RESULTS_DIR / "METHODOLOGY.md"
    out_md.write_text(render_methodology(result), encoding="utf-8")

    print(render_markdown(result))
    print(f"-> {out_json}")
    print(f"-> {out_md}")


if __name__ == "__main__":
    main()
