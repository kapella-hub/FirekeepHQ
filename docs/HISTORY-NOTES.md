# History Notes

Past events and removals, moved out of the per-session-loaded `CLAUDE.md` so it
states what IS, not what changed. Nothing here is load-bearing for current work —
it's context for "why is it like this?". Operational invariants stay in `CLAUDE.md`.

## Office cluster embedding backend (before 2026-07-14)

Before 2026-07-14 the office cluster had **no embedding backend at all**: every
`LLM_BASE_URL` call failed DNS, so knowledge ingest returned 500 and vector
recall ran graph-only. After the in-cluster `firekeep-ollama` image
(`granite-embedding:30m`, native 384-dim) was enabled, all existing vectors were
migrated in place via `POST /admin/embeddings/reembed`.

The invariant that outlived this (still in `CLAUDE.md`): never change the office
embedding model without a reembed pass, and never change the dim without a
collection rebuild. See also the v0.1.1–v0.1.6 chunked-ollama deploy saga, which
is recorded in FirekeepCortex memory (domain `firekeep-ops`).

## Corpus entity/relationship graph (removed 2026-05-27)

Corpus originally extracted a Neo4j entity/relationship graph from ingested
documents. Removed 2026-05-27: an audit found 0 entities had ever been extracted
in production, and ~500 LOC of write-only LLM extraction machinery was deleted.
Corpus now stores chunks in Qdrant only (discoverable via `memory_recall`).

## A2A JSON-RPC gateway + SSE streaming (removed)

The A2A surface originally included a JSON-RPC gateway and SSE streaming. Both
were removed after it was found that zero external callers ever connected; only
the `GET /.well-known/agent.json` discovery endpoint remains.

## ML recall ranker (removed 2026-07-17)

A GradientBoosting recall ranker (`RANKER_ENABLED`, `app/engine/ranker.py`,
scikit-learn) shipped but never functioned: feedback was captured to Qdrant
payloads, but the bridge turning those into training rows and calling `train()`
was never built, so the model was never created and `rerank()` always fell back
to score-sort. Removed along with its scikit-learn dependency. LLM re-ranking
(`RERANK_ENABLED`) and `/memory/feedback` capture (used by GC) are unaffected.

## Shadow delta measurement (2026-07-30) — the Phase C go/no-go gate

The lossless-token-reduction plan made Phase C (the shadow "residency contract",
i.e. `ctx_get_shadow(since=cursor)` returning a delta) conditional on a real
measurement, because a lossless delta must resend `scratch` in full (no per-entry
timestamp exists) and `proactive_memories` in full (replaced wholesale, not
appended) — so it was plausible that the delta could only omit the smaller half of
the document and would not be worth its blast radius.

Measured against 26 real sessions on a live Bridge with `tiktoken` (cl100k_base),
not `chars/4`. A delta was simulated at a cursor taken 75% of the way through each
session: decisions/progress/files filtered to entries at-or-after the high-water
mark, scratch and Relevant Past Experience resent in full, an unchanged plan
omitted, header always kept.

    total shadow tokens, 26 sessions      16,725
    delta                                 10,075
    saved                                  6,650   = 39.8%
    sessions >= 1000 tokens (7 of 26)              = 50.7%

Decomposition of the saving: **80% comes from filtering decision/progress/file
entries, only 20% from omitting an unchanged plan.** Scratchpad — the section that
cannot be filtered — is just **14.1%** of all shadow tokens, so the concern that it
would dominate and erase the saving is not borne out on this data. Verdict:
PROCEED.

Two caveats. This is one operator's 26 sessions on one machine, so like the symdex
12% figure it is an in-sample number, not a prediction for another team's usage
pattern. And the saving is worst exactly where an agent has dumped a large report
into a scratch value: the session with 33% of its shadow in Scratchpad saved only
12.2%.

**A first pass measured 0.4% and would have cancelled Phase C.** That was a bug in
the measurement, not in the design: it split sections on every `### ` line, but
agent-authored scratch content contains its own markdown headings, so ~2,000 tokens
of one session's scratch value were mis-attributed to phantom top-level sections
and counted as unfilterable. `assemble_shadow` emits a FIXED set of section names
(Plan, Decisions, Files Known, Progress, Scratchpad, Relevant Past Experience) —
anything else that looks like a heading is content. Recorded because the wrong
number is easy to reproduce and would have killed a feature that measures 39.8%.
