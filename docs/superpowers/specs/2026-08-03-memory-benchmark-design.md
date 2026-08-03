# Memory Benchmark Harness — LongMemEval against the Cortex recall path

**Date:** 2026-08-03
**Status:** Approved design (approach A)
**Goal:** Publishable, reproducible LongMemEval-S numbers for Firekeep Cortex's recall
path, produced entirely locally (no cloud APIs), with methodology honest enough to
survive a competitor's scrutiny.

## Why

Every competitor we're compared against (Zep: 63.8% LongMemEval w/ GPT-4o reader;
Mem0: LoCoMo) publishes benchmark numbers. Firekeep has none. In procurement
conversations "what's your LongMemEval score" is question one, and today the answer
is silence. This harness closes the cheapest, highest-leverage credibility gap.

## Constraints

- **No cloud APIs.** All embedding, reading, and judging runs on local hardware
  (RTX 5080 16GB, Ollama). Models with a `:cloud` suffix are refused — they route to
  a third party.
- **Measure the product, not the code.** All traffic goes through the real REST
  surface (`POST /memory/learn`, `POST /memory/recall`) of a stock cortex container.
  No in-process shortcuts to `VectorClient`/`RAGEngine`.
- **Never touch team memory.** The benchmark runs against a fresh, isolated compose
  stack with its own Qdrant collection, Neo4j and Redis volumes. The VPS is never
  involved.

## What we measure

### Primary: reader-independent retrieval metrics (no LLM involved)

LongMemEval annotates, per question, which haystack sessions contain the evidence
(`answer_session_ids`). We stamp every ingested memory with its haystack session id
and score directly from the recall response's `sources[]`:

- **Evidence Recall@k** — fraction of questions where ≥1 evidence session appears in
  the top-k recalled sources.
- **Evidence Coverage@k** — fraction of each question's evidence sessions retrieved
  (matters for multi-session questions).
- **NDCG@k / MRR** — rank quality of evidence among the top-k.
- All broken down by LongMemEval question type (single-session-user,
  single-session-assistant, single-session-preference, multi-session,
  temporal-reasoning, knowledge-update). Abstention questions (`*_abs`) are excluded
  from retrieval metrics (they have no evidence to retrieve).

These are deterministic, free, and isolate the layer Firekeep actually owns.
Published LongMemEval scores conflate "did the memory system find the evidence" with
"did GPT-4o reason over it"; Evidence Recall@k cannot be discounted by reader choice.

### Secondary: local-reader QA accuracy (clearly labeled non-comparable)

A local reader model answers each question from the recalled context; a local judge
compares against gold answers using the LongMemEval judge prompts. Reported as
"local-reader condition — NOT comparable to published GPT-4o rows" in every artifact.
Reader/judge: the strongest text model that fits 16GB VRAM via Ollama (target:
`qwen3:14b`; pinned exactly in results metadata). Abstention questions are scored per
the benchmark's rules (credit for declining to answer).

## Benchmark and dataset

- **Round 1: LongMemEval-S** (~500 questions; each has a haystack of chat sessions
  totalling ~115k tokens, `haystack_dates`, question, gold answer, evidence session
  ids). Downloaded once from the official distribution (HuggingFace) into
  `benchmarks/memory/data/` (gitignored); the download script records the dataset
  revision/checksum into results metadata. Exact field names are verified at
  implementation time against the downloaded artifact — the plan must not hard-code
  them from memory.
- **LoCoMo** is explicitly out of scope for round 1 (follow-up; the harness's
  ingest/recall/score stages should not structurally preclude it).

## Architecture

```
benchmarks/memory/
├── README.md                  # how to run, hardware requirements
├── docker-compose.bench.yml   # isolated stack (cortex-api, qdrant, neo4j, redis)
├── requirements.txt           # harness-only deps (httpx, tqdm, ...)
├── download_dataset.py        # fetch + checksum LongMemEval-S
├── ingest.py                  # haystack → POST /memory/learn, resumable
├── recall.py                  # questions → POST /memory/recall, both configs
├── score_retrieval.py         # Recall@k / Coverage@k / NDCG / MRR from sources[]
├── qa_local.py                # local reader answers from recalled context
├── judge_local.py             # local judge vs gold answers
├── report.py                  # results/*.json → markdown table + METHODOLOGY.md
├── run_bench.py               # orchestrator: --limit N --config defaults|bench|both
│                              #   --skip-qa; stages are individually resumable
└── results/                   # committed: per-run JSON + generated methodology
```

The harness is plain Python scripts (stdlib + httpx + tqdm), NOT part of any shipped
wheel or service image. It follows the `symdex/benchmarks/` precedent: a sibling
`benchmarks/` tree with its own requirements file, runnable from a checkout.

### Isolated stack

`docker-compose.bench.yml` reuses the existing service images/Dockerfiles with a
benchmark env: `AUTH_ENABLED=false`, `GC_ENABLED=false`, `AGENT_ENABLED=false`,
`SKILL_SYNTHESIS_ENABLED=false`, `OWM_ENABLED=false`, dedicated volumes, dedicated
`QDRANT_COLLECTION=longmemeval`, ports offset so it can coexist with a dev stack.
Embedding via the HOST's GPU Ollama (`host.docker.internal:11434`),
`EMBEDDING_MODEL=mxbai-embed-large` (the repo default, 1024-dim). Background
mutation passes are disabled so results are a function of the write/read path, not
of when a GC tick happened; the **synchronous** write path (contradiction detection,
auto-supersession) stays ON because it is part of the product's learn behavior —
the methodology documents this.

### Ingest strategy

- One `POST /memory/learn` per **turn-pair** (user turn + following assistant turn),
  the closest analogue to how agents write memories in production, and small enough
  that `EMBED_MAX_CHARS=2000` rarely truncates.
- `namespace = question id` (normalized) — each question's haystack is its own
  isolated memory space, matching how the benchmark defines its haystacks.
- Each memory carries its **haystack session id** and the session's **date** (from
  `haystack_dates`) so retrieval scoring can join recalled sources back to evidence
  sessions. Primary mechanism: tags/metadata on the learn call, read back from
  `sources[].metadata`; if the API round-trip drops them, fallback is a structured
  one-line header inside the memory text (`[session:<id> date:<date>]`), which
  `score_retrieval.py` parses. The plan verifies which mechanism works and picks ONE.
- Note: `/memory/learn` stamps ingest time as the payload timestamp; haystack dates
  ride in content/metadata only. Type-specific decay therefore applies (near-)equally
  to all benchmark memories and does not confound ranking. Temporal-reasoning
  questions depend on dates in the text, same as production.
- Ingest is **resumable and idempotent**: a per-(question, session, turn-pair)
  ledger on disk; re-running skips completed work. A full ingest is an hours-scale
  GPU job and must survive interruption.

### Recall configurations (reported side by side)

| Row | top_k | format | token budget | synthesis |
|-----|-------|--------|--------------|-----------|
| **product defaults** | 3 | synthesized | 600 | on |
| **benchmark config** | 10 | raw | effectively unlimited (per-request param) | off |

Retrieval metrics are computed for BOTH rows (cheap). QA runs only on the benchmark
config row (the defaults row's 600-token context would measure the trim policy, not
retrieval; one sentence in the methodology says exactly that). If the REST recall
body cannot express a per-request token budget / top_k override, the harness runs
the stack twice with different env — a plan-time decision, but per-request params
are preferred.

The defaults row exists for honesty (it's what a stock install does); the benchmark
config row is the comparable one — competitors also run tuned retrieval settings.

## Outputs

- `results/<timestamp>-<git_sha>.json` — full run record: dataset revision, cortex
  git SHA + `/version` output, exact model tags (embed/reader/judge), every config
  value, per-question rows (retrieved session ids, ranks, QA answer, judge verdict),
  aggregate metrics.
- `results/METHODOLOGY.md` — generated: what was run, both metric families, the
  non-comparability caveat for local-reader QA, the defaults-vs-bench distinction,
  known limitations (below). This is the publishable artifact.

## Error handling

- Any stage failure leaves the ledger consistent; re-run resumes.
- A learn/recall HTTP failure retries with backoff (bounded); persistent failure
  records the question as `errored` in results rather than aborting the run —
  aggregate metrics report the errored count, never silently shrink the denominator.
- The reader/judge refusing or emitting unparseable output marks that question
  `judge_error` (counted, reported), one retry.
- Pre-flight checks before any ingest: stack healthy (`/health`), embed model
  present, reader model present, dataset checksum matches, disk headroom.

## Testing

- **Unit tests (CI-runnable, no stack, no GPU):** scoring math on fixture data
  (known ranks → known NDCG/MRR/Recall@k), ledger resume logic, judge-output
  parsing, session-id join incl. the fallback header parse. Live under
  `benchmarks/memory/tests/`.
- **Smoke run (manual, documented in README):** `run_bench.py --limit 2` against the
  bench stack end-to-end before any full run.
- The full 500-question run is an operator action, not a test.

## Known limitations (stated up front in METHODOLOGY.md)

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

## Out of scope

- LoCoMo (round 2), cloud-reader comparability runs (needs API access), CI
  integration of the full run, any change to Cortex itself. If the benchmark
  exposes recall-path bugs, those are separate follow-up tasks.
