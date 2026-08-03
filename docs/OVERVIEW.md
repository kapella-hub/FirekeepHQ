# Firekeep — Quick Overview

**Self-hosted memory, safety, and learning infrastructure for AI coding agents.**

---

## What It Does

Firekeep sits behind your AI coding agent (Claude Code, Cursor, Aider) and gives it superpowers:

- **Remembers everything.** What worked, what failed, what your business does — across sessions.
- **Stops unsafe edits.** Policy engine blocks risky file changes before they happen.
- **Gets smarter over time.** Each session's replay data feeds pattern discovery. Proven strategies appear in the next session's briefing.
- **Coordinates multiple agents.** File locks, task queues, direct messages. No more agents overwriting each other.
- **Asks the right questions.** When a task is ambiguous, the local Decision Board synthesizes clarifying questions from the whole team's accumulated memory — you answer once, in your browser.
- **Full visibility.** Every action traced. Quality metrics computed. Trends tracked.

---

## How It Works

```
Your Agent ── stdio ──► local Firekeep gateway ── MCP ──► Firekeep (your VPS)
                                                              │
                    ┌─────────┼─────────┐
                    │         │         │
                 Memory    Safety    Learning
                    │         │         │
                 Recall    Policy    Patterns
                 Learn     Block     Experiments
                 Corpus    Warn      A/B Testing
```

**4 core services + dashboard. 13 containers. 6 logical MCP backends, ~102 tools, one client-visible gateway. One `docker compose up`.**

---

## The Numbers

| What | How Fast |
|------|----------|
| Memory recall (`format="raw"`) | 387ms |
| Policy check | 4ms |
| Pattern query | 3ms |
| Vault retrieve | 3ms |
| Session list | 3ms |
| DM send | 4ms |
| 47 out of 48 endpoints | Under 50ms |

**Automated test suites across all services. Zero cloud dependencies. Zero API costs.**

---

## The Learning Loop

Every session makes the next one better:

1. Agent works → replay traces everything
2. Session ends → 10 quality metrics computed
3. Patterns discovered → strategies promoted through candidate → trial → validated
4. Next session → briefing includes tested tips
5. Bad tips? Quarantine instantly. A/B testing proves what works.

---

## Get Started

```bash
# VPS
git clone <repo> && cd Firekeep && bash install.sh

# Your machine (teammate bootstrap — fetches a checksum-verified wheel):
curl -fsSL https://kapella-hub.github.io/firekeep-dist/latest/install.sh \
  | FIREKEEP_DIST_BASE=https://kapella-hub.github.io/firekeep-dist sh

# ...or from a checkout:
cd client && ./install     # or: firekeep install --runtime claude
```

Next time you open Claude Code, it has memory, safety, and learning — automatically.

---

*Replay + evals + pattern learning turn each agent session into better briefings for the next one.*
