# Knowledge Lifecycle Management — Design Document

## Problem

FirekeepCortex stores memories indefinitely with no concept of currency. When knowledge changes (e.g., "migrated from MySQL to Postgres"), old memories persist at full weight alongside new ones, leading to stale or contradictory advice during recall.

## Solution

Add automated knowledge lifecycle management: memory states, supersession chains, contradiction detection, and confidence scoring.

## Data Model

### Qdrant Payload Fields (new)

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `status` | string | `"active"` | One of: active, superseded, deprecated, archived |
| `confirmed_count` | int | 0 | Times recalled and confirmed useful |
| `contradicted_count` | int | 0 | Times contradicted by newer knowledge |
| `last_confirmed_at` | string\|null | null | ISO timestamp of last confirmation |
| `superseded_by` | string\|null | null | Memory ID of the superseding entry |

### Neo4j Edge (new)

```
(newer_action)-[:SUPERSEDES {reason: "...", detected: "auto"|"manual", timestamp: "..."}]->(older_action)
```

## Recall Scoring

```
final_score = base_score * status_multiplier * confidence_factor

status_multiplier:
  active     = 1.0
  superseded = 0.5
  deprecated = 0.1
  archived   = 0.0 (excluded by default)

confidence_factor:
  (1 + confirmed_count) / (1 + contradicted_count)
```

## Contradiction Detection (automatic)

Triggered on every `/memory/learn` call:

1. Embed the new action+outcome text
2. Search Qdrant for top-3 similar active memories (score > 0.85, same domain + namespace)
3. If high-similarity match found with different outcome → auto-supersede old memory
4. Create `SUPERSEDES` edge in Neo4j
5. Update old memory's Qdrant payload: `status="superseded"`, `superseded_by=new_id`
6. Return supersession info in the learn response

## New Endpoints

### POST /memory/deprecate

Manually change memory status.

```json
{
  "memory_ids": ["id-1", "id-2"],
  "status": "deprecated",
  "reason": "No longer using this approach",
  "superseded_by": "id-3"  // optional
}
```

### POST /memory/confirm

Confirm memories are still valid — resets decay, bumps confidence.

```json
{
  "memory_ids": ["id-1", "id-2"]
}
```

### GET /memory/{memory_id}/history

Returns the supersession chain for a memory.

```json
{
  "memory_id": "id-2",
  "status": "superseded",
  "superseded_by": {"id": "id-3", "text": "..."},
  "supersedes": [{"id": "id-1", "text": "..."}],
  "confirmed_count": 5,
  "last_confirmed_at": "2026-03-01T..."
}
```

## Recall Changes

- `ContextQuery` gets `include_archived: bool = False`
- Qdrant search adds filter: `status != "archived"` (and `!= "deprecated"` unless score is very high)
- Superseded memories in results include `superseded_by` in metadata
- Context block shows `[SUPERSEDED]` or `[DEPRECATED]` labels

## Learn Response Changes

`LearnResponse` gets:
- `superseded: list[str]` — IDs of memories that were auto-superseded

## Files to Modify

- `app/models.py` — new request/response models, ContextQuery.include_archived
- `app/db/vector.py` — lifecycle payload fields, status filtering, confirm/deprecate methods
- `app/db/graph.py` — SUPERSEDES edge, history query
- `app/engine/rag.py` — lifecycle scoring in recall
- `app/lifecycle.py` (new) — contradiction detection, deprecate/confirm/history router
- `app/main.py` — wire lifecycle router, update learn endpoint
- Tests for all new functionality
