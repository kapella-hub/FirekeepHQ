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
| `DECAY_REFERENCE_DAYS` | `0` | Reference-memory recall half-life; `0` disables age decay and automatic age archival |
| `DECAY_PROCEDURAL_DAYS` | `180` | Procedural-memory recall and maintenance half-life in days |
| `DECAY_EPISODIC_DAYS` | `90` | Episodic-memory recall and maintenance half-life in days |
| `DECAY_TRANSIENT_DAYS` | `14` | Transient-memory recall and maintenance half-life in days |
| `GC_ENABLED` | `True` | Run scheduled archive-first memory maintenance |
| `GC_DRY_RUN` | `False` | Evaluate and audit scheduled maintenance without changing Qdrant or Neo4j |
| `GC_SCHEDULE_HOURS` | `24` | Interval between scheduled memory-maintenance runs |
| `GC_ARCHIVE_GRACE_DAYS` | `90` | Recovery window recorded on GC-origin archives before they become purge-eligible |
| `GC_PURGE_ENABLED` | `False` | Explicitly allow hard deletion of expired GC-origin archives and Neo4j orphan cleanup |
| `EVICTION_THRESHOLD` | `1.5` | Composite aging score above which an active memory is archived |
| `SKILL_STALE_AFTER_DAYS` | `90` | Mark, but never delete, active skills not explicitly recalled in this many days |
| `NB_PROACTIVE_RECALL_ENABLED` | `True` | Auto-inject memories on ctx_update |
| `RP_ENABLED` | `True` | Enable replay trace event recording |
| `RP_RETENTION_DAYS` | `30` | How long replay events are retained |
| `BIND_ADDR` | `127.0.0.1` | Host interface the six published app ports (8040-8100) bind to, and therefore the address every device invite hands out. Loopback by default — a fresh install is reachable only from the machine it runs on, so invites fall back to an SSH tunnel. See [Binding and exposure](#binding-and-exposure). |
| `AUTH_ENABLED` | `True` | Enforce per-key `X-API-Key` authentication on every MCP and REST surface. **Changed from `False` on 2026-07-26** — see [Authentication](#authentication). |
| `FIREKEEP_SSH_USER` | `root` | SSH account carried by loopback-server join codes; combine with `VPS_IP` to start the client tunnel. |
| `ENROLL_TICKET_TTL_HOURS` | `24` | Single-use join-code validity. |
| `ENROLL_TOMBSTONE_DAYS` | `7` | Retain used/expired ticket metadata so retries get a precise explanation. |
| `ENROLL_KEY_EXPIRES_DAYS` | `90` | Default enrolled device credential lifetime; `0` means never. |
| `ENROLL_RATE_LIMIT` | `10/minute` | Enrollment route limiter. |
| `ENROLL_MAX_ATTEMPTS_PER_HOUR` | `60` | Redis-global enrollment ceiling, independent of proxy source IP. |
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
| `FIREKEEP_BRIDGE_KEY` | `.env`, wired to ONLY the bridge container | `memory:write`, `session:read`, `eval:read`, `eval:write`, `eval:grade` — the only credential in the fleet carrying `eval:grade`, a service-only scope no admin key can mint |
| `DASHBOARD_API_KEY` | `.env`, injected by dashboard nginx as `X-API-Key` on `/api/*` | `*` (admin) — behind nginx basic auth |
| admin key | printed once to the terminal, never written to disk | `*` |

`DASHBOARD_API_KEY` is load-bearing now, not optional: empty means nginx sends no
header and every dashboard data tab 401s. Customer devices get credentials from
Dashboard → Devices or `deploy/firekeep-admin invite --json`; the client creates
the plaintext secret and sends only its hash during the single-use enrollment.
`deploy/firekeep-admin keys create` remains available for manual/generic clients.

See [DEPLOYMENT.md → Access and authentication](DEPLOYMENT.md#access-and-authentication)
for the first-call walkthrough and [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md)
for the multi-person key model.

## Install-Time Overrides

Not `.env` settings — flags and environment variables read by `install.sh` and
`firekeep init` **while they run**. Listed here because they are the only
configuration surface that has no `.env` line to discover it from.

`install.sh` asks nothing by default: the host address is detected and the Neo4j
password is generated, along with the vault key, the dashboard password and the
service keys. These override that.

| Flag | Env | Default | Description |
|---|---|---|---|
| `--ip <addr>` | `FIREKEEP_VPS_IP` | detected via `ip route get` | The address of this host as an **ssh and CORS** destination. Becomes `VPS_IP`, the default `ssh_target` in tunnel join codes, and the CORS origin. It is **not** what an invite tells a device to call — `BIND_ADDR` is, because that is where the ports actually answer, and the two routinely differ (a tailnet address that serves :8100 against a public address that publishes nothing). `VPS_IP` is consulted for a device address only when `BIND_ADDR` is a wildcard, where every interface is published and only `VPS_IP` names which one to hand out. Set it when the routed address is not the reachable one (NAT, floating IP, a DNS name). Getting it wrong is recoverable — the invite API answers `400 t=tunnel requires ssh_target` and you pass `--ssh-target` explicitly. |
| `--neo4j-password <pw>` | `FIREKEEP_NEO4J_PASSWORD` | generated (24 bytes hex) | Machine-to-machine only; nothing but the containers reads it. **It is baked into the Neo4j data volume at first boot** — editing `NEO4J_PASSWORD` in `.env` afterwards breaks the stack rather than changing it. Supply it only when restoring a backup or satisfying a secrets policy. |
| `--wait-for-models` | — | off | Block until the ~3.3 GB Ollama pull completes instead of backgrounding it. Use when a machine must be fully ready on exit (CI, image builds). |
| — | `FIREKEEP_MODEL_PULL_GRACE` | `120` | Seconds to wait for the model pull before handing it to a background watcher. A warm model volume finishes instantly and still reports `[OK]`. |
| `--pull` | — | off | Use published images instead of building from a checkout. Implied by `firekeep init`. |
| `--office` | — | off | Pin `docker-compose.office.yml` (Caddy TLS front). |
| `--insecure-no-auth` | — | off | Disable auth. Read the banner it prints before using it. |

`firekeep init` additionally takes:

| Flag | Env | Default | Description |
|---|---|---|---|
| `--no-self-enroll` | — | self-enrol **on** | Provision only. By default `init` enrols the machine it runs on against the new server and prints a paste-ready join command for the next one. Turn it off for headless provisioning where minting a device credential into the image would be wrong. |
| — | `FIREKEEP_SERVER_INSTALL_TIMEOUT` | `3600` | Seconds before `init` gives up on `install.sh`. Separate from `FIREKEEP_INSTALL_TIMEOUT` (600, for pip): a shared value meant the documented happy path timed out on success. |

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
| `ENROLL_` | Single-use client enrollment |
| `FIREKEEP_SYMDEX_` | FirekeepSymdex (client-side stdio server only — not a server `.env` prefix; e.g. `FIREKEEP_SYMDEX_MAX_FILES`) |
| `FIREKEEP_DOCDEX_` | FirekeepDocdex (client-side only; the disclosed caps `FIREKEEP_DOCDEX_MAX_FILES`, `_MAX_FILE_MB`, `_MAX_EXTRACT_KB`, `_SYNC_INTERVAL_HOURS` — defaults and breach behaviour in [guides/dexes.md](guides/dexes.md)) |

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

Local MCP backends ship in the client kit and bind no port: `firekeep-decision` (the Decision Board, backed by Cortex `POST /decision/synthesize`) and `firekeep-symdex` (code intelligence). Both are always installed. Decision is core and always started behind the single `firekeep` stdio gateway; symdex is a **dex** and is started only when registered — which it is by default since client 1.2.0 (`firekeep dex remove symdex` is the off-switch). The other client-side packages, `firekeep-docdex` and `firekeep-maildex`, are also always installed but have no MCP server at all — ingest clients driven by `firekeep docdex …` / `firekeep maildex …` and a background sync. See [guides/dexes.md](guides/dexes.md).

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

`BIND_ADDR` is also the address enrollment hands out. An invite that names no
transport asks the server where it publishes: a concrete non-loopback
`BIND_ADDR` mints a direct `http://<BIND_ADDR>:8100` code, a wildcard falls back
to `VPS_IP`, and a loopback binding mints an SSH-tunnel code because nothing off
the machine can reach the ports. `GET /enroll/defaults` (admin) returns that
decision and the one-line reason behind it; the dashboard's **Devices → Add
device** field is prefilled from it and can be overridden per invite. Prefer a
private address here — a direct code is plain HTTP, so the API key it issues
crosses the network in cleartext on anything but a tailnet, VPN or LAN.

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
Every memory is typed as `reference` (no age decay by default), `procedural`
(180-day half-life), `episodic` (90-day, default), or `transient` (14-day).
Ordinary `memory_learn` calls default to episodic unless the caller supplies a
type. The sleep cycle classifies knowledge it extracts from raw `memory_stream`
events; it does not retroactively classify every direct learn.

The same `DECAY_*_DAYS` settings drive recall ranking and archive eligibility.
Decay lowers the rank of older results; it does not delete them.

### Memory Maintenance and Recovery

The daily GC task uses age, recall count, confidence, and outcome efficacy to
evaluate active memories. A qualifying memory is first changed to `archived`,
with an audit event and a `purge_eligible_at` date. Archived memories disappear
from normal recall but remain visible in the dashboard's **Memory → Archived**
view, where they can be restored. The same tab provides a no-write preview and
the recent maintenance audit.

Hard purge is disabled by default. Setting `GC_PURGE_ENABLED=true` permits the
task to delete only records that it archived itself and whose
`GC_ARCHIVE_GRACE_DAYS` recovery window has elapsed; manual and legacy archives
are never guessed at or purged. The switch also gates destructive Neo4j orphan
cleanup. Use `GC_DRY_RUN=true` to exercise the scheduled evaluation without
changing either knowledge store.

Confirmed memories are protected from automatic aging. Reference memories with
their default zero half-life, skills, and corpus document chunks are never
age-archived. Skills instead use a review signal: explicit `skill_recall`
results advance their usage/freshness timestamps, while dashboard browsing,
`skill_list`, and automatic briefing impressions do not. After
`SKILL_STALE_AFTER_DAYS`, an unused active skill is marked stale for human
review, not disabled or deleted. Editing a skill's content, trigger, or symptoms
re-embeds it before atomically replacing the vector and payload.

Qdrant is authoritative for lifecycle state when a Neo4j row is linked to an
exact vector memory ID. Those linked graph results are admitted only while the
vector record is in an allowed state, preventing an archived memory from
resurfacing through the graph retrieval leg. Unlinked Neo4j rows remain
recallable because the sleep cycle intentionally creates graph-owned knowledge;
they are returned with `lifecycle_verified=false` rather than being assigned a
made-up vector lifecycle.

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
