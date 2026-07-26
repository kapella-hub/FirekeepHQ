# Memory Agent — Autonomous Knowledge Custodian

## Overview

A Celery periodic task (`memory_agent`) that runs every 6 hours, performing six autonomous analysis and repair passes against the knowledge corpus. Acts directly (custodian model), reports via webhooks, and uses LLM-synthesized merges with deterministic fallback.

## Architecture

### New Files
- `app/workers/memory_agent.py` — Celery task with six analysis passes
- `tests/test_memory_agent.py` — Tests for each analysis pass

### Modified Files
- `app/workers/sleep_cycle.py` — Register new beat schedule entry
- `app/webhooks.py` — Add new event types to `VALID_EVENT_TYPES`
- `app/config.py` — Add agent configuration fields
- `CLAUDE.md` — Document the new feature

## The Six Passes

Each pass runs sequentially within a single task execution. Each pass is a standalone function that can be tested independently.

### 1. Duplicate Detection & Merge

- Query Qdrant for all active memories (paginated scroll)
- Pairwise similarity scan: group memories with >0.9 similarity into clusters
- For each cluster:
  - **LLM merge (primary):** Send all duplicates to LLM, ask it to synthesize a single improved memory combining the best information
  - **Fallback:** If LLM unavailable or result is bad (too short, lost key info), keep the memory with highest confidence score, supersede the rest
  - Store merged memory via the same logic as `/learn` (graph + vector)
  - Supersede all originals pointing to the new merged memory
- Webhook: `agent.merged` — `{merged_into: id, superseded: [ids], method: "llm"|"fallback"}`

### 2. Orphan Cleanup

- Cypher query for nodes with degree 0 (no relationships at all)
- Also find Concept nodes connected only to a single deleted/archived memory
- Delete orphaned nodes from Neo4j
- Webhook: `agent.orphan_cleaned` — `{nodes_removed: [{label, name}]}`

### 3. Deep Contradiction Scan

- For each active memory, find similar memories (0.85-0.95 similarity) not yet linked by SUPERSEDES
- Filter to pairs that weren't caught at learn-time (different creation timestamps, different domains)
- Run through existing contradiction detection logic in `app/contradiction.py`
- Auto-supersede the stale side (older memory with lower confidence)
- Webhook: `agent.contradiction_found` — `{kept: id, superseded: id, similarity: float}`

### 4. Backlink Reinforcement

- Find memories with zero BACKLINK edges in Neo4j (pre-feature or missed)
- Run backlink discovery logic from `app/backlinks.py` (0.4-0.84 similarity range)
- Create missing bidirectional BACKLINK edges
- Webhook: `agent.backlinks_added` — `{memory_id: id, new_backlinks: int}`

### 5. Confidence Decay

- Query Qdrant for active memories where `last_confirmed_at` or `created_at` is older than N days (default 180)
- If confidence factor `(1 + confirmed) / (1 + contradicted)` is below 0.5: auto-deprecate via existing lifecycle logic
- Otherwise: decrement `confirmed_count` by 1 (floor at 0) to gradually reduce confidence
- Webhook: `agent.confidence_decayed` — `{memory_id: id, action: "deprecated"|"reduced", new_count: int}`

### 6. Cluster Coherence

- For each domain with 3+ memories, compute average pairwise similarity
- Flag memories more than 1 standard deviation below the domain mean
- For each flagged memory, compute similarity against centroids of all other domains
- If a better-fit domain is found (similarity > current domain similarity + 0.1 margin): move the memory
  - Update `domain` field in Qdrant payload
  - Update domain relationships in Neo4j
- Webhook: `agent.reclassified` — `{memory_id: id, from_domain: str, to_domain: str, similarity_improvement: float}`

## Webhook Events

All new event types, fired through existing webhook system in `app/webhooks.py`:

| Event | Payload |
|-------|---------|
| `agent.merged` | `{merged_into, superseded, method}` |
| `agent.orphan_cleaned` | `{nodes_removed}` |
| `agent.contradiction_found` | `{kept, superseded, similarity}` |
| `agent.backlinks_added` | `{memory_id, new_backlinks}` |
| `agent.confidence_decayed` | `{memory_id, action, new_count}` |
| `agent.reclassified` | `{memory_id, from_domain, to_domain, similarity_improvement}` |

## Configuration

New settings in `app/config.py`:

```python
AGENT_ENABLED: bool = True
AGENT_SCHEDULE_HOURS: int = 6
AGENT_DUPLICATE_THRESHOLD: float = 0.9
AGENT_CONFIDENCE_DECAY_DAYS: int = 180
AGENT_BATCH_LIMIT: int = 100
```

## Safeguards

- **Kill switch** — `AGENT_ENABLED=false` disables all passes
- **Batch limits** — Each pass processes at most `AGENT_BATCH_LIMIT` items per run (default 100)
- **Dry-run logging** — Every action is logged with full context before execution
- **Idempotency** — Re-running the same pass on unchanged data produces no new actions
- **Lock guard** — Redis SETNX lock (`firekeep:memory_agent:lock`) with TTL prevents overlapping runs
- **Per-pass error isolation** — If one pass fails, remaining passes still execute

## Implementation Notes

- Reuse existing modules: `app/contradiction.py` for contradiction logic, `app/backlinks.py` for backlink discovery, `app/lifecycle.py` for deprecation
- The agent needs direct access to the Qdrant and Neo4j clients (not via HTTP API) since it runs as a Celery worker
- LLM calls for merge synthesis use the same `httpx` client pattern as `sleep_cycle.py`
- All Neo4j writes wrapped in explicit transactions (same pattern as sleep cycle)
- Webhook firing reuses `app/webhooks.py:fire_webhook()` with Redis client from worker context
