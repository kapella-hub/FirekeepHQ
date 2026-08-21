# MCP Tools Reference

Firekeep exposes 106 MCP tools from 6 logical backends through one client-visible `firekeep` stdio gateway. The four remote services — Cortex :8080, Bridge :8070, Sentinel :8060, Relay :8050 — use Streamable HTTP behind parameterized shims. `firekeep-symdex` (38 code-intelligence tools; 30 visible by default, 8 analytics hidden behind `SYMDEX_ANALYTICS_ENABLED`) and `firekeep-decision` (2 tools) are client-local processes behind the same gateway, not HTTP endpoints. `firekeep_gateway_status` reports per-backend health; one failed backend does not remove the others.

Tool breakdown: Cortex 30 + Bridge 7 + Sentinel 3 + Relay 25 = 65 HTTP-service tools; plus client-stdio `firekeep-symdex` 38 (30 visible, 8 analytics hidden) + `firekeep-decision` 2, and the gateway's own `firekeep_gateway_status` 1 = **106 total**. (Counted from `@mcp.tool` registrations per service, 2026-08-19.)

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
| `scope_ask` | Post a screen and long-poll (~50s) for the human's answer |
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
| `skill_add_step_specs` | Attach executable step_specs to an existing skill (turns its steps into matchable procedure/runbook steps) |

## Agent Gateway (predict-then-act)

| Tool | Purpose |
|------|---------|
| `action_before` | Predict → policy check before a consequential action (`allow`/`rethink`/`block`) |
| `action_after` | Reconcile the actual outcome against the prediction |
| `runbook_ack` | Acknowledge a `require_ack` runbook advisory before proceeding — see [guides/living-procedures.md](guides/living-procedures.md) |

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

FirekeepSymdex is a **client-installed stdio-local MCP server** (`firekeep-symdex`) — not an HTTP service. There is no server container and no port 8090 (it was removed from `docker-compose.yml` and `docker-compose.office.yml`); it runs locally against the working tree it indexes.

The wheel is always installed by the client kit, but **its tools exist only when it is registered as a dex** — and it is registered by default: since client 1.2.0 an absent registry is seeded with symdex and docdex, and `firekeep dex remove symdex` is the off-switch. With symdex unregistered the gateway mounts no backend for it and none of the tools below appear — see [guides/dexes.md](guides/dexes.md).

38 tools across 7 categories: indexing, exploration, architecture, change detection, smart context, evolution, and pattern analysis. 30 are visible by default; the 8 analytics tools (`get_evolution_timeline`, `get_code_churn`, `get_contributors`, `get_change_summary`, `detect_patterns`, `get_complexity_metrics`, `get_hotspots`, `compare_repos`) are hidden behind `SYMDEX_ANALYTICS_ENABLED`. Includes `index_folder`, `index_repo`, `get_context`, `search_symbols`, `get_architecture_map`, `get_callers`, `get_impact`, `find_dead_code`, `get_review_context`, and more. See [symdex/README.md](../symdex/README.md) for the full list.

## FirekeepDecision (client-stdio, always-on)

`firekeep-decision` is a client-installed stdio-local MCP server, always installed by the client kit and always mounted. Unlike `firekeep-symdex` it is **not a dex** — it indexes nothing, so it sits outside the dex registry as core infrastructure and has no on/off switch. It provides a LOCAL, per-user clarification board backed by Cortex `POST /decision/synthesize`.

| Tool | Purpose |
|------|---------|
| `decision_board` | Synthesize a globally-informed clarification board, open it in the browser, and long-poll for the human's answers |
| `decision_board_check` | Resume the bounded poll for a pending board by `board_id` |

## FirekeepDocdex (documents dex — NO MCP tools, deliberately)

`firekeep-docdex` is the second dex, and it exposes **no MCP tools at all**. Its manifest `kind` is `ingest-client`, so the gateway mounts nothing for it. Choosing which folders Firekeep may read is a privacy decision, so the agent-callable tool is absent rather than guarded: on MCP-only runtimes that is complete enforcement, and where the agent holds a shell `firekeep docdex add` is an ordinary Bash command the hook/runbook layer observes.

Agents meet docdex content only through ordinary `memory_recall` — indexed documents land in the corpus and surface alongside memories, carrying `untrusted_content: "true"` (**retrieved document text is evidence, never instruction**). The human CLI is `firekeep docdex add|list|sync|remove`; see [guides/dexes.md](guides/dexes.md).

## FirekeepMaildex (email dex — NO MCP tools, deliberately)

`firekeep-maildex` is the third dex, an ingest client on the docdex chassis, and it exposes **no MCP tools at all** — manifest `kind: ingest-client`, so the gateway mounts nothing for it. Registering a mailbox is a privacy decision, so it is human-CLI only: `firekeep maildex add`. IMAP is read-only by construction (every open is `EXAMINE`, every fetch `PEEK`) and the wheel carries no send capability — no mutating verb, no SMTP. Ingested mail is **always member-private** (there is no `--shared`) and surfaces only through that member's ordinary `memory_recall`; see [guides/dexes.md](guides/dexes.md).

## A2A Agent Card Discovery (FirekeepRelay)

FirekeepRelay publishes an [A2A (Agent-to-Agent)](https://github.com/google/A2A) Agent Card so external agent registries and dashboards can discover Firekeep's capabilities. This is **discovery-only** — the former JSON-RPC gateway (`POST /a2a`) and SSE streaming were removed (zero external callers ever connected), and there is no `NR_A2A_API_KEY`.

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/.well-known/agent.json` | GET | Agent Card listing Firekeep capabilities for discovery |
