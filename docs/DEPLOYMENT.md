# Firekeep Deployment Guide

## Prerequisites

- Linux VPS with Docker and Docker Compose v2
- **RAM:** 16 GB recommended for the full default stack (Neo4j JVM + Qdrant +
  Redis + Ollama + 7 Python services). 8 GB is the practical floor and requires
  a small embedding model (`EMBEDDING_MODEL=granite-embedding:30m`,
  `EMBEDDING_DIM=384`). Below that, containers are OOM-killed while HTTP health
  checks still pass — a failure mode that is easy to misdiagnose. (See
  "Resource Limits" below for the full breakdown.)
- Ports 8040-8100 open for external access (or use a reverse proxy)
- Git installed

## Fresh Installation

```bash
# Clone
git clone https://github.com/kapella-hub/Firekeep.git
cd Firekeep

# Install (interactive — prompts for config)
bash install.sh
```

`bash install.sh` performs a standard install. Pass `--office` **only** on the
internal office cluster — it pins the Caddy TLS front and the 127.0.0.1 port
rebind from `docker-compose.office.yml`, and builds images from an internal
registry that is unreachable from anywhere else.

### What `install.sh` Does

1. Checks Docker and Docker Compose are installed
2. Creates `.env` from `.env.example`
3. Prompts for:
   - **VPS IP** — used for CORS origins and printed in MCP URLs
   - **Neo4j password** — required, no default
4. Bootstraps auth keys (`deploy/bootstrap-keys.sh`) — mints `FIREKEEP_INTERNAL_KEY` and `DASHBOARD_API_KEY` into `.env` and prints a one-time admin key. See [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md) for the full per-person key model.
5. Runs `docker compose up -d --build`
6. Waits for all services to pass health checks
7. Prints a status table with all MCP URLs

The installer generates `dashboard/.htpasswd` with user `admin` and a random
password. The password is written **once** to `dashboard/.htpasswd.cred`
(mode 0600) — it is never printed to the terminal or logs, since
install.sh's output is captured verbatim by CI. Read it, save the password
somewhere durable, then delete the file. To change it later, regenerate the
hash the same way install.sh does — SHA-512 crypt, never plain `htpasswd`
with no algorithm flag, which defaults to weak apr1-MD5:
```bash
printf '%s' 'new-password' | openssl passwd -6 -stdin
# edit the admin: line in dashboard/.htpasswd to "admin:<hash printed above>"
docker compose restart dashboard
```

### Manual Installation

If you prefer to configure manually:

```bash
cp .env.example .env
chmod 600 .env      # .env holds secrets: NEO4J_PASSWORD, and VAULT_KEY once you set one
# Edit .env with your values
vim .env

# Dashboard basic-auth file -- compose bind-mounts ./dashboard/.htpasswd; a
# missing source makes Docker create a DIRECTORY there instead, and nginx
# then fails every request. Generate it before first `up` (see install.sh's
# own "Dashboard basic auth" block for the full htpasswd/openssl fallback
# logic; this is the same SHA-512-crypt shortcut):
DASH_PASS="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"
printf 'admin:%s\n' "$(printf '%s' "$DASH_PASS" | openssl passwd -6 -stdin)" > dashboard/.htpasswd
chmod 0644 dashboard/.htpasswd
echo "dashboard password: $DASH_PASS"   # save this now -- it is not stored anywhere else

# Bootstrap auth keys (mints FIREKEEP_INTERNAL_KEY / DASHBOARD_API_KEY into .env)
docker compose up -d redis
bash deploy/bootstrap-keys.sh

# Build and start
docker compose up -d --build

# Check status
docker compose ps
```

## Updating

```bash
cd /path/to/Firekeep
bash update.sh
```

This runs `git pull`, rebuilds only changed images, restarts services, and verifies health.

## Operations

### What version am I running?

Every service answers without authentication and without touching its backends:

```bash
for p in 8100 8070 8060 8050; do curl -fsS "http://127.0.0.1:$p/version"; echo; done
```

bridge, relay, and sentinel return `{"service","version","git_sha","build_time"}`.
Cortex (`:8100`) omits `service` and returns `{"version","git_sha","build_time"}` — its
endpoint contract predates the shared provenance module. Quote `git_sha` on any support
request — it identifies the exact code, which a version number alone does not.

### Backups

```bash
bash deploy/backup.sh                 # writes ./backups/firekeep-backup-<timestamp>/
bash deploy/backup.sh /mnt/backups    # or somewhere else
```

Backs up all four persistent volumes. It exits non-zero if a volume it found fails to
archive, or if none of the four volumes matched the derived prefix at all. A volume that
is simply missing under that prefix is silently **SKIP**ped, not counted as a failure —
so exit 0 means "everything the script found was archived," not necessarily "all four
volumes were archived." Check the per-volume `SKIP`/`ok` lines in the output to confirm
the count, especially after a rename or migration (see
[Upgrading from a pre-compose-managed-volumes install](#upgrading-from-a-pre-compose-managed-volumes-install)).
Treat a non-zero exit as "no backup taken", not "mostly fine".

Restore requires the stack to be stopped:

```bash
docker compose down
bash deploy/restore.sh backups/firekeep-backup-20260726T120000Z
docker compose up -d
```

**Test your restore.** A backup that has never been restored is not a backup.

### Support bundle

```bash
bash deploy/support-bundle.sh
```

Writes `firekeep-support-<timestamp>.tar.gz` containing per-service `/version` and `/health`,
container status, the last 200 log lines, host resources, and your `.env` **with all
values redacted** — keys are preserved so support can see what is configured, values
never leave your infrastructure. Review the archive before sending if your deployment
holds sensitive paths.

## Service Dependencies

```
Infrastructure (must start first):
  neo4j, qdrant, redis, ollama → ollama-pull (init)

Application layer:
  cortex-api    ← depends on all infrastructure
  cortex-mcp    ← depends on cortex-api
  cortex-worker ← depends on redis, neo4j, qdrant, ollama
  cortex-beat   ← depends on redis
  bridge        ← depends on redis (+ cortex-api for distillation)
  sentinel      ← depends on redis
  relay         ← depends on redis
  dashboard     ← no dependencies (static nginx)
```

## Health Endpoints

Every service exposes a health endpoint:

| Service | URL | Response |
|---------|-----|----------|
| Cortex API | `GET :8100/health` | `{status, version, uptime, memory_count}` |
| Cortex MCP | TCP check on :8080 | — |
| FirekeepBridge | TCP check on :8070 | — |
| FirekeepSentinel | `GET :8060/health` | `{status, redis}` |
| FirekeepRelay | `GET :8050/health` | `{status, redis, active_channels, bulletin_count, active_claims}` |
| Relay A2A | `GET :8050/.well-known/agent.json` | A2A Agent Card for external discovery (discovery-only) |
| Dashboard | HTTP check on :8040 | nginx serves index.html |

## Resource Limits

Defined in `docker-compose.yml`:

| Container | Memory | CPU |
|-----------|--------|-----|
| neo4j | 2 GB | 1.0 |
| ollama | 8 GB | 4.0 |
| cortex-api | 512 MB | 1.0 |
| cortex-worker | 2 GB | 2.0 |
| cortex-mcp | 256 MB | 0.25 |
| cortex-beat | 128 MB | 0.1 |
| qdrant | 512 MB | 0.5 |
| redis | 256 MB | 0.25 |
| bridge | 256 MB | 0.25 |
| sentinel | 256 MB | 0.25 |
| relay | 256 MB | 0.25 |
| dashboard | 64 MB | 0.1 |

**Total:** ~14.4 GB memory limit (actual usage will be lower).

## Volumes

Persistent data is stored in Docker volumes:

| Volume | Service | Contents |
|--------|---------|----------|
| `neo4j_data` | neo4j | Knowledge graph data |
| `qdrant_data` | qdrant | Vector embeddings |
| `redis_data` | redis | All Redis DBs (sessions, events, relay, queues) |
| `ollama_data` | ollama | Downloaded LLM models |

### Backup

See [Operations → Backups](#backups) above — `bash deploy/backup.sh` backs up all
four volumes and derives the project prefix for you. Two procedures that can drift
is worse than one, so this is the only supported backup path.

### Upgrading from a pre-compose-managed-volumes install

Versions before this change declared the four volumes `external: true` under
the fixed names `firekeepcortex_neo4j_data`, `firekeepcortex_qdrant_data`,
`firekeepcortex_redis_data`, `firekeepcortex_ollama_data`. If your existing
installation was created before this change, its data lives in those legacy
volumes and `docker compose up` will start against brand-new, empty
project-scoped volumes unless you migrate first:

```bash
docker compose down
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"
for vol in neo4j_data qdrant_data redis_data ollama_data; do
  docker volume inspect "firekeepcortex_${vol}" >/dev/null 2>&1 || {
    echo "error: legacy volume firekeepcortex_${vol} not found — list actual names with: docker volume ls" >&2
    exit 1
  }
  docker volume create "${PROJECT}_${vol}"
  docker run --rm -v "firekeepcortex_${vol}":/from -v "${PROJECT}_${vol}":/to alpine \
    sh -c 'cd /from && cp -a . /to'
done
docker compose up -d
```

Verify the stack comes up healthy and your data is present before removing
the old `firekeepcortex_*` volumes. Keep them as a rollback for at least a week.

## Networking

- All services run on the default Docker Compose bridge network
- Services communicate by container name (e.g., `cortex-api`, `redis`)
- Infrastructure ports (Neo4j, Qdrant, Redis, Ollama) are bound to `127.0.0.1` only
- Application ports (8040-8100) are on `0.0.0.0`

### Security Considerations

- **No TLS between services** — all communication is on the internal Docker network
- **Docker socket access** — **not mounted by default** (changed 2026-07-26). Sentinel's
  docker collector is opt-in via `NS_DOCKER_COLLECTOR_ENABLED=true`, which also requires
  restoring the `/var/run/docker.sock` mount in `docker-compose.yml`.

  This entry previously read *"This is read-only but grants visibility into all
  containers."* **That was wrong in both halves.** The mount carried no `:ro` flag, and
  `:ro` would not have made it read-only anyway — it restricts the socket *file*, not the
  Docker API served over it, so `POST /containers/create` with a host bind mount still
  works. Access to that socket is root on the host, not visibility. If you enable the
  collector, front the socket with a read-only proxy exposing only `GET /containers/json`
  — the one call the collector makes.
- **Sentinel filesystem access** — the repository root is **no longer** bind-mounted into
  the Sentinel container. It previously mounted `./:/watch:ro`, which put `.env`
  (`NEO4J_PASSWORD`, `VAULT_KEY`, minted API keys) inside a service whose HTTP port is
  published and unauthenticated by default. Mount only the trees you want watched and
  point `NS_WATCH_PATHS` at them.
- **API keys** — set `AUTH_ENABLED=true` in `.env` to require a valid `X-API-Key` on all MCP and REST endpoints. Keys are minted per-agent via `deploy/bootstrap-keys.sh` / `deploy/firekeep-admin` (no single shared `API_KEY` — see [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md)).
- **CORS** — configure `CORS_ORIGINS` in `.env` to restrict dashboard access.

## Troubleshooting

### Service won't start
```bash
docker compose logs <service-name>
```

### Neo4j won't connect
Check the password matches between `.env` and what Neo4j was initialized with. If you need to reset:
```bash
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]')}"
docker compose down
docker volume rm "${PROJECT}_neo4j_data"
docker compose up -d
```

### Ollama models not loading
The `ollama-pull` init container downloads models on first start. Check its logs:
```bash
docker compose logs ollama-pull
```

### Redis connection errors
Verify the Redis DB numbers don't conflict. Each service uses a dedicated DB (0-7): Cortex(0), Celery broker(1), Celery results(2), Bridge(3), Sentinel(4), Relay(5), Replay+Evals(6), Auth(7).

### Dashboard shows "Service unreachable"
- Check CORS: `CORS_ORIGINS` in `.env` must include `http://<VPS_IP>:8040`
- Check the service is running: `docker compose ps`
- Check browser console for network errors

## Local Development

For developing individual services locally without Docker:

```bash
# Start infrastructure only
docker compose up -d neo4j qdrant redis ollama ollama-pull

# Run a service locally
cd bridge
pip install -r requirements.txt
NB_REDIS_URL=redis://localhost:6379/3 python -m app.mcp_server

# Or Cortex
cd cortex
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

If you want to use embedding fine-tuning locally or in Docker, install the optional training stack separately:

```bash
cd cortex
pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-finetune.txt
```

For Docker Compose, set `CORTEX_INSTALL_FINETUNE_DEPS=true` in `.env` and rebuild `cortex-worker`.

## Monitoring

### Docker Compose status
```bash
docker compose ps
```

### Logs (follow)
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f cortex-api
```

### Resource usage
```bash
docker stats
```

### Sentinel events (via MCP)
The Sentinel service monitors Docker container health and reports events. Check via the dashboard Events tab or call the `sentinel_get_events` MCP tool.

## Office Deployment (TLS + auth)

For the office instance — Caddy TLS front with an internal CA, `AUTH_ENABLED=true`,
per-person API keys, and app ports rebound to 127.0.0.1 — see
[DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md).
