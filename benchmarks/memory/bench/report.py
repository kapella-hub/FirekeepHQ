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

from bench.common import DATA_DIR, RESULTS_DIR, WORK_DIR

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


def build_result(scores: dict[str, dict], qa: dict | None, meta: dict) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": meta,
        "retrieval": scores,
        "qa_local": qa,
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
    configs = {
        name: {k: v for k, v in scores.items()
               if k not in ("errored_questions", "missing_questions",
                            "by_question_type")}
        for name, scores in result["retrieval"].items()
    }

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


def _load_meta(cortex_url: str, ollama_url: str) -> dict:
    dataset_meta_path = DATA_DIR / "dataset_meta.json"
    dataset = (
        json.loads(dataset_meta_path.read_text(encoding="utf-8"))
        if dataset_meta_path.exists() else {}
    )
    cortex_version = _fetch_json(f"{cortex_url}/version")
    tags = _fetch_json(f"{ollama_url}/api/tags")
    models = tags if isinstance(tags, str) else tags.get("models", tags)
    return {"dataset": dataset, "cortex_version": cortex_version, "models": models}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-label", required=True)
    ap.add_argument("--cortex-url", default="http://127.0.0.1:18100")
    ap.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    args = ap.parse_args()

    scores = {}
    for name in ("defaults", "bench"):
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

    meta = _load_meta(args.cortex_url, args.ollama_url)
    result = build_result(scores, qa, meta)

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
