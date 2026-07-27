# Configuration Reference

All configuration is via environment variables in `.env`. Copy the example and edit:

```bash
cp .env.example .env
```

## Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `NEO4J_PASSWORD` | (required) | Neo4j database password |
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins. Only affects browsers calling a service **directly across origins** — the bundled dashboard on :8040 is same-origin through nginx, so this neither breaks nor fixes it |
| `LLM_MODEL` | `qwen3:4b` | Ollama model for LLM inference |
| `EMBEDDING_MODEL` | `mxbai-embed-large` | Embedding model |
| `MULTIHOP_ENABLED` | `True` | Enable multi-hop graph traversal |
| `NB_PROACTIVE_RECALL_ENABLED` | `True` | Auto-inject memories on ctx_update |
| `RP_ENABLED` | `True` | Enable replay trace event recording |
| `RP_RETENTION_DAYS` | `30` | How long replay events are retained |
| `BIND_ADDR` | `127.0.0.1` | Host interface the six published app ports (8040-8100) bind to. Loopback by default — a fresh install is reachable only from the machine it runs on. See [Binding and exposure](#binding-and-exposure). |
| `AUTH_ENABLED` | `True` | Enforce per-key `X-API-Key` authentication on every MCP and REST surface. **Changed from `False` on 2026-07-26** — see [Authentication](#authentication). |
| `SECRET_SCAN_ENABLED` | `True` | Scan memory writes for secrets |
| `SECRET_SCAN_MODE` | `warn` | `warn` (log) or `block` (reject) when secrets found |
| `EVAL_LLM_ENABLED` | `False` | Enable Tier 2 LLM-judged eval metrics |
| `NS_WATCH_PATHS` | (empty) | Comma-separated paths for git commit watching |
| `NS_AUTO_INDEX_ENABLED` | `True` | Best-effort git-commit reindex POST to `SYMDEX_URL`. Vestigial: with no server Symdex (client-stdio only), the POST has no listener and fails fast into a swallowed debug log — a no-op seam. |
| `NS_ALERT_SEVERITIES` | `error,critical` | Severities that trigger Relay alerts |
| `CORPUS_ENABLED` | `True` | Enable corpus business knowledge module |
| `VAULT_ENABLED` | `True` | Enable encrypted secret vault |
| `VAULT_KEY` | (required) | Fernet encryption key for vault |
| `VAULT_REDIS_URL` | `redis://redis:6379/7` | Redis connection for vault |
| `SKILL_SYNTHESIS_ENABLED` | `False` | Server-side session→skill synthesis. Off by default — skills are client-authored via the `skill_create` MCP tool. Enable only on a fast-LLM deploy. |
| `KNOWLEDGE_ENABLED` | `True` | Docs→skills pipeline (`knowledge_ingest` / `knowledge_ingest_url` — corpus ingest + draft-skill queue) |
| `COLLECTORS_ENABLED` | `False` | Master switch for scheduled doc collectors (opt-in). Gates the `GET /collectors` router and every per-collector run. |
| `CONFLUENCE_COLLECTOR_ENABLED` | `False` | Confluence (wiki) collector; requires `COLLECTORS_ENABLED=true` and `CONFLUENCE_SPACE_KEYS` |
| `DECISION_ENABLED` | `True` | Enable the Decision Board synthesize endpoint (`POST /decision/synthesize`), backing the client-stdio `firekeep-decision` server |
| `POLICY_DENY_PATHS` | `.env,*.key,*.pem,*.secret` | Comma-separated glob patterns for files that should be blocked from editing |

## Authentication

`AUTH_ENABLED` defaults to **`true`**. Every MCP and REST request needs a valid
`X-API-Key` header; `/health`, `/version` and `/.well-known/agent.json` are the
only pre-auth paths (plus `/docs`, `/redoc`, `/openapi.json` and the keyless
`/dashboard` HTML shell on Cortex REST).

The default flipped on 2026-07-26. It used to be `false`, which meant every
caller on a fresh install was anonymous and held the `admin` scope — enough to
read `GET /vault/secrets` and mint keys via `POST /auth/keys`. Combined with the
old `0.0.0.0` port bindings, that was internet-reachable on a stock install.

`install.sh` and `update.sh` both run `deploy/bootstrap-keys.sh` before the app
containers start, so the keys exist by the time anything enforces them. That
script writes Redis DB 7 directly with `redis-cli` rather than POSTing to a
now-gated `/auth/keys`, so it cannot lock itself out.

**`AUTH_ENABLED=false` is no longer an admin bypass.** The anonymous identity
handed to callers when enforcement is off now carries every scope *except*
`admin`, and the scope check actually runs on that path — so vault CRUD,
`/auth/keys`, DLQ requeue, policy-rule toggles and pattern quarantine are all
refused whether auth is on or off. Narrowing the scope list alone would have
been half a fix: until 2026-07-26 the disabled path returned the anonymous
identity without consulting the required scope at all.

Two independent layers do this, and they answer with different status codes —
worth knowing before you diagnose one as the other:

| Layer | With auth **off** you get | Why |
|---|---|---|
| Router mounting (`cortex/app/main.py`) | **503** on any `/vault/*` or `/auth/*` path | those routers are not mounted at all; a stand-in answers |
| Scope check (`require_scope`) | **403** on other admin-gated routes (DLQ requeue, policy toggles, pattern quarantine) | the router is mounted, the scope is refused |

So a 503 from `/vault/secrets` on an auth-off box is the control working, not a
broken backend. See the 503 entry under Troubleshooting in
[DEPLOYMENT.md](DEPLOYMENT.md), which covers the other cause.

**That is not the same as safe.** With auth off, everything below admin —
reading and writing memories, sessions, relay traffic, replay, evals — is open
to whoever can reach the port, with no per-caller identity and no attribution.
Do not turn it off on a stack anything else can reach. If you want single-user
convenience without managing keys, be casual by leaving `BIND_ADDR` at
`127.0.0.1` and keeping auth on, not by disabling auth.

Keys minted by `deploy/bootstrap-keys.sh`:

| Key | Where it lands | Scopes |
|-----|----------------|--------|
| `FIREKEEP_INTERNAL_KEY` | `.env` | `memory:write`, `session:read`, `eval:read`, `eval:write` — deliberately **not** admin |
| `DASHBOARD_API_KEY` | `.env`, injected by dashboard nginx as `X-API-Key` on `/api/*` | `*` (admin) — behind nginx basic auth |
| admin key | printed once to the terminal, never written to disk | `*` |

`DASHBOARD_API_KEY` is load-bearing now, not optional: empty means nginx sends no
header and every dashboard data tab 401s. Teammate keys come from
`deploy/firekeep-admin keys create --agent <name>` (full non-admin scope set).

See [DEPLOYMENT.md → Access and authentication](DEPLOYMENT.md#access-and-authentication)
for the first-call walkthrough and [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md)
for the multi-person key model.

## Model Sizing

`ollama-pull` (in `docker-compose.yml`) downloads whatever `LLM_MODEL` and
`EMBEDDING_MODEL` are set to in `.env` at install time — it is no longer
hardcoded, so changing these before the first install changes what gets
pulled, not just what Cortex asks Ollama for.

**Small-machine profile (8 GB):** set `EMBEDDING_MODEL=granite-embedding:30m`
and `EMBEDDING_DIM=384` in `.env` **before the first install**. Changing either
after memories exist requires a Qdrant collection rebuild — the dimension is
baked into the collection at creation.

## Redis DB Allocation

| DB | Service | Prefix |
|----|---------|--------|
| 0 | Cortex data | — |
| 1 | Celery broker | — |
| 2 | Celery results | — |
| 3 | FirekeepBridge | `nb:` |
| 4 | FirekeepSentinel | `ns:` |
| 5 | FirekeepRelay | `nr:` |
| 6 | Replay Engine + Evals | `rp:` |
| 7 | Auth + Vault (shared DB, distinct key prefixes) | `auth:`, `vault:secret:` |

## Environment Variable Prefixes

| Prefix | Service |
|--------|---------|
| `NB_` | FirekeepBridge |
| `NS_` | FirekeepSentinel |
| `NR_` | FirekeepRelay |
| `RP_` | Replay Engine |
| `AUTH_` | Auth |
| `FIREKEEP_SYMDEX_` | FirekeepSymdex (client-side stdio server only — not a server `.env` prefix; e.g. `FIREKEEP_SYMDEX_MAX_FILES`) |

## Service Ports

| Port | Service | Protocol |
|------|---------|----------|
| 8100 | Cortex API | REST |
| 8080 | Cortex MCP | MCP (HTTP) |
| 8070 | FirekeepBridge | MCP (HTTP) |
| 8060 | FirekeepSentinel | MCP (HTTP) |
| 8050 | FirekeepRelay | MCP (HTTP) + A2A discovery (agent card) |
| 8040 | Dashboard | HTTP (nginx) |
| 7687 | Neo4j | Bolt (localhost) |
| 6333 | Qdrant | gRPC (localhost) |
| 6379 | Redis | Redis (localhost) |
| 11434 | Ollama | HTTP (localhost) |

Two MCP servers ship in the client kit as **stdio-local** processes and bind no port: `firekeep-symdex` (code intelligence, always installed) and `firekeep-decision` (the Decision Board, always installed — backed by Cortex `POST /decision/synthesize`).

### Binding and exposure

Infrastructure ports (Neo4j, Qdrant, Redis, Ollama) are bound to `127.0.0.1`
literally and are **not** affected by `BIND_ADDR`. Widening the app surface
should never publish the datastores: Redis here has no password at all, Qdrant
holds every memory in plaintext, and Neo4j's password lives in the same `.env`
an exposed service could leak. Need one remotely? Tunnel it
(`ssh -L 6379:127.0.0.1:6379 <host>`).

Application ports (8040-8100) bind to `${BIND_ADDR:-127.0.0.1}`. The default is
loopback, so a stock install serves only the machine it runs on. Reach the
dashboard from elsewhere with a tunnel:

```bash
ssh -L 8040:127.0.0.1:8040 user@host    # then open http://localhost:8040
```

To serve remote clients — a laptop running the agent kit against a VPS — set it
explicitly:

```bash
# .env ships BIND_ADDR=127.0.0.1 — edit the line in place, never append a second one
sed -i 's/^BIND_ADDR=.*/BIND_ADDR=0.0.0.0/' .env
docker compose up -d          # recreates the app containers with new bindings
```

> **A host firewall will not contain a published port.** Docker publishes a port
> by writing its own `DOCKER` iptables chain, which is evaluated *before* ufw's
> `INPUT` rules — so `ufw deny 8100` does not close it, and `ufw allow from
> <ip>` does not restrict it to that IP. This is not theoretical: it is exactly
> how this stack's ports stayed open to the internet behind an active ufw. If
> you need host-level filtering on a published port, write it into the
> `DOCKER-USER` chain, which *is* traversed first; otherwise bind to loopback
> and put a reverse proxy (see [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md)) or
> an SSH tunnel in front. `BIND_ADDR=0.0.0.0` plus `AUTH_ENABLED=true` means the
> API key is the boundary — treat it that way.

## Intelligence Features

### Memory Type Classification
Every memory is typed as `reference` (no decay), `procedural` (180-day half-life), `episodic` (90-day, default), or `transient` (14-day). The sleep cycle LLM auto-classifies new memories.

### Multi-hop Graph Reasoning
Graph queries traverse up to 3 hops with 0.5x score decay per hop, finding indirect connections that single-hop queries miss. Enabled by default (`MULTIHOP_ENABLED=True`).

### User Profile Model
The memory agent automatically extracts skills, preferences, and goals from your memory corpus into Person nodes in Neo4j. Recall results are boosted for domains matching your expertise.

### Proactive Recall
When you call `ctx_update` with a plan or progress update, FirekeepBridge automatically queries FirekeepCortex for relevant past experience and injects it into your session shadow.

### Embedding fine-tuning — REMOVED, not disabled
This section previously described `POST /admin/embeddings/finetune` and a
`CORTEX_INSTALL_FINETUNE_DEPS=true` rebuild. **Neither exists.** There is no such
route, nothing imports `sentence_transformers`, the package is in no requirements
file, and the build arg was declared by no Dockerfile — `docker-compose.yml` passed
it into a build that ignored it. A reader following these instructions would have
set a flag, rebuilt, and called an endpoint that 404s.

Removed rather than implemented: the capability was never built here, and shipping
documentation for a feature a customer cannot use is worse than not offering it.
