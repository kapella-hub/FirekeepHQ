# Relay — leases, tasks, presence, scope, DMs

> Moved out of the root `CLAUDE.md`, which is a prompt prefix loaded into every
> session. This content is reference and decision history: read it when you are
> working on this area, not on every task. Nothing was reworded in the move.

## Fencing Token Leases (Relay)
Upgrades claims to leases with monotonic fencing tokens, heartbeat extension, and wait queues.

**`resource_id` accepts real file paths.** `_normalize_resource_id` validated against `_validate_name`'s `[a-zA-Z0-9._-]` charset — right for a channel or agent id, wrong for a path — so it rejected every Windows absolute path (the drive-letter colon) and every path containing a space on any OS. The consumer is what made that fatal: `client/firekeep_client/hooks/pre_tool.py` derives its resource_id from `os.path.normpath(file_path)`, so on Windows it always asked about `E:/Documents/...` and always got `{"error": "Invalid resource_id: ...", "status": "unavailable"}` — then tested `lease.get("held")`, which is absent from that dict, so the edit was allowed with nothing printed and nothing logged (`hooklog.log_failure` only fired on a raised exception). **The lease coordination gate could not fire at all on Windows, silently.** Fixed on both sides: a separate `_VALID_RESOURCE_ID` regex adds `:` and space (path traversal, control characters, globs and the 200-char cap are unchanged, and `_validate_name` stays narrow for its ~15 other call sites), and the hook now treats a response carrying an `error` key as an explicit check failure — still failing OPEN, because a coordination check must not stop someone editing their own files, but saying so on stderr and in the hook log. Guards: `relay/tests/test_resource_id_paths.py`, `client/tests/hooks/test_pre_tool.py::TestLeaseCheckFailureIsVisible`.

**MCP Tools:** `relay_lease`, `relay_heartbeat`, `relay_lease_status`

## Task Queue (Relay)
Structured task assignment for multi-agent workflows. Agents create, list, and update tasks. Tasks are stored in Redis with sorted set indexing.

**MCP Tools:** `relay_task_post`, `relay_task_list`, `relay_task_update`, `relay_task_delete`

## Presence Registry (Relay)
Persistent agent presence with computed status. No TTL — presence persists until deregistered or manually removed. Status is computed dynamically: "active" (heartbeat within 10 minutes) or "idle" (older heartbeat). Index key is `nr:presence:__index` (double-underscore prefix to avoid collision with agent_id "index").

**MCP Tools:** `relay_register`, `relay_heartbeat_presence` (accepts optional `goal` param), `relay_deregister`, `relay_who_is_online`
**REST Endpoints (on Relay :8050):** `GET /presence`, `GET /presence/{agent_id}`, `DELETE /presence/{agent_id}`

## FirekeepScope (Relay) — Phase A
Default-on scope-clarification sessions (SP2). Sessions and screens live in Relay Redis DB 5 (`nr:scope:*`), following the presence/tasks hash-plus-sorted-set-index pattern. `origin: "cli"` sessions (from the local companion, not yet built — planned in the 2026-07-09 SP2 FirekeepScope design) own their own Bridge persistence; `origin: "mcp"` sessions (headless/MCP-only agents) have Relay persist Bridge decisions itself via a new Bridge REST route. First-answer-wins via Redis `SET NX`. 72h no-activity sweep to `abandoned`, 7-day TTL after `abandoned`/`completed`.

**MCP Tools:** `scope_start`, `scope_ask` (bounded long-poll, ~24s per call), `scope_post` (async), `scope_check`, `scope_complete`. `scope_answer` is deliberately not an MCP tool — answering is a human act, REST/dashboard only.
**REST Endpoints (on Relay :8050):** `POST /scope/sessions`, `GET /scope/sessions?status=active`, `GET /scope/sessions/{scope_id}`, `POST /scope/sessions/{scope_id}/screens`, `POST /scope/sessions/{scope_id}/screens/{screen_id}/answer`, `GET /scope/sessions/{scope_id}/events?since=`. Scope-gated `relay:read`/`relay:write` via a new Starlette-level `require_scope_asgi` helper in `auth/asgi.py` (the existing FastAPI `require_scope` can't run on FastMCP's `@mcp.custom_route` handlers).
**REST Endpoints (on Bridge :8070):** `POST /sessions/{agent_id}/context` — REST equivalent of `ctx_update`, used by Relay to persist decisions for `origin: "mcp"` sessions. This Relay→Bridge persistence requires `NR_FIREKEEP_API_KEY` to be set to a key with `session:write` scope when `AUTH_ENABLED=true`; this key currently must be provisioned manually (SP1a's automated key-bootstrap doesn't yet issue one for Relay→Bridge calls — a known follow-up, not solved by this fix).
**Dashboard:** Scope tab — lists active sessions, answers screens.
**Not yet built (Phase B, blocked on SP1's `client/` kit):** local companion (CLI + browser page), PreToolUse hook gate on `AskUserQuestion`, CLAUDE.md/kiro instruction-layer wiring, sandboxed embed (mermaid/html) rendering. Until Phase B ships, FirekeepScope is opt-in (any MCP-capable agent can call the tools above) rather than default-on.

## Direct Messages (Relay)
Agent-to-agent and dashboard-to-agent messaging. Messages stored in Redis DB 5 with 24h TTL. Delivered via poll hook or dashboard DM section.

**MCP Tools:** `relay_send_dm`, `relay_get_dm` (default limit=20)
**REST Endpoints (on Relay :8050):** `POST /dm/{agent_id}`, `GET /dm/{agent_id}`, `POST /dm/{agent_id}/read`

## A2A Agent Card Discovery (Relay)
Minimal A2A discovery endpoint (discovery only — a former JSON-RPC gateway + SSE streaming were removed; see `docs/HISTORY-NOTES.md`).

**Endpoint:**
- `GET /.well-known/agent.json` — Agent Card listing Firekeep capabilities for discovery by external agent registries and dashboards.
