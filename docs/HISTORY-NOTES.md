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
