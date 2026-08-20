# Firekeep Design Spec

**Date:** 2026-03-13
**Status:** Approved
**Author:** Alex + Claude

## Overview

Firekeep is a unified deployment repository that consolidates five Firekeep services into a single cognitive stack for AI agents. It combines code intelligence, persistent memory, session context, environment observation, and agent communication into one deployable unit.

### Deployment Topology

- **VPS (docker-compose):** FirekeepCortex, FirekeepBridge, FirekeepSentinel, FirekeepRelay, Dashboard, infrastructure (Neo4j, Qdrant, Redis, Ollama)
- **Local (client kit):** the Decision Board (`firekeep-decision`) — an always-installed, always-mounted stdio MCP server that runs next to the agent for fast local file access — plus the **dexes**, the domain indexes the Keep understands: FirekeepSymdex (`firekeep-symdex`, code) and FirekeepDocdex (`firekeep-docdex`, documents). Both dex wheels are always installed; the dex registry (`~/.firekeep/dexes.json`, `firekeep dex add|remove`) decides which of them are active — see [guides/dexes.md](guides/dexes.md)

## Repository Structure

```
Firekeep/
├── symdex/                  # FirekeepSymdex — client-stdio package source (not a VPS service)
│   ├── src/firekeep_symdex/
│   ├── pyproject.toml
│   └── Dockerfile           # legacy HTTP image — unused; symdex now ships client-side (stdio)
├── cortex/                  # FirekeepCortex (from FirekeepCortex/)
│   ├── app/
│   ├── requirements.txt
│   └── Dockerfile
├── bridge/                  # FirekeepBridge (from CortexBridge/, full rename)
│   ├── app/
│   ├── requirements.txt
│   └── Dockerfile
├── sentinel/                # FirekeepSentinel (new)
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI + FastMCP (webhook intake + MCP tools)
│   │   ├── config.py        # Pydantic settings (NS_ prefix)
│   │   ├── models.py        # Event models
│   │   ├── collectors/
│   │   │   ├── docker.py    # Docker API polling (container health)
│   │   │   ├── git.py       # Git activity (commits, branch changes)
│   │   │   └── files.py     # Filesystem watcher
│   │   ├── store.py         # Redis streams for event storage
│   │   └── mcp_server.py    # FastMCP HTTP server
│   ├── requirements.txt
│   └── Dockerfile
├── relay/                   # FirekeepRelay (new)
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py          # FastAPI + FastMCP (REST API + MCP tools)
│   │   ├── config.py        # Pydantic settings (NR_ prefix)
│   │   ├── models.py        # Message models
│   │   ├── pubsub.py        # Redis pub/sub manager
│   │   ├── bulletin.py      # Persistent bulletin board (Redis sorted sets)
│   │   └── mcp_server.py    # FastMCP HTTP server
│   ├── requirements.txt
│   └── Dockerfile
├── dashboard/               # Unified static SPA
│   └── index.html           # Self-contained, zero dependencies, dark theme
├── docker-compose.yml       # All VPS services (13 containers)
├── .env.example             # Combined config for all services
├── install.sh               # Fresh VPS setup script
├── update.sh                # Pull + rebuild + restart
├── client/                 # Portable client kit (firekeep-client: shim, hook cores, sidecar, adapters, CLI)
└── README.md
```

## Service Map

| Port  | Service       | Container      | Purpose                              |
|-------|---------------|----------------|--------------------------------------|
| 7474  | Neo4j HTTP    | neo4j          | Graph DB (localhost only)            |
| 7687  | Neo4j Bolt    | neo4j          | Graph DB (localhost only)            |
| 6333  | Qdrant        | qdrant         | Vector DB (localhost only)           |
| 6379  | Redis         | redis          | Cache/queue/storage (localhost only) |
| 11434 | Ollama        | ollama         | LLM inference (localhost only)       |
| 8100  | Cortex API    | cortex-api     | REST API (`GET /health` included)    |
| 8080  | Cortex MCP    | cortex-mcp     | MCP tools (memory)                   |
| —     | Cortex Worker | cortex-worker  | Celery sleep cycle                   |
| —     | Cortex Beat   | cortex-beat    | Celery scheduler                     |
| 8070  | FirekeepBridge   | bridge         | MCP tools (session context)          |
| 8060  | FirekeepSentinel | sentinel       | MCP tools + REST (environment)       |
| 8050  | FirekeepRelay    | relay          | MCP tools + REST (agent comms)       |
| 8040  | Dashboard     | dashboard      | nginx serving static SPA             |

**Total: 13 containers** on VPS (12 services + 1 init container `ollama-pull`). FirekeepSymdex is no longer a VPS container — it ships client-side as the `firekeep-symdex` stdio MCP server (see below).

### Redis DB Allocation

| DB | Service               |
|----|-----------------------|
| 0  | Cortex data           |
| 1  | Celery broker         |
| 2  | Celery results        |
| 3  | FirekeepBridge           |
| 4  | FirekeepSentinel         |
| 5  | FirekeepRelay            |
| 6  | Replay Engine + Evals |
| 7  | Auth (API keys)       |

## Rename: CortexBridge → FirekeepBridge

Full rename scope:

| What                    | From               | To               |
|-------------------------|--------------------|------------------|
| Directory               | `CortexBridge/`    | `Firekeep/bridge/` |
| Env var prefix          | `CB_`              | `NB_`            |
| Redis key prefix        | `cb:`              | `nb:`            |
| Docker compose service  | `cortexbridge`     | `bridge`         |
| MCP server name         | `cortex-bridge`    | `firekeep-bridge`   |
| `.claude.json` MCP entry| `cortex-bridge`    | `firekeep-bridge`   |
| Distiller namespace     | `cortexbridge`     | `firekeepbridge`    |
| Config `app_name`       | —                  | `FirekeepBridge`    |

**Not renamed:** Python package (`app/`), internal class names.

## FirekeepSentinel — Environment Observer

### Purpose
Watch the environment and surface events to agents. Hybrid push (webhook intake) + pull (polling collectors).

### Storage
Redis DB 4, key prefix `ns:`. Events stored in Redis streams. Retention enforced via `MAXLEN` approximate trimming on each write (default 10,000 entries) plus a background task every hour that trims entries older than `NS_EVENT_RETENTION_HOURS` by timestamp ID.

### Collectors

| Collector | Watches                               | Default Interval |
|-----------|---------------------------------------|-----------------|
| Docker    | Container health, restarts, resources | 30s             |
| Git       | Commits, branch changes on repos      | 60s             |
| Files     | File changes in watched directories   | 30s             |

### Webhook Intake
`POST /events/ingest`
```json
{
  "source": "github-actions",
  "event_type": "build.failed",
  "summary": "Tests failed on main",
  "details": {"repo": "Firekeep", "run_id": 123},
  "severity": "error",
  "tags": ["ci", "tests"]
}
```

### MCP Tools (port 8060, `/mcp`)

| Tool                  | Purpose                                           |
|-----------------------|---------------------------------------------------|
| `sentinel_get_events` | Recent events, filter by source/type/severity/time |
| `sentinel_get_health` | Health status of all docker-compose services       |
| `sentinel_push_event` | Agent pushes an observation                        |

### Configuration (NS_ prefix)
- `NS_REDIS_URL=redis://redis:6379/4`
- `NS_MCP_HOST=0.0.0.0`
- `NS_MCP_PORT=8060`
- `NS_DOCKER_SOCKET=/var/run/docker.sock`
- `NS_POLL_INTERVAL_DOCKER=30`
- `NS_POLL_INTERVAL_GIT=60`
- `NS_POLL_INTERVAL_FILES=30`
- `NS_EVENT_RETENTION_HOURS=72`
- `NS_EVENT_MAXLEN=10000`

### Health
`GET /health` — returns `{status: "ok", redis: bool, collectors: {docker: bool, git: bool, files: bool}, event_count: int}`

### Security Note
The Docker collector requires mounting the Docker socket (`/var/run/docker.sock:/var/run/docker.sock`). This grants the Sentinel container read access to the Docker API. The container runs as non-root (uid 1000) but the socket mount is still a privileged operation — acceptable for a private VPS, not suitable for shared infrastructure.

### Files Collector Scope
The files collector watches directories mounted into the Sentinel container. By default, the docker-compose mounts the Firekeep repo root as read-only (`./:/watch:ro`). Additional watch paths can be configured via `NS_WATCH_PATHS` (comma-separated).

## FirekeepRelay — Agent Communication

### Purpose
Let agents coordinate in real-time and share persistent context across sessions, terminals, and human operators.

### Storage
Redis DB 5, key prefix `nr:`.

### Two Communication Modes

**1. Channels (real-time, ephemeral)** — Redis pub/sub
- Topic-based channels: `build`, `deploy`, `debug`, `general`, or custom
- Fire-and-forget delivery
- Backlog buffer: last 50 messages per channel in Redis list for late-joining agents

**2. Bulletin Board (persistent, TTL-based)** — Redis sorted sets
- Posts persist for configurable TTL (default 24h)
- Scored by timestamp, queryable by tag/author/recency
- Used for cross-session context and human→agent communication

### MCP Tools (port 8050, `/mcp`)

| Tool              | Purpose                                        |
|-------------------|------------------------------------------------|
| `relay_broadcast` | Send message to a channel (real-time)          |
| `relay_get_messages` | Get recent messages from channel backlog (poll-based, not real-time) |
| `relay_post`      | Post to bulletin board (persistent)            |
| `relay_read`      | Read bulletin board, filter by tags/author     |
| `relay_claim`     | Claim a task/file to prevent duplicate work    |
| `relay_release`   | Release a claimed task/file                    |
| `relay_status`    | Active agents, claims, channel activity        |

### Claim System
- `relay_claim` uses Redis SETNX for atomic locking
- Claims auto-expire (default 30 min) to handle crashed agents
- Key pattern: `nr:claim:{resource_id}` → `{agent_id, timestamp, ttl}`
- **Conflict behavior:** If a resource is already claimed, `relay_claim` returns `{claimed: false, held_by: agent_id, expires_in: seconds}` so the caller knows who holds it and when it frees up

### Configuration (NR_ prefix)
- `NR_REDIS_URL=redis://redis:6379/5`
- `NR_MCP_HOST=0.0.0.0`
- `NR_MCP_PORT=8050`
- `NR_BULLETIN_TTL_HOURS=24`
- `NR_CHANNEL_BACKLOG_SIZE=50`
- `NR_CLAIM_TTL_MINUTES=30`

## Unified Dashboard

### Purpose
Single web UI to observe and interact with the entire stack. Static SPA served by nginx on port 8040.

### Tech
- Single `index.html`, vanilla JS, CSS grid
- Zero external dependencies
- Dark theme
- Talks directly to each service API via fetch

### Tabs

Fourteen tabs (`data-tab` attributes in `index.html`):

| Tab        | Data Source       | Features                                              |
|------------|-------------------|-------------------------------------------------------|
| Overview   | All services      | Health grid, active sessions, recent events, relay    |
| Sessions   | Bridge :8070      | Active/paused/completed, view shadow context          |
| Events     | Sentinel :8060    | Live event feed (polling), service health, filtering  |
| Relay      | Relay :8050       | Bulletin board, channel activity, claims, **post form** |
| Scope      | Relay :8050       | Scope-clarification sessions — answer open screens    |
| Memory     | Cortex :8100      | Browse/search, graph viz, **manual text input form**, DLQ |
| Skills     | Cortex :8100      | Team playbooks; draft → active review                 |
| Knowledge  | Cortex :8100      | Docs→skills ingestion + draft-skill approval queue    |
| Patterns   | Cortex :8100      | Discovered strategies, confidence, effectiveness lift |
| Replay     | Cortex :8100      | Per-session trace timeline, context-at, narrowing     |
| Evals      | Cortex :8100      | Tier-1 quality metrics, trend arrows                  |
| Vault      | Cortex :8100      | Encrypted secret metadata (list/store/delete)         |
| Policy     | Cortex :8100      | Policy rules + block/rethink decision audit log       |
| Operations | Cortex :8100      | Celery workers + Redis queue depths                   |

### Manual Text Input (Memory tab)
Form fields: Domain, Namespace, Action, Outcome, Tags → calls `POST /memory/learn` on Cortex.

### Manual Post (Relay tab)
Form fields: Channel/Board, Message, Tags → calls relay API.

### CORS
All services read `CORS_ORIGINS` from the shared `.env` file. Default value includes the dashboard origin:
```
CORS_ORIGINS=["http://<VPS_IP>:8040"]
```
The `install.sh` script sets this automatically from the host address it detects
(`--ip` / `FIREKEEP_VPS_IP` override it); nothing is prompted. All services (Cortex, Bridge, Sentinel, Relay) use the same env var name for consistency.

## FirekeepBridge — Configuration (NB_ prefix)

Renamed from CortexBridge. All `CB_` prefixed env vars become `NB_`.

- `NB_REDIS_URL=redis://redis:6379/3`
- `NB_MCP_HOST=0.0.0.0`
- `NB_MCP_PORT=8070`
- `NB_FIREKEEP_API_URL=http://cortex-api:8000`
- `NB_FIREKEEP_API_KEY=`
- `NB_FIREKEEP_NAMESPACE=default`
- `NB_SESSION_TTL_DAYS=7`
- `NB_MAX_SESSIONS=100`
- `NB_DEFAULT_AGENT_ID=default`

### Health
`GET /health` — returns `{status: "ok", redis: bool, active_sessions: int}`

## FirekeepRelay — Health

`GET /health` — returns `{status: "ok", redis: bool, active_channels: int, bulletin_count: int, active_claims: int}`

## FirekeepSymdex — Local (stdio) only

Code intelligence is **client-side only**. The server-side HTTP container was removed from both `docker-compose.yml` and `docker-compose.office.yml` — a VPS/K8s box has no developer working tree to index, so it was vestigial. Symdex ships as a local process behind the one `firekeep` stdio gateway; its wheel is installed automatically by the client kit (the `--with-symdex` flag was removed), and the **dex registry** decides whether the gateway mounts it — registered by default (since client 1.2.0 an absent registry is seeded with symdex and docdex; `firekeep dex remove` is the off-switch) ([guides/dexes.md](guides/dexes.md)).

- Runs as `firekeep-symdex` from the client-kit venv console script; the gateway starts and fronts it
- Direct local file access, near-zero latency; must be local to the working tree it indexes
- 38 tools total: 30 visible by default, plus 8 analytics tools hidden behind `SYMDEX_ANALYTICS_ENABLED` — across 12 languages
- Per-index file ceiling via `FIREKEEP_SYMDEX_MAX_FILES` (default 1500)
- No HTTP surface, no port 8090, no `FIREKEEP_SYMDEX_MODE`/`_HOST`/`_PORT` server env. Sentinel's git collector still best-effort POSTs to `SYMDEX_URL` on commit activity, but with no server listener it fails fast into a swallowed debug log (a vestigial no-op)

## Deployment Scripts

### `install.sh` (VPS)
1. Check/install Docker + Docker Compose
2. Run in place — from an unpacked release bundle (the customer path, downloaded
   and checksum-verified by `firekeep init`) or a source checkout (developers)
3. Copy `.env.example` → `.env`
4. Derive what it used to prompt for: host address detected, Neo4j password and
   `VAULT_KEY` generated, auth keys bootstrapped (`deploy/bootstrap-keys.sh`)
5. `docker compose up -d`
6. Grace the model pull briefly, then hand it to a detached watcher
   (`--wait-for-models` blocks instead)
7. Wait for health checks to pass
8. Print status table + MCP URLs

### `update.sh` (VPS)
1. `git pull`
2. `docker-compose build` (incremental, only changed images)
3. `docker-compose up -d` (rolling restart)
4. Health check verification
5. Print status

### `firekeep install` (client kit)
1. Create a versioned venv at `~/.firekeep/venvs/<version>` (selected by the `~/.firekeep/current` link; updates provision beside the running venv and flip the link) and install the client kit — a checksum-verified wheel handed to `uv pip install` by local path (teammate bootstrap), or the local checkout (`cd client && ./install`). Never `pip install firekeep-client` by name (`firekeep-client` on PyPI is a third party's package). Users are given one URL — `https://firekeep.ai/latest/install` (`.ps1` on Windows) — which redirects to the artifact root the bootstrap then fetches everything else from: GitHub Pages, `FIREKEEP_DIST_BASE=https://kapella-hub.github.io/firekeep-dist` (see `docs/RELEASE-GITHUB.md`).
2. Bootstrap `~/.firekeep/` (config skeleton `0600`, hook cores, contract fragment, CA slot)
3. Render each runtime's native config with one absolute-path `firekeep gateway` MCP entry plus its supported hook/instruction surfaces. Re-rendering removes the six retired Firekeep entries but preserves foreign config.
4. An interactive install runs a wizard (`firekeep_client/wizard.py`) asking two things: agent identity, then where the server is — set one up on this machine (chains into `firekeep init`), redeem a join code, an existing server (`host`/`api_key`, or `base_url`/`ca_path`/`api_key` for a path-routed connection), or decide later. A machine that already has a `[server]` skips the routing question and gets the connection prompts prefilled with their current values, so Enter-through is a no-op. It never asks for a profile; Firekeep is one product, so there is no edition to ask about. A non-interactive/CI install (no TTY or `--non-interactive`) writes `[identity]` + `[server]` (`agent_id = CHANGEME` when no `--agent-id` is passed); run `firekeep doctor` to verify

## Docker Compose Dependency Chain

| Service       | Depends on                           |
|---------------|--------------------------------------|
| cortex-api    | neo4j, qdrant, redis, ollama         |
| cortex-mcp    | cortex-api                           |
| cortex-worker | redis, neo4j, qdrant, ollama         |
| cortex-beat   | redis                                |
| bridge        | redis (+ cortex-api for distillation)|
| sentinel      | redis                                |
| relay         | redis                                |
| dashboard     | none (nginx, static files)           |

## Integration Points

| From → To              | Mechanism                         | Purpose                              |
|-------------------------|-----------------------------------|--------------------------------------|
| Bridge → Cortex        | HTTP (`/memory/learn`)            | Distill completed sessions to memory |
| Symdex → Cortex        | HTTP from the client-stdio `firekeep-symdex` (carries `FIREKEEP_INTERNAL_KEY`) | `learn_from_changes`, `recall_with_code` — no server symdex container |
| Sentinel → Relay       | HTTP alert-broadcast (→Relay `/mcp`; carries the internal key under office auth) | Auto-share environment alerts        |
| Dashboard → All        | HTTP (fetch to each service)      | Unified UI                           |
| All → Redis            | Direct connection (per-DB)        | Storage, pub/sub, streams            |

## Operational Notes

### Single Redis Trade-off
Six logical databases on one Redis instance is simple and sufficient for current scale. If Relay pub/sub or Sentinel streams cause performance issues, the first mitigation is splitting Redis into two instances (infra DBs 0-2 + app DBs 3-5). No code changes needed — just env var updates.

### Logging
All services use Python `logging` to stdout. Docker captures logs via the default `json-file` driver. Use `docker-compose logs -f <service>` for debugging. Structured JSON logging is a future improvement but not required for initial deployment.

### Backups
Persistent data lives in Docker volumes: `neo4j_data`, `qdrant_data`, `redis_data`, `ollama_data`. Backup strategy: periodic `docker run --rm -v <volume>:/data -v /backups:/backup alpine tar czf /backup/<volume>.tar.gz /data`. Can be added to crontab via `install.sh`. Not implemented in v1.

### Migrations
No schema migration system. Neo4j schema is append-only (MERGE-based). Qdrant collections are created on first use. Redis keys are self-describing. The rename from `CB_`→`NB_` / `cb:`→`nb:` is a one-time migration — existing Bridge sessions on the old keys will be orphaned (acceptable since sessions have 7-day TTL).

## Out of Scope (for now)

- TLS between services (handled at network level or reverse proxy)
- Proactive agent notification (MCP doesn't support server-push yet)

> **Since shipped (no longer out of scope):** scope-based API-key auth — `AUTH_ENABLED` is **on by default** as of 2026-07-26 (it was optional and default-off; a stock install treated every caller as an anonymous admin, which is how vault secrets left this project's own VPS). Even with it explicitly disabled, the admin-gated surface stays refused. Ports bind `127.0.0.1` by default via `BIND_ADDR`. And FirekeepSentinel → FirekeepRelay forwarding (Sentinel now HTTP-broadcasts alerts to Relay).
