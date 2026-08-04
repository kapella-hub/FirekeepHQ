# Dreaming — automated memory consolidation and person profiles

**Date:** 2026-08-04
**Status:** Design (supersedes the pre-risk-hunt sketch)
**Scope:** Cortex server-side. Round 1 = **additive dreams, no archival** (see "The archival decision").

## What it is

An automated Celery pass that runs when the stack is idle and does two things:

- **Consolidation** — clusters older episodic memories that belong together and has the local LLM
  synthesize each cluster into a durable insight, linked by provenance to the episodes it came from.
- **Person profiles** — maintains one continuously-updated profile per human, surfaced in the session
  briefing, so each session starts already knowing who it is working with and how they work.

Both run unattended. No human invocation, no review queue (per the decision board: auto-approve with
rails). This document is what the rails actually are, after an adversarial audit of the integration.

## Ground truth this design is built on

Measured on the live VPS 2026-08-04, not assumed:

| Fact | Value | Consequence for the design |
|---|---|---|
| Total points | 4,327 | — |
| **Active** memories | **538** (87% are `superseded`) | The dreamable pool is hundreds, not thousands. Clustering is cheap. |
| Active by type | 272 `episodic`, 266 **no `memory_type` at all** | Filtering on `memory_type == "episodic"` silently ignores half the store. Candidate selection must treat a missing type as episodic-equivalent (matching `rag.py`'s decay fallback). |
| `member_id` on active | **uniform** (`member-7312be8f…`) | **Profiles key on `member_id`, not `agent_id`.** |
| `agent_id` on active | **7 distinct values for one human** (`agent-marat_pc-5a60`, `Oganesyan, Marat`, `Marat`, `mogan`, `unknown`, `default`, `legacy-pre-team-continuity`) | Keying profiles on `agent_id` would build seven partial profiles of the same person. This was the design's biggest latent flaw. |
| `project` on active | set on ~45% (`firekeep` 140, `nexusstack` 63, `timegrapher` 25, …; 297 unset) | Multi-project store. Clusters must not span projects; unset is its own bucket, not a wildcard. |
| Synthesis latency | **22.5s** per cluster (qwen3:4b, 4 vCPU) **with `think:false`** | Server-side CPU dreaming is viable. |
| Synthesis latency **without** `think:false` | **101s and returns EMPTY** | qwen3 under a JSON grammar burns its whole budget on blocked thinking. Non-negotiable requirement, pinned by a test. |

## The archival decision (changed from the original sketch)

The original design synthesized a cluster and then **archived its constituent episodes**. The audit
found that archival is where nearly all the irreversible risk lives: no atomicity between insert and
archive, restore that overwrites `timestamp`, export/import that resurrects archived points as active,
graph rows that keep serving the content anyway, and archives that either purge later (delayed delete)
or never purge (unbounded growth).

**Round 1 is therefore additive: dreams are written, nothing is archived.** Consolidation still
delivers its primary value — a durable, retrievable insight that outranks scattered episodes — and we
learn whether dreams are any good *before* trusting them enough to remove their sources.

Archival becomes Round 2, gated on measured evidence from the LongMemEval harness (below). This is the
same discipline the benchmark work established today: measure before you claim, and never let an
automated process take an irreversible action on the strength of an unvalidated assumption.

## Architecture

New package `cortex/app/dreams/`:

| File | Responsibility |
|---|---|
| `config.py` | (fields live in `app/config.py`) — documented here for the flag list |
| `select.py` | Pure: candidate selection + partitioning + clustering. No I/O. |
| `synthesize.py` | The one LLM call: prompt, `think:false`, strict-JSON parse, repair, validation. |
| `store.py` | The dedicated write path (raw `PointStruct`, deterministic IDs, top-level provenance). |
| `profile.py` | Person-profile assembly, keyed by `member_id`. |
| `state.py` | Redis run-record + journal (`CollectorState` pattern). |
| `task.py` | The Celery task: gate → lock → one unit of work → record. |
| `api.py` | `GET /dreams` status endpoint. |

### Execution model — one unit of work per tick

**This is the fix for the worst operational finding.** The single worker runs
`--concurrency=1 --pool=solo` (`docker-compose.yml:437`), so a long task blocks *every* other periodic
task — including the 60s agent-gateway sweeper, which loses reconcile data while blocked.

So the dream task is **chunked**: beat fires every `DREAM_TICK_MINUTES` (default 5) and each invocation
processes **at most one cluster or one profile**, then returns. Maximum block ≈ one synthesis ≈ 25s.
Progress lives in Redis, so the pass advances across ticks and a crash costs one unit, not a run.
Hard `time_limit`/`soft_time_limit` on the task bound a hung LLM call.

### The gate (evaluated before any client is built)

1. `DREAM_ENABLED` (default **false** — opt-in, like every other new subsystem here).
2. Generation backend reachable — reuse `classifier._is_backend_unavailable` semantics; absent
   generation → status `unavailable`, never an error (the `corpus_only` precedent).
3. **Idle**: no memory *write* in the last `DREAM_IDLE_MINUTES` (default 30). Deliberately **not**
   Bridge "active sessions" (never returns to idle after a crashed agent) and **not** Relay presence
   (its stored `status` field is a permanent lie — audit finding). A write-recency counter is cheap,
   self-correcting, and cannot get stuck.
4. **Work available**: ≥ `DREAM_MIN_NEW_MEMORIES` (default 25) new *non-dream* memories since the last
   completed run. Counting only non-dream writes is what stops the pass from feeding itself
   (audit: an activity gate on any store-level counter is satisfied by dreaming's own output).
5. Redis `SETNX` lock, TTL `DREAM_LOCK_TTL_SECONDS`.

### Candidate selection and clustering (`select.py`, pure)

Candidates: `status == "active"`, `source != "corpus"`, `memory_type` in `{episodic, MISSING}`,
`source != "dream"` (no dream-of-a-dream), `confirmed_count == 0` (never consolidate a
human-confirmed memory), age ≥ `DREAM_MIN_AGE_DAYS` (2), and **not** already consolidated.

OWM as the credit signal: a memory whose `owm_efficacy` is below `DREAM_OWM_FLOOR` at
`owm_n >= OWM_PRIOR_N` is **excluded** — demonstrably misleading episodes do not deserve abstraction.
Symmetrically, a memory with a *proven* track record (efficacy above neutral at n ≥ prior) is also
excluded in round 1: it already earns its rank, and consolidating it would hand its position to a
memory with no history (audit finding — OWM evidence is not transferable).

**Partition before clustering, never after**: bucket by `(workspace_id, namespace, project)`. These are
hard `must` filters in `VectorClient.search`, and `workspace_id` is a **tenancy boundary derived from
the verified principal** — a cluster that spans two workspaces and is written with one value is a
cross-tenant leak. A cluster must be homogeneous in all three or it is not a cluster; a heterogeneous
candidate set is refused and logged.

Within a bucket: greedy cosine clustering over vectors Qdrant already stores, `DREAM_MIN_CLUSTER=4`,
`DREAM_CLUSTER_THRESHOLD=0.72`, deterministic ordering (sorted by id) so runs are reproducible.

### Synthesis (`synthesize.py`)

One call per cluster, matching `classifier.py`'s conventions (httpx, `response_format` JSON,
`content or reasoning` fallback) **plus `think:false`**. Output is validated, not trusted:

- strict JSON, one retry on malformed, then the cluster is marked failed and skipped (visible, never
  retried forever);
- 1–3 insights, each ≤ `DREAM_MAX_INSIGHT_CHARS` (**800**, ~200 tokens). This cap is load-bearing:
  `RECALL_TOKEN_BUDGET=600` with `trim_to_budget` means one long memory eats the whole budget and
  collapses `top_k=3` to 2 results (audit finding);
- `source_indices` must reference real cluster members, or the insight is rejected.

**`memory_type` is `procedural`, never `reference`.** `reference` means *no age decay at all* —
permanent, unconditional rank immunity — which is precisely what an auto-approved, unreviewed,
LLM-generated memory must not get. `procedural`'s 180-day half-life means a dream that stops proving
useful fades on its own.

### The write path (`store.py`) — dedicated, never `/memory/learn`

Three audit findings force a dedicated writer:

1. **`/memory/learn` runs contradiction detection**, which auto-supersedes up to 4 live memories at
   0.85 cosine within the domain — and its `find_similar` filter checks status/namespace/domain but
   **not `confirmed_count`**. A dream is by construction a high-similarity summary of a neighbourhood,
   making it the single most likely input to trip this, *including over human-confirmed memories*. The
   "never touch confirmed memories" rail simply does not hold on that path.
2. **`VectorClient.upsert` derives the point ID as `uuid5(text)`** and exposes no ID parameter. A dream
   whose text collides with an existing point inherits that point's lifecycle via `_merge_lifecycle` —
   including being **born archived**. It also makes "one continuously-updated profile" impossible:
   every revision becomes a new live near-duplicate.
3. **`upsert` puts unrecognised metadata in a nested dict**, while scroll/filter read top-level — so a
   `source="dream"` written that way is invisible to the very filter meant to prevent dream-of-a-dream.

So dreams are written with a raw `PointStruct` (the precedent already exists in
`skills/synthesizer.py`), with:

- **deterministic IDs**: `uuid5(DREAM_NS, cluster_key)` for insights, `uuid5(DREAM_NS,
  f"profile::{member_id}::{workspace_id}")` for profiles — so re-dreaming *updates in place*;
- **top-level** provenance: `source="dream"`, `dream_run_id`, `dreamed_from=[ids]`, plus the promoted
  keys (`agent_id`, `project`, `workspace_id`, `member_id`, `namespace`) copied from the cluster;
- `memory_type` written **both** top-level and nested (recall and GC read it from different places —
  audit finding);
- contradiction detection **not** invoked.

A shared `VectorClient.upsert_point(point_id, text, payload)` is added so this is the second, not the
third, copy of the raw-PointStruct workaround.

### Person profiles (`profile.py`)

One pinned profile per **`member_id`** (the ground-truth finding — `agent_id` is fragmented seven ways
for one human). Assembled from that member's recent memories, session outcomes and correction patterns;
synthesized by the same LLM path; written to the deterministic profile ID so it updates rather than
accumulates. Never archived, GC-protected like skills.

**Surfaced in the briefing** as a new `profile` section in `briefing/api.py`'s `builders` dict —
in-process, inside the 2.0s per-section budget, degrading to `status: empty` (never `unavailable`, which
would flip the whole envelope to `degraded`). That is the loop closing: work → memories → nightly
dream → next session opens already knowing you.

### Required changes to existing code (not additive)

These are audit findings that dreaming cannot be safe without:

1. `contradiction.find_similar` — add the `confirmed_count == 0` guard that GC already has. This is a
   **pre-existing defect** in its own right: today any `/memory/learn` can supersede a human-confirmed
   memory.
2. `memory_agent`'s `duplicate_detection_pass` and `deep_contradiction_pass` — skip `source == "dream"`
   points. Otherwise the 6-hourly pass merges two dreams, or supersedes a dream with a source, with no
   dream code involved at all.
3. `_projected_metadata` — include `memory_type` so recall and GC stop reading it from different places.

## Measurement — the honesty gate

The audit's sharpest finding: **the existing eval surface cannot detect a dreaming regression.**
`_memory_freshness_at_recall` averages `RecallResponse.score`, which is `max(sources[].score)` after
min-max normalisation — i.e. pinned to 1.0 by construction.

So the gate is the **LongMemEval-S harness built today** (`benchmarks/memory/`): run the full 500-question
benchmark before and after a dreaming pass over the same store, and compare Evidence Recall@k /
Coverage@k / NDCG. Dreaming ships enabled only if retrieval quality is **not worse**; it earns the
"recall improves as the store grows" claim only if it is measurably better. Round 2 (archival) requires
this evidence.

## Configuration

| Var | Default | Purpose |
|---|---|---|
| `DREAM_ENABLED` | `false` | Master switch (opt-in) |
| `DREAM_TICK_MINUTES` | `5` | Beat interval; one unit of work per tick |
| `DREAM_IDLE_MINUTES` | `30` | No memory write for this long ⇒ idle |
| `DREAM_MIN_NEW_MEMORIES` | `25` | Non-dream writes required since last run |
| `DREAM_MIN_AGE_DAYS` | `2` | Never dream fresh working memory |
| `DREAM_MIN_CLUSTER` | `4` | Smallest consolidatable cluster |
| `DREAM_CLUSTER_THRESHOLD` | `0.72` | Cosine floor for cluster membership |
| `DREAM_MAX_CLUSTERS_PER_RUN` | `20` | Bounds a run |
| `DREAM_MAX_INSIGHT_CHARS` | `800` | Keeps one dream from eating `RECALL_TOKEN_BUDGET` |
| `DREAM_OWM_FLOOR` | `0.35` | Below this (at n≥prior) a memory is excluded |
| `DREAM_SYNTH_TIMEOUT_SECONDS` | `120` | Bounds one synthesis |
| `DREAM_LOCK_TTL_SECONDS` | `1800` | Redis SETNX lock TTL |
| `DREAM_PROFILES_ENABLED` | `true` | Sub-flag for feature B |

Wiring per the repo's Change Consistency Checklist: `config.py`, `sleep_cycle.py` (include + beat),
`main.py` (router mount), `docker-compose.yml` (**both** `cortex-worker` and `cortex-beat`),
`dashboard/index.html`, `CLAUDE.md`.

## Testing

Pure functions (selection, partitioning, clustering, gating, validation) unit-test directly. Beyond that,
tests that pin the audit findings — each is a test that fails if the defect returns:

- a dream is never written through `/memory/learn` (no contradiction call);
- deterministic IDs: two consecutive profile updates leave **exactly one** active profile point;
- clusters never span `workspace_id` / `namespace` / `project`;
- `think:false` is present on every synthesis call;
- `memory_type` is never `reference`;
- insights over the char cap are rejected;
- confirmed memories are never selected;
- dream points are excluded from candidate selection (no dream-of-a-dream);
- the activity gate ignores dream-authored writes;
- one tick processes at most one unit of work;
- `find_similar` skips confirmed memories (the pre-existing-defect fix).

## Deliberately out of scope for round 1

Archival of constituents (round 2, gated on measurement); the GPU/hybrid client-side drain; a dashboard
tab; graph-side (Neo4j) consolidation — archiving a vector does not remove its content from the graph
leg, so round 1 does not pretend to reduce graph noise; SSE recall parity (`recall_streaming` applies no
lifecycle/OWM multipliers, so it is excluded from any dreaming A/B claim).
