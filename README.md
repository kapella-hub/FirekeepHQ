# Firekeep

**Your agents need more than memory. They need a Keep.**

Firekeep is the **self-hosted operating layer for connected AI agents**. It carries durable knowledge, working context, procedures, coordination, and replayable evidence across sessions, models, machines, and teammates. It connects through MCP today: shipped adapters configure Claude Code, Claude Desktop (auto-detected when the app's config dir exists), Codex, Kiro, and OpenCode, while other MCP clients can use the generic configuration path. The server stack and its default inference path are local; optional connectors and Symdex AI providers contact third-party services only when you configure them.

**[Website](https://firekeep.ai) · [Live demo](https://firekeep.ai/?demo=1#cross-runtime-demo) · [Install guide](https://firekeep.ai/docs.html#server) · [Case study](https://firekeep.ai/case-study.html) · [Concepts](https://firekeep.ai/agents-md-vs-memory.html)**

---

## The Problem

AI agents are powerful, but each session starts with partial amnesia. They lose context, repeat discovery, miss operational state, and struggle to explain why they made an earlier decision. When something goes wrong, there is often no reliable trace to inspect. When multiple agents or teammates share a project, coordination becomes fragile.

Firekeep fixes this by giving agents durable memory, live operational awareness, and shared coordination infrastructure.

## What Firekeep Does

| Capability | What it means |
|---|---|
| **Memory** | Agents remember what worked, what failed, and what matters across sessions. Semantic + graph retrieval, confidence scoring, contradiction handling, four memory types (reference / procedural / episodic / transient) with type-aware recall decay, recoverable archive-first aging, and token-conscious recall with optional LLM synthesis. Recall is re-ranked by recorded session outcomes (outcome-weighted memory) and by agent feedback on knowledge that was actually acted on (`memory_feedback`). |
| **Knowledge Autopilot** | The knowledge base maintains itself without deciding anything on its own. When two unconfirmed memories genuinely conflict, neither is silently dropped — both stay recallable, marked contested, until a human verdict (`/memory/contested/resolve`). A session reaper closes out crashed/walked-away sessions so failures count in outcome scoring. Every review queue (draft skills, stale skills, procedure proposals, contested pairs, eval dead letters) lands in one inbox with a weekly digest, and `/memory/{id}/evidence` shows every signal behind a memory's rank in one read. |
| **Team Continuity** | Memories carry verified workspace/member provenance plus an untrusted runtime `agent_id` label and project. Per-contributor activity reports and LLM-synthesized handoff briefs let one agent pick up where another left off. |
| **Session Continuity** | Plans, decisions, and progress recorded through the session tools survive context compression. Crashed sessions are auto-detected on next start and offered for resumption with a periodic workspace snapshot (git branch, recent commits, diff stats) embedded in the shadow. |
| **Environment Awareness** | Configured Docker, git, and file collectors monitor operational state instead of relying only on prompts. Container restarts, new commits, and file changes flow into a replayable event stream. |
| **Agent Coordination** | Shared channels, bulletin board, structured task queue, resource leases with monotonic fencing tokens, presence registry, and direct messages. Concurrent agents can assign work, track progress, and use leases to prevent overlapping edits; hook-enabled clients block an edit when another agent already holds the file lease. |
| **Predict-then-Act Gateway** | Agents declare intent before consequential actions (`action_before` → `allow | rethink | block`), then reconcile outcomes (`action_after`). Combines a runtime policy engine (lease, file risk, path deny, session health, recent failure) with a fast-path cache for repeated low-risk actions. |
| **Skills** | Agents author reusable "what to do when X happens" playbooks via the `skill_create` tool (client-side, with full session context); a docs→skills pipeline drafts more from wikis/runbooks under human review. Top matches are injected into the next session's briefing. (Server-side auto-synthesis exists behind `SKILL_SYNTHESIS_ENABLED` but is off by default — the CPU-only deploy can't run the generation LLM in workable time.) |
| **Enforced Runbooks** | A skill whose steps carry command matchers is a runbook a human can arm: `advise` (round-1 advisories), `require_ack` (a matched command is challenged and proceeds only after an audited acknowledgement — one-use permit bound to workspace, member, session, command hash, step, bundle version and execution), or `block` (fails closed, with a server receipt the client requires before honoring an allow). Evidence is scored by *success* — a step counts only when its command exits 0 — and every enforcement event lands in a deviation ledger (dashboard + inbox), storing command hashes, never command text. Modes are set by a human on an admin-only route; agents can propose runbooks, never arm them. Opt-in via `PROCEDURE_ENABLED`, currently being dogfooded on our own deploys — see [docs/guides/living-procedures.md](docs/guides/living-procedures.md). |
| **Decision Board** | When a clarification needs more than a couple of questions, the agent opens a local browser board pre-populated with evidence retrieved from team memory — better questions, informed by what the team already learned. The local gateway fronts the Decision Board process and Cortex `/decision/synthesize`. |
| **Living Instructions** | The instruction layer measures itself: a per-instruction compliance table computed deterministically from replay (did sessions recall before answering, record as they went, declare consequential actions), with trend over time, per-runtime slices, and exposure receipts — sessions carry a content hash of the instruction text that actually reached them, and anything unverifiable reports as *unknown* rather than counted. Honest about its limits by construction: it measures behavior, not whether the behavior helped. Fleet-drafted rewrites under human verdict and A/B validation are roadmap. |
| **Trust Ledger** | A per-agent employment record built from the declarations agents already make through the gateway (`action_before`/`action_after`): declared-action count, reconciliation rate, prediction-match calibration (Brier over stated confidence vs reconciled outcome), reversals, sessions, first/last seen. Visibility only — it reports, it never gates. Honest by construction: calibration is behavior not competence, only declared actions are seen, `agent_id` is a self-reported label so the record is per declared identity, and calibration reads *not enough signal* below a threshold rather than inventing a number. The enforcing half (a capability broker turning the record into earned autonomy) is roadmap. |
| **Auto-Evals + Pattern Discovery** | Quality metrics computed from replay traces on session completion. Pattern detection is enabled; automated promotion/validation and A/B experiment endpoints are implemented but disabled by default until a deployment has enough session volume to use them responsibly. |
| **Replay & Explainability** | Every memory read/write, session lifecycle event, environment change, coordination action, and gateway decision is recorded as a structured trace. Inspect, narrow, and reconstruct context at any prior event. |
| **Encrypted Secrets** | Fernet-backed vault for infrastructure credentials, API tokens, and connection strings. Distinct from memory — secrets never appear in recall. |
| **Business Knowledge** | Ingest company documents (wiki pages, tickets, API docs) — manually, or via scheduled Confluence collectors. Chunks land in the vector store and surface naturally during memory recall alongside operational memories. |
| **Code Intelligence** | Tree-sitter-based symbol search, caller graphs, architecture maps, and impact analysis that returns symbol slices instead of whole files — runs **client-side** through the local gateway (`firekeep-symdex` is installed with the kit). 38 MCP tools (8 analytics tools hidden by default behind `SYMDEX_ANALYTICS_ENABLED`). |

## Why Firekeep Is Different

Firekeep is not another chatbot wrapper or prompt orchestration layer.

It is an **operating layer for connected AI agents** — infrastructure that sits behind your existing tools and makes them better.

- **Self-hosted by default.** The server, datastores, embeddings, and default generation model run on your infrastructure. Third-party egress occurs only when you opt into a connector or external Symdex AI provider.
- **Deep where the work happens.** Coding is the strongest shipped workflow, with Symdex for code intelligence. It is not the product boundary: Docdex brings selected document folders into the same Keep, Maildex brings a member's email (read-only IMAP, always member-private, `firekeep maildex add`), and the shared continuity, coordination, governance, and evidence layers are domain-independent.
- **Persistence + observability.** Most agent tools focus on making the agent smarter in the moment. Firekeep focuses on what happens *between* sessions and *after* things go wrong.
- **MCP-native.** Four remote services and two client-local backends expose [Model Context Protocol](https://modelcontextprotocol.io/) tools through one local `firekeep` stdio gateway. Shipped adapters configure Claude Code, Claude Desktop, Codex, Kiro, and OpenCode; other MCP clients can be configured manually.
- **Agent-agnostic.** Swap the agent client without rebuilding the underlying memory and coordination layer. Cursor has a documented manual MCP path; Aider does not currently have a shipped adapter.
- **A2A-discoverable.** Relay publishes an [Agent-to-Agent](https://github.com/google/A2A) agent card at `/.well-known/agent.json` for capability discovery. This is discovery-only, not an A2A task-execution endpoint.

## Install

**[firekeep.ai/docs.html](https://firekeep.ai/docs.html) is the install guide** —
requirements, the server, every client runtime, updating, troubleshooting. It is
maintained there rather than duplicated here; this section is the pointer.

```bash
curl -fsSL https://firekeep.ai/latest/install | sh    # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex         # Windows
```

One command, two required questions: the **agent identity** every memory, session and
replay event is attributed to, and **where your Firekeep server is** — set one
up on this machine with Docker, redeem a join code, point at one that is
already running, or decide later (`firekeep doctor` then tells you how to
finish). Setting one up here runs `firekeep init` for you; that installer asks
nothing, and the machine enrols itself once the stack is up, so `firekeep
doctor` is green with no dashboard, no tunnel, and no pasted key. It prints the
paste-ready command for your second machine when it finishes.

The installer then offers one skippable prompt for another MCP client's rules
file; the four shipped adapters are rendered either way.

Current server images target `linux/amd64`: run them on an x86-64 Linux host,
or through Docker Desktop with amd64 container support. The client bootstrap is exercised in CI on
Ubuntu, Debian, Alpine, Fedora, Rocky, Arch and openSUSE (x86_64 and aarch64);
macOS runs the same script but is not covered by CI.

- Requirements and sizing — [firekeep.ai/docs.html#requirements](https://firekeep.ai/docs.html#requirements)
- Server install — [firekeep.ai/docs.html#server](https://firekeep.ai/docs.html#server)
- Connecting agents and teammates — [firekeep.ai/docs.html#connect](https://firekeep.ai/docs.html#connect)
- Troubleshooting — [firekeep.ai/docs.html#troubleshooting](https://firekeep.ai/docs.html#troubleshooting)

### From this checkout

```bash
bash install.sh              # build the server from source
bash install.sh --pull       # the same installer against the published images
cd client && ./install       # the kit from this checkout (.\install.ps1 on Windows)
firekeep install             # re-render the runtime adapters only
```

`install.sh` prompts for nothing: the host address is detected and the Neo4j
password generated (`--ip` / `--neo4j-password`, or `FIREKEEP_VPS_IP` /
`FIREKEEP_NEO4J_PASSWORD`, override either). It returns as soon as the stack is
up rather than blocking on the ~3.3 GB model pull — until that finishes, memory
writes return `status="partial"` (stored and queued for backfill, not yet
searchable) and `firekeep doctor` carries an `embeddings` WARN row saying so.
`bash install.sh --wait-for-models` blocks instead.

Operating the server afterwards — access and authentication, the dashboard,
backups, updates, exposing ports deliberately — is
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

### Removing it

```bash
firekeep uninstall            # remove the client kit from this machine
firekeep uninstall --server   # also tear down the server + ALL its data
```

`firekeep uninstall` removes only what the kit installed — the Firekeep blocks
in each runtime's config (your own settings stay intact), the launcher and its
PATH entry, and `~/.firekeep` — and asks first (`--yes` skips the prompt). It
never touches a server you set up. `--server` additionally runs `docker compose
down -v` on the stack this machine provisioned, deleting the Neo4j/Qdrant/Redis
volumes — every memory, session and secret, permanently — behind a separate
data-loss confirmation that a plain uninstall or `--yes` can never trigger. Back
up first ([docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#removing-the-server)) if you
might want the data back.

## Firekeep Studio (0.4.0 preview)

[`studio/`](studio/) is a separate desktop client for people who want Firekeep to be the
only agent application they open. It gives Codex, Claude Code, Kiro CLI, and Grok one
runtime-neutral conversation surface: any supported runtime can be the explicit primary,
other runtimes can review it in fresh read-only contexts, and `/compare`, `/consensus`,
an explicit shared workspace, Windows voice input and system voice output, cache-aware token guards, named and color-coded local sessions,
live provider model/reasoning discovery, and typed Client Kit controls are built in. The
selected runtime is visually explicit, the full inspector can be hidden, and Studio links
to the dashboard from the existing Client Kit connection without exposing its credentials.
Fenced Mermaid responses render as native zoomable diagrams, and the existing Firekeep
Decision Board opens as a native question/evidence/action panel inside Studio. Its local
authenticated push preserves the original long poll, avoiding an extra agent turn merely to
discover that the board is ready.
The primary runtime picker shows live readiness and transport context, response scrolling
respects readers who move away from the tail, and copy/paste uses bounded text-only native
clipboard operations. Completed answers lead each run while detailed working events fold into
a **Work log**. A runtime-neutral **Agents** view tiles Codex, Claude, Kiro, Grok, or any future
chat-capable adapter into selectable panes with independent native continuations; the shared
composer targets the selected pane, and active runs remain serialized for workspace safety.
Mission Mode adds an outcome-bounded harness: one primary
writer, deterministic local checks, bounded repair, independent review evidence, explicit
human acceptance, and a task result stored separately from every agent's prose.

Studio consumes provider-supported structured boundaries rather than scraping TUIs, keeps
provider authentication provider-owned (except OS-encrypted API keys), and continues to use
the existing Python Client Kit wherever a runtime's native configuration provides Keep memory,
hooks, policy, and connectivity. Runtime cards report that evidence explicitly; the direct xAI
Grok adapter is clearly marked as having no Keep memory rather than inheriting another runtime's
claim. The preview
build and installer commands, security boundaries, runtime matrix, and full `/` command
inventory are in the [Studio README](studio/README.md).

Studio 0.4 ships an x64 Windows installer and a universal macOS installer from an isolated,
signed release channel. Packaged builds check for Studio updates shortly after launch; Windows
downloads and installs a verified update on restart, while macOS uses native automatic updates
only when the release is Apple-signed and notarized. The current release page, checksums, and
both installers are published under
[Firekeep Studio releases](https://github.com/kapella-hub/firekeep-dist/releases?q=studio-v).

## A Real Workflow

Here's what happens when an agent uses Firekeep:

**1. Session starts.** On runtimes with lifecycle hooks, the client fetches Cortex's aggregated `GET /briefing` first, including relevant memories, tasks, environment state, and skills. The agent then calls `ctx_start_session("fix auth middleware bug")`; Bridge creates the durable session and the shim ties it to that briefing.

**2. Code exploration.** Instead of reading every file, the agent calls `search_symbols("auth middleware")` and `get_callers("require_scope")` on Symdex. It gets a targeted list of files and callers in milliseconds.

**3. Environment check.** Sentinel has been watching Docker. It detected that the `cortex-api` container restarted 3 times in the last hour and pushed an alert to Relay's `#alerts` channel. The agent sees this in its context.

**4. Work happens.** The agent edits files, runs tests, and records important progress with `ctx_update`. Bridge persists what was explicitly recorded. When the context window compresses, the agent calls `ctx_get_shadow` and gets that durable working state back — plans, decisions, progress, and file knowledge.

**5. Session completes.** The agent calls `ctx_complete_session`, optionally passing a structured self-grade — `task_result` (`"success"` / `"partial"` / `"failure"`, the TASK's outcome, not the RPC's) plus up to 10 short `task_evidence` claims backing it. The grade is accepted only from the session's verified owner and stored as that session's one authoritative, never-overwritten result. Bridge atomically marks the session complete and queues background distillation into episodic memory; failures retry and eventually move to a visible dead-letter queue. Auto-evals compute quality metrics from the replay trace — a session with no recognized grade reads as `unknown` rather than a guessed success, so outcome-driven scoring (Outcome-Weighted Memory, quality trends) never counts silence as a win. Next time someone works on auth middleware, the memories are there once distillation succeeds.

**6. Something went wrong?** Open the Replay tab in the dashboard. Load the session. See every action, every memory read, every decision point. Click on a failure event and run narrowing — it walks back through the causal chain to help identify the root cause.

## Architecture

```
Local Machine                               VPS
┌──────────────────────────┐              ┌────────────────────────────────────┐
│ Claude / Codex / Kiro /  │              │ FirekeepCortex   — Memory & RAG    │
│ OpenCode / MCP client    │              │ FirekeepBridge   — Sessions        │
│           │              │              │ FirekeepSentinel — Monitoring      │
│  one `firekeep` gateway  │◄──── MCP ───►│ FirekeepRelay    — Coordination    │
│    ├─ symdex (local)     │              │ Dashboard        — Web UI          │
│    └─ decision (local)   │── Browser ──►│ Neo4j · Qdrant · Redis · Ollama    │
└──────────────────────────┘              └────────────────────────────────────┘
```

Symdex and the Decision Board run **client-side** as stdio-local MCP servers (installed with the kit) — Symdex must be local to the working tree it indexes, so it is no longer a VPS container.

The two arrows crossing that boundary are **not publicly reachable by default**.
Out of the box the VPS side listens on loopback and requires an API key, so the
local-machine half reaches it over an SSH tunnel, a private network, or an HTTPS
front end you deliberately configure. See
[docs/DEPLOYMENT.md → Access and authentication](docs/DEPLOYMENT.md#access-and-authentication).

| Service | What it does |
|---------|-------------|
| **FirekeepCortex** | Long-term memory. Semantic + graph RAG, archive/restore lifecycle, sleep-cycle consolidation, four memory types with type-aware decay, versioned memories with confidence scoring, automatic contradiction supersession, agent-authored skills (`skill_create`) + a docs→skills pipeline, and the Agent Gateway (predict-then-act surface). |
| **FirekeepBridge** | Session persistence. Preserves working context (plans, decisions, progress, file knowledge) through context compressions. Auto-distills to Cortex on completion. Crashed-session detection with workspace-snapshot resumption. A session reaper abandons sessions idle past 72h so walked-away sessions register as non-successes in outcome scoring. Emits session lifecycle events to the replay stream. |
| **FirekeepSentinel** | Environment observer. Docker health, git commits, file changes. Broadcasts alerts to Relay on errors. Container restarts, new commits, and file changes flow into a replayable event stream. |
| **FirekeepRelay** | Agent coordination. Real-time pub/sub channels, persistent bulletin board, structured task queue, resource leases with monotonic fencing tokens, presence registry, direct messages, and an A2A agent card endpoint for external discovery. |
| **Dashboard** | Web UI covering coordination, memory, diagnostics, devices, members, policy, vault, and operations. |
| **Firekeep Studio** | Optional local desktop console and Mission harness for primary agents, deterministic verification, independent reviewers, Windows dictation, system voice replies, cross-runtime comparison, and Client Kit control. It is not a VPS service. |

Code intelligence (**FirekeepSymdex** — 38 MCP tools, 8 analytics hidden behind a flag) and the **Decision Board** (`firekeep-decision`) run **client-side** as stdio-local MCP servers installed with the kit, not as VPS containers.

Shared modules (no extra containers): **Replay Engine** (structured trace log across all services), **Auth** (API key scopes), **Vault** (Fernet-encrypted secret storage), **Corpus** (business document ingestion → vector chunks; scheduled Confluence collectors), **Auto-Evals** (10 Tier-1 quality metrics + trend tracking + regression detection), **Pattern Engine** (strategy detection; promotion validation and A/B experiments are feature-flagged off by default), **Policy Engine** (compound pre-edit safety checks — lease, file risk, path deny, session health, recent failure), **Agent Gateway** (predict-then-act surface with fast-path cache for repeated low-risk actions), **Skills** (agent-authored via `skill_create` + docs→skills drafts under human review; server-side synthesis off by default), **Memory Improvements** (archive-first composite aging with preview/audit/restore, token budgets, LLM synthesis pass, embed input capped + shrink-to-fit).

> For the full design specification, see **[docs/DESIGN.md](docs/DESIGN.md)**.

## Dashboard

Access at `http://localhost:8040` on the host, or through a tunnel from
elsewhere ([docs/DEPLOYMENT.md → Reaching the dashboard](docs/DEPLOYMENT.md#reaching-the-dashboard)).
It has its own basic-auth login (user
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
| **Memory** | Recall search, active/archive browsers, one-click restore, maintenance preview/audit, store form, contributors, namespace/tag stats |
| **Skills** | Agent-authored + doc-derived skill cards — review, activate, edit, retire — plus the Living Procedures runbook cards: enforcement-mode badge and admin mode control, per-step observation stats, deviation view, and a NOT ACTIVELY ENFORCED warning when an armed runbook has no session holding the current bundle |
| **Knowledge** | Docs→skills ingestion (paste / URL) + the draft-skill approval queue |
| **Autopilot** | The review inbox (draft/stale/re-review skills, procedure proposals, runbook deviations, contested memory pairs, eval dead letters), the "what changed this week" digest, the Living Instructions compliance table (per-instruction rates, trend, per-runtime slices, exposure states), and the Trust Ledger card (per-agent declared/reconciled/calibration/reversals) — read-only; it proposes and reports, never mutates |
| **Patterns** | Discovered strategy cards; promotion validation and experiment controls when their feature flags are enabled |
| **Ops** | Workers, queue depths, active agents, vector store info, Discipline counter (untagged memory calls) |
| **Policy** | Runtime policy rules for pre-edit safety checks, toggle per rule |
| **Vault** | Encrypted secret management (Fernet-backed, Redis DB 7) |
| **Replay** | Session trace timelines, event inspector, root cause narrowing |
| **Evals** | Aggregate quality metrics, per-session scorecards, quality trends |
| **Devices** | Device credentials and single-use enrollment commands |
| **Members** | People and single-use member invites |

The dashboard is a zero-dependency static SPA. No build step, no npm, no framework.

## Documentation

| Document | Contents |
|----------|----------|
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Running a server: access and authentication, updating, backups, troubleshooting, local development (installing is [firekeep.ai/docs.html](https://firekeep.ai/docs.html)) |
| **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)** | All environment variables, Redis DB allocation, intelligence features |
| **[docs/MCP-TOOLS.md](docs/MCP-TOOLS.md)** | Complete MCP tools reference across the server and registered client-local backends; visible inventory varies by dex registration and feature flags |
| **[docs/DESIGN.md](docs/DESIGN.md)** | Full architecture specification, service contracts, integration points |
| **[docs/COMPARISON.md](docs/COMPARISON.md)** | Feature-by-feature comparison vs. base Claude Code |
| **[docs/SETUP-CODEX.md](docs/SETUP-CODEX.md)** | Codex integration guide |
| **[docs/SETUP-CLAUDE-CODE.md](docs/SETUP-CLAUDE-CODE.md)** | Claude Code integration guide |
| **[docs/MULTI-AGENT.md](docs/MULTI-AGENT.md)** | Agent intelligence: pre-flight briefing, session debrief, multi-agent coordination |
| **[studio/README.md](studio/README.md)** | Firekeep Studio runtime matrix, commands, security model, development, and packaging |

## Tech Stack

Python 3.11 server services (client supports Python 3.10+) / FastAPI / FastMCP / Neo4j / Qdrant / Redis / Ollama / Docker Compose / tree-sitter / TypeScript / Electron / React

Server-side embeddings and generation default to local Ollama, so the default
server path has no model API bill. Symdex can optionally use Anthropic, Gemini,
or an OpenAI-compatible endpoint for symbol summaries and scaffolding; enabling
one of those providers may send code snippets off-machine and incur provider
costs. Background auto-indexing explicitly disables AI summaries.

## Roadmap

### Working now
- Full memory lifecycle (learn, recall, decay, contradiction detection, versioning)
- Four memory types (reference / procedural / episodic / transient) with type-aware recall decay; direct learns default to episodic while raw event consolidation classifies extracted knowledge
- Token-conscious recall with optional LLM synthesis
- Archive-first composite aging (age × access × confidence) with dashboard preview, audit and restore; confirmed memories, skills and corpus chunks are never age-archived, and automatic hard purge is off by default
- Team continuity: verified workspace/member provenance plus runtime `agent_id` + `project`, contributor reports, LLM-synthesized handoff briefs
- Session persistence through context compressions, with crash detection and workspace-snapshot resumption
- Bridge replay emission on session lifecycle (started / updated / completed / abandoned)
- Docker + git + file monitoring with Relay alerting; container/commit/file activity flows to the replay stream
- Agent coordination: channels, bulletin board, fencing-token leases, structured task queue, presence registry, direct messages
- Pre-flight briefing assembles intelligence from all services at session start; surfaces discipline warnings when memory calls arrive without identity headers
- Session debrief: guided completion with task updates and lease cleanup
- Multi-agent support (task assignment, inbox polling, file lease enforcement)
- Skills: agent-authored via `skill_create` (client-side) + a docs→skills pipeline (paste / URL / scheduled Confluence collectors) drafting skills under human review; top matches injected into the next briefing (server-side auto-synthesis exists behind `SKILL_SYNTHESIS_ENABLED`, off by default)
- Night Shift: `firekeep night-shift` drains the session-distillation queue the session-end hook fills, running on your own machine against a local model — LM Studio or Ollama, auto-detected, no configuration — and writing memories plus draft skills attributed to the *original* session rather than the worker. Keeps generation off the server entirely; cloud-hosted models are refused by default so session content cannot leave the machine
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
- Knowledge Autopilot round 1 (visibility, never autonomous mutation): feedback-weighted recall + the `memory_feedback` tool, a Bridge session reaper so crashed sessions count as failures, contested-not-superseded handling for unconfirmed memory conflicts with human verdicts, the Autopilot review inbox + weekly digest, and a per-memory evidence ledger (`/memory/{id}/evidence`) — see [docs/guides/knowledge-autopilot.md](docs/guides/knowledge-autopilot.md)
- Living Instructions rounds 1 + 2 (measurement): the per-instruction compliance table on the Autopilot tab (predicates frozen to a pre-registered baseline), trend over time, and the round-2 measurement contract — instruction-content hashes stamped into the rendered block, five attribution headers, per-runtime slices, and exposed/not-exposed/unknown states with everything unverifiable reported as unknown. Rewrites under human verdict and briefing-delivered A/B variants are roadmap — see the design spec in [docs/ROADMAP.md](docs/ROADMAP.md)
- Dreaming: automated memory consolidation + person profiles (`DREAM_ENABLED`, off by default)
- Living Procedures rounds 1 + 2: skills observed as procedures with frequency/efficacy proposals under human review, and — round 2 — Enforced Runbooks: command-step matchers, human-armed `advise`/`require_ack`/`block` enforcement with success-gated evidence, a challenge→ack→one-use-permit protocol, fail-closed block mode, and a per-workspace deviation ledger surfaced on the dashboard and in the Autopilot inbox (`PROCEDURE_ENABLED`, off by default; dogfooding before any announcement) — see [docs/guides/living-procedures.md](docs/guides/living-procedures.md)
- Trust Ledger round 1: a per-agent employment record aggregated on demand from replay gateway events (`GET /autopilot/trust` + a dashboard card) — declared/reconciled counts, prediction-match calibration, reversals, sessions. Visibility only, deployment-global, frozen formulas pre-registered before the first published number. The enforcing capability broker (earned autonomy) is a later round
- Corpus tenancy hardening: member-private document sources with a shared visibility filter at every egress, source-scoped point identity (identical text across members no longer collapses to one deletable point), principal-aware source authorization, and a committed-generation gate — general infrastructure beneath the forthcoming client dexes
- One degrading local MCP gateway registered by every adapter, aggregating four remote services plus the client-local Decision Board and whichever dexes are registered
- The dex registry: dexes are the domain indexes the Keep understands, listed and switched with `firekeep dex list|add|remove` against `~/.firekeep/dexes.json`. All three wheels ship bundled and checksum-verified with every release — **registration gates activity, not installation**. Symdex and docdex are registered by default (since client 1.2.0 an absent registry is seeded with both); `firekeep dex remove` is the off-switch, and removals stick across updates — see [docs/guides/dexes.md](docs/guides/dexes.md)
- Code intelligence: client-side `firekeep-symdex` behind the gateway (38 tools, 8 analytics hidden behind a flag), mounted when registered as a dex
- Documents: client-side `firekeep-docdex` — folders a human registers (`firekeep docdex add ~/Notes`) extracted to text and ingested into the corpus, surfacing through ordinary `memory_recall`. Private to the member by default even on a shared Keep, `--shared` for the workspace; md/txt/pdf/docx, no OCR; deletion of a local file removes its corpus replica on the next sync
- Email: client-side `firekeep-maildex` — IMAP mailboxes a member registers (`firekeep maildex add`), read-only (every open is EXAMINE, every fetch PEEK; no send capability in the wheel), always member-private
- Docs→skills knowledge pipeline (`knowledge_ingest`, URL ingest) + opt-in scheduled Confluence collectors (SP3)
- Web dashboard with Devices and Members management alongside memory, coordination, replay, policy, and operations
- A2A agent card endpoint (`/.well-known/agent.json`) for external discovery
- Business knowledge ingestion: chunk documents into vector store, surface during memory recall
- Webhook notifications (Slack, Discord, generic HTTP)
- Authentication: per-key API scopes (memory:read/write, session:read/write, replay:read, eval:read, admin, …), **on by default**, with keys bootstrapped by the installer

### Promised (the two roadmap rungs published on firekeep.ai)
- **Linked instances** — multiple Firekeep servers sharing knowledge across an organisation, so what one team learns is recallable by another
- **Domain profiles** — separate experiences (coding and documents today; research ahead) as profiles of the same client kit over one shared brain: never separate products, never separate memory stores

The decision record behind both — profiles-not-clients, the linkage-layer
prerequisite, outcome-signal gating, sequencing — is
[docs/ROADMAP.md](docs/ROADMAP.md).

### Planned (smaller)
- Grafana metrics export
- Jira connector ingestion (wiki/Confluence auto-sync already shipped, opt-in)
- Skill versioning and rollback

## Status

Firekeep is in active development and used daily. The core implementations — memory, sessions, environment monitoring, coordination, replay, the client kit, and Symdex — are covered by more than 4,000 passing automated tests in the current repository. The architecture is designed for a single-host deployment.

This public, source-available repository is in early access.

## License

Firekeep is source-available under BUSL-1.1, and self-hosted production use is
free for individuals and for teams — a workspace of any size, on infrastructure
you control, free for teams while Firekeep is in early access. The commercial
tier is Enterprise governance and support (write to sales@firekeep.ai).
Each version converts to Apache-2.0 four years after its first public release.
See [LICENSE](LICENSE) for the full grant and [docs/LICENSING.md](docs/LICENSING.md)
for status. Symdex
remains under this license until its standalone Core is extracted and released
separately under Apache-2.0.
