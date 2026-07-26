# CortexBridge — Shadow Context Manager

## Overview

CortexBridge is a lightweight MCP server that maintains a live "shadow" of an AI agent's working state. When an LLM's context window gets compressed or a session restarts, the agent pulls its shadow context — a concise document containing its plan, progress, decisions, and file knowledge — and picks up exactly where it left off.

**Primary problem**: Mid-task amnesia. AI agents lose track of their plan, re-read files, contradict earlier decisions, and waste context re-establishing state after compression events.

**Primary consumer**: Claude Code (generalize to other agents later).

**Deployment**: VPS at `/opt/CortexBridge`, same server as FirekeepCortex. Reuses existing Redis and Ollama infrastructure.

## Core Concepts

### Session

A bounded unit of work (e.g., "implement namespace normalization"). Has a lifecycle: `active` → `paused` | `completed` | `abandoned`. Can span multiple Claude Code conversations. Each agent has at most one active session at a time, scoped by `agent_id`.

### Agent Identity

Every tool call includes an `agent_id` string (defaults to `"default"`). This scopes the active session pointer so multiple Claude Code instances (e.g., two terminal windows) don't clobber each other. The `agent_id` is a simple string — no auth, no registration.

### Shadow Context

A structured, always-current document assembled on-demand from a session's components. This is the single artifact an agent reads to restore its full working state after context loss. It answers three questions: *What am I doing? What do I know? What's left?*

### Shadow Context Components

| Component | Purpose | Write pattern | Max size |
|-----------|---------|---------------|----------|
| **Plan** | Ordered steps with completion status | Full replacement on each update | 10 KB |
| **Decisions** | Key choices made and rationale | Append-only, FIFO eviction | 50 entries |
| **File Map** | Files read/modified with compact summaries | Upsert by file path | 100 entries |
| **Progress** | What's done, what's in flight, what's next | Append-only, FIFO eviction | 50 entries |
| **Scratchpad** | Arbitrary key-value notes | Upsert by key | 50 entries |

When a component hits its limit, the oldest entries are evicted (FIFO). `ctx_update` returns the current count so the agent knows the state.

## MCP Tools

Seven tools exposed via Streamable HTTP MCP:

### `ctx_start_session`

Create a new session with a goal description.

**Input**: `{ goal: string, agent_id?: string, tags?: string[] }`
**Output**: `{ session_id: string, created_at: string }`
**Behavior**: If an active session already exists for this `agent_id`, it is automatically paused. Uses a Redis transaction (MULTI/EXEC) to atomically: create the session hash, set the active pointer, and add to the session index.

### `ctx_update`

Write a structured update to the active session's shadow context.

**Input**: `{ category: "plan" | "decision" | "file" | "progress" | "scratch", content: string, key?: string, agent_id?: string }`
**Output**: `{ status: "ok", component_count: int }`
**Behavior**:
- `plan`: Replaces the full plan text (Markdown checklist format). Rejects if >10 KB.
- `decision`: Appends a new decision entry. Evicts oldest if >50 entries.
- `file`: Upserts a file summary keyed by path. `key` = file path, `content` = summary. Agent always provides the summary. Evicts oldest if >100 entries.
- `progress`: Appends a progress log entry with auto-timestamp. Evicts oldest if >50 entries.
- `scratch`: Upserts a scratchpad entry. `key` = name, `content` = value. Evicts oldest if >50 entries.

### `ctx_get_shadow`

Returns the assembled shadow context document for a session.

**Input**: `{ session_id?: string, agent_id?: string }` (defaults to active session for agent)
**Output**: `{ session_id: string, goal: string, status: string, shadow: string }`
**Behavior**: Assembles all components into a Markdown document in fixed order: Plan, Decisions, Files Known, Progress, Scratchpad. This is the "restore my brain" call. Designed to be injected into an LLM prompt as-is.

### `ctx_complete_session`

Mark a session as completed. Triggers distillation to FirekeepCortex.

**Input**: `{ session_id?: string, outcome?: string, agent_id?: string }` (defaults to active session)
**Output**: `{ status: "completed", distillation: "success" | "failed", firekeep_memory_id?: string }`
**Behavior**: Atomically sets session status to `completed` and clears the active pointer. Distills the shadow context into an action/outcome pair and calls FirekeepCortex `/memory/learn`. Sets `distillation` field to indicate success or failure. If distillation fails, session is still marked completed — learnings can be retried or are recoverable from the shadow context before TTL expiry. Applies TTL to all session keys.

### `ctx_abandon_session`

Abandon a session without distillation.

**Input**: `{ session_id?: string, agent_id?: string }` (defaults to active session)
**Output**: `{ status: "abandoned" }`
**Behavior**: Sets session status to `abandoned`, clears the active pointer. No FirekeepCortex call. Applies TTL to all session keys for cleanup.

### `ctx_list_sessions`

List recent sessions.

**Input**: `{ status?: "active" | "paused" | "completed" | "abandoned", agent_id?: string, limit?: int }`
**Output**: `{ sessions: [{ session_id, goal, status, created_at, updated_at, agent_id }] }`

### `ctx_resume_session`

Resume a paused session.

**Input**: `{ session_id: string, agent_id?: string }`
**Output**: Same as `ctx_get_shadow` — returns the shadow context so the agent can immediately continue.
**Behavior**: Atomically sets session status back to `active` and updates the active pointer. Pauses any currently active session for this agent. Uses Redis transaction.

## Shadow Context Document Format

```markdown
## Session: implement namespace normalization
**Status**: active | **Started**: 2026-03-14T02:00:00Z | **Updated**: 2026-03-14T02:15:00Z

### Plan
- [x] Step 1: Add normalize_namespace function to models.py
- [x] Step 2: Add field validators to 3 request models
- [ ] Step 3: Fix broken tests
- [ ] Step 4: Run full test suite

### Decisions
- [02:05] Using field_validator not model_validator — simpler for single-field validation
- [02:08] Response models intentionally NOT normalized — they reflect stored values
- [02:10] strip() kept in normalize function for migration task (API regex blocks whitespace anyway)

### Files Known
- **app/models.py** — Added normalize_namespace() function. Added field_validator("namespace") to ContextQuery, ActionLog, GenericEventIngest. 274 lines.
- **tests/test_models.py** — 7 new normalization tests added to TestNamespaceValidation class. 2 existing tests updated for normalized assertions. 632 lines.

### Progress
- [02:06] Steps 1-2 complete, committed as 12b7200
- [02:12] Currently on step 3: fixing broken test assertions

### Scratchpad
- namespace_pattern: ^[a-zA-Z0-9_-]+$
- broken_test_line: 397
```

## Architecture

```
┌─────────────────────┐
│   Claude Code       │
│   (MCP Client)      │
│                     │
│  ctx_update()       │──────────┐
│  ctx_get_shadow()   │          │
│  ctx_start_session()│          │
└─────────────────────┘          │
                                 │ MCP (Streamable HTTP)
                                 │ Port 8070
                                 ▼
┌─────────────────────────────────────────┐
│         CortexBridge                     │
│         (FastAPI + MCP Server)           │
│                                          │
│  ┌──────────────┐  ┌─────────────────┐  │
│  │ Session Mgr  │  │ Shadow Assembler│  │
│  │ (CRUD, state)│  │ (Markdown gen)  │  │
│  └──────┬───────┘  └────────┬────────┘  │
│         │                    │           │
│  ┌──────▼────────────────────▼────────┐  │
│  │          Redis DB 3                │  │
│  │  Hash per session, sorted sets     │  │
│  └────────────────────────────────────┘  │
│                                          │
│  ┌──────────────┐                        │
│  │ Distiller    │                        │
│  │ (→ FirekeepCortex)                       │
│  └──────────────┘                        │
└──────────────────────────────────────────┘
         │
         ▼
┌──────────────┐
│ FirekeepCortex  │
│ /memory/learn│
└──────────────┘
```

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| API/MCP | FastAPI + fastmcp | Same stack as FirekeepCortex, proven pattern |
| Storage | Redis DB 3 (existing instance) | Sessions are ephemeral, Redis is fast, already running. DB 3 avoids collision with FirekeepCortex (DB 0), Celery broker (DB 1), and Celery results (DB 2). |
| MCP Protocol | Streamable HTTP on port 8070 | Same pattern as FirekeepCortex MCP on 8080 |
| Long-term persistence | FirekeepCortex API | Called on session completion to persist learnings |
| Containerization | Docker Compose (standalone) | Own compose file, connects to FirekeepCortex's Redis via network |

No Ollama dependency. The agent provides file summaries directly — it already has the file contents in context when it calls `ctx_update`. This eliminates a failure mode and keeps the service simple.

## Redis Data Model

All keys are prefixed with `cb:` for namespace isolation.

```
# Session metadata (Hash)
cb:session:{session_id}
  → goal: string
  → status: "active" | "paused" | "completed" | "abandoned"
  → agent_id: string
  → created_at: ISO timestamp
  → updated_at: ISO timestamp
  → tags: JSON array
  → outcome: string | null
  → distillation: "pending" | "success" | "failed" | null

# Active session pointer per agent (String)
cb:active:{agent_id}
  → session_id

# Plan (String — full replacement on each update)
cb:session:{session_id}:plan
  → Markdown checklist text

# Decisions (List — append-only, capped at 50)
cb:session:{session_id}:decisions
  → JSON: { timestamp, content }

# File map (Hash — keyed by file path, capped at 100)
cb:session:{session_id}:files
  → field = file_path, value = JSON: { summary, last_action }

# Progress log (List — append-only, capped at 50)
cb:session:{session_id}:progress
  → JSON: { timestamp, content }

# Scratchpad (Hash — keyed by name, capped at 50)
cb:session:{session_id}:scratch
  → field = key, value = content string

# Session index (Sorted Set — scored by updated_at for listing)
cb:sessions
  → member = session_id, score = epoch timestamp
```

### Atomicity

State transitions (`start`, `resume`, `complete`, `abandon`) use Redis MULTI/EXEC transactions to ensure all related key updates happen atomically. This prevents orphaned state if the process crashes mid-transition.

### TTL and Cleanup

- **Completed/abandoned sessions**: All session keys get `EXPIRE` set to `SESSION_TTL_DAYS` (default 7 days) when the session completes or is abandoned. Applied atomically in the completion transaction.
- **Session index cleanup**: A lightweight cleanup runs on `ctx_list_sessions` — removes index entries whose session hash no longer exists (expired by TTL).
- **MAX_SESSIONS eviction**: When the session index exceeds `MAX_SESSIONS`, the oldest completed/abandoned sessions are removed (sorted set score = epoch time). Active and paused sessions are never evicted.

## Session Lifecycle

1. **Start**: Agent calls `ctx_start_session(goal)`. Atomically: creates Redis keys, pauses current active session (if any), sets active pointer.
2. **Work**: Agent calls `ctx_update` as it works. Minimal overhead — just Redis writes.
3. **Context loss**: Agent calls `ctx_get_shadow()`. Gets full state back in one call.
4. **Pause**: Implicit when a new session starts, or when another session is resumed.
5. **Resume**: Agent calls `ctx_resume_session(id)`. Atomically sets active, returns shadow context.
6. **Complete**: Agent calls `ctx_complete_session()`. Atomically marks completed, distills to FirekeepCortex, applies TTL.
7. **Abandon**: Agent calls `ctx_abandon_session()`. Atomically marks abandoned, applies TTL, no distillation.
8. **Expiry**: TTL-expired keys are automatically removed by Redis. Index entries cleaned up lazily.

## Distillation to FirekeepCortex

When a session completes, CortexBridge calls FirekeepCortex `POST /memory/learn`:

```json
{
  "action": "{goal} — {completed plan steps summarized}",
  "outcome": "{outcome if provided, else last progress entry}",
  "resolution": "{key decisions joined, max 3}",
  "tags": ["{session tags}", "cortexbridge"],
  "domain": "development",
  "namespace": "{configurable, default: cortexbridge}"
}
```

The distillation is structured extraction, not LLM-dependent. It concatenates plan steps marked done, joins decisions, and uses the outcome if provided.

**Failure handling**: If FirekeepCortex is unreachable, the session is still marked `completed` with `distillation: "failed"`. The shadow context remains in Redis until TTL expires, so the data is not lost — it can be retrieved via `ctx_get_shadow` and manually submitted.

## Configuration

All via environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | `redis://localhost:6379/3` | Redis connection (DB 3, avoids FirekeepCortex DB 0, Celery broker DB 1, Celery results DB 2) |
| `MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `MCP_PORT` | `8070` | MCP server port |
| `FIREKEEP_API_URL` | `http://localhost:8100` | FirekeepCortex API for distillation |
| `FIREKEEP_API_KEY` | `""` | FirekeepCortex API key (if required) |
| `FIREKEEP_NAMESPACE` | `cortexbridge` | Namespace for distilled memories |
| `SESSION_TTL_DAYS` | `7` | How long completed/abandoned sessions persist in Redis |
| `MAX_SESSIONS` | `100` | Max sessions in index (evicts oldest completed/abandoned) |
| `DEFAULT_AGENT_ID` | `default` | Agent ID used when not specified in tool calls |

## What This Does NOT Do

- Does NOT manage the context window directly (that's the LLM client's job)
- Does NOT intercept or modify agent messages
- Does NOT store full file contents (only agent-provided summaries)
- Does NOT replace FirekeepCortex — it feeds into it
- Does NOT require the agent to change its workflow radically — just add a few tool calls at natural points
- Does NOT depend on Ollama or any LLM — pure structured data operations
