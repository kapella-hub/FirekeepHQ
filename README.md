# Firekeep

**Persistent memory, live environment awareness, and replayable decision traces for AI coding agents — fully self-hosted.**

Firekeep is a **self-hosted control plane for AI coding agents**. It gives Claude Code, Codex, Kiro, OpenCode, and other MCP-capable clients persistent memory, session continuity, environment awareness, agent coordination, and replayable traces. The server stack and its default inference path are local; optional connectors and Symdex AI providers contact third-party services only when you configure them.

---

## The Problem

AI coding agents are powerful, but each session starts with partial amnesia. They lose context, re-read files, miss environment state, and struggle to explain why they made a decision earlier in the workflow. When something goes wrong, there is often no reliable trace to inspect. When multiple agents work in the same codebase, coordination becomes fragile.

Firekeep fixes this by giving agents durable memory, live operational awareness, and shared coordination infrastructure.

## What Firekeep Does

| Capability | What it means |
|---|---|
| **Memory** | Agents remember what worked, what failed, and what matters across sessions. Semantic + graph retrieval, confidence scoring, contradiction handling, four memory types (reference / procedural / episodic / transient) with type-aware decay, token-conscious recall with optional LLM synthesis. |
| **Team Continuity** | Memories carry `agent_id` and `project` attribution. Per-contributor activity reports and LLM-synthesized handoff briefs let one agent pick up where another left off. |
| **Session Continuity** | Plans, decisions, and progress recorded through the session tools survive context compression. Crashed sessions are auto-detected on next start and offered for resumption with a periodic workspace snapshot (git branch, recent commits, diff stats) embedded in the shadow. |
| **Environment Awareness** | Configured Docker, git, and file collectors monitor operational state instead of relying only on prompts. Container restarts, new commits, and file changes flow into a replayable event stream. |
| **Agent Coordination** | Shared channels, bulletin board, structured task queue, resource leases with monotonic fencing tokens, presence registry, and direct messages. Concurrent agents can assign work, track progress, and use leases to prevent overlapping edits; hook-enabled clients block an edit when another agent already holds the file lease. |
| **Predict-then-Act Gateway** | Agents declare intent before consequential actions (`action_before` → `allow | rethink | block`), then reconcile outcomes (`action_after`). Combines a runtime policy engine (lease, file risk, path deny, session health, recent failure) with a fast-path cache for repeated low-risk actions. |
| **Skills** | Agents author reusable "what to do when X happens" playbooks via the `skill_create` tool (client-side, with full session context); a docs→skills pipeline drafts more from wikis/runbooks under human review. Top matches are injected into the next session's briefing. (Server-side auto-synthesis exists behind `SKILL_SYNTHESIS_ENABLED` but is off by default — the CPU-only deploy can't run the generation LLM in workable time.) |
| **Decision Board** | When a clarification needs more than a couple of questions, the agent opens a local browser board pre-populated with evidence retrieved from team memory — better questions, informed by what the team already learned. Client-stdio `firekeep-decision` server + Cortex `/decision/synthesize`. |
| **Auto-Evals + Pattern Discovery** | Quality metrics computed from replay traces on session completion. Pattern detection is enabled; automated promotion/validation and A/B experiment endpoints are implemented but disabled by default until a deployment has enough session volume to use them responsibly. |
| **Replay & Explainability** | Every memory read/write, session lifecycle event, environment change, coordination action, and gateway decision is recorded as a structured trace. Inspect, narrow, and reconstruct context at any prior event. |
| **Encrypted Secrets** | Fernet-backed vault for infrastructure credentials, API tokens, and connection strings. Distinct from memory — secrets never appear in recall. |
| **Business Knowledge** | Ingest company documents (wiki pages, tickets, API docs) — manually, or via scheduled Confluence collectors. Chunks land in the vector store and surface naturally during memory recall alongside operational memories. |
| **Code Intelligence** | Tree-sitter-based symbol search, caller graphs, architecture maps, and impact analysis that returns symbol slices instead of whole files — runs **client-side** (stdio-local `firekeep-symdex`, installed with the kit). 38 MCP tools (8 analytics tools hidden by default behind `SYMDEX_ANALYTICS_ENABLED`). |

## Why Firekeep Is Different

Firekeep is not another chatbot wrapper or prompt orchestration layer.

It is a **control plane for AI coding agents** — infrastructure that sits behind your existing tools and makes them better.

- **Self-hosted by default.** The server, datastores, embeddings, and default generation model run on your infrastructure. Third-party egress occurs only when you opt into a connector or external Symdex AI provider.
- **Built for coding agents.** Not general-purpose AI. Every feature is designed for the workflow of code reading, editing, testing, and deploying.
- **Persistence + observability.** Most agent tools focus on making the agent smarter in the moment. Firekeep focuses on what happens *between* sessions and *after* things go wrong.
- **MCP-native.** Four remote services and two client-local servers expose [Model Context Protocol](https://modelcontextprotocol.io/) tools. Shipped adapters configure Claude Code, Codex, Kiro, and OpenCode; other MCP clients can be configured manually.
- **Agent-agnostic.** Swap the agent client without rebuilding the underlying memory and coordination layer. Cursor has a documented manual MCP path; Aider does not currently have a shipped adapter.
- **A2A-discoverable.** Relay publishes an [Agent-to-Agent](https://github.com/google/A2A) agent card at `/.well-known/agent.json` for capability discovery. This is discovery-only, not an A2A task-execution endpoint.

## Quick Start

### Prerequisites

- A Linux VPS (or any Docker host). **RAM:** 16 GB recommended for the full default stack (Neo4j JVM + Qdrant +
  Redis + Ollama + 7 Python services). 8 GB is the practical floor and requires
  a small embedding model (`EMBEDDING_MODEL=granite-embedding:30m`,
  `EMBEDDING_DIM=384`). Below that, containers are OOM-killed while HTTP health
  checks still pass — a failure mode that is easy to misdiagnose.
- Docker and Docker Compose v2
- Git
- No public Firekeep application ports are required. A default install binds everything to `127.0.0.1`; serving
  another machine is an explicit opt-in (see [Reaching it](#reaching-it)).

### Deploy

**If you were given a licence** — the server images are published; you do not
need the source:

```bash
echo <your-token> | docker login ghcr.io -u <your-username> --password-stdin
cp .env.example .env && $EDITOR .env      # set IMAGE_TAG to your version
bash install.sh --pull
```

**If you have source access** — build it:

```bash
git clone https://github.com/kapella-hub/FirekeepHQ.git   # private
cd Firekeep
bash install.sh
```

Either way the installer prompts for your VPS IP and Neo4j password, brings up the full stack (13 containers: the Cortex API / MCP / worker / beat quartet, Bridge, Sentinel, Relay, the dashboard, the Neo4j / Qdrant / Redis / Ollama backends, and a one-shot Ollama model puller), mints your API keys, and prints MCP URLs when ready.

Among those keys is an **admin key generated only on the first bootstrap**. It
appears in the bootstrap output and is repeated in the final summary so it is
harder to lose; it is never written to disk. Save it in a password manager.

**The install is closed by default.** `AUTH_ENABLED=true`, so protected MCP and
REST routes need an `X-API-Key`; health and version probes remain public.
`BIND_ADDR=127.0.0.1`, so application ports listen on loopback only. Installs
created before the 2026-07-26 security-default change may differ. If an older
guide now produces 401s or connection refusals, verify the current auth and
binding settings before changing them — see
**[docs/DEPLOYMENT.md → Access and authentication](docs/DEPLOYMENT.md#access-and-authentication)**.

### Reaching it

From the host, `http://localhost:8040`. From anywhere else, tunnel:

```bash
ssh -L 8040:127.0.0.1:8040 user@vps-host      # then open http://localhost:8040
```

Any private network or VPN, including Tailscale or WireGuard, can provide the
route instead once the application is bound or proxied onto that network;
Firekeep does not require a particular network vendor. The client only needs a
reachable server URL and the correct TLS/auth configuration.

To place the application ports on a LAN/private network or behind a TLS reverse
proxy, set `BIND_ADDR=0.0.0.0` in `.env` and `docker compose up -d` — but read
[the exposure warning](docs/DEPLOYMENT.md#exposing-the-stack-deliberately) first:
Docker's published-port rules are evaluated *before* ufw's, so a host firewall
does not contain a published port. Do not send `X-API-Key` over untrusted plain
HTTP; use an SSH tunnel, private network, or HTTPS termination.

### Connect Codex

Add Firekeep's MCP servers to Codex and start Codex from this repository so it picks up the root `AGENTS.md`.

```bash
firekeep install --runtime codex
```

See **[docs/SETUP-CODEX.md](docs/SETUP-CODEX.md)** for the full Codex setup. As
with every runtime, `[server]` needs an API key and a reachable host — see the
note under Connect Claude Code below.

### Connect Claude Code

If the server is reachable over ssh, one command does everything:

```bash
firekeep connect root@<server>
```

It probes the server, mints an API key for you, starts an SSH tunnel if the
server binds to loopback (the shipped default), writes `~/.firekeep/config`, and
runs `firekeep doctor` to prove it worked. Re-running it is safe — an existing
tunnel is reused, not duplicated.

The manual path, if you would rather do it yourself or have no ssh access:

```bash
./install            # or: firekeep install --runtime claude
```

This installs the `firekeep-client` kit into `~/.firekeep/venv`, bootstraps `~/.firekeep/config`, and prepares every shipped adapter: Claude Code, Codex, Kiro, and OpenCode. Claude gets user-scoped `~/.claude.json` + `~/.claude/settings.json` (MCP servers via `firekeep-shim`, five hook cores); the other clients receive their native MCP and instruction files. Two stdio-local servers — code intelligence (`firekeep-symdex`) and the Decision Board (`firekeep-decision`) — are installed automatically, always-on, no flag needed. Use `firekeep install --runtime <name>` only for a targeted re-render or repair.

Every runtime reads the same connection; there is no client-specific profile:

```ini
[identity]
agent_id = alice

[server]
kind = ports
scheme = http
host = 127.0.0.1
verify_tls = false
api_key = nxs_...
```

The installer prompts for the one `[server]` connection **and an API key**. Mint one on the
server with `deploy/firekeep-admin keys create --agent <you>`; `firekeep-shim`
then injects it on every request. A connection with no key against a keyed server
fails every protected tool call. `firekeep doctor` catches this for both HTTP
and HTTPS connections: it asks the server
whether it enforces auth rather than inferring from the scheme, because the
standard self-hosted shape is plain http to 127.0.0.1 over a tunnel against a
keyed server — which the old scheme-based check skipped silently.

### Verify

On the host, open `http://localhost:8040` (or tunnel — see [Reaching it](#reaching-it)) — the dashboard shows service health, memory stats, active sessions, and live events.

Or check from the command line:

```bash
curl -fsS http://127.0.0.1:8100/health                          # pre-auth, no key needed
curl -fsS -H "X-API-Key: $KEY" http://127.0.0.1:8100/memory/stats   # keyed route
```

> For detailed installation, updating, backups, and troubleshooting, see **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## A Real Workflow

Here's what happens when an agent uses Firekeep:

**1. Session starts.** On runtimes with lifecycle hooks, the client fetches Cortex's aggregated `GET /briefing` first, including relevant memories, tasks, environment state, and skills. The agent then calls `ctx_start_session("fix auth middleware bug")`; Bridge creates the durable session and the shim ties it to that briefing.

**2. Code exploration.** Instead of reading every file, the agent calls `search_symbols("auth middleware")` and `get_callers("require_scope")` on Symdex. It gets a targeted list of files and callers in milliseconds.

**3. Environment check.** Sentinel has been watching Docker. It detected that the `cortex-api` container restarted 3 times in the last hour and pushed an alert to Relay's `#alerts` channel. The agent sees this in its context.

**4. Work happens.** The agent edits files, runs tests, and records important progress with `ctx_update`. Bridge persists what was explicitly recorded. When the context window compresses, the agent calls `ctx_get_shadow` and gets that durable working state back — plans, decisions, progress, and file knowledge.

**5. Session completes.** The agent calls `ctx_complete_session`. Bridge atomically marks the session complete and queues background distillation into episodic memory; failures retry and eventually move to a visible dead-letter queue. Auto-evals compute quality metrics from the replay trace. Next time someone works on auth middleware, the memories are there once distillation succeeds.

**6. Something went wrong?** Open the Replay tab in the dashboard. Load the session. See every action, every memory read, every decision point. Click on a failure event and run narrowing — it walks back through the causal chain to help identify the root cause.

## Architecture

```
Local Machine                              VPS
┌─────────────────────────┐              ┌────────────────────────────────────┐
│  Claude Code / Codex /   │              │  FirekeepCortex    — Memory & RAG    │
│  Kiro / OpenCode / MCP   │ ◄──── MCP ───►  FirekeepBridge    — Sessions        │
│                          │              │  FirekeepSentinel  — Env monitoring  │
│  firekeep-symdex   (stdio)  │              │  FirekeepRelay     — Coordination    │
│  firekeep-decision (stdio)  │   Browser    │  Dashboard      — Web UI          │
│                          │ ───────────► │                                   │
└─────────────────────────┘              │  Neo4j · Qdrant · Redis · Ollama  │
                                         └────────────────────────────────────┘
```

Symdex and the Decision Board run **client-side** as stdio-local MCP servers (installed with the kit) — Symdex must be local to the working tree it indexes, so it is no longer a VPS container.

The two arrows crossing that boundary are **not publicly reachable by default**.
Out of the box the VPS side listens on loopback and requires an API key, so the
local-machine half reaches it over an SSH tunnel, a private network, or an HTTPS
front end you deliberately configure. See [Reaching it](#reaching-it).

| Service | What it does |
|---------|-------------|
| **FirekeepCortex** | Long-term memory. Semantic + graph RAG, knowledge lifecycle, sleep-cycle consolidation, four memory types with type-aware decay, versioned memories with confidence scoring, automatic contradiction supersession, agent-authored skills (`skill_create`) + a docs→skills pipeline, and the Agent Gateway (predict-then-act surface). |
| **FirekeepBridge** | Session persistence. Preserves working context (plans, decisions, progress, file knowledge) through context compressions. Auto-distills to Cortex on completion. Crashed-session detection with workspace-snapshot resumption. Emits session lifecycle events to the replay stream. |
| **FirekeepSentinel** | Environment observer. Docker health, git commits, file changes. Broadcasts alerts to Relay on errors. Container restarts, new commits, and file changes flow into a replayable event stream. |
| **FirekeepRelay** | Agent coordination. Real-time pub/sub channels, persistent bulletin board, structured task queue, resource leases with monotonic fencing tokens, presence registry, direct messages, and an A2A agent card endpoint for external discovery. |
| **Dashboard** | Web UI with fourteen tabs covering coordination (Overview, Sessions, Events, Relay, Scope), memory and knowledge (Memory, Skills, Knowledge, Patterns), operations (Ops, Policy, Vault), and diagnostics (Replay, Evals). |

Code intelligence (**FirekeepSymdex** — 38 MCP tools, 8 analytics hidden behind a flag) and the **Decision Board** (`firekeep-decision`) run **client-side** as stdio-local MCP servers installed with the kit, not as VPS containers.

Shared modules (no extra containers): **Replay Engine** (structured trace log across all services), **Auth** (API key scopes), **Vault** (Fernet-encrypted secret storage), **Corpus** (business document ingestion → vector chunks; scheduled Confluence collectors), **Auto-Evals** (10 Tier-1 quality metrics + trend tracking + regression detection), **Pattern Engine** (strategy detection; promotion validation and A/B experiments are feature-flagged off by default), **Policy Engine** (compound pre-edit safety checks — lease, file risk, path deny, session health, recent failure), **Agent Gateway** (predict-then-act surface with fast-path cache for repeated low-risk actions), **Skills** (agent-authored via `skill_create` + docs→skills drafts under human review; server-side synthesis off by default), **Memory Improvements** (composite eviction, token budgets, LLM synthesis pass, embed input capped + shrink-to-fit).

> For the full design specification, see **[docs/DESIGN.md](docs/DESIGN.md)**.

## Dashboard

Access at `http://localhost:8040` on the host, or through a tunnel from
elsewhere ([Reaching it](#reaching-it)). It has its own basic-auth login (user
`admin`; the password is written once to `dashboard/.htpasswd.cred`). Behind
that, nginx injects the dashboard's API key on every backend call, so the SPA
works against the auth-gated stack without you pasting a key into the browser.

| Tab | What it shows |
|-----|--------------|
| **Overview** | Service health, quick stats, recent events and memories |
| **Sessions** | Active/paused/completed/abandoned sessions, shadow context inspector |
| **Events** | Sentinel event feed with source and severity filters |
| **Relay** | Task queue, channels, bulletin board, direct messages, active claims/leases |
| **Scope** | FirekeepScope clarification sessions — active screens, answer prompts |
| **Memory** | Recall search, memory browser, store form, contributors, namespace/tag stats |
| **Skills** | Agent-authored + doc-derived skill cards — review, activate, edit, retire |
| **Knowledge** | Docs→skills ingestion (paste / URL) + the draft-skill approval queue |
| **Patterns** | Discovered strategy cards; promotion validation and experiment controls when their feature flags are enabled |
| **Ops** | Workers, queue depths, active agents, vector store info, Discipline counter (untagged memory calls) |
| **Policy** | Runtime policy rules for pre-edit safety checks, toggle per rule |
| **Vault** | Encrypted secret management (Fernet-backed, Redis DB 7) |
| **Replay** | Session trace timelines, event inspector, root cause narrowing |
| **Evals** | Aggregate quality metrics, per-session scorecards, quality trends |

The dashboard is a zero-dependency static SPA. No build step, no npm, no framework.

## Documentation

| Document | Contents |
|----------|----------|
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Installation, updating, backups, troubleshooting, local development |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | All environment variables, Redis DB allocation, intelligence features |
| **[docs/MCP-TOOLS.md](docs/MCP-TOOLS.md)** | Complete MCP tools reference (~102 tools across 6 MCP servers — cortex 27, relay 25, sentinel 3, bridge 7 over HTTP, plus client-stdio firekeep-symdex 38 [8 hidden] and firekeep-decision 2) |
| **[docs/DESIGN.md](docs/DESIGN.md)** | Full architecture specification, service contracts, integration points |
| **[docs/COMPARISON.md](docs/COMPARISON.md)** | Feature-by-feature comparison vs. base Claude Code |
| **[docs/SETUP-CODEX.md](docs/SETUP-CODEX.md)** | Codex integration guide |
| **[docs/SETUP-CLAUDE-CODE.md](docs/SETUP-CLAUDE-CODE.md)** | Claude Code integration guide |
| **[docs/MULTI-AGENT.md](docs/MULTI-AGENT.md)** | Agent intelligence: pre-flight briefing, session debrief, multi-agent coordination |

## Tech Stack

Python 3.11 server services (client supports Python 3.10+) / FastAPI / FastMCP / Neo4j / Qdrant / Redis / Ollama / Docker Compose / tree-sitter

Server-side embeddings and generation default to local Ollama, so the default
server path has no model API bill. Symdex can optionally use Anthropic, Gemini,
or an OpenAI-compatible endpoint for symbol summaries and scaffolding; enabling
one of those providers may send code snippets off-machine and incur provider
costs. Background auto-indexing explicitly disables AI summaries.

## Roadmap

### Working now
- Full memory lifecycle (learn, recall, decay, contradiction detection, versioning)
- Four memory types (reference / procedural / episodic / transient) with type-aware decay, sleep-cycle LLM classification
- Token-conscious recall with optional LLM synthesis
- Composite eviction (age × access × confidence); confirmed memories are never evicted
- Team continuity: `agent_id` + `project` on every memory, contributor reports, LLM-synthesized handoff briefs
- Session persistence through context compressions, with crash detection and workspace-snapshot resumption
- Bridge replay emission on session lifecycle (started / updated / completed / abandoned)
- Docker + git + file monitoring with Relay alerting; container/commit/file activity flows to the replay stream
- Agent coordination: channels, bulletin board, fencing-token leases, structured task queue, presence registry, direct messages
- Pre-flight briefing assembles intelligence from all services at session start; surfaces discipline warnings when memory calls arrive without identity headers
- Session debrief: guided completion with task updates and lease cleanup
- Multi-agent support (task assignment, inbox polling, file lease enforcement)
- Skills: agent-authored via `skill_create` (client-side) + a docs→skills pipeline (paste / URL / scheduled Confluence collectors) drafting skills under human review; top matches injected into the next briefing (server-side auto-synthesis exists behind `SKILL_SYNTHESIS_ENABLED`, off by default)
- Decision Board: agent-spawned local clarification board pre-populated with retrieved team-memory evidence (`firekeep-decision` stdio server + Cortex `/decision/synthesize`)
- Personal / bypass mode: in-session `/personal` (or `firekeep personal` / `FIREKEEP_BYPASS=1`) makes Firekeep fully dormant for private work — hooks, sidecar, decision board, and shim all honor one `is_bypassed()` gate; auto-clears at session end
- Pattern Engine: strategy detection, category classification (procedural / risk / behavioral), and quarantine controls; automated promotion/validation is implemented behind `PATTERN_VALIDATION_ENABLED=false` by default
- Experiment framework: named datasets, chi-square significance tests, effect-size confidence intervals, and controlled strategy-tip experiments behind `PATTERN_EXPERIMENTS_ENABLED=false` by default
- Optional feedback loop for measuring whether briefing tips improve outcomes when pattern validation is enabled
- Runtime policy engine: compound pre-edit checks (lease, file risk, path deny, session health, recent failure)
- Agent Gateway (predict-then-act): MCP `action_before` / `action_after`, gateway returns `allow | rethink | block`, fast-path cache for repeated low-risk actions
- Encrypted secrets vault (Fernet) with MCP tools and REST API
- Replay traces with root-cause narrowing and per-event context reconstruction
- Auto-evals: 10 Tier-1 quality metrics computed on session completion, trend tracking, regression detection
- Code intelligence: client-side stdio `firekeep-symdex` (38 tools, 8 analytics hidden behind a flag), always installed with the kit
- Docs→skills knowledge pipeline (`knowledge_ingest`, URL ingest) + opt-in scheduled Confluence collectors (SP3)
- Fourteen-tab web dashboard (adds Scope + Knowledge) with webhook management, pattern visualization, and a Discipline counter for untagged memory calls
- A2A agent card endpoint (`/.well-known/agent.json`) for external discovery
- Business knowledge ingestion: chunk documents into vector store, surface during memory recall
- Webhook notifications (Slack, Discord, generic HTTP)
- Authentication: per-key API scopes (memory:read/write, session:read/write, replay:read, eval:read, admin, …), **on by default**, with keys bootstrapped by the installer

### Planned
- Grafana metrics export
- Jira connector ingestion (wiki/Confluence auto-sync already shipped, opt-in)
- Skill versioning and rollback
- Cross-VPS federation (multiple Firekeep instances sharing pattern/skill libraries)

## Status

Firekeep is in active development and used daily. The core implementations — memory, sessions, environment monitoring, coordination, replay, the client kit, and Symdex — are covered by more than 3,500 passing automated tests in the current repository. The architecture is designed for a single-host deployment.

This is currently a private repository preparing for early access.

## License

Firekeep is source-available under BUSL-1.1. Production use is free for one
natural person in one workspace; Group use requires a commercial license.
Each version converts to Apache-2.0 four years after its first public release.
See [LICENSE](LICENSE) for the full grant and [docs/LICENSING.md](docs/LICENSING.md)
for status. Symdex
remains under this license until its standalone Core is extracted and released
separately under Apache-2.0.
