# MCP Tools Reference

Firekeep exposes 104 MCP tools from 6 logical backends through one client-visible `firekeep` stdio gateway. The four remote services — Cortex :8080, Bridge :8070, Sentinel :8060, Relay :8050 — use Streamable HTTP behind parameterized shims. `firekeep-symdex` (38 code-intelligence tools; 30 visible by default, 8 analytics hidden behind `SYMDEX_ANALYTICS_ENABLED`) and `firekeep-decision` (2 tools) are client-local processes behind the same gateway, not HTTP endpoints. `firekeep_gateway_status` reports per-backend health; one failed backend does not remove the others.

Tool breakdown: Cortex 29 + Bridge 7 + Sentinel 3 + Relay 25 = 64 HTTP-service tools; plus client-stdio `firekeep-symdex` 38 (30 visible, 8 analytics hidden) + `firekeep-decision` 2 = **104 total**. (Counted from `@mcp.tool` registrations per service, 2026-08-09.)

## FirekeepBridge (session context)

| Tool | Purpose |
|------|---------|
| `ctx_start_session` | Start a new working session with a goal |
| `ctx_update` | Record plan, decisions, file knowledge, progress, or scratch notes |
| `ctx_get_shadow` | Retrieve full working context as Markdown |
| `ctx_complete_session` | Complete session and distill learnings to Cortex |
| `ctx_abandon_session` | Abandon session without saving |
| `ctx_list_sessions` | List sessions filtered by status/agent |
| `ctx_resume_session` | Resume a paused session |

## FirekeepSentinel (environment observer)

| Tool | Purpose |
|------|---------|
| `sentinel_get_events` | Get recent events (filter by source/type/severity) |
| `sentinel_get_health` | Health status of all Docker services |
| `sentinel_push_event` | Push an agent observation as an event |

## FirekeepRelay (agent communication)

| Tool | Purpose |
|------|---------|
| `relay_broadcast` | Send message to a real-time channel |
| `relay_get_messages` | Get recent messages from channel backlog |
| `relay_post` | Post to the persistent bulletin board |
| `relay_read` | Read bulletin board (filter by tags/author) |
| `relay_claim` | Claim a resource to prevent duplicate work |
| `relay_release` | Release a lease or legacy claim resource |
| `relay_status` | Active agents, channels, claims overview |
| `relay_lease` | Acquire lease with fencing token (safe claims) |
| `relay_heartbeat` | Extend lease TTL (requires fencing token) |
| `relay_lease_status` | Check lease holder, token, and wait queue |
| `relay_task_post` | Create and assign a task to an agent |
| `relay_task_list` | List tasks filtered by assignee or status |
| `relay_task_update` | Update task status, result, or reassign |
| `relay_task_delete` | Delete a task permanently from the queue |
| `relay_send_dm` | Send a direct message to an agent |
| `relay_get_dm` | Get direct messages for an agent |
| `relay_register` | Register agent presence |
| `relay_heartbeat_presence` | Heartbeat presence with optional goal update |
| `relay_deregister` | Deregister agent presence |
| `relay_who_is_online` | List online agents with status |
| `scope_start` | Open a scope-clarification session |
| `scope_ask` | Post a screen and long-poll (~24s) for the human's answer |
| `scope_post` | Post a screen without blocking (async) |
| `scope_check` | Poll a scope session for new answers |
| `scope_complete` | Close a scope-clarification session |

(FirekeepScope's `scope_answer` is deliberately REST/dashboard-only — answering is a human act — not an MCP tool.)

## FirekeepCortex (memory)

| Tool | Purpose |
|------|---------|
| `memory_recall` | Query memories with semantic + graph search |
| `memory_learn` | Store action/outcome pairs (with secret scanning) |
| `memory_stream` | Ingest raw events for batch processing |
| `memory_health` | Check memory service health |
| `memory_handoff` | Generate an LLM handoff brief for a project |
| `memory_feedback` | Report whether recalled knowledge held up when acted on — accumulates per-memory counters that feed recall ranking (Knowledge Autopilot) |
| `corpus_ingest` | Ingest business documents (chunk + embed to Qdrant; surfaced via `memory_recall`) |
| `corpus_sources` | List ingested document sources |
| `corpus_delete` | Delete a source and all its data |
| `vault_store` | Store an encrypted secret |
| `vault_retrieve` | Retrieve and decrypt a secret |
| `vault_list` | List secret metadata (no values) |
| `vault_delete` | Delete a secret |

## Replay Engine (trace observability)

| Tool | Purpose |
|------|---------|
| `replay_timeline` | Event timeline for a session (filterable by type) |
| `replay_inspect` | Full details of a single trace event |
| `replay_context_at` | Reconstruct agent context at a specific event |
| `replay_narrow` | Root cause narrowing from a failure event |
| `replay_summary` | Session summary: event counts, duration, failures |

## Auto-Evals + Audit

| Tool | Purpose |
|------|---------|
| `eval_session` | Quality metrics for a completed session |
| `eval_summary` | Aggregate metrics across recent sessions |
| `audit_memory` | Memory access trail: who read/wrote what, when |

## Knowledge (docs→skills pipeline)

| Tool | Purpose |
|------|---------|
| `knowledge_ingest` | Ingest a document to the corpus and draft skills from any procedures it contains |
| `knowledge_ingest_url` | Crawl a URL (SSRF-guarded) and run the same corpus + draft-skill pipeline |

## Skills (team-shareable playbooks)

| Tool | Purpose |
|------|---------|
| `skill_create` | Author a reusable skill (trigger, symptoms, steps, gotchas) — the primary, client-authored path |
| `skill_recall` | Retrieve active skills matching a task and record the returned skills as explicitly used for freshness tracking |
| `skill_list` | List skills filtered by status/project |

## Agent Gateway (predict-then-act)

| Tool | Purpose |
|------|---------|
| `action_before` | Predict → policy check before a consequential action (`allow`/`rethink`/`block`) |
| `action_after` | Reconcile the actual outcome against the prediction |

## Pattern Engine (REST on Cortex :8100)

Background analysis that discovers what strategies work across sessions.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/patterns/` | GET | List all discovered patterns with confidence scores |
| `/patterns/relevant` | GET | Get patterns relevant to a goal/files (used by briefing) |
| `/patterns/analyze` | POST | Manually trigger pattern analysis |
| `/patterns/tip-shown` | POST | Record that patterns were shown in a briefing |
| `/patterns/effectiveness` | GET | Measured tip effectiveness (feedback loop) |
| `/patterns/{id}/quarantine` | POST | Immediately quarantine a pattern |
| `/patterns/{id}/unquarantine` | POST | Lift quarantine, return to candidate |
| `/patterns/datasets` | POST | Create a dataset (filtered session subset) |
| `/patterns/datasets` | GET | List all datasets |
| `/patterns/datasets/{id}` | GET | Get dataset details |
| `/patterns/datasets/{id}` | DELETE | Delete a dataset |
| `/patterns/experiments` | POST | Create an experiment (pattern + dataset) |
| `/patterns/experiments` | GET | List all experiments |
| `/patterns/experiments/{id}` | GET | Get experiment with statistical results |
| `/patterns/experiments/{id}/conclude` | POST | Manually conclude an experiment |

## Policy Engine (REST on Cortex :8100)

Runtime policy evaluation for pre-edit safety checks. Consulted by the pre-edit hook before file edits — the live path is the Agent Gateway's `POST /agent/action/before` (see the `action_before` / `action_after` MCP tools above).

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/policy/evaluate` | POST | **Deprecated alias** for `POST /agent/action/before`. Evaluate policy rules for a file edit (body: `{file_path, agent_id, session_id}`) |
| `/policy/rules` | GET | List active rules and their enabled/disabled status |
| `/policy/rules/{name}/toggle` | POST | Toggle a rule on or off |

**Rules:** `lease` (no-op, lease checked by hook), `file_risk` (pattern hotspots), `session_health` (failure rate), `path_deny` (deny globs), `recent_failure` (file failure history).

## FirekeepSymdex (code intelligence)

FirekeepSymdex is a **client-installed stdio-local MCP server** (`firekeep-symdex`), always installed by the client kit — not an HTTP service. There is no server container and no port 8090 (it was removed from `docker-compose.yml` and `docker-compose.office.yml`); it runs locally against the working tree it indexes.

38 tools across 7 categories: indexing, exploration, architecture, change detection, smart context, evolution, and pattern analysis. 30 are visible by default; the 8 analytics tools (`get_evolution_timeline`, `get_code_churn`, `get_contributors`, `get_change_summary`, `detect_patterns`, `get_complexity_metrics`, `get_hotspots`, `compare_repos`) are hidden behind `SYMDEX_ANALYTICS_ENABLED`. Includes `index_folder`, `index_repo`, `get_context`, `search_symbols`, `get_architecture_map`, `get_callers`, `get_impact`, `find_dead_code`, `get_review_context`, and more. See [symdex/README.md](../symdex/README.md) for the full list.

## FirekeepDecision (client-stdio, always-on)

`firekeep-decision` is a client-installed stdio-local MCP server (like `firekeep-symdex`, always installed by the client kit — no opt-in flag). It provides a LOCAL, per-user clarification board backed by Cortex `POST /decision/synthesize`.

| Tool | Purpose |
|------|---------|
| `decision_board` | Synthesize a globally-informed clarification board, open it in the browser, and long-poll for the human's answers |
| `decision_board_check` | Resume the bounded poll for a pending board by `board_id` |

## A2A Agent Card Discovery (FirekeepRelay)

FirekeepRelay publishes an [A2A (Agent-to-Agent)](https://github.com/google/A2A) Agent Card so external agent registries and dashboards can discover Firekeep's capabilities. This is **discovery-only** — the former JSON-RPC gateway (`POST /a2a`) and SSE streaming were removed (zero external callers ever connected), and there is no `NR_A2A_API_KEY`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/agent.json` | GET | Agent Card listing Firekeep capabilities for discovery |
