# Firekeep vs. Base Claude Code

> **Replay + evals + pattern learning turn each agent session into better briefings for the next one.**

---

## What Changes When You Add Firekeep

| Capability | Without Firekeep | With Firekeep |
|---|---|---|
| **Memory** | Every session starts fresh. Agent re-discovers the same things. | Persistent semantic + graph memory. Agents recall what worked, what failed, and why — across sessions, across weeks. |
| **Session continuity** | Lost on context compression. | Plans, decisions, progress survive compression. `ctx_get_shadow` restores full state. |
| **Business knowledge** | Agent knows nothing about your company. | Ingest wiki pages, Jira tickets, API docs. Business context flows into every `memory_recall`. |
| **Environment awareness** | Agent doesn't know what's running. | Docker health, git commits, file changes monitored. Agents see real system state. |
| **Pre-edit safety** | Agent can edit anything. | Policy engine checks 5 rules before every edit. Blocks `.env`, `*.key`, risky files. |
| **Multi-agent coordination** | Agents overwrite each other's files. | File leases, task queue, direct messages, bulletin board, presence registry. |
| **Strategy learning** | Agent doesn't learn what works. | Pattern engine discovers strategies. Promotion ladder prevents bad tips. A/B tested. Quarantine safety net. |
| **Experiments** | You guess whether the agent did well. | Named datasets, chi-square tests, effect size CI. Controlled experiments on strategy effectiveness. |
| **Replay & debugging** | No trace of what happened. | Every action recorded. Timeline viewer. Root cause narrowing. |
| **Quality metrics** | None. | 10 auto-computed metrics per session. Trends over time. Regression detection. |
| **Code intelligence** | Reads files sequentially. | 38 tree-sitter tools via the client-installed `firekeep-symdex` stdio server: symbol search, caller graphs, architecture maps, impact analysis. |
| **External interop** | None. | A2A Agent Card discovery (`GET /.well-known/agent.json`) for external registries. |
| **Secrets** | Plain text in env vars. | Fernet-encrypted vault. Store/retrieve via MCP. |
| **Ambiguous requirements** | Agent guesses, or asks inline one at a time. | Decision Board (`firekeep-decision`) synthesizes clarifying questions from the whole team's memory; you answer once in your browser. |

---

## Performance (Live Benchmark — March 25, 2026)

**48 endpoints. 47 return HTTP 200. Median response: 4ms.**

| Tier | Time | Count | Examples |
|------|------|-------|---------|
| **Instant** (1-5ms) | ≤5ms | 31 | Policy evaluate, pattern queries, vault CRUD, relay DMs, health checks |
| **Fast** (5-50ms) | 5-50ms | 11 | Memory stats, corpus sources, eval trends, session list |
| **Standard** (50-500ms) | 50-500ms | 4 | Memory recall (387ms, `format="raw"`), corpus ingest (440ms), corpus delete (40ms) |
| **Embedding** (500ms+) | ~1.2s | 1 | Memory learn (embedding generation via Ollama) |

### By Service

| Service | Endpoints | Median |
|---------|----------|--------|
| Cortex (Memory, Corpus, Vault, Evals, Patterns, Policy, Replay) | 33 | 5ms |
| Bridge (Sessions) | 3 | 3ms |
| Relay (Coordination, DMs, A2A discovery) | 8 | 2ms |
| Sentinel (Monitoring) | 1 | 2ms |
| Symdex (Code Intelligence)¹ | 1 | 4ms |

¹ This benchmark predates the move of Symdex to a client-installed stdio server (`firekeep-symdex`); it is no longer a VPS-hosted HTTP endpoint.

---

## The Self-Improvement Loop

```
Agent works → Replay captures everything → Session completes →
10 metrics computed → Patterns discovered → Strategies promoted →
Next session briefing includes tested tips → Agent works better →
Feedback refines recall → Quality trends detect regressions → Repeat
```

**Pattern promotion ladder prevents bad advice:**

```
candidate → observed → trial → validated → stale → retired
                                  ↓
                             quarantined (instant kill switch)
```

Only `trial+` patterns with `procedural` or `risk` category appear in briefings. Max 3 tips. Behavioral correlations stay in analytics only.

**Experiments validate strategies rigorously:**
- Named datasets (filtered session subsets)
- Chi-square significance tests (p < 0.05)
- Cohen's h effect size with 95% CI
- Treatment vs. control group comparison

> **Availability:** the promotion ladder and A/B validation ship disabled
> (`PATTERN_VALIDATION_ENABLED`, `PATTERN_EXPERIMENTS_ENABLED`). Pattern
> *detectors* run and surface descriptively from the first session; statistical
> promotion needs roughly 25 sessions of history.

---

## Real Workflow

1. **Session starts.** Briefing hook assembles intelligence from all services: environment health, pending tasks, DMs, strategy tips (A/B tested), and active skills.

2. **Agent works.** Every `memory_recall`, `memory_learn`, `ctx_update` is traced with session context. Policy engine checks each edit before it happens.

3. **Session completes.** Replay trace → 10 eval metrics → feature extraction → pattern analysis. Learnings from this session improve the next one.

4. **Something went wrong?** Replay tab → load session → see every action → run narrowing → find root cause in minutes.

---

## Concrete Numbers

| Metric | Value |
|--------|-------|
| Services | 4 server-side + 2 client-stdio MCP servers + 7 shared modules |
| Docker containers | 13 |
| MCP tools | 102 across 6 servers (cortex 27, bridge 7, sentinel 3, relay 25, symdex 38, decision 2) |
| REST endpoints | 60+ |
| Tests | Automated suites across all services and the client kit |
| Pattern detectors | 6 (memory-first, file hotspot, tool sequence, memory usage, duration, failure mode) |
| Policy rules | 5 (lease, file risk, path deny, session health, recent failure) |
| Eval metrics | 10 Tier 1 (auto) + optional Tier 2 (LLM-judged) |
| Auth scopes | 10 per-tool scopes |
| Setup time | `bash install.sh` (server) + `./install` / `firekeep install` (client kit) |
| Protocol | MCP (HTTP + client-stdio); A2A discovery-only (`/.well-known/agent.json`) |
| Infrastructure | Neo4j, Qdrant, Redis, Ollama — all self-hosted, zero cloud dependency |

---

## What Firekeep Is NOT

- **Not an agent framework.** It doesn't orchestrate agents. It makes your existing agents better.
- **Not a chatbot wrapper.** No prompt engineering. Infrastructure, not orchestration.
- **Not cloud-dependent.** Everything runs on your VPS. Zero API costs.
- **Not a connector catalog.** Your agent fetches from Jira/Slack. Firekeep handles what happens after.

---

*Firekeep: Because your AI assistant shouldn't have amnesia.*
