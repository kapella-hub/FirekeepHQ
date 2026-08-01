# Firekeep

*A cognitive substrate for AI agents — the difference between an AI that forgets and one that compounds value.*

---

## The Problem

Every AI agent in production today, no matter how capable, restarts from zero on every conversation. It cannot remember last week's incident. It cannot see what a colleague just changed. It cannot prove what it did, prevent itself from touching a secret file, or learn that a particular strategy keeps failing.

The result is predictable: AI assistants that look impressive in a demo but plateau in production. They re-discover the same bugs, repeat the same mistakes, leave no audit trail, and need a human babysitter for any consequential action.

**Firekeep is the missing layer between "an AI model" and "an AI workforce."**

---

## What It Does

Firekeep gives every AI agent — Claude, Cursor, Codex, custom Python — five capabilities that compound over time:

| Capability | What it changes | Where it shows up |
|------------|-----------------|-------------------|
| **Long-term memory** | Agents recall prior decisions, incidents, and resolutions across sessions, weeks, and team members | Faster debugging, fewer repeat mistakes, instant onboarding |
| **Session continuity** | Mid-task context survives context-window compression and crashes | No more "what was I doing?" after long sessions |
| **Multi-agent coordination** | Agents claim files, post tasks, send direct messages, and avoid stepping on each other | Parallel work without merge conflicts or stale leases |
| **Observability & replay** | Every memory read, every action, every decision is logged with structured traces | Full audit trail for compliance and post-mortems |
| **Predict-then-act safety** | A policy engine evaluates consequential actions *before* they happen — blocks edits to `.env`, `*.key`, and other risky targets | Risky operations gated automatically; humans only paged for true ambiguity |

These aren't isolated features. They feed each other: actions produce traces, traces produce patterns, patterns become skills, skills get injected back into future sessions.

---

## The Compounding Loop

The most under-appreciated property of Firekeep is that **agents on it get better with use, without code changes.**

1. An agent works through a hard session — say, debugging a tricky migration.
2. The agent authors a **Skill** from what it learned — a structured "what to do when X happens" entry — via the `skill_create` tool, using its full session context. (A server-side 4-signal auto-synthesizer exists behind `SKILL_SYNTHESIS_ENABLED`, but it is off by default: the CPU-only Ollama box can't run the generation LLM in workable time, so the default deploy relies on client-authored skills plus draft skills mined from the docs→skills pipeline.)
3. Next time *anyone* on the team faces a similar task, the briefing hook injects the relevant skills into their session before they type their first prompt.
4. Pattern engine watches outcomes across sessions. Strategies that correlate with success get promoted (`candidate → observed → trial → validated`). Strategies that fail silently get retired.
5. A/B testing randomly withholds tips from a control group to prove causal lift, not just correlation.
6. The dashboard's Evals tab shows quality trends — success rate, failure rate, memory freshness, claim contention — over time, per agent, per project.

> **Availability:** the promotion ladder and A/B validation ship disabled
> (`PATTERN_VALIDATION_ENABLED`, `PATTERN_EXPERIMENTS_ENABLED`). Pattern
> *detectors* run and surface descriptively from the first session; statistical
> promotion needs roughly 25 sessions of history.

Six months in, the system isn't running on the playbook it shipped with. It's running on a playbook it built from your team's actual work.

---

## What's Deployed Today

Firekeep is not a roadmap. It is a running system.

### Server services (four, in one stack)

| Service | Purpose |
|---------|---------|
| **FirekeepCortex** | Long-term memory: semantic (Qdrant) + graph (Neo4j) hybrid recall, with LLM synthesis trimmed to a token budget |
| **FirekeepBridge** | Session continuity across context compressions and crashes |
| **FirekeepSentinel** | Environment observer — git, container, file activity |
| **FirekeepRelay** | Agent-to-agent coordination: leases, presence, tasks, direct messages |

### Client-side (stdio, installed with the kit)

Two always-on local MCP backends run next to the agent behind the single
`firekeep` stdio gateway, not on the VPS:

| Server | Purpose |
|--------|---------|
| **FirekeepSymdex** (`firekeep-symdex`) | Code intelligence via tree-sitter AST parsing — 38 tools (30 visible, 8 analytics hidden) across 12 languages. Token savings depend entirely on the task — see [Symdex token savings](#symdex-token-savings-measured) for the measured figures and their scopes. Must be local to the working tree it indexes, so it ships client-side (no VPS container) |
| **FirekeepDecision** (`firekeep-decision`) | Globally-informed local clarification board — synthesizes clarifying questions from the whole team's memory (Cortex `POST /decision/synthesize`) and answers them in the human's browser |

### Shared infrastructure

- **Replay Engine** — structured trace log; every memory read/write and lifecycle event is recoverable
- **Vault** — Fernet-encrypted secret storage with REST + MCP access
- **Auth** — scope-based API key management
- **Corpus** — business knowledge ingestion (manuals, runbooks) that surfaces naturally in agent recall
- **Agent Gateway** — predict-then-act surface for any agent runtime
- **Pattern Engine** — six pattern detectors, promotion ladder, A/B testing, cross-agent learning

### What it integrates with

- **Claude Code** — first-class hooks at SessionStart, Stop, PreToolUse, UserPromptSubmit, PreCompact, PostToolUse
- **Any MCP client** — Cursor, Codex, Kiro, custom runtimes via standard MCP tools
- **Any HTTP-capable agent** — REST endpoints mirror every MCP tool

---

## The Numbers

| What | Value |
|------|-------|
| Memory recall (P50), `format="raw"` | **9.8 ms** measured 2026-07-26 on a fresh install |
| Memory recall (P50), **shipped default** (synthesis on, CPU Ollama) | **30.0 s — see below** |
| Policy check | **4 ms** |
| Pattern query | **3 ms** |
| Vault retrieve | **3 ms** |
| Session list | **3 ms** |
| Direct-message send | **4 ms** |
| Endpoints under 50 ms | **47 of 48** |
| Test coverage | Automated test suites across all services and the client kit |
| External SaaS dependencies | **Zero** — Neo4j, Qdrant, Redis, Ollama all self-hosted |
| API costs to Anthropic / OpenAI for memory or recall | **Zero** — embeddings and synthesis run on local Ollama |

> **Unmeasured.** The synthesis-path figure has not been measured. This document must not be published or shown to a prospect until it is.

> **Read the second row before quoting the first.** Measured 2026-07-26 against a real
> `docker compose` install on commodity hardware: with the shipped defaults, `memory_recall`
> takes **30 seconds**, and the five samples landed within 19 ms of each other
> (30.013 / 30.014 / 30.015 / 30.016 / 30.031 s). That flatness is the tell — it is not work
> taking 30 s, it is the hardcoded 30 s timeout in `cortex/app/engine/rag.py:127` expiring on
> every call. The LLM synthesis pass never completes on CPU; recall falls back to the raw
> context block, so the caller waits 30 s to receive **exactly what `format="raw"` returns in
> 9.8 ms**.
>
> The historical **387 ms** figure was measured on a populated instance and is retained for
> comparison only. The 9.8 ms above is a fresh install holding one memory and is *not*
> representative of a loaded store either. Neither number should be quoted without saying which
> configuration and how much data produced it.
>
> **Mitigation shipped 2026-07-26:** `RECALL_SYNTHESIS_ENABLED` now defaults to `false`. Enable
> it only where a fast generation backend exists — on CPU it costs 30 s per call and returns
> nothing extra.

### Symdex token savings (measured)

Measured by `symdex/benchmarks/benchmark_runner.py` against `pallets/click` (17 files,
767 symbols): 20 questions × 3 runs = **60 paired samples**. Each question is answered twice
— once from a Symdex-built context, once from raw file reads — and an LLM judge scores both
for accuracy, completeness and relevance on a 5-point rubric. Savings without a quality
control mean nothing, since returning less always saves tokens.

| Task type | Context tokens saved | Symdex accuracy | Raw accuracy | n |
|---|---|---|---|---|
| **Comprehension** ("how does X work") | **−45.8%** | 4.62 | 4.67 | 21 |
| **Navigation** ("where is X") | **−20.8%** | 4.43 | 4.43 | 21 |
| **Modification** ("add X") | **+37.8% (more)** | 4.17 | 4.22 | 18 |
| **Mixed workload, all 60** | **−12.0%** | **4.42** | **4.45** | 60 |

Overall: **12% fewer context tokens at accuracy that is statistically indistinguishable**
(4.42 vs 4.45; 37 of 60 comparisons were exact ties). Completeness and relevance came out
marginally *higher* for Symdex (4.62 vs 4.52, 4.98 vs 4.93).

**Modification tasks deliberately cost more.** `build_symdex_context` routes them to raw files
*plus* structural intelligence, on the theory that extending code needs full context. The
measurement does not support a quality gain from that (4.17 vs 4.22) — it is 38% more tokens
for the same answer. Stated because a prospect running a modification-heavy workload will
measure a negative number, and should hear it here first.

**Single targeted lookups are a different scope entirely.** Fetching one function costs ~280
tokens against ~6,755 to read its file — **96%** (`symdex/README.md`). That is the number this
document used to quote as a flat product claim. It is real, but it describes one lookup, not
one task, and a customer's bill is per task. Both scopes are given above so neither can be
mistaken for the other.

**Two limits worth knowing.** `stdev` on per-question savings is 40 points — the mean hides a
range from −63% to +79%. And 24 context-construction strategies were evaluated against these
same 20 questions on this same repository, so 12% is an **in-sample** figure: it is the
honest ceiling on what can be claimed today, not a prediction for another codebase. A
held-out repository is the next measurement.

Cost surface is bounded by what you choose to spend on the LLM tier; the cognitive infrastructure itself runs on commodity hardware.

---

## What Management Actually Sees

The **dashboard** on port 8040 is the public face — "public" to the operator, not
to the network: it binds to loopback by default and sits behind its own login,
so reaching it from a laptop means an SSH tunnel or a deliberate decision to
expose it. It is not a feature museum — it is operational:

- **Today's events** — live count of memory writes, recalls, coordination actions
- **Agent inventory** — who's online, what they're working on, who's holding which leases
- **Quality trends** — Tier 1 metrics computed on every session completion, with improving / stable / degrading arrows
- **Patterns** — discovered strategies with confidence scores (effectiveness lift versus control sessions requires the A/B validation pass, disabled by default — see Availability note above)
- **Skills** — agent-authored playbooks (via `skill_create`) plus draft skills mined from the docs→skills pipeline; draft → active workflow under human review
- **Replay** — drill into any past session, see every decision and the context at any point in time

Compliance officers see audit trails. Engineers see why an agent did something. Leaders see whether the AI investment is working.

---

## Differentiation

> "We already pay for Claude / Copilot / Cursor. Why this?"

Off-the-shelf AI assistants give you a smart contractor with amnesia and no supervisor. Firekeep gives that contractor:

- **A notebook they read before starting** (briefing + memory recall)
- **A team to coordinate with** (leases, presence, tasks)
- **A logbook of what they did and why** (replay)
- **A policy manual they must follow** (gateway + policy engine)
- **A mentor that reviews their work and updates the playbook** (pattern engine + agent-authored and doc-derived skills)

It is fully model-agnostic. Cortex is the memory layer regardless of whether the agent on top is Claude, GPT, Gemini, or local Llama. The investment compounds across model upgrades, not against them.

### What changed in the last hardening pass (2026-07-26 → 29)

Stated because most of it is the kind of thing a security review asks for and most solo
products cannot answer:

- **The default install was insecure and is not any more.** It shipped `AUTH_ENABLED=false`,
  and with auth off every caller was handed wildcard scope — `GET /vault/secrets` and
  `POST /auth/keys` were open to anyone who could reach the port, on ports bound to `0.0.0.0`.
  Auth is now on by default, the anonymous identity no longer carries `admin`, and ports bind
  loopback. Verified on the project's own deployment, where that configuration had exposed
  real secrets.
- **`bash install.sh` exited 1 on every clean install** — a service-table parsing bug meant
  every health probe hit a malformed URL. Fixed, with a regression test that executes the
  shell expansion rather than re-implementing it.
- **Test frameworks were shipping inside the production images** and were the only CVE in the
  shipped dependency set. Removed; the gate now starts from zero.
- **Sentinel mounted the Docker socket read-write** (host-root-equivalent) and the whole
  repository including `.env`. Neither did anything by default; both are gone, the docker
  collector is opt-in.
- **Backups were not restorable.** They tarred live databases — Neo4j Community has no
  online-backup facility, so the archive could capture a torn write and restore without error.
  Now quiesced, and verified by a full wipe-and-restore round trip.

**Not yet done, and load-bearing for a first sale:** no licence-key mechanism, no seat
counting, no entitlement surface — so the free/paid boundary is currently unenforceable. No
release tag has been cut, so nothing is published for `--pull` to fetch yet.

---

## Operational Profile

| Property | Value |
|----------|-------|
| **Containers** | 13 (Cortex API/MCP/worker/beat, Bridge, Sentinel, Relay, dashboard, four data stores, + one init) |
| **Deployment** | Single VPS, `docker compose up -d`; helper scripts for VPS provisioning and updates |
| **Data stores** | Neo4j (graph), Qdrant (vectors), Redis (cache / streams / queues), Ollama (LLM inference) — all self-hosted |
| **Local dev story** | `./install` / `firekeep install` installs the portable `firekeep-client` kit (one `[identity]` + `[server]` connection in `~/.firekeep/config`, `firekeep-shim` transport, hook cores, sidecar); agents connect to the Firekeep server over HTTP(S) with keyed, attributed identity |
| **Security posture** | Closed by default: app ports bind to localhost (`BIND_ADDR`, opt-in to widen); the datastores are loopback-only unconditionally and no setting exposes them; per-key scoped API auth is **on** (`AUTH_ENABLED=true`) with keys minted by the installer. Plus: Fernet-encrypted vault, pre-edit policy engine, deny-list for sensitive paths (`.env`, `*.key`, `*.pem`) |
| **Symdex languages** | Python, JavaScript, TypeScript, Go, Rust, Java, PHP, C, C#, Ruby, Kotlin, Swift |
| **Delivery** | Four publicly pullable service images plus a checksummed, source-free deployment bundle on each `vX.Y.Z` tag; `firekeep init` needs no source or registry credential. Developers build from a checkout with the same compose file |
| **Reproducible builds** | Every Python dependency hash-pinned in a per-service `requirements.lock`; every base and datastore image pinned to an immutable digest. Two builds of one commit produce the same images |
| **Supply chain** | `pip-audit --strict` gates each shipped dependency set in CI (currently zero CVEs), CycloneDX SBOM per artifact, gitleaks over the working tree and full history |
| **Disaster recovery** | `deploy/backup.sh` quiesces Neo4j/Qdrant/Redis before snapshotting, so the archive is actually restorable, and restarts them on every exit path. `update.sh` takes a backup before it rebuilds and refuses `--no-backup` if a datastore image moved — Neo4j store upgrades are one-way |
| **Disclosure** | `SECURITY.md` with scope and response targets; `docs/THREAT-MODEL.md` covering all four services, the client kit and the URL crawler, listing what is *not* mitigated |


---

## The Demo (10 minutes)

**Set up before the room fills.** Every angle below ends at a dashboard tab, and the
dashboard binds to loopback by default — run the demo on the host itself, or bring the
tunnel up in advance (`ssh -L 8040:127.0.0.1:8040 user@host`) and confirm the page loads.
Have the dashboard login to hand. Discovering this in front of management is a bad ten
minutes.

For a management briefing, run one real task end-to-end. Three angles, pick one:

1. **Memory + skills** — Open a fresh session on a repo. Briefing hook injects relevant patterns and skills from past work. Agent solves a task that would otherwise require ramp-up. Show the Evals tab afterwards — the session contributes a data point to the trend.
2. **Safety** — Ask an agent to "tidy up the .env file." Watch the policy engine block the edit, log the reason to replay, and return a structured `block` decision. Open the replay viewer and inspect the trace.
3. **Compounding value** — Open the Patterns and Skills tabs. Show entries the system auto-discovered without anyone writing rules. Click one — see the supporting sessions and the confidence score. (Effectiveness lift
versus control sessions requires the A/B validation pass, which is disabled by
default and needs ~25 sessions of history — do not demo it on a fresh install.)

The throughline in any demo: *we are not showing you a smarter AI. We are showing you an AI that gets smarter.*

---

## Status & Direction

Firekeep is in active production use. Recent direction:

- **Audit & consolidation** — recent passes removed ~5,500 LOC of write-only machinery (modules that produced data nothing read), keeping the surface honest
- **Observability discipline** — `/admin/untagged-calls` surfaces calls arriving without identity headers, turning telemetry hygiene into a measurable habit
- **Agent Gateway** — predict-then-act flow now covers any MCP-capable runtime, not just Claude Code
- **Cross-agent learning** — patterns discovered by one agent's sessions surface in briefings for others (gated per-agent to keep cohorts clean)

The internal rule: write-only code is a bug. The bias is toward features that get *used*, not features that ship.

---

## In One Sentence

**Firekeep turns AI assistants from amnesiac contractors into a team that remembers, coordinates, learns, and is accountable — running on your infrastructure, on your data, under your policy.**
