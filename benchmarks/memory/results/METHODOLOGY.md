# LongMemEval Benchmark Methodology

Generated: 2026-08-03T20:15:03.395736+00:00

## What was run

- Dataset: `{"sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442", "source": "hf://xiaowu0162/longmemeval-cleaned/longmemeval_s_cleaned.json", "questions": 500, "fetched_at": "2026-08-03T16:25:00.208798+00:00"}`
- Cortex version: `{"version": "9f94dd4-dirty", "git_sha": "9f94dd4", "build_time": "2026-08-03T16:52:51Z"}`
- Models: `{"reader": "qwen3:14b", "embed": "mxbai-embed-large"}`
- Config rows (verbatim): `{"defaults": {"top_k": 3, "token_budget": 600, "format": "synthesized"}, "bench": {"top_k": 10, "token_budget": 10000, "format": "raw"}}`

## Metric definitions

- A hit is *relevant* iff its `session_id` is in the question's `answer_session_ids`.
- **Evidence Recall@k** = 1 if any of the first k hits is relevant.
- **Evidence Coverage@k** = |distinct evidence sessions among first k hits| / |evidence sessions|.
- **MRR** = 1/rank of the first relevant hit (0 if none in top k).
- **NDCG@k**: binary gains, `DCG = Σ rel_i / log2(i+1)`; `IDCG` assumes the top `min(k, n_relevant_available)` slots are all relevant, where `n_relevant_available` = total memories ingested for that question's evidence sessions (from the ledger). Graph-only hits (`session_id=None`) count as non-relevant but occupy rank slots — that is deliberate: they consumed a top-k slot the product actually spent.
- Abstention questions (`*_abs`) and errored questions are excluded from aggregates and counted separately.

## The two rows explained

- **defaults** — the product's stock-install recall settings (`top_k=3`, `token_budget=600`, `format=synthesized`). This is the honesty row: it's what a customer gets out of the box.
- **bench** — `top_k=10`, `token_budget=10000`, `format=raw`. This is the comparable row; competitors also tune retrieval settings for their published benchmark numbers, and the defaults row's 600-token context measures the trim policy as much as retrieval.

## Local-reader QA caveat

The local-reader QA row is NOT comparable to published GPT-4o-reader numbers. It is reported to show the full local pipeline works end to end, not as a head-to-head reader comparison. Reader context is date-prefixed from the ingest-time `lm_date` tags, so the reader sees the same temporal grounding a production agent would.

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

## Reproduction

```bash
cd benchmarks/memory
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m bench.download
docker compose -f docker-compose.bench.yml -p firekeep-bench up -d --build
.venv/Scripts/python -m bench.run --limit 2 --config both --run-label smoke
.venv/Scripts/python -m bench.run --config both --run-label full
```
