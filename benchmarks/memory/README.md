# LongMemEval Benchmark Harness

This harness measures Firekeep Cortex's memory recall against
[LongMemEval-S](https://github.com/xiaowu0162/LongMemEval): 500 questions, each
paired with a synthetic multi-session chat haystack, that probe whether a
memory system can retrieve the right evidence session(s) and answer correctly
days or weeks after the fact. It runs against a fully isolated Cortex stack —
own Docker project, own ports, own volumes — ingests each question's haystack
as turn-pair memories via `POST /memory/learn`, recalls against two recall
configs (the product's stock defaults, and a "bench" row tuned the way a
published benchmark number is tuned), scores four retrieval metrics per
question, and runs a local-model QA pass over the recalled context. Everything
is orchestrated by `bench.run`; see
`docs/superpowers/specs/2026-08-03-memory-benchmark-design.md` for the full
design rationale (why these two config rows, why LongMemEval, what's
deliberately out of scope).

## Hardware prerequisites

- Docker, to run the isolated bench stack (`docker-compose.bench.yml`:
  Neo4j, Qdrant, Redis, and a `cortex-api` built from the repo's own
  `cortex/Dockerfile`). It shares no ports, volumes, or project name with the
  root `docker-compose.yml` dev/prod stack.
- A GPU-backed [Ollama](https://ollama.com) reachable at
  `http://127.0.0.1:11434` on the host — the bench `cortex-api` container
  reaches it via `host.docker.internal` for embeddings, and the harness itself
  calls it directly for the local QA reader/judge. CPU-only Ollama works but
  makes the QA phase (see timings below) considerably slower.
- Two models pulled on the host: `ollama pull mxbai-embed-large` (1024-dim
  embeddings; this is also `EMBEDDING_MODEL` in the bench compose file — do
  not swap it without also changing `EMBEDDING_DIM` and rebuilding the Qdrant
  collection) and `ollama pull qwen3:14b` (local QA reader + judge). If
  `qwen3:14b` isn't in your Ollama library, pass any other local
  ~14B-or-under text model via `--reader-model`; the one hard constraint is
  that `:cloud`-suffixed models are refused (`qa.refuse_cloud`) — this
  benchmark is local-only by design.
- Roughly 10 GB of free disk: the LongMemEval-S dataset JSON is ~265 MB,
  `qwen3:14b` (Q4_K_M) is ~9.3 GB and `mxbai-embed-large` is ~670 MB, plus a
  few hundred MB for the bench stack's Neo4j/Qdrant/Redis volumes once ~500
  questions' worth of haystacks are ingested.

## Reproduction

Five commands, run from `benchmarks/memory`:

```bash
cd benchmarks/memory
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m bench.download
docker compose -f docker-compose.bench.yml -p firekeep-bench up -d --build
.venv/Scripts/python -m bench.run --limit 2 --config both --run-label smoke
.venv/Scripts/python -m bench.run --config both --run-label full
```

Notes on each step:

1. Creates the venv and installs `requirements.txt` (`huggingface_hub`,
   `httpx`, `tqdm`, and friends — see the file for the full pinned set).
2. `bench.download` fetches LongMemEval-S from HuggingFace
   (`xiaowu0162/longmemeval-cleaned`, file `longmemeval_s_cleaned.json`),
   verifies every row carries the keys the rest of the harness depends on,
   and writes `data/longmemeval_s.json` + `data/dataset_meta.json` (sha256,
   question count, fetch timestamp). The upstream location has already moved
   once (the original `xiaowu0162/longmemeval` repo now 404s) — if this
   command 404s again, check the LongMemEval GitHub README for the current
   location and update `HF_REPO`/`HF_FILE` in `bench/download.py`.
3. Brings up the isolated bench stack and builds the `cortex-api` image from
   this repo's own `cortex/Dockerfile`. Give it a minute on first run —
   `--build` and the Neo4j/Qdrant first-start both take longer than
   subsequent `up -d`s.
4. The smoke run: 2 questions through every stage (ingest → recall → score →
   QA → report) end to end. This is the gate — run it after any code change
   to the harness or to Cortex's recall path, before trusting a multi-hour
   full run to a change that might be broken. It finishes in under a minute
   on GPU Ollama.
5. The full run: all ~500 questions, no `--limit`. This is hours-scale (see
   below) — plan to run it unattended (e.g. overnight).

## Expected full-run duration

Measured on this harness's own smoke run (2 questions, GPU Ollama,
`qwen3:14b` reader):

| Stage | Smoke (2 questions) | Per-question | Extrapolated to 500 |
|---|---|---|---|
| Ingest (98 sessions / 521 `/memory/learn` calls) | 8.6s | ~4.3s | ~36 min |
| Recall (both configs combined) | ~1.9s | ~1.0s | ~8 min |
| Local QA (reader + judge, bench config only) | 26.4s | ~13.2s | ~110 min |

QA dominates because it makes two sequential `qwen3:14b` calls per question
(answer, then judge) — that's a property of the reader model and hardware,
not the retrieval path being measured. Total: expect a full run on comparable
hardware to take **on the order of 2.5–3 hours**, mostly QA. If you only need
the retrieval comparison (the head-to-head claim the benchmark is really
for), pass `--skip-qa` — that removes the ~110-minute QA tail entirely and
leaves ingest + recall, well under an hour.

These numbers will vary with your Ollama hardware, the reader model you
choose, and disk/network speed for ingest; treat them as an order-of-magnitude
planning estimate, not a promise.

## Where results land

- `results/<timestamp>-<run-label>.json` — one immutable, publishable
  run-record per `bench.run` invocation: dataset provenance (sha256, source,
  question count), Cortex `/version`, a snapshot of the host's Ollama
  `/api/tags`, retrieval scores per config (overall and broken down by
  LongMemEval question type), and the local QA accuracy. `results/` is
  committed to git by design — it accumulates a history of runs rather than
  being overwritten.
- `results/METHODOLOGY.md` — regenerated from scratch on every run; defines
  every metric (Evidence Recall@k, Coverage@k, MRR, NDCG@k), explains the two
  config rows, states the local-QA-vs-published-GPT-4o caveat, and lists known
  limitations. Always reflects the most recent run, not necessarily the run
  you care about — the per-run JSON is the citable artifact.
- `work/` — intermediate, resumable state: `ingest_ledger.jsonl`,
  `recall_<config>.jsonl`, `scores_<config>.json`, `qa_bench.jsonl`.
  Gitignored. Safe to delete between runs you want fully independent of each
  other; deleting it forces a complete re-ingest and re-recall on the next
  `bench.run`.
- `data/` — the downloaded dataset and its metadata. Gitignored.
  `bench.download` is a no-op once `data/longmemeval_s.json` exists.

## Resumability

Every stage keeps its own on-disk state, and the answer to "something broke
partway through" is always: fix the underlying problem, then **re-run the
exact same `bench.run` command** — nothing already completed is repeated.

- **Ingest** maintains a ledger (`work/ingest_ledger.jsonl`) of sessions
  already ingested; a re-run skips them and only sends the ones still
  missing.
- **Recall** writes one line per finished question to
  `work/recall_<config>.jsonl` per config and skips question ids already
  present in that file.
- **QA** appends to `work/qa_bench.jsonl` and skips question ids already
  answered.
- **Scoring** and **report generation** are cheap, idempotent
  recomputations over whatever is currently on disk — they always run in
  full on every invocation, but finish in seconds regardless of dataset size.

One consequence worth knowing: because the smoke run and the full run both
read `data/longmemeval_s.json` in the same fixed order and key their ledgers
by session id (not by question index), the smoke run's `--limit 2` is a
literal prefix of the full run's 500 questions. Running the smoke test before
the full run means those first 2 questions' ingest/recall/QA state is already
present and gets skipped, not redone, when the full run reaches them — this
is expected and harmless, not stale data bleeding across runs.

## Methodology and metric definitions

`docs/superpowers/specs/2026-08-03-memory-benchmark-design.md` has the full
design spec. `results/METHODOLOGY.md`, regenerated by every run, has the
precise metric definitions, the rationale for the two recall config rows, and
the disclosed limitations (local-reader QA is not comparable to published
GPT-4o-reader numbers, the judge is the same model as the reader, LongMemEval
haystacks are synthetic and likely a floor rather than a ceiling for
Firekeep's actual production write path, and more) — read it before quoting
any number from `results/*.json` out of context.
