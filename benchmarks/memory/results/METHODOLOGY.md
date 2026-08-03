# LongMemEval Benchmark Methodology

Generated: 2026-08-03T16:28:44.456041+00:00

## What was run

- Dataset: `{"sha256": "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442", "source": "hf://xiaowu0162/longmemeval-cleaned/longmemeval_s_cleaned.json", "questions": 500, "fetched_at": "2026-08-03T16:25:00.208798+00:00"}`
- Cortex version: `{"version": "0.6.0", "git_sha": "unknown", "build_time": "unknown"}`
- Models: `[{"name": "mxbai-embed-large:latest", "model": "mxbai-embed-large:latest", "modified_at": "2026-08-03T10:26:52.9309599-06:00", "size": 669615493, "digest": "468836162de7f81e041c43663fedbbba921dcea9b9fefea135685a39b2d83dd8", "details": {"parent_model": "", "format": "gguf", "family": "bert", "families": ["bert"], "parameter_size": "334M", "quantization_level": "F16"}}, {"name": "qwen3:14b", "model": "qwen3:14b", "modified_at": "2026-08-03T10:26:49.5963634-06:00", "size": 9276198565, "digest": "bdbd181c33f2ed1b31c972991882db3cf4d192569092138a7d29e973cd9debe8", "details": {"parent_model": "", "format": "gguf", "family": "qwen3", "families": ["qwen3"], "parameter_size": "14.8B", "quantization_level": "Q4_K_M"}}, {"name": "qwen3-vl:8b", "model": "qwen3-vl:8b", "modified_at": "2026-03-03T21:06:21.7750884-07:00", "size": 6140415879, "digest": "901cae73216286ea8c5aba8b46d307ff7188f737285ec500c795a12f05225d28", "details": {"parent_model": "", "format": "gguf", "family": "qwen3vl", "families": ["qwen3vl"], "parameter_size": "8.8B", "quantization_level": "Q4_K_M"}}, {"name": "minimax-m2:cloud", "model": "minimax-m2:cloud", "remote_model": "minimax-m2", "remote_host": "https://ollama.com:443", "modified_at": "2026-03-03T21:04:36.387563-07:00", "size": 382, "digest": "698ab6d56142621ba4c208e06cb65004035556a51c01dbd7d7bfca16af0e94c1", "details": {"parent_model": "", "format": "", "family": "minimaxm2", "families": ["minimaxm2"], "parameter_size": "230B", "quantization_level": "FP8"}}, {"name": "qwen3:30b", "model": "qwen3:30b", "modified_at": "2026-03-03T21:00:05.3801944-07:00", "size": 18556699314, "digest": "ad815644918f0eaab341c12b67837cc6dd4562342cdaf118f83d5d554cb37226", "details": {"parent_model": "", "format": "gguf", "family": "qwen3moe", "families": ["qwen3moe"], "parameter_size": "30.5B", "quantization_level": "Q4_K_M"}}, {"name": "llama3:latest", "model": "llama3:latest", "modified_at": "2025-11-15T09:06:43.6409248-07:00", "size": 4661224676, "digest": "365c0bd3c000a25d28ddbf732fe1c6add414de7275464c4e4d1c3b5fcb5d8ad1", "details": {"parent_model": "", "format": "gguf", "family": "llama", "families": ["llama"], "parameter_size": "8.0B", "quantization_level": "Q4_0"}}, {"name": "gemma3:4b", "model": "gemma3:4b", "modified_at": "2025-11-15T09:05:03.8129864-07:00", "size": 3338801804, "digest": "a2af6cc3eb7fa8be8504abaf9b04e88f17a119ec3f04a3addf55f92841195f5a", "details": {"parent_model": "", "format": "gguf", "family": "gemma3", "families": ["gemma3"], "parameter_size": "4.3B", "quantization_level": "Q4_K_M"}}]`
- Config rows (verbatim): `{"defaults": {"k": 3, "overall": {"n": 2, "recall_at_k": 0.5, "coverage_at_k": 0.5, "mrr": 0.5, "ndcg_at_k": 0.23463936301137822}, "abstention_excluded": 0}, "bench": {"k": 10, "overall": {"n": 2, "recall_at_k": 1.0, "coverage_at_k": 1.0, "mrr": 0.5833333333333334, "ndcg_at_k": 0.20519578401166222}, "abstention_excluded": 0}}`

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

## Reproduction

```bash
cd benchmarks/memory
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m bench.download
docker compose -f docker-compose.bench.yml -p firekeep-bench up -d --build
.venv/Scripts/python -m bench.run --limit 2 --config both --run-label smoke
.venv/Scripts/python -m bench.run --config both --run-label full
```
