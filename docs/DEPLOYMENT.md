# Firekeep Deployment Guide

## Prerequisites

- Linux VPS with Docker and Docker Compose v2
- **RAM:** 16 GB recommended for the full default stack (Neo4j JVM + Qdrant +
  Redis + Ollama + 7 Python services). 8 GB is the practical floor and requires
  a small embedding model (`EMBEDDING_MODEL=granite-embedding:30m`,
  `EMBEDDING_DIM=384`). Below that, containers are OOM-killed while HTTP health
  checks still pass — a failure mode that is easy to misdiagnose. (See
  "Resource Limits" below for the full breakdown.)
- Git installed
- **No open ports required.** A default install binds its app ports (8040-8100)
  to `127.0.0.1` and is reachable only from the host. Serving a laptop or a
  teammate is an explicit opt-in — see
  [Access and authentication](#access-and-authentication).

## Fresh Installation

There are two paths, and they are not interchangeable.

### As a customer — pull the published images

You need three things: the deployment files, a registry token, and the version
you were sold. The server images are private packages on `ghcr.io`; the token is
issued with your licence.

```bash
# 1. Authenticate to the registry (token issued with your licence)
echo <your-token> | docker login ghcr.io -u <your-username> --password-stdin

# 2. Set the version you were given
#    IMAGE_TAG=dev is the default and is NEVER published — install.sh --pull
#    stops and tells you so rather than failing later with 'manifest unknown'.
cp .env.example .env
$EDITOR .env            # set IMAGE_TAG=v0.1.0

# 3. Install (interactive — prompts for VPS IP and Neo4j password)
bash install.sh --pull
```

`--pull` verifies the registry is readable **before** it writes anything or
starts a container, so a missing credential fails at the top instead of half-way
through.

Only the four Firekeep service images are published. Neo4j, Redis, Qdrant and
Ollama are pulled by your own Docker daemon from their upstream registries —
Firekeep never redistributes them (see
[THIRD-PARTY-DATASTORES.md](THIRD-PARTY-DATASTORES.md)).

### As a developer — build from source

```bash
git clone https://github.com/kapella-hub/FirekeepHQ.git   # private; requires access
cd Firekeep
bash install.sh
```

Without `--pull`, `install.sh` builds all seven services from this checkout,
which is what you want when you are changing them. The mode is chosen by the
flag, never guessed from which files happen to be present — a partial checkout
would otherwise silently build something different from what was released.

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
4. Bootstraps auth keys (`deploy/bootstrap-keys.sh`) — mints `FIREKEEP_INTERNAL_KEY` and `DASHBOARD_API_KEY` into `.env` and prints a one-time admin key. **Copy that admin key somewhere durable before the terminal scrolls; it is never written to disk.** See [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md) for the full per-person key model.
5. Runs `docker compose up -d --build`
6. Waits for all services to pass health checks
7. Prints a status table with all MCP URLs

Step 4 runs *before* step 5 on purpose: auth is enforced from the first request,
so the keys have to exist before anything is listening. The health checks in
step 6 still pass — `/health` and `/version` are pre-auth, and the one probe
that does hit a gated path (`GET /mcp` on cortex-mcp) is satisfied by the 401,
which is itself proof the route is mounted and enforcing.

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

## Access and authentication

A fresh install is **closed by default** in two independent ways:

| Default | Value | Was |
|---------|-------|-----|
| `AUTH_ENABLED` | `true` | `false` |
| `BIND_ADDR` | `127.0.0.1` | ports published on `0.0.0.0` |

Both changed on 2026-07-26 and both are security fixes. Previously a stock
install published six ports on every interface and treated every caller as an
anonymous admin — enough to read the vault and mint API keys. That combination
leaked twelve real secrets off this project's own VPS.

If you are following an older walkthrough and getting `401` or "connection
refused", nothing is broken. Read on.

### Your first authenticated call

Use the admin key `deploy/bootstrap-keys.sh` printed during install. On the host:

```bash
KEY="nxs_..."   # the one-time admin key from the installer

# Pre-auth paths still answer keyless — this is how you tell "up" from "gated":
curl -fsS http://127.0.0.1:8100/health
curl -fsS http://127.0.0.1:8100/version

# A real route without a key: 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/memory/stats     # 401

# ...and with one: 200
curl -fsS -H "X-API-Key: $KEY" http://127.0.0.1:8100/memory/stats
```

`{"detail":"Missing X-API-Key header"}` means the stack is healthy and doing its
job. Don't reach for `AUTH_ENABLED=false` — mint yourself a day-to-day key
instead, so the admin key stays reserved for key management and vault access:

```bash
deploy/firekeep-admin keys create --agent "$(whoami)"   # full non-admin scope set, printed once
```

That prompts for the admin key (silently) unless you pass it as
`FIREKEEP_ADMIN_KEY=... deploy/firekeep-admin ...` — mind your shell history if
you use the env-var form.

Lost the admin key? Do not disable auth to recover — see
[DEPLOYMENT-OFFICE.md → Recovery](DEPLOYMENT-OFFICE.md#6-recovery).

### Reaching the dashboard

`http://<VPS_IP>:8040` no longer resolves on a default install. From the host,
`http://localhost:8040`. From anywhere else, tunnel:

```bash
ssh -L 8040:127.0.0.1:8040 user@vps-host
# then open http://localhost:8040
```

The dashboard asks for its own basic-auth credentials (user `admin`, password in
`dashboard/.htpasswd.cred`). Behind that, nginx injects `DASHBOARD_API_KEY` on
every `/api/*` proxy, so the SPA reaches auth-gated routes without you pasting a
key into the browser. If the tabs load but show errors, that key is missing —
re-run `bash deploy/bootstrap-keys.sh` and recreate the container
(`docker compose up -d dashboard`); it is read at container start, not per
request.

### Connecting an agent from another machine

Open the dashboard, choose **Devices → Add device**, and paste its complete
install command on the new machine. The join code tells the client whether to
use an SSH tunnel, direct TLS, or explicitly insecure HTTP; the installer does
not ask the customer to choose a network shape, profile, server, or API key.

On the shipped loopback configuration the code carries
`FIREKEEP_SSH_USER@VPS_IP`, starts the required six-port SSH tunnel, and redeems
over `http://127.0.0.1:8100`. Set those two values correctly in `.env` before
issuing the code. The server-shell fallback is:

```bash
deploy/firekeep-admin invite --agent laptop --json
```

An already-installed client can redeem the returned bare code with
`firekeep join <code>`. `firekeep connect user@vps-host` remains an SSH shortcut:
it issues an invite on the server and hands it to the same join implementation.
The client generates the plaintext credential locally; only its SHA-256 hash is
sent during enrollment. Every authenticated call afterward sends the plaintext
as `X-API-Key`, so direct plain HTTP is safe only on a trusted network. Firekeep
does not require Tailscale or any other VPN vendor.

### Exposing the stack deliberately

```bash
# .env already carries BIND_ADDR=127.0.0.1 — edit that line, don't append a second one
sed -i 's/^BIND_ADDR=.*/BIND_ADDR=0.0.0.0/' .env
docker compose up -d          # recreates app containers with the new bindings
```

(If your `.env` predates the variable and has no `BIND_ADDR` line at all,
`update.sh` adds one for you — see
[Upgrading](#upgrading-across-the-2026-07-26-security-defaults).)

Only the six app ports move. Neo4j, Qdrant, Redis and Ollama stay pinned to
`127.0.0.1` literally, by design — nothing about serving agents should also
publish a passwordless Redis and a plaintext vector store.

> **A host firewall will not contain a published port.** Docker publishes a port
> by writing its own `DOCKER` iptables chain, which is traversed *before* ufw's
> `INPUT` rules. `ufw deny 8100` does not close it and `ufw allow from <ip> to
> any port 8040:8100` does not restrict it to that IP — the packet is DNAT'd
> past your policy before ufw ever sees it. That is precisely how this stack sat
> open to the internet behind an ufw its owner believed was working.
>
> If you need host-level filtering in front of a published port, write the rules
> into the `DOCKER-USER` chain, which *is* evaluated first. Otherwise keep
> `BIND_ADDR=127.0.0.1` and front the stack with a reverse proxy (see
> [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md) for the Caddy + internal-CA
> pattern) or an SSH tunnel.
>
> With `BIND_ADDR=0.0.0.0`, the API key is the only boundary left. Keep
> `AUTH_ENABLED=true`, and remember the ports are plaintext HTTP — anything on
> the path can read the key and everything it carries. Over an untrusted
> network, terminate TLS in front.

## Updating

```bash
cd /path/to/Firekeep
bash update.sh
```

This runs `git pull`, rebuilds only changed images, restarts services, and verifies health.

### Upgrading across the 2026-07-26 security defaults

**Run `update.sh`; do not upgrade with a bare `docker compose up -d`.** Both new
defaults are compose-level fallbacks read from `.env`, and your `.env` predates
them. `update.sh` inspects it after the `git pull` and tells you — loudly — what
each one is about to do. A bare `docker compose up -d` applies the same defaults
with no explanation.

What actually happens to an existing install depends on what your `.env` already
says, and the two variables land in opposite directions:

**`BIND_ADDR` — your reachability is preserved, not silently cut.** Your `.env`
has no `BIND_ADDR` line, so the new `127.0.0.1` default would otherwise drop all
six ports to loopback on restart and cut off every remote client with nothing but
"connection refused" to explain it. `update.sh` therefore appends
`BIND_ADDR=0.0.0.0` to your `.env` and prints a notice saying so: an upgrade
script should not sever your access as a side effect. Your exposure is unchanged
— which means it is still exposure. Once your clients are keyed, tighten it:

```bash
sed -i 's/^BIND_ADDR=.*/BIND_ADDR=127.0.0.1/' .env
bash update.sh
```

**`AUTH_ENABLED` — this is the part that needs you.** Two cases:

- **`.env` has an explicit `AUTH_ENABLED=false`** (most installs — it came from
  the old `.env.example`). An explicit value beats a compose default, so **the
  fix does not reach you**. Your stack stays open to anyone who can reach the
  port. `update.sh` prints a warning block about this every run; it is the
  loudest thing it says. Fix it:
  ```bash
  bash deploy/bootstrap-keys.sh                       # idempotent; prints an admin key once
  sed -i 's/^AUTH_ENABLED=.*/AUTH_ENABLED=true/' .env
  chmod 600 .env                                      # it now holds live keys
  bash update.sh
  ```
  Bootstrap the keys **first**. Flipping the flag with no keys registered locks
  you out through the API — `POST /auth/keys` needs a key it cannot give you.
  Nothing is unrecoverable: `deploy/bootstrap-keys.sh` writes Redis directly and
  works against a locked stack ([Recovery](DEPLOYMENT-OFFICE.md#6-recovery)).
  Doing it in the right order just saves you the detour.
- **`.env` has no `AUTH_ENABLED` line at all.** The new default applies and auth
  switches on at this restart. `update.sh` announces it. The dashboard and all
  internal service-to-service calls keep working (their keys are minted into
  `.env` by `deploy/bootstrap-keys.sh`, which `update.sh` runs); anything else
  calling the API directly needs a key.

Either way, turning auth on is a breaking change for anything already talking to
the stack: client-kit `[server]` needs `api_key` set, and hand-rolled scripts need an
`X-API-Key` header. Nothing degrades gracefully — an unkeyed caller gets a 401,
by design.

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

Every row above answers **without a key** — `/health`, `/version` and the A2A
agent card are on the auth skip list, and the dashboard's 401 is nginx basic
auth, not the API gate. That is deliberate: a monitor should be able to tell
"the service is down" from "you didn't authenticate". None of them touch a
backend either, so they still answer when Neo4j or Redis is unavailable.

## Resource Limits

Defined in `docker-compose.yml`:

| Container | Memory | CPU |
|-----------|--------|-----|
| neo4j | 2 GB | 1.0 |
| ollama | 8 GB | `${OLLAMA_CPUS:-2.0}` |
| cortex-api | 512 MB | 1.0 |
| cortex-worker | 2 GB | 2.0 |
| cortex-mcp | 256 MB | 0.25 |
| cortex-beat | 256 MB | 0.1 |
| qdrant | 512 MB | 0.5 |
| redis | 256 MB | 0.25 |
| bridge | 256 MB | 0.25 |
| sentinel | 256 MB | 0.25 |
| relay | 256 MB | 0.25 |
| dashboard | 64 MB | 0.1 |

**Total:** ~14.5 GB memory limit and ~8 CPU-core limits (actual usage will be lower — these are caps, not reservations, and Docker does not check their sum).

**Minimum host: 2 cores.** CPU limits are not like memory limits — Docker refuses to *create* a container whose `cpus` exceeds the host's core count, so an over-large value fails `docker compose up` outright instead of degrading. Every per-service limit above is therefore at or below 2.0, and ollama's is configurable via `OLLAMA_CPUS` (default 2.0). Raise it on a larger host; ollama is the inference engine under every memory operation and benefits most.

**Measured, not estimated.** The stranger-install smoke test records real usage on every run.
On a 2-core / 7.8 GiB runner with the whole stack up and a memory round-tripped:

| | |
|---|---|
| Whole system, stack running | **3.4 GiB used** of 7.8 GiB |
| Largest container (ollama) | 812 MiB |
| Everything else combined | ~715 MiB |

So the ~14.5 GB total above is the sum of **caps**, and overstates real usage by roughly 4x.
Budget against the measured figure; the caps exist to stop one service starving the rest, not
to describe demand. CI fails the install if any container exceeds 85% of its own cap
(`scripts/check_container_headroom.py`) - that gate exists because cortex-beat was found
sitting at 91% of a 128 MB limit, where an OOM kill would have silently stopped every
scheduled task.

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
- Infrastructure ports (Neo4j, Qdrant, Redis, Ollama) are bound to `127.0.0.1` literally — `BIND_ADDR` does not move them
- Application ports (8040-8100) bind to `${BIND_ADDR:-127.0.0.1}`, i.e. loopback unless you opt out ([Access and authentication](#access-and-authentication))

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
  (`NEO4J_PASSWORD`, `VAULT_KEY`, minted API keys) inside a service whose HTTP port was
  published on every interface and unauthenticated by default. Mount only the trees you
  want watched and point `NS_WATCH_PATHS` at them.
- **API keys** — `AUTH_ENABLED=true` is the default; a valid `X-API-Key` is required on
  all MCP and REST endpoints except the pre-auth paths (`/health`, `/version`,
  `/.well-known/agent.json`, and Cortex's `/docs`, `/redoc`, `/openapi.json` and keyless
  `/dashboard` HTML shell). Keys are minted per-agent via `deploy/bootstrap-keys.sh` /
  `deploy/firekeep-admin` — there is no single shared `API_KEY` (see
  [DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md)). Setting `AUTH_ENABLED=false` no longer
  hands out `admin` — the anonymous identity is granted every scope except that one, and
  the check runs. `/vault/*` and `/auth/*` go further: they are not mounted at all with
  auth off and answer **503**, while other admin-gated routes answer 403. It still opens
  everything below admin (memory, sessions, relay, replay, evals) to anyone who can reach
  the port, with no attribution; do not do it on a stack anything else can reach.
- **CORS** — `CORS_ORIGINS` restricts *browser* callers that hit a service directly
  across origins. It is **not** in the path for the bundled dashboard on :8040: that SPA
  is served by nginx and calls its own origin (`/api/*`), which nginx proxies
  server-side, so the browser never makes a cross-origin request. Tightening
  `CORS_ORIGINS` will not break it, and loosening it will not fix it.

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

### Everything returns 401
Expected on a default install — auth is on. Send `X-API-Key` (see
[Your first authenticated call](#your-first-authenticated-call)). If you hold a key
and still get 401, check that the key is registered rather than merely present in
`.env`: `bash deploy/bootstrap-keys.sh` is idempotent and re-registers key hashes that
a `docker compose down -v` wiped from Redis DB 7.

A **503** has two causes and they are opposite in meaning. Check the path first:

1. **On `/vault/*` or `/auth/*`, with `AUTH_ENABLED=false`** — expected, and it is the
   control working. Those routers are deliberately not mounted while auth is off, so a
   stand-in answers every method on every path under them. Nothing is broken and there
   is nothing to repair; set `AUTH_ENABLED=true` and re-run `bash update.sh` if you want
   the vault back. `install.sh`'s summary reports this as
   `Vault: KEY SET, BUT NOT SERVED`.
2. **On any other path** — auth is enabled and its Redis DB 7 is unreachable, so the
   middleware fails closed rather than passing requests through. Compose healthchecks
   are TCP-only, so containers stay green while this happens. Check
   `docker compose exec redis redis-cli -n 7 ping` first.

### Connection refused from another machine
Also expected — app ports bind to `127.0.0.1` by default. Tunnel, or set
`BIND_ADDR=0.0.0.0` deliberately: see
[Access and authentication](#access-and-authentication).

### Dashboard shows "Service unreachable"
- Check the service is running: `docker compose ps`
- Check browser console for network errors
- If the tabs load but data calls fail, `DASHBOARD_API_KEY` is missing or stale —
  nginx drops empty `proxy_set_header` values, so the SPA sends no key at all. Re-run
  `bash deploy/bootstrap-keys.sh`, then `docker compose up -d dashboard`; the value is
  read at container start.
- CORS is almost certainly not your problem: the bundled dashboard is same-origin
  through nginx's `/api/*` proxies. `CORS_ORIGINS` only matters if you pointed the SPA
  at a different origin via its own config override, or wrote a browser client of your
  own against a service directly.

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

## Office Deployment (TLS front)

Auth and loopback binding are now the baseline everywhere, so the office instance is no
longer distinguished by those. What it adds is a Caddy TLS front with an internal CA,
path routing (`/mcp/<svc>` and `/api/<svc>/`), and a per-person key model — see
[DEPLOYMENT-OFFICE.md](DEPLOYMENT-OFFICE.md).
