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
- Disk space: the harness's preflight check (`bench.run.preflight`) only
  requires at least 5 GB free on the drive holding `work/`. Budget for it
  separately from the model/dataset downloads and Docker's own volumes,
  which the check does not measure and which may live on an entirely
  different drive (e.g. Docker Desktop's WSL2 virtual disk on Windows):
  the LongMemEval-S dataset JSON is ~265 MB, `qwen3:14b` (Q4_K_M) is ~9.3 GB,
  `mxbai-embed-large` is ~670 MB, and the bench stack's Neo4j/Qdrant/Redis
  volumes add a few hundred MB once ~500 questions' worth of haystacks are
  ingested.

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
   `httpx`, `tqdm`, and friends — see the file for the full set of version
   ranges; unlike the service Dockerfiles' `requirements.lock`, this harness
   is not hash-pinned).
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

- `results/<timestamp>-<run-label>-<digest>.json` — one immutable, publishable
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
- `work/` — intermediate, resumable state. Gitignored. Two tiers, and the
  split is load-bearing:
  - `work/ingest_ledger.jsonl` (plus `work/ingest_errors.json`) is **shared
    across every run label**. The ingested store is the fixture under test:
    both legs of an A/B must measure the same corpus, and ingest is idempotent
    by ledger, so re-ingesting per label would cost ~10 minutes *and* destroy
    the comparison.
  - `work/<run-label>-<digest>/` holds everything a single leg owns —
    `recall_<config>.jsonl`, `scores_<config>.json`, `recall_counts.json`,
    `qa_bench.jsonl`. These resume by question id, so scoping them by label is
    what keeps a resumed leg cheap while making it impossible for one label's
    rows to be silently reused by another. The directory name is the label
    with anything path-unsafe replaced by `_`, plus a short digest of the raw
    label: the replacement alone is many-to-one (`post dream`, `post/dream`
    and `post_dream` all cleaned to `post_dream`, as did any two labels
    agreeing on their first 100 characters), and two labels sharing a
    directory share a `recall_<config>.jsonl` — which is the cross-label
    resume leak this layout exists to prevent.

  Safe to delete a label's directory to force that leg to re-recall from
  scratch; deleting all of `work/` additionally forces a complete re-ingest.
- `data/` — the downloaded dataset and its metadata. Gitignored.
  `bench.download` is a no-op once `data/longmemeval_s.json` exists.

## Resumability

Every stage keeps its own on-disk state, and the answer to "something broke
partway through" is always: fix the underlying problem, then **re-run the
exact same `bench.run` command** — nothing already completed is repeated.

- **Ingest** maintains a ledger (`work/ingest_ledger.jsonl`) of sessions
  already ingested; a re-run skips them and only sends the ones still
  missing. This ledger is shared by every run label, deliberately.
- **Recall** writes one line per finished question to
  `work/<run-label>-<digest>/recall_<config>.jsonl` per config and skips question ids
  already present in that file. **Changing `--run-label` therefore forces a
  full re-recall** — that is the point: the resume set is keyed by question
  id, so an unscoped file made a second label skip all 500 recalls and
  re-score the first label's rows.
- **QA** appends to `work/<run-label>-<digest>/qa_bench.jsonl` and skips question ids
  already answered.
- **Scoring** and **report generation** are cheap, idempotent
  recomputations over whatever is currently on disk — they always run in
  full on every invocation, but finish in seconds regardless of dataset size.

One consequence worth knowing: because the smoke run and the full run both
read `data/longmemeval_s.json` in the same fixed order and key their ledgers
by session id (not by question index), the smoke run's `--limit 2` is a
literal prefix of the full run's 500 questions. Running the smoke test before
the full run means those first 2 questions' **ingest** state is already
present and gets skipped, not redone, when the full run reaches them — this
is expected and harmless, not stale data bleeding across runs. Their recall
and QA state is only reused if the full run uses the *same* `--run-label`
(the reproduction commands above use `smoke` and `full`, so it does not).

If you have a `work/` from before recall artefacts were label-scoped, you
will see unscoped `recall_*.jsonl` / `scores_*.json` / `qa_*.jsonl` files
sitting directly in `work/`. `bench.run` names them at startup and **never
reads, moves or deletes them** — which run produced them is unknowable, and
guessing is the bug this layout exists to prevent. To resume one under a
label, move it into `work/<run-label>-<digest>/` yourself (run the leg once to
see the directory name it prints), and only if you are certain
that label produced it.

## Methodology and metric definitions

`docs/superpowers/specs/2026-08-03-memory-benchmark-design.md` has the full
design spec. `results/METHODOLOGY.md`, regenerated by every run, has the
precise metric definitions, the rationale for the two recall config rows, and
the disclosed limitations (local-reader QA is not comparable to published
GPT-4o-reader numbers, the judge is the same model as the reader, LongMemEval
haystacks are synthetic and likely a floor rather than a ceiling for
Firekeep's actual production write path, and more) — read it before quoting
any number from `results/*.json` out of context.

## Dreaming A/B: the regression gate

**The rule: Dreaming ships enabled only on a measured non-regression against
this benchmark.** No other signal in the stack can make that call. Cortex's
existing auto-eval, `_memory_freshness_at_recall`, averages
`RecallResponse.score`, which is `max(sources[].score)` after min-max
normalisation — it is pinned to 1.0 by construction and cannot detect a
retrieval regression no matter how badly Dreaming degrades recall. This
LongMemEval-S harness is the only instrument in the repo that measures actual
retrieval quality end to end, which is why it is the gate.

`DREAM_ENABLED` defaults to `false` (`cortex/app/config.py`), so a completely
ordinary full run of this harness against an unmodified stack **is** the
"before" measurement — no special setup needed for that half.

### Running the A/B

The two legs **must** use different `--run-label`s, and that is now sufficient
as well as necessary: recall rows, scores and QA answers live under
`work/<run-label>-<digest>/`, so the "after" leg cannot resume the "before"
leg's rows.
Expect the after leg to take as long as the before leg did — a label change
forces a full re-recall by design. A post-dream leg that finishes in seconds
did not run; check the `completed=` counts the recall stage prints. Re-running
the SAME label is also a no-op — `bench.dream_ab` detects and refuses that
(see "The positive control" below), but it is cheaper to notice here.

```bash
cd benchmarks/memory
# 1. Full benchmark with dreaming off (the default) — this is "before".
.venv/Scripts/python -m bench.run --config both --run-label pre-dream
#    -> results/<timestamp>-pre-dream-<digest>.json
#    -> work/pre-dream-<digest>/{recall_*.jsonl,scores_*.json,recall_counts.json}

# 2. Enable dreaming: add `DREAM_ENABLED: "true"` to cortex-api's
#    `environment:` block in docker-compose.bench.yml (it defaults false and
#    is absent there today), then recreate the container so it's picked up:
docker compose -f docker-compose.bench.yml -p firekeep-bench up -d --build cortex-api
#    Then let it accumulate real ticks — DREAM_TICK_MINUTES defaults to 5,
#    but a single tick is not representative; give it the same order of
#    magnitude of wall-clock time / session volume you expect a customer
#    deployment to reach before dreaming has had a chance to act on
#    (DREAM_MIN_NEW_MEMORIES=25, DREAM_MIN_AGE_DAYS=2 by default — a fresh
#    bench stack needs to clear both before the first cluster fires).

# 3. Full benchmark again, same dataset, same stack, DIFFERENT label — this
#    is "after". The label change forces a full re-recall; the shared ingest
#    ledger means the store itself is NOT re-ingested.
.venv/Scripts/python -m bench.run --config both --run-label post-dream
#    -> results/<timestamp>-post-dream-<digest>.json
#    -> work/post-dream-<digest>/{recall_*.jsonl,scores_*.json,recall_counts.json}

# 4. Compare.
.venv/Scripts/python -m bench.dream_ab \
  --before results/<timestamp>-pre-dream-<digest>.json \
  --after results/<timestamp>-post-dream-<digest>.json
```

`bench.dream_ab` runs **two independent gates** and exits non-zero if either
fires. First it prints one markdown table per recall config (`defaults` and
`bench`, whichever configs are common to both result files) showing the delta
on every aggregate metric, followed by a verdict line per config. Then it
prints an **Evidence displacement** section (see "Displacement analysis" at the
bottom of this file) — the aggregate metrics are means and cannot see a dream
taking a single top-k slot from real evidence. Finally it prints one overall
verdict naming which gate, if either, failed. It can gate CI or a release
checklist directly:

```bash
.venv/Scripts/python -m bench.dream_ab --before results/X.json --after results/Y.json \
  || { echo "dreaming regressed retrieval — do not ship DREAM_ENABLED=true"; exit 1; }
```

The **aggregate** gate flags `regressed=True` if **any** of Recall@k,
Coverage@k, or NDCG@k drops by more than `--tolerance` (default `0.005`)
between before and after — MRR is reported for visibility but does not gate. Comparisons that
are not apples-to-apples fail loudly instead of producing a number: a `k`
mismatch between the two runs, or a run missing its `overall` block, is
reported as an `ERROR:` verdict with `regressed=True` rather than a computed
(and meaningless) delta.

**The positive control.** Every run record carries, per recall config, a
`recall_counts` block recording what the recall stage actually executed:
`completed` / `errored` accumulate across resumed invocations of that label,
`skipped` and **`completed_last_invocation`** are the last invocation's only.

`completed_last_invocation` is the figure that gates, and the distinction is
load-bearing rather than pedantic. Re-running a label is a no-op — every
question is already on disk and skipped — so "run the leg, enable dreaming,
run the identical command again" (and `--run-label` defaults to `bench`, so
this is easy to do by accident) leaves a record whose cumulative `completed`
is still large while its final invocation measured nothing at all. Gating on
the last invocation separates that from a legitimate resume mechanically: a
resumed 4-hour leg's final invocation completes the questions that remained
(`> 0`), a no-op re-run completes `0`. Re-running a leg that had already
finished is likewise not evidence about the store as it stands now, and is
likewise refused.

So if the **after** run's last invocation recorded `completed: 0` it recalled
nothing, its scores are a re-score of artefacts an earlier invocation
produced, and `bench.dream_ab` refuses the comparison outright (`ERROR: …
completed=0`, `regressed=True`) rather than reporting the +0.0000 delta it
would otherwise compute. A run record from before these fields existed still
compares, but prints a `WARNING: UNVERIFIED …` line naming what it could not
check (no counts at all, or a cumulative count with no per-invocation one),
and — because it cannot be checked mechanically — an additional `WARNING:
SUSPECT …` if every metric is bit-identical. Warnings do not fail the gate (deterministic scoring means an
unchanged store legitimately produces identical numbers, so identity is
suspicious, not proof), but the final line reads `VERDICT: OK (WITH
WARNINGS)` so it cannot be missed.

`compare_runs(before, after)` is the pure comparison primitive: it takes a
single pair of `score_run` dicts (`work/scores_<config>.json`'s shape — one
recall config's `{"k": ..., "overall": {...}}`) and returns one verdict.
`compare_result_files(before, after)` is what the CLI actually calls — it
accepts full `results/<timestamp>-<label>-<digest>.json` run-records (or bare
`score_run`s) and compares every recall config present in both.

### Displacement analysis: the tail the aggregate gate cannot see

`bench.displacement` is the second gate, and it exists because the first one
demonstrably cannot do this job.

**What happened.** The first real Dreaming A/B ran at low dream density — 38
dream insights, 0.040% of a 96,123-point store — and the aggregate comparison
came back:

```
recall_at_k +0.0000  coverage_at_k +0.0000  mrr +0.0000  ndcg_at_k -0.0000  -> PASSES
```

A hand audit of the same two run records found something that table could not
show. Under the `bench` config (k=10), 2 of 500 result sets had changed, three
top-10 slots were now held by untagged (dream-shaped) points, and one question
— `031748ae_abs` — went from 9 evidence hits to 8. **A dream displaced real
evidence.** (The other changed question, `00ca467f`, was neutral: a dream took
a slot from a distractor session, which is the feature working.) Under
`defaults` (k=3) nothing changed at all — dreams never reached the top 3.

**Why the aggregate cannot see it, and why tightening `--tolerance` would not
help.** The two instruments measure different things. `compare_runs` watches
four *means* over ~470 questions; displacement is a *tail*. One lost evidence
hit moves `ndcg_at_k` by roughly 1e-5, four orders of magnitude below the
0.005 tolerance, so a design that leaks evidence at that rate passes the gate
indefinitely while getting worse. And the one question that lost evidence was
an abstention question (`*_abs`), which `score_run` excludes from every
aggregate — so that loss was not merely below tolerance, it was outside the
metric's scope entirely.

**What it measures**, per recall config, over every question the two runs have
in common (abstention included):

| measure | meaning |
|---|---|
| result sets changed | how many questions' ordered top-k session lists differ at all |
| evidence hits in top-k | slots holding a session in the question's `answer_session_ids`, before then after |
| questions that LOST / GAINED evidence | the event counts the means average away |
| net evidence delta | after minus before, summed over every compared question — reported, but deliberately **not** part of the gate; see below |
| best evidence rank shift | for evidence still in top-k on both sides: mean and worst movement of its best rank |
| questions whose best rank worsened by >= N | the tail of that, named question by question (`--rank-shift-warn`, default 3) |
| untagged slots (`session_id=None`) | slots whose hit carries no `lm_session:` tag |
| foreign-session slots | tagged hits naming a session outside the question's own haystack |
| 'after' leg recall provenance | whether the after leg's final invocation demonstrably executed its recalls |
| scored / abstention split | where the losses sit, since only the scored half reaches the metrics |

For every question that lost evidence it also prints **rank-level detail**: the
ranks that evidence session held before, and which of them the loss is
attributed to. Occurrences of one session are interchangeable — nothing in a
recall row says *which* memory left — so n lost occurrences are attributed to
the n deepest ranks, which is what a top-k truncation does. `ranks_before` is
printed in full so you can read it differently.

A note on the word "untagged". A hit's `session_id` comes from the ingest-time
`lm_session:` tag; a dream insight carries no such tag and so surfaces as
`session_id=null`. That was verified against the artefacts rather than assumed:
the pre-dream leg has **zero** untagged slots across 1019 + 5000 slots, the
post-dream leg has exactly three, and all three hold generalised insight prose.
But "untagged" is not a synonym for "dream" — `results/METHODOLOGY.md` already
documents graph-only hits taking rank slots the same way — so the counter is
named for what it observes, in the rendered table as well as here: the row
reads `untagged slots (session_id=None)` and carries a footnote saying in as
many words that it records a shape, not a cause. (It used to read `untagged
(dream-shaped) slots`, which asserted provenance the data cannot support to the
one audience that only ever sees the table.) `foreign_session_slots` sits
alongside it as a floor against a future dream that somehow inherited a tag; it
measures 0 on both legs.

**The gate rule.** It fires on **breadth of per-question loss**, and on nothing
else:

```
lost_evidence_questions >= max(--min-lost-questions,
                               ceil(--lost-question-rate * compared))
```

Defaults: `--min-lost-questions 3`, `--lost-question-rate 0.005`. On the
500-question split that is a threshold of **3 lost-evidence questions**. A
question is counted at most once, and only if *its own* evidence occupancy
fell.

**There is deliberately no `net_evidence_delta < 0` condition.** There used to
be, and it was a hole of exactly the kind this module exists to close: the net
is a global sum, so five questions each losing one evidence hit while one
unrelated question gained five came to a net of 0 and the gate stayed silent,
raising at most a `CHURN` warning. That is a mean wearing an event counter's
clothes. Displacement is a *tail*; summing across questions is the very
averaging that hid the defect in `compare_runs` one level up. Question B
gaining an evidence hit does not repair question A's answer — different query,
different user. Netting *within* one question is legitimate and is retained
(evidence session `e1` handing a slot to evidence session `e2` leaves that
question flat and is correctly not counted); netting *across* questions is not.
Gains are still reported, in their own column and in the `Gained evidence:`
detail, and when the gate fires with a non-negative net the verdict says so and
says why it is not a defence.

The floor is what stops breadth firing on noise. Retrieval scoring here is
deterministic, so any store mutation reshuffles something and single events are
expected. One lost question is one displacement event: real, reported in full,
but not a pattern — it cannot distinguish "dreams displace evidence" from "the
store changed". Two is still consistent with two unrelated one-offs. Three
independent questions losing evidence is the smallest count at which "pattern"
is a defensible word, and it is 3x the rate measured at the density that
produced the passing run — so the gate has honest headroom against today's
baseline while firing at roughly 3x that dream density. The rate is 0.5%
because `ceil(0.005 * 500) = 3`, so on this dataset it changes nothing; it
exists so that a 5000-question split does not inherit a fixed count of 3 and
become hypersensitive. The two are combined with `max`, which means
**tightening the gate requires lowering both knobs**.

**Rank degradation warns; it does not gate.** Evidence can also get worse
without leaving top-k: a hit moving from rank 1 to rank 9 inside k=10 kept
every occurrence it had, so `lost=0, net=0` and — before this was added — no
row anywhere in the report said anything had happened, while that question's
MRR went 1.00 → 0.11 and the aggregate moved by ~1e-3 over 500 questions. Same
tail-versus-mean blindness, one level further down. So the report now carries
the mean and worst best-rank shift for evidence present on both sides, plus a
`RANK DEGRADATION` warning naming every question whose best evidence rank
slipped by `--rank-shift-warn` (default 3) or more. It warns rather than gates
because a hit at rank 9 of 10 has *not* left the top-k the product spends, and
reordering is what dreams are for — an insight that legitimately outranks one
raw episode pushes everything below it down a slot. A gate firing on that would
fire on the feature working. Invisible, however, was never acceptable.

**The positive control.** `bench.displacement` runs the same zero-recall check
`bench.dream_ab` does, from the same implementation in `bench.common`: if the
'after' leg's final invocation completed 0 recalls it did not measure the
store, it re-scored rows an earlier invocation left on disk. That failure is
*worse* here than in the aggregate — two identical row sets compared slot by
slot yield a table of perfect zeroes and the most reassuring verdict this tool
can print — so such a pair is **refused**, with `VERDICT: NOT CERTIFIED` and a
non-zero exit, naming whether the recalls were skipped (a resume/run-label
problem) or errored (a backend problem). Provenance that is merely *absent* —
as in the two committed A/B records, which predate `recall_counts` — reports
`UNVERIFIED` and warns; it never passes silently. The provenance verdict is a
row in every rendered table.

Abstention questions are counted and they do gate, even though the aggregate
metrics exclude them. The displacement mechanism does not care which subset a
question belongs to, and a gate restricted to the scored subset would reproduce
exactly the blind spot that let this through. The scored/abstention split is
always reported so you can see where a loss sits.

**How to read the output.** `result sets changed` tells you whether dreaming
touched retrieval at all. `untagged slots` tells you whether dreams are
reaching top-k. Those two rising with a flat `net evidence delta` is the
healthy shape — dreams are being retrieved and are taking slots from
distractors. `questions that LOST evidence` is the number to watch, not the
net: it is reported with a `BELOW GATE` warning long before it reaches the
threshold, precisely so a rising trend is visible across successive A/Bs rather
than arriving as a surprise the day it crosses. Check the provenance row first
— a table full of zeroes from an `UNVERIFIED` leg is not a result — and read
`RANK DEGRADATION` even when nothing gated, since evidence sliding down the
page is the failure mode with no event count of its own.

**Running it standalone:**

```bash
.venv/Scripts/python -m bench.displacement \
  --before results/<timestamp>-pre-dream-<digest>.json \
  --after  results/<timestamp>-post-dream-<digest>.json
```

It prints the same markdown section `bench.dream_ab` embeds and exits non-zero
when the gate fires — or when it cannot run at all, since displacement is its
whole job. `--dataset`, `--min-lost-questions` and `--lost-question-rate` are
accepted by both tools; `bench.dream_ab` additionally takes `--no-displacement`
to skip the section (which then reports itself as skipped and downgrades the
final line to `OK (WITH WARNINGS)`).

**Where the evidence join comes from.** Deciding whether a slot held *evidence*
needs the question's `answer_session_ids`, which live only in the 265 MB
dataset — and `data/` is gitignored while `results/` is committed. So the
scoring step stamps a `displacement` block onto every score record
(`retrieval[<config>].displacement`) carrying the `answer_session_ids` map plus
that run's own `evidence_hits_at_k` / `untagged_slots_at_k` counts. Two records
written by a current harness are therefore analysable on a machine that never
downloaded LongMemEval-S, and the two headline counts are readable straight off
the JSON with no tooling at all. That block deliberately spans **every**
question including abstention ones, unlike the metrics beside it, for the
reason given above. Resolution order is: an explicit `--dataset` (the only
source that also enables foreign-session detection), then the records' stamped
block, then `data/longmemeval_s.json` if it happens to exist.

Two stamps that **disagree** about a question's evidence are refused, at both
scopes and with the same force: two configs of one record ("run record
disagrees with itself") and the two records of a pair ("the two run records
disagree"). The second used to be silent last-wins — a `before` stamping
`q1 -> [A]` and an `after` stamping `q1 -> [Z]` resolved to `[Z]` with no
error, which does not merely mislabel one side, it silently reclassifies which
slots held evidence on **both**. Records that disagree about what the evidence
*is* are not comparable.

**The ground-truth tests do not need the dataset.** The three tests pinning
this module to the hand audit used to be `skipif`'d on
`data/longmemeval_s.json`, which is gitignored and which no CI job fetches — so
the load-bearing guarantee ran only where someone happened to have downloaded
265 MB. The evidence join is the only part they needed it for, and that part is
32 KB: `tests/fixtures/ab_answer_session_ids.json` carries
`{question_id: answer_session_ids}` for all 500 questions — byte-for-byte the
map `displacement_facts` now stamps into new records — and a drift check
verifies it against the real dataset whenever the file *is* present. Only
`foreign_session_slots` still requires the dataset (it needs every question's
~48 haystack ids: 430 KB of fixture to assert a zero), and that single
assertion is split out and gated on its own. `benchmarks/memory/` also now has
a CI job (`benchmarks` in `.github/workflows/ci.yml`) — before this it had
none at all, and no workflow referenced `benchmarks/` in any form.

Records written **before** that block existed — including the two committed A/B
records — have no stamped evidence, so they need `data/` present or an explicit
`--dataset`. Inside `bench.dream_ab` that shortfall is a warning, not a
failure: it prints `DISPLACEMENT GATE DID NOT RUN: …` and the final line reads
`OK (WITH WARNINGS)`, because refusing to compare two published records on a
machine with no dataset would break the tool for its commonest use. A record
that *does* carry rows but cannot be compared (a `k` mismatch, a malformed row,
a question with no evidence entry) still fails loudly — that is a broken
comparison, not a missing capability, and `compare_displacement` returns a
verdict naming the cause rather than a number computed over a partial join.

`compare_displacement(before_rows, after_rows, evidence)` is the pure
primitive; `compare_displacement_files(before, after, evidence)` is what the
CLIs call, comparing every recall config with per-question rows in both
records.
