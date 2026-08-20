# Agent Intelligence & Coordination

Firekeep makes every Claude Code session smarter through automated briefings, session debriefs, and cross-session coordination — all running as hooks with zero manual steps.

## Pre-Flight Briefing

Every session starts with an automated intelligence pass (the `session_start` hook core fetching Cortex `GET /briefing`) that consolidates all Firekeep services into a situational brief:

```
=== PRE-FLIGHT BRIEFING ===
You are agent-coder. Goal: fix auth middleware.
ENVIRONMENT: Events: 261. All collectors healthy.
ERRORS: 1 recent error(s): Container cortex-api restarted
TASKS: 2 pending task(s):
  - Write auth tests [high] from agent-alpha
QUALITY: From 3 recent sessions: tool success rate is 87%
=== END BRIEFING ===
```

**What it pulls:** `GET /briefing` aggregates 13 sections:
- **Environment** — Sentinel collector health, recent errors
- **Tasks** — pending assignments from other agents (via Relay)
- **Bulletins** — recent announcements on the bulletin board
- **Quality** — eval patterns from recent sessions (tool success rate, failure rate)
- **Strategy tips** — trial/validated patterns from the pattern engine
- **Observed patterns** — patterns observed in this agent's own sessions
- **Cross-agent learnings** — patterns discovered by other agents
- **Skills** — active skills matching the session goal
- **Vault** — available secrets (admin-scoped callers only)
- **Profile** — the person profile from Dreaming (empty unless `DREAM_ENABLED=true`)
- **Resumable sessions** — paused/crashed sessions to pick up
- **Discipline** — untagged-call visibility
- **DLQ** — dead-letter queue depth (backfill/distill)

Connection details come from the single `[server]` section in `~/.firekeep/config` (via the resolver); the briefing itself is fetched server-side from Cortex `GET /briefing`. Adapts to single-agent or multi-agent mode. No extra hook configuration is needed.

## Session Debrief

At the end of every assistant turn, the `Stop` hook (`firekeep_client.hooks.stop`) provides guided completion — note Claude fires `Stop` per turn, not at session end; true session end is the `SessionEnd` event (`hooks.session_end`):

- Reminds to call `ctx_complete_session` with an outcome summary
- Reminds to store non-obvious learnings with `memory_learn`
- In multi-agent mode: update tasks, release file leases, post status to Relay

Each completed session feeds back into the briefing for the next one — evals score the session, learnings go to Cortex, and the next briefing includes those quality patterns.

## The Learning Loop

```
Session N starts → Briefing pulls evals + memories from sessions 1..N-1
    → Agent works smarter (knows past mistakes, current env state)
    → Debrief captures learnings → Evals score session N
    → Session N+1 briefing is better
```

This is automatic. No manual steps. Each session improves the next.

## Multi-Agent Coordination

For work that benefits from multiple agents, Firekeep provides task assignment, inbox polling, and file locking across separate Claude Code sessions.

### Starting Named Agents

```bash
FIREKEEP_AGENT_ID=agent-alpha firekeep doctor && claude   # start your runtime with this identity
# In another terminal:
FIREKEEP_AGENT_ID=agent-beta firekeep doctor && claude
```

Each agent gets a unique identity (`agent-alpha`, `agent-beta`), separate Bridge sessions, and its own task inbox.

### Task Queue

Agents assign work to each other using structured tasks:

```
relay_task_post(
    title="Write unit tests for auth/middleware.py",
    assignee="agent-beta",
    assigner="agent-alpha",
    description="Cover require_scope, key validation, error paths",
    files=["auth/middleware.py", "auth/tests/test_middleware.py"],
    priority="high"
)
```

The assignee sees the task in their pre-flight briefing and on every turn via the `UserPromptSubmit` polling hook.

### File Locking

A `PreToolUse` hook runs before every Edit/Write. It checks if another agent holds a lease on the file. If yes, the edit is blocked:

```
relay_lease(resource_id="auth/tests/test_middleware.py", agent_id="agent-beta")
```

The hook also sends the active Bridge session ID into the Cortex policy engine when available, so `session_health` policy checks can actually evaluate the current session.

If Cortex auth is enabled, the hook reads the API key from `[server]` in `~/.firekeep/config` (via the resolver) before calling `POST /agent/action/before`.

### Status Updates

```
relay_task_update(task_id="task-abc12345", status="in-progress")
relay_task_update(task_id="task-abc12345", status="completed", result="5 tests passing")
```

## Using Sub-Agents (Single Session)

For parallelism within a single session, use Claude Code's built-in `Agent` tool with `isolation: "worktree"`. The sub-agent works in an isolated git branch and can use all Firekeep MCP tools (memory, Relay, Sentinel).

This is simpler than multi-terminal coordination and sufficient for most tasks. Use named agents only when you need persistent cross-session handoffs.

## Decision Board

> When a clarification needs more than a couple of questions, call `decision_board(context, draft_questions)` instead of asking the questions inline.

This is a LOCAL, per-user clarification surface — distinct from the Relay task/bulletin board above, which is team-visible. It's served by its own always-on stdio MCP server, `firekeep-decision`: stdio-local like `firekeep-symdex`, wheels installed unconditionally, but Decision is core infrastructure with no switch while symdex mounts only when registered as a dex (registered by default; `firekeep dex remove symdex` is the off-switch — see [guides/dexes.md](guides/dexes.md)).

- `decision_board(context, draft_questions=[])` — asks Cortex to synthesize a board (evidence + suggested answers per question, pulled from a memory recall across all teammates' knowledge), opens it in your browser, and waits for your answers. Returns the answers (markdown) once submitted, or `{status: "pending", board_id, board_url, next}` if you haven't answered yet — `board_url` lets you open the board manually if the auto-open failed.
- `decision_board_check(board_id)` — call with the `board_id` from a pending response to collect the answers once you've submitted them.

No browser available (headless/CI)? The board is returned as plain text to answer inline instead.

## Personal / Bypass Mode

For personal work you can make Firekeep go **dormant** — nothing is logged, recalled, or sent to the server:

- **In a Claude session:** type `/personal` (or `! firekeep personal`). It takes effect **live** — the next turn shows a "⚠ PERSONAL MODE" banner, the hooks stop briefing/presence/gate/capture, the sidecar stops sending presence, and the decision board suppresses itself. Type `/personal` again to rejoin team mode.
- **In a kiro session:** typing `/personal [on|off|status|toggle]` as plain chat text works too — the hook dispatcher intercepts it (kiro has no slash-command surface, so there is no rendered command, but the typed text does the same toggle). **Codex and OpenCode:** use `firekeep personal on|off|status|toggle` (Codex is hookless; OpenCode's bridge delivers no prompt text to intercept). `firekeep doctor` shows a WARN row while bypass is active, so it's never silently left on.
- **Auto-clears at session end** (Claude and OpenCode) — the `session_end` hook core wipes the marker (Claude's `SessionEnd`, OpenCode's `session.deleted`), so it can't leak into your next session. The `stop` hook deliberately does **not** clear it — Stop fires every assistant turn, and clearing there ended personal mode after turn 1. kiro has no session-end event and Codex is hookless, so on both the marker clears only via `firekeep personal off` or the 12h TTL backstop (`FIREKEEP_PERSONAL_TTL_HOURS`), which also covers a crash on any runtime.
- **Whole session personal from launch:** set `FIREKEEP_BYPASS=1` before starting your runtime — the hard cutoff where even the MCP servers (via the shim) serve zero tools, so nothing can reach the server at all.

The marker (`~/.firekeep/personal`) is separate from your config — toggling it never rewrites `~/.firekeep/config`. It is machine-global, so concurrent sessions share personal mode.

## Secrets Vault

Sensitive credentials should be stored in the encrypted vault, not in memory or files:

- `vault_store(key, value, category?, description?)` — encrypt and store a secret
- `vault_retrieve(key)` — decrypt and retrieve a secret
- `vault_list(category?)` — list available secrets (metadata only, no values)

Use `memory_learn` with `namespace="infrastructure"` for non-secret operational facts (IPs, URLs, hostnames). Use the vault for actual secrets (passwords, tokens, keys).

The briefing hook shows available vault secrets at session start. Agents should proactively store credentials they encounter during work.

## MCP Tools

| Tool | Purpose |
|------|---------|
| `relay_task_post` | Create and assign a task |
| `relay_task_list` | List tasks (filter by assignee, status) |
| `relay_task_update` | Update task status, result, or reassign |
| `relay_lease` | Acquire a file lease with fencing token |
| `relay_release` | Release a file lease or legacy claim |
| `relay_heartbeat` | Extend a lease TTL |
| `relay_lease_status` | Check who holds a file lease |
| `relay_broadcast` | Send a message on a channel |
| `relay_get_messages` | Read channel message backlog |
| `vault_store` | Encrypt and store a secret |
| `vault_retrieve` | Decrypt and retrieve a secret |
| `vault_list` | List available secrets (no values) |

## Hook Cores (client kit — `firekeep_client.hooks`)

The five bash hooks are retired. The adapter wires stdlib Python hook cores at install (Claude `settings.json`, kiro inline hooks, OpenCode via a rendered JS plugin at `~/.config/opencode/plugins/firekeep-hooks.js`; Codex has no hook surface):

| Retired bash hook | Client-kit replacement (event) |
|---|---|
| `briefing.sh` | `firekeep_client.hooks.session_start` (SessionStart / kiro agentSpawn) — thin fetch of Cortex `GET /briefing` |
| `debrief.sh` | `firekeep_client.hooks.stop` (Stop) |
| `multi-agent-poll.sh` | `firekeep_client.hooks.prompt` (UserPromptSubmit) |
| `multi-agent-precheck.sh` | `firekeep_client.hooks.pre_tool` (PreToolUse — blocking) |
| `multi-agent-postaction.sh` | `firekeep_client.hooks.post_tool` (PostToolUse) |
| `start-agent.sh` | retired — set `FIREKEEP_AGENT_ID` in the environment (overrides `[identity] agent_id`) and start your runtime |

Two newer cores have no bash predecessor: `session_end` (presence deregistration + personal-mode clear at *real* session end — Claude `SessionEnd`, OpenCode `session.deleted`) and `precompact` (Claude-only `PreCompact` checkpoint).

Presence registration (`session_start`) and heartbeat (`prompt`) are owned directly by the hook cores above. Clean-exit deregistration lives in `session_end`, **not** `stop` — Stop fires at the end of every assistant turn, and deregistering there deleted presence after turn 1. That means the presence lifecycle differs by runtime: Claude and OpenCode deregister at real session end; kiro wires the five original cores to its `agentSpawn`/`userPromptSubmit`/`preToolUse`/`postToolUse`/`stop` events but has **no session-end event** (and passes no session id), so a kiro session cannot deregister — it leaves at most one idle presence record per agent_id, reclaimed on that agent's next `agentSpawn` (see docs/KIRO-VALIDATION.md). The **sidecar** (`firekeep-sidecar`) is the *intended* presence owner only for MCP-only runtimes with no hook lifecycle at all — Codex today — but nothing currently spawns it automatically; a Codex user has no presence path unless they run `firekeep-sidecar` by hand. The sidecar and every adapter use the same `[server]` connection. Each adapter registers one local `firekeep` gateway; it starts the four parameterized remote shims plus local Symdex and Decision Board backends.

## Environment Variables

| Variable | Example | Set by |
|----------|---------|--------|
| `FIREKEEP_AGENT_ID` | `agent-alpha` | exported manually per-process; overrides `[identity] agent_id` |
| `FIREKEEP_AGENT_GOAL` | `"fix auth bugs"` | exported manually per-process; read directly by the `session_start`/sidecar cores |
| `FIREKEEP_BYPASS` | `1` | set before launch for a hard whole-session bypass (personal mode) — hooks no-op, shim serves 0 tools, sidecar/decision go inert. See Personal / Bypass Mode above |
| `FIREKEEP_PERSONAL_TTL_HOURS` | `12` | staleness backstop for the `~/.firekeep/personal` marker: an un-cleared marker (crash) is treated as off past this horizon |

`FIREKEEP_PROFILE`, `FIREKEEP_RELAY_URL`, `FIREKEEP_BRIDGE_URL`, `FIREKEEP_CORTEX_URL`, and `FIREKEEP_SENTINEL_URL` are retired — no client-kit code reads them. URL/auth/TLS now come from the one `[server]` section in `~/.firekeep/config`. `firekeep install` removes stale managed copies from rendered runtime entries, so an upgraded machine is left with one source of truth rather than a stale second one; `FIREKEEP_AGENT_ID` is untouched, since it is a live per-process override. If `FIREKEEP_PROFILE` remains in a shell startup file, `firekeep doctor` warns that it is ignored.

One server per machine is the supported setup. If a machine genuinely needs a
second server, use a separate config file as an explicit escape hatch:

```bash
cp ~/.firekeep/config ~/.firekeep/client-b.conf
# edit client-b.conf so [server] points at the second server
FIREKEEP_CONFIG=~/.firekeep/client-b.conf firekeep doctor
```

To bind only one runtime to that file, add `FIREKEEP_CONFIG` to that runtime's
managed MCP entry. This is intentionally not a managed pin mechanism: a later
`firekeep install` re-render replaces Firekeep-owned entries and removes the
hand-added environment value, so reapply it after each re-render.

`agent_id` **is** prompted at install: an interactive `firekeep install` asks for the agent identity (defaulting to the configured value, else your OS username), then where the server is — set one up on this machine, redeem a join code, one that is already running, or not yet — writing `[identity]` and `[server]` in `~/.firekeep/config`. It does **not** ask for a profile or which client to prepare (Firekeep is one product, so there is no edition to ask about): without `--runtime`, every shipped adapter (Claude Code, Codex, Kiro, OpenCode) is rendered; `--runtime <name>` is the targeted re-render/repair path. Hitting Enter through every prompt keeps the current values, so re-running the installer after a kit upgrade is safe. With no TTY (CI, `./install < /dev/null`) or with `--non-interactive`, nothing is prompted and the config skeleton is written as before; `--agent-id` and `--host` supply the answers for scripted installs.

The client kit installs from a release, not a repo checkout:
`curl -fsSL https://firekeep.ai/latest/install | sh` (`irm
https://firekeep.ai/latest/install.ps1 | iex` on Windows). The published bootstrap carries
its own artifact root, so there is no `FIREKEEP_DIST_BASE` to pass. It brings its own
Python. The install guide is [firekeep.ai/docs.html](https://firekeep.ai/docs.html).
`firekeep update` keeps it current; `firekeep doctor` reports `client-version` when a newer
release exists.

## A2A Agent Card Discovery (External Agents)

Firekeep publishes an [A2A](https://github.com/google/A2A) Agent Card so external agent registries and dashboards can discover its capabilities. This is **discovery-only** — the former JSON-RPC gateway (`POST /a2a`) and SSE streaming were removed (zero external callers ever connected), so there is no A2A task-submission path.

```bash
# Discover Firekeep's capabilities
curl http://YOUR_VPS:8050/.well-known/agent.json
```

## Limitations

- **Polling, not push.** Agents see new messages only when the user sends a message. No real-time notification during autonomous runs.
- **Hook timeout.** Briefing has 15s, poll has 8s. If VPS is slow, some data may be missed on that turn.
- **Lease expiry.** File leases expire after 30 minutes by default. Long edits need periodic `relay_heartbeat` calls.
