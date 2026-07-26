# Firekeep

Unified cognitive stack for AI agents. Consolidates four server services (Cortex, Bridge, Sentinel, Relay) plus a dashboard into one deployable unit; code intelligence (Symdex) ships client-side in the kit.

## Architecture

| Service | Directory | Port | Purpose |
|---------|-----------|------|---------|
| FirekeepCortex | `cortex/` | 8100 (API), 8080 (MCP) | Long-term memory (semantic + graph RAG) |
| FirekeepBridge | `bridge/` | 8070 | Session context persistence across compressions |
| FirekeepSentinel | `sentinel/` | 8060 | Environment observer (collectors + webhook intake). **Docker collector is opt-in** — `NS_DOCKER_COLLECTOR_ENABLED=false` by default, and `docker-compose.yml` no longer bind-mounts `/var/run/docker.sock` or the repo root (`./:/watch:ro`). Reaching the Docker API is root on the host: a caller can `POST /containers/create` with a host bind mount, and `:ro` restricts the socket *file*, not the API. The old repo-root mount also put `.env` — `NEO4J_PASSWORD`, `VAULT_KEY`, minted API keys — inside a service with a published, unauthenticated port. Neither mount did anything by default (the collector makes one call, `GET /containers/json`; git/file watches come from Redis + `NS_WATCH_PATHS`, both empty). To opt in, set the flag and restore the mount per the comments in the compose `sentinel:` block — preferably behind a read-only socket proxy. Guarded by `sentinel/tests/test_docker_collector_optin.py`. |
| FirekeepRelay | `relay/` | 8050 | Agent-to-agent communication (pub/sub + bulletin board) |
| FirekeepSymdex | `symdex/` | stdio (local, client-installed) | Code intelligence (tree-sitter AST parsing). **CLIENT-SIDE ONLY** — it must be local to the codebase it indexes, so it ships as the standalone `firekeep-symdex` stdio MCP server (`firekeep_symdex.server:main`), an **always-installed** client MCP server (bundled checksum-verified wheel installed by the bootstrap, or from the local `symdex/` dir on a checkout install) — joining `firekeep-decision` as always-on (the old `firekeep install --with-symdex` opt-in flag is retired). The server-side HTTP container was removed from both `docker-compose.yml` and `docker-compose.office.yml` (a VPS/K8s box has no developer working tree to index — it was vestigial). 8 analytics tools (`get_evolution_timeline`, `get_code_churn`, `get_contributors`, `get_change_summary`, `detect_patterns`, `get_complexity_metrics`, `get_hotspots`, `compare_repos`) require indexed repos and are hidden by default (`SYMDEX_ANALYTICS_ENABLED=false`). Per-index file ceiling via `FIREKEEP_SYMDEX_MAX_FILES` (default 1500). `list_repos` exposes indexed-repo inventory. (Sentinel's git collector still best-effort POSTs to `SYMDEX_URL` on commit activity; with no server symdex it fails fast into a swallowed debug log — harmless, and the seam a team would reuse if it ever re-adds a server symdex.) |
| Dashboard | `dashboard/` | 8040 | Unified web UI (static SPA) |

## Infrastructure (VPS, localhost-only ports)

- Neo4j 5.x (7687) — knowledge graph
- Qdrant (6333) — vector embeddings
- Redis 7 (6379) — cache, queues, streams, pub/sub
- Ollama (11434) — LLM inference

## Shared Modules (v2)

| Module | Directory | Purpose |
|--------|-----------|---------|
| Replay Engine | `replay/` | Structured trace log for agent actions (emitter, reader, narrowing) |
| Auth | `auth/` | API key management, scope-based authorization |
| Vault | `vault/` | Encrypted secret storage (Fernet + Redis) |
| Corpus | `corpus/` | Business knowledge documents → Qdrant chunks (surfaced in memory recall) |

These are shared libraries imported by multiple services (not standalone containers).

## Redis DB Allocation

| DB | Service |
|----|---------|
| 0 | Cortex data |
| 1 | Celery broker |
| 2 | Celery results |
| 3 | FirekeepBridge |
| 4 | FirekeepSentinel |
| 5 | FirekeepRelay |
| 6 | Replay (events, context snapshots, evals) |
| 7 | Auth (API keys) |

## Commands

### Deploy to VPS (first time)
```bash
bash install.sh
```

### Update VPS deployment
```bash
bash update.sh
```

### Deploy to Kubernetes (office — two-repo pattern)
This repo builds and publishes artifacts only; the helm chart and Rancher deployment live in the config repo `<config-repo>/helm-chart` (chart v0.2.0, ported from the office GitLab `main` 2026-07-14 and adapted to this codebase: server-side symdex removed, SP1a key model — `app.internalKey` / `app.dashboardApiKey` / `app.relayInternalApiKey` instead of the retired `API_KEY`, full v2 env surface; secrets flow through an Ansible-Vault `encrypted_values.yaml` with a `{TAG}` placeholder).

`.gitlab-ci.yml` (ported from office main 2026-07-14, merged with the pre-existing client-release job) runs on web trigger or tags only. Tag `vX.Y.Z` → 8 image builds (root `Dockerfile` = cortex via the shared `<ci-pipeline-include>` include; `docker/Dockerfile.*` = infra mirrors + dashboard; symdex build removed) → registry promote → artifact copy → `verify_pull` kubelet-style pullability gate. Once `verify_pull` is green, deploy by running the **config repo's** web pipeline with `IMAGE_TAG=<tag>`. Tag `client-vX.Y.Z` → client tests + wheel/uv release to the GitLab generic package registry (deploy jobs are guarded with `$CI_COMMIT_TAG !~ /^client-v/`). The dashboard image builds from `dashboard/nginx.conf.template` + `docker/dashboard-htpasswd.sh` (basic auth on only when `DASHBOARD_HTPASSWD` is set; `DASHBOARD_API_KEY` envsubst as on the VPS). NOTE: the office deployment's embedding/LLM backend is an in-cluster CPU ollama image with MODELS BAKED AT BUILD TIME and the GPU libs dropped, published as a CHUNKED image (no blob above the ~168MB replication ceiling; see the Dockerfile header + `docs/HISTORY-NOTES.md`). Since the registry replication ticket landed (2026-07-17, deployed in v0.2.3), the DEPLOYED image is the full **`firekeep-ollama:<tag>`** on the DEDICATED path (`docker/Dockerfile.ollama`: `granite-embedding:30m` for 384-dim embeddings + **`llama3.2:3b`** for CPU generation, ~3.3GB/~35 chunks). Chart (the config repo) is BAKED mode: `ollama.image=firekeep-ollama`, `ollama.tag=""` (follows `imageTag`), `ollama.models=[granite-embedding:30m, llama3.2:3b]`, `app.llmModel=llama3.2:3b`, RAM 6Gi (fits llama3.2:3b's ~4GB resident); the ollama pod is deliberately STATELESS (a PVC over /root/.ollama would shadow the baked models). Classify runs ~56s on CPU, within the 300s worker timeout. The `embed-<tag>` PIGGYBACK on the `firekeep-dashboard` path (`docker/Dockerfile.embed`, granite-only) is RETIRED to an embeddings-only fallback — still built (`build-embed`) but NON-GATING in `verify_pull`; `verify_pull` gates only on the 8 small release images and treats the large ollama image as a non-gating informational probe (a ~3.3GB image replicates slower than the job's hard ~20min runner cap — gating on it only produced false-negative timeouts; the ollama readiness probe tolerates the wait while the old pod keeps serving, so an unconfirmed ollama image only delays the roll, never takes the stack down). EMBEDDING_MODEL stays `granite-embedding:30m`/384-dim — `LLM_MODEL` (llama3.2:3b) is independent; conflating them forces a Qdrant rebuild. The repo default embedding remains `mxbai-embed-large`/1024 — never change the office embedding model without a reembed pass, and never change the dim without a collection rebuild. **Deploy fragility learned in v0.2.3: the chart change (ollama pivot) must be MERGED to the config repo's main BEFORE running the deploy pipeline** — a deploy against an unmerged chart change silently ships the app images with the OLD ollama, and the runner caps `verify_pull` at 20min regardless of its declared `timeout: 2h`. (Full history — the v0.1.x chunked-ollama saga, the embed-piggyback interim, and the abandoned qwen2.5:1.5b piggyback — is in `docs/HISTORY-NOTES.md` + FirekeepCortex memory.)

### Local setup (portable client kit — `~/.firekeep` + runtime adapters)

**Teammates (bare machine, nothing installed):**
```bash
curl -fsSL <release-base>/latest/install.sh | sh      # macOS / Linux
irm <release-base>/latest/install.ps1 | iex           # Windows
```
Since client 0.1.15 the PUBLISHED bootstraps carry their own release URL — `make_release.py
--dist-base` bakes it (each release path bakes its own: GitLab CI the registry URL, the GitHub
workflow the Pages URL) BEFORE the bootstrap hashes are computed, so `firekeep update`'s
script-verification still holds. `FIREKEEP_DIST_BASE` still overrides when set, and the REPO
copies keep the `__FIREKEEP_DIST_BASE_DEFAULT__` placeholder — a raw-checkout run still fails
loudly with nowhere to fetch from. New-teammate sugar: the wizard prefills the office
connection from `<release-base>/latest/org-defaults.json` when the office profile is
unconfigured (published only via the GitLab registry from the `ORG_DEFAULTS_JSON` CI variable
— internal hostnames never go to public GitHub Pages; absent variable = no file = plain
prompts). Update awareness + auto-update: the `session_start` hook checks the dist host's
`latest.json` once per day (failures cached too, 3s timeout, silent on any failure) and, when
it's newer, background-auto-updates the client by default (client 0.1.20; opt out with
`FIREKEEP_NO_AUTO_UPDATE` / `firekeep update --auto off` — see Background auto-update below), falling
back to a one-line "client update available" nudge when opted out.
`<release-base>` is **version-agnostic**. Interim client releases are cut via GitHub Actions
(`.github/workflows/release.yml`) and served from GitHub Pages —
`FIREKEEP_DIST_BASE=https://kapella-hub.github.io/Firekeep` (see `docs/RELEASE-GITHUB.md`); the
GitLab generic package registry root (`.../packages/generic/firekeep-client`, via `.gitlab-ci.yml`)
remains the office path. Either way `latest/` is the stable entry point
(`install.sh`, `install.ps1`, `latest.json`), while every version keeps its own directory
(`<version>/SHA256SUMS`, `<version>/uv-<target>`, `<version>/firekeep_client-<version>-py3-
none-any.whl`), which is what lets `firekeep update --to <older>` reach that version's own
wheel instead of 404ing. The bootstrap (`client/bootstrap/`) resolves the version, fetches
that version's `SHA256SUMS` once, and checksum-verifies **both** a mirrored `uv` **and the
wheel** against it before either is used — the wheel is fetched to a local file and handed
to `uv pip install` by local path, never by URL (`uv pip install <url>` does no hash
checking). The wheel is never resolved **by name** either (`firekeep-client` on PyPI is a third
party's package). Both bootstraps (`client/bootstrap/install.sh` ~lines 22-35, `install.ps1`
mirrored) also export `UV_NATIVE_TLS=1` and neutralize a set `SSL_CERT_FILE` (a warning is
printed; `FIREKEEP_KEEP_SSL_CERT_FILE=1` opts back in) before invoking `uv`/pip — rustls treats
`SSL_CERT_FILE` as the EXCLUSIVE trust store (the native OS store is ignored), so a
corporate-CA-only file left behind by a proxy workaround would otherwise break every
NON-intercepted host; routing through the OS store instead is what MDM-managed corporate
machines need, since the corporate interception CA lives there alongside the public roots.
It then runs `firekeep install`, which prompts for identity/profile/
connection and renders the runtime adapters.

**`firekeep` on PATH (`firekeep_client/pathenv.py`, client 0.1.20):** every install path funnels
through `firekeep install` (fresh bootstrap, `firekeep update` re-exec, checkout `./install`), so
that is where a `firekeep` launcher gets put on PATH — best-effort (a PATH failure NEVER fails
the install). It does **not** PATH `~/.firekeep/venv/bin`: that dir holds the kit's standalone
CPython (`python`/`python3`/`pip`) and every internal entry point (`firekeep-shim`,
`firekeep-sidecar`, `firekeep-decision`, `firekeep-symdex`), so prepending it would shadow the user's
own `python3`. Instead it drops ONE launcher — `firekeep` — into a dedicated `~/.firekeep/shims`
dir (POSIX: a `#!/bin/sh` wrapper `exec`ing the venv firekeep; Windows: `firekeep.cmd` →
`%~dp0..\venv\Scripts\firekeep.exe`) and PATHs only that (the pipx/rustup pattern). POSIX writes
a marker-delimited `export PATH=...` block into the shell rc for `$SHELL` (zsh→`.zshrc`;
bash→`.bashrc` + existing `.bash_profile`/`.profile`; fish→`conf.d/firekeep.fish`; else
`.profile`) — extras are updated only if they already exist, so a login-shell sourcing chain
is never disrupted; Windows prepends the shim dir to the `HKCU\Environment` `Path` via
`winreg` (REG_EXPAND_SZ type preserved, `WM_SETTINGCHANGE` broadcast). Idempotent (collapses
ALL prior firekeep blocks on re-render). Opt out with `firekeep install --no-modify-path` or
`FIREKEEP_NO_MODIFY_PATH=1` (sysadmins who manage PATH centrally). `pathenv.remove_from_path`
is the inverse, ready for a future `firekeep uninstall` (no such command today).

**Developers (from a checkout):**
```bash
cd client && ./install              # POSIX; .\install.ps1 on Windows
firekeep install --runtime claude      # re-render one runtime: claude | codex | kiro | opencode | all
# firekeep-symdex (stdio-local code intelligence) now installs automatically — no flag needed
firekeep install --non-interactive --agent-id ci-bot --host 10.0.0.4   # scripted/fleet
```
`./install` (from a checkout, requires a system `python3 >= 3.10`) installs the kit into
`~/.firekeep/venv` and renders the adapters. `firekeep install` (from that venv) **re-renders
adapters only** — it skips pip, because the code it would install is the code already
running.

**Updating:** `firekeep update` (`--check` to report only, `--to X.Y.Z` to pin or roll back).
It re-execs the bootstrap rather than pip-installing over itself — on Windows the running
`Scripts\firekeep.exe` is locked and cannot be overwritten in place. Install and update are
therefore one code path. The Windows bootstrap additionally (a) pins `uv venv
--python-preference only-managed` so interpreter discovery never walks the PATH into a
dangling Windows Store python alias (zero-byte APPEXECLINK stub → "os error 3") nor binds
the venv to a non-standalone system Python, and (b) refuses to replace a `~/.firekeep/venv`
that live agent processes still run from (every open Claude Code/kiro session runs the
kit's stdio MCP servers — firekeep-decision, firekeep-symdex, shims — from that venv): it names
the holder processes and asks you to close those sessions, instead of letting uv die with
a bare "Access is denied (os error 5)". POSIX needs neither guard (unlink of in-use files
succeeds; only-managed is mirrored there for the standalone-CPython contract, not the
crash). `~/.firekeep/config`'s `[dist] base_url` (written by the bootstrap, or
via `firekeep install --dist-base URL`) is how `firekeep update` knows where its releases live; a
checkout install has no `[dist]` section and `firekeep update` says so plainly. `firekeep doctor`
reports a `client-version` check when a newer release exists.

**Background auto-update (`firekeep_client/autoupdate.py`, client 0.1.20) — ON by default.** The
`session_start` daily version check (below) no longer only nudges: when a newer release
exists it fire-and-forgets a DETACHED `firekeep update` (`autoupdate.maybe_spawn` →
`subprocess.Popen([venv/firekeep, "update"], start_new_session=True` / Windows
`DETACHED_PROCESS`). This automates the SAME operation a user runs by hand mid-session, so it
carries the same safety: `firekeep update` rebuilds `~/.firekeep/venv`, which can't replace the
install it runs from — POSIX unlink-safety means the running session keeps working and the
new version applies to the **next** session. **Windows caveat:** the detached update is
refused by the bootstrap's live-holder guard whenever a session's stdio MCP servers
(firekeep-decision, firekeep-symdex, shims) still hold the venv — which at session start they
always do — so on Windows the background update silently no-ops during active use and only
lands once no session holds the venv; Windows teammates should treat `firekeep update`
(manual) as the reliable path. The failed attempt is harmless (detached, output to
DEVNULL) and the daily guard means at most one such attempt per day. Guarded to **at most one spawn per calendar day per
target version** (the daily check caches a 'newer' verdict, so without this every session
start that day would relaunch — the guard is a `today|latest` stamp in scratch). The
detached update runs `--non-interactive` (no tty), so it never prompts and preserves config.
When auto-update is on the briefing line reads "updating client in background: X → Y (applies
next session)"; opted-out it's the old "run: firekeep update" nudge. Opt out with
`FIREKEEP_NO_AUTO_UPDATE=1` (env), `[dist] auto_update = false` (config), or `firekeep update
--auto off` (which writes that config key and does nothing else). Never blocks or fails a
session — `maybe_spawn` swallows every error and returns False. Release-host fetches on this
path (`firekeep update`'s manifest/wheel downloads, `firekeep doctor`'s `client-version` check) go
through a scoped `truststore` OS-trust SSL context (`client/firekeep_client/updater.py:
_dist_ssl_context`) — `truststore` is a new `truststore>=0.9.1` dependency in
`client/pyproject.toml` — never `truststore.inject_into_ssl()`, which replaces the
process-wide default context and would widen an office profile's pinned `ca_path` trust
instead of leaving it scoped; when `truststore` isn't installed the call returns `None`
and the caller falls back to the stdlib default context.

`~/.firekeep/config` (INI, `0600`) is the single source of truth for connection/auth/identity. `firekeep profile use personal|office` flips the active profile (effective next agent start); per-runtime pins in `[pins]` (`firekeep profile pin <runtime> <profile>` / `firekeep profile unpin <runtime>`) hard-lock one runtime to a fixed profile regardless of `[active]` — the pinned runtime's rendered MCP/hook entries bake in `FIREKEEP_PROFILE=<pinned>` (mirrors the `FIREKEEP_AGENT_ID` per-process override), so the pin survives re-renders; profile names in a pin are restricted to `^[A-Za-z0-9_-]+$` (anything else is rejected at write time, never reaching a rendered command string). `firekeep doctor` runs health + version-skew + client-version + key-ACL + CA-expiry + pin hygiene (and api-key/CA checks for pinned profiles) preflight.

**Install prompts (`firekeep_client/wizard.py`):** an interactive install asks for the agent identity, which profile to configure (personal / office / both), and that profile's connection — `host` (+ optional `api_key`) for a `kind=ports` profile, `base_url` + `ca_path` + `api_key` for `kind=paths`. Every prompt is prefilled with the current value, so Enter-through is a no-op and re-running the installer after a kit upgrade is safe. `ca_path` accepts the literal **`os`** (`resolver.OS_TRUST`) to verify TLS against the operating-system trust store instead of a CA file — the MDM-managed-corporate-CA case, where the CA lives in the OS keychain and there is no PEM to point at; the wizard offers `os` as the default automatically when a read-only TLS probe (`wizard._probe_os_trust`, best-effort — any failure just keeps the file prompt) shows the server cert verifying against the OS store, but never overrides a deliberately configured ca_path. Under the hood `transport._build_ssl_context("os")` builds a scoped `truststore` context (stdlib default-context fallback if truststore is missing) shared by the stdlib and shim/httpx paths — still verified TLS, never a bypass — and `firekeep doctor`'s ca-expiry check reports `ok` for `os` (the OS owns rotation). A ports-style profile is deliberately **not** offered a TLS toggle: `resolver._verify_for()` refuses `scheme=https` without both `verify_tls=true` and a `ca_path`, so a bare yes/no could only produce a config that fails to resolve. No TTY (CI, piped) or `--non-interactive` → no prompts, skeleton written as before; `--agent-id` / `--host` / `--profile` seed the prompts interactively and are written directly otherwise.

**Legacy-hook migration (`adapters/base.py`, `LEGACY_HOOK_MARKERS` / `LEGACY_ENV_KEYS`):** the retired bash hook layer and the retired `FIREKEEP_*_URL` env keys are treated as **firekeep-owned**, not foreign, so `render()` removes them from `~/.claude/settings.json` and `unrender()` cleans them up. Without this, a machine upgraded from the pre-kit installer fires every lifecycle event twice — once into a now-deleted shell script (a "No such file or directory" hook error at every session start), once into the real hook core. `upsert_hook_group()` collapses *all* firekeep groups for an event into the one rendered group (not just the first match) — that is what makes a both-layers-present machine converge instead of duplicating. The legacy `PreCompact` echo hook and `FIREKEEP_AGENT_ID` are intentionally left in place: both still work.

**kiro legacy migration (`adapters/kiro.py`, `_migrate_legacy`):** kiro's `render()` gets the same firekeep-owned-artifact treatment as the claude adapter above: it drops every `~/.kiro/settings/mcp.json` `mcpServers` entry whose key is a kit name or `<key>_`-prefixed (covers parked `firekeep-cortex_DISABLED`-style variants), and archives `~/.kiro/agents/firekeep.json` + `~/.kiro/firekeep.env` (pre-kit manual-setup artifacts) to `.bak`. Best-effort like the claude precedent: a missing file is a silent no-op, a malformed/wrong-shaped `mcp.json` is left untouched, and no migration step may ever fail `render()` or the install; it is one-way — `unrender()` does not restore the archived artifacts.

### Run all services
```bash
docker compose up -d
```

### Run tests (per service)
```bash
cd cortex && pytest tests/ -v
cd bridge && pytest tests/ -v
cd sentinel && pytest tests/ -v
cd relay && pytest tests/ -v
cd symdex && pytest tests/ -v
```

## Intelligence Features (Cortex)

- **Memory types**: `reference` (no decay), `procedural` (180d), `episodic` (90d), `transient` (14d). Auto-classified by sleep cycle LLM.
- **Multi-hop graph**: 3-hop traversal with 0.5x decay per hop. Config: `MULTIHOP_ENABLED`, `MULTIHOP_MAX_HOPS`.
- **Proactive recall**: Bridge auto-queries Cortex on `ctx_update` for plan/progress. Injected into shadow as "Relevant Past Experience."
- **Episodic distillation**: Session completion preserves full decision/progress sequences with `→` separators and file paths.
- **Composite eviction**: GC scores each memory as `(age/half_life) × 1/(1+ln(access+1)) × (1-confidence) × (0.5 + (1 − owm_efficacy))` — the last factor is OWM's (neutral 1.0 when unscored or `OWM_ENABLED=false`). Confirmed memories never evicted. Config: `EVICTION_THRESHOLD=1.5`, `DEDUP_SIMILARITY_THRESHOLD=0.78`.
- **Token-conscious recall**: Results trimmed to `token_budget` (≥2 results always kept) then optionally synthesized by LLM into coherent Markdown. Config: `RECALL_TOKEN_BUDGET=600`, `RECALL_SYNTHESIS_ENABLED=true`. Set `format="raw"` to skip synthesis.
- **Outcome-Weighted Memory (OWM, `cortex/app/owm.py`)**: recall ranked by real-world results — the nightly Celery pass (`app.owm.run_owm_scoring`, interval `OWM_SCHEDULE_HOURS=24`) joins replay `memory_read` events (which now stamp the RETURNED `memory_ids` — `app/main.py` recall handler) to session outcomes (auto-evals `failure_rate` bands: ≤0.2 success, ≥0.5 failure, middle band EXCLUDED; Bridge `abandoned` overrides as failure; eval `outcome` as fallback), computes Beta-shrunk efficacy `(s + P/2)/(n + P)` per memory (`OWM_PRIOR_N=5` — neutral 0.5 at low N by construction), and writes `owm_efficacy`/`owm_n`/`owm_updated_at` onto Qdrant payloads. Recall applies `score × (1 + OWM_WEIGHT·2·(eff−0.5))` in the lifecycle scorer (`OWM_WEIGHT=0.15`, clamped; absent field or `OWM_ENABLED=false` → bit-identical to pre-OWM ranking); the GC composite eviction score gains an efficacy factor (neutral 0.5 bit-identical; misleading memories age out up to 1.5× faster). Deterministic end to end — no LLM, no ML (the deleted recall-ranker lesson); the pass recomputes from scratch each run over `OWM_WINDOW_DAYS=30` (matching the 30d eval TTL — a larger window is dead weight) and DELETES the OWM keys from previously-scored memories with no in-window evidence, so penalties decay back to neutral rather than ratcheting (no death spiral). Fairness: sessions count ONCE per memory, one agent identity contributes at most `OWM_AGENT_CAP=5` observations per memory (a CI bot's failing loop can't bury shared memories), and corpus chunks/skill points are excluded from scoring. Kill switch is total: `OWM_ENABLED=false` neutralizes the recall multiplier AND the GC factor (stale payload values ignored). Scope caveats, documented: applies to `POST /memory/recall` only — the SSE streaming path neither applies lifecycle/OWM multipliers (pre-existing divergence) nor stamps `memory_ids`; abandoned-session detection rides Bridge's 200-newest status window (best-effort beyond it).
- **Team continuity**: All memories carry `project` and `agent_id` (contributor) fields. `GET /memory/contributors` reports per-contributor activity. `POST /memory/handoff` generates an LLM-synthesized handoff brief for a project. `memory_handoff` MCP tool wraps this.

## v2 Features

### Replay Engine (`replay/`)
Structured trace log across all services. Every memory read/write, session update, environment change, and coordination event is recorded with trace links (observed/declared/inferred).

**Emitters by service:**
- Cortex emits `memory_read` and `memory_write` on every `/memory/recall` and `/memory/learn` (tagged with `X-Session-Id` / `X-Agent-Id` from request headers, defaulting to `"unknown"` when absent — see `/admin/untagged-calls` for discipline visibility).
- Bridge emits `session.started` / `session.updated` / `session.completed` / `session.abandoned` on the corresponding `SessionManager` lifecycle methods, best-effort with lazy import and exception swallowing.
- Relay emits `coordination` events for presence updates, claims, and direct messages.
- Sentinel emits `env_change` events for container, git, and file activity.
- Agent Gateway emits `agent.action.predict` / `reconcile` events for the predict-then-act surface.

**MCP Tools:** `replay_timeline` (default limit=20), `replay_inspect` (opt-in `brief=True` for one-line payload summary), `replay_context_at`, `replay_narrow`, `replay_summary`
**REST Endpoints (on Cortex :8100):** `GET /replay/sessions/{sid}/events`, `GET /replay/events/{eid}`, `GET /replay/sessions/{sid}/context-at/{eid}`, `GET /replay/sessions/{sid}/summary`, `POST /replay/sessions/{sid}/narrow`
**Config:** `RP_ENABLED=true`, `RP_REDIS_URL=redis://redis:6379/6`, `RP_RETENTION_DAYS=30`, `RP_STREAM_MAXLEN=100000`

### Fencing Token Leases (Relay)
Upgrades claims to leases with monotonic fencing tokens, heartbeat extension, and wait queues.

**MCP Tools:** `relay_lease`, `relay_heartbeat`, `relay_lease_status`

### Task Queue (Relay)
Structured task assignment for multi-agent workflows. Agents create, list, and update tasks. Tasks are stored in Redis with sorted set indexing.

**MCP Tools:** `relay_task_post`, `relay_task_list`, `relay_task_update`, `relay_task_delete`

### Presence Registry (Relay)
Persistent agent presence with computed status. No TTL — presence persists until deregistered or manually removed. Status is computed dynamically: "active" (heartbeat within 10 minutes) or "idle" (older heartbeat). Index key is `nr:presence:__index` (double-underscore prefix to avoid collision with agent_id "index").

**MCP Tools:** `relay_register`, `relay_heartbeat_presence` (accepts optional `goal` param), `relay_deregister`, `relay_who_is_online`
**REST Endpoints (on Relay :8050):** `GET /presence`, `GET /presence/{agent_id}`, `DELETE /presence/{agent_id}`

### FirekeepScope (Relay) — Phase A
Default-on scope-clarification sessions (SP2). Sessions and screens live in Relay Redis DB 5 (`nr:scope:*`), following the presence/tasks hash-plus-sorted-set-index pattern. `origin: "cli"` sessions (from the local companion, not yet built — see `docs/superpowers/specs/2026-07-09-sp2-firekeep-scope-design.md`) own their own Bridge persistence; `origin: "mcp"` sessions (headless/MCP-only agents) have Relay persist Bridge decisions itself via a new Bridge REST route. First-answer-wins via Redis `SET NX`. 72h no-activity sweep to `abandoned`, 7-day TTL after `abandoned`/`completed`.

**MCP Tools:** `scope_start`, `scope_ask` (bounded long-poll, ~24s per call), `scope_post` (async), `scope_check`, `scope_complete`. `scope_answer` is deliberately not an MCP tool — answering is a human act, REST/dashboard only.
**REST Endpoints (on Relay :8050):** `POST /scope/sessions`, `GET /scope/sessions?status=active`, `GET /scope/sessions/{scope_id}`, `POST /scope/sessions/{scope_id}/screens`, `POST /scope/sessions/{scope_id}/screens/{screen_id}/answer`, `GET /scope/sessions/{scope_id}/events?since=`. Scope-gated `relay:read`/`relay:write` via a new Starlette-level `require_scope_asgi` helper in `auth/asgi.py` (the existing FastAPI `require_scope` can't run on FastMCP's `@mcp.custom_route` handlers).
**REST Endpoints (on Bridge :8070):** `POST /sessions/{agent_id}/context` — REST equivalent of `ctx_update`, used by Relay to persist decisions for `origin: "mcp"` sessions. This Relay→Bridge persistence requires `NR_FIREKEEP_API_KEY` to be set to a key with `session:write` scope when `AUTH_ENABLED=true`; this key currently must be provisioned manually (SP1a's automated key-bootstrap doesn't yet issue one for Relay→Bridge calls — a known follow-up, not solved by this fix).
**Dashboard:** Scope tab — lists active sessions, answers screens.
**Not yet built (Phase B, blocked on SP1's `client/` kit):** local companion (CLI + browser page), PreToolUse hook gate on `AskUserQuestion`, CLAUDE.md/kiro instruction-layer wiring, sandboxed embed (mermaid/html) rendering. Until Phase B ships, FirekeepScope is opt-in (any MCP-capable agent can call the tools above) rather than default-on.

### Direct Messages (Relay)
Agent-to-agent and dashboard-to-agent messaging. Messages stored in Redis DB 5 with 24h TTL. Delivered via poll hook or dashboard DM section.

**MCP Tools:** `relay_send_dm`, `relay_get_dm` (default limit=20)
**REST Endpoints (on Relay :8050):** `POST /dm/{agent_id}`, `GET /dm/{agent_id}`, `POST /dm/{agent_id}/read`

### Briefing Endpoint (Cortex, SP1b-server)
Server-side pre-flight aggregator that consolidates the checks the retired `briefing.sh` bash script previously assembled into a single authenticated call. `rendered` is a plain-text pre-flight briefing intended to be authoritative for thin clients; it replaces the now-retired `briefing.sh` bash assembly; the `session_start` hook core is a thin fetch of this endpoint.

**REST Endpoints (on Cortex :8100):** `GET /briefing?agent_id=&goal=&project=` — returns an envelope `{generated_at, server_version, agent_id, goal, project, briefing_id, degraded, sections{...}, instructions, rendered}` covering 11 sections (`environment`, `tasks`, `bulletins`, `quality`, `strategy_tips`, `cross_agent`, `skills`, `vault`, `resumable_sessions`, `discipline`, `dlq`): 7 assembled in-process on Cortex (quality, strategy_tips, cross_agent, skills, vault, discipline, dlq), 4 via outbound fan-in (environment ← Sentinel, tasks + bulletins ← Relay, resumable_sessions ← Bridge). Every outbound call carries the internal key. Each section always reports `{status: ok|empty|unavailable, error, data}`; `degraded=true` if any section is unavailable, but the endpoint returns HTTP 200 whenever the briefing host itself is up — fail-loud per-section, never fail-open. Gated with `require_scope("session:read")` as a deliberate aggregator-level check; individual sub-sections do not re-check their own per-scope permissions. The `vault` section is populated only for callers whose scopes include `admin` or `*`, otherwise it reports `omitted_reason: insufficient scope`. Router: `cortex/app/briefing/`.
**Relay REST additions (on Relay :8050):** `GET /tasks?assignee=&status=&limit=` → `{tasks, count}`; `GET /bulletin?limit=` → `{posts, count}` — thin wrappers over `list_tasks`/`read_bulletin` added to feed the briefing aggregator, behind SP1a auth.
**Sentinel REST additions (on Sentinel :8060):** `GET /environment` → `{status, redis, collectors, event_count, healthy, containers, container_count}` (named `/environment` rather than `/health/full` because the auth skip-list check is a prefix match on `/health`); `GET /events?source=&event_type=&severity=&limit=` → `{events, total_in_stream, returned}`.
**Config:** `RELAY_URL` (default `http://relay:8050`), `SENTINEL_URL` (default `http://sentinel:8060`) — briefing aggregator's outbound targets, passed to both cortex-api and cortex-mcp in docker-compose (mirrors `BRIDGE_URL`).
**Bridge:** `ctx_start_session` (MCP tool) and `SessionManager.start_session()` gained an optional `briefing_id`, threaded into the session hash to link a briefing to the session it originated — closing the strategy-tip A/B feedback loop (see carryover note in `docs/superpowers/specs/2026-07-08-team-activity-hub-master-design.md`). `GET /briefing`'s `instructions` field renders the server-minted `briefing_id` into every branch's suggested `ctx_start_session(goal=..., briefing_id='<id>')` call, so an agent that follows the printed instruction supplies it; the join in `GET /patterns/effectiveness` (see Feedback Loop below) only closes for sessions whose agent actually passed it along — Bridge has no way to force that, it's a documented instruction, not an enforced contract.

### Session Hooks (client kit — `firekeep_client.hooks`)
The five bash hooks are retired; the adapter wires stdlib Python hook cores at install (Claude `settings.json`, kiro inline hooks, OpenCode via a rendered JS plugin bridge; Codex has no hook surface):
- `session_start` (SessionStart / kiro agentSpawn) — thin fetch-and-print of Cortex `GET /briefing` (server-side aggregator; auth via the resolver) plus local presence registration. Replaces the 610-line briefing assembly and structurally kills its `$SESSION_ID`-unbound + shell-injection bugs. Also stashes the server-minted `briefing_id` into the session stash (`state.write_session_stash`, `session_current_{agent}@{profile}`) for the bridge shim's identity tap. Runs the once-a-day client-update check (`_update_nudge`): when a newer release exists it spawns the detached background auto-update (on by default — see Background auto-update above) and appends a one-line "updating in background" notice (or the manual "run: firekeep update" nudge when opted out).

**Identity auto-injection (client 0.1.17):** the shim attaches `X-Session-Id` on every proxied request WITHOUT the agent passing `session_id` — killing the untagged-calls discipline problem structurally rather than nagging about it, and closing the briefing_id→session A/B join mechanically. Mechanism (all client-side; cortex/bridge unchanged — `_resolve_identity` already special-cases `session_id="unknown"` to fall through to the header): (1) the **bridge** shim runs a `_BridgeSessionTap` on both pump directions — it injects the stashed `briefing_id` into a `ctx_start_session`/`ctx_resume_session` the agent sends without one, and captures the returned `session_id` into the session stash (clearing it on `ctx_complete_session`/`ctx_abandon_session`); (2) **every** shim's httpx client carries `_StashSessionAuth`, which reads the stash per-request and sets `X-Session-Id` when a fresh id exists and `/personal` bypass is off. The stash is `{agent}@{profile}`-keyed with a self-enforced TTL (`FIREKEEP_SESSION_STASH_TTL_HOURS`, default 12 — `reap_stale` does not sweep `scratch/`). Lifecycle: `session_start` clears the stash UNCONDITIONALLY at the top (a new session never inherits a crashed one's id, even if the briefing fetch fails) then writes `briefing_id` if present; the bridge tap clears it on `ctx_complete_session`/`ctx_abandon_session` (server-authoritative session end); the TTL backstops a crash. `stop` deliberately does NOT clear the stash — the `Stop` event fires every assistant turn, not at session end, so clearing there would drop attribution for turns 2..N. briefing_id is injected ONLY into `ctx_start_session` (not `ctx_resume_session`, whose bridge signature has no such param — FastMCP would reject the kwarg and break resume); both start and resume are tracked for session_id capture. Injection is a DEFAULT, not an override: an explicit agent-supplied `session_id`/`briefing_id` still wins server-side. First-turn pre-`ctx_start_session` calls stay `"unknown"` (correct — the discipline metric won't hit zero). The pump transform never raises, forwards byte-identical on error, and is GIL-safe (synchronous, no await between the pending-map check and set). **Concurrency limitation (known):** the stash is one machine-global slot per `{agent}@{profile}`, so two concurrent sessions under the SAME identity (two Claude windows as the same person on one profile) are last-writer-wins — window B's `ctx_start_session` overwrites the slot and window A's still-running shims then attach B's `session_id` to A's calls (active mis-attribution of replay/eval joins, not merely missing headers). Consistent with Bridge's own one-active-session-per-`agent_id` model; the supported partition for genuinely concurrent work is a distinct `FIREKEEP_AGENT_ID` per terminal (it flows into the stash key, the shim headers, and Bridge sessions coherently). A true fix (per-runtime-session stash keying) needs the shim to know its runtime session id — a follow-up. NOT changed here: `state.resolve_session_id`'s precedence (what `/agent/action/before` + evals key by) — a distinct attribution concern deferred to its own task.
- `stop` (Stop) — guided completion: final workspace snapshot, distill/tasks/lease reminders, and presence deregistration (race-guarded against a newer session's registration).
- `prompt` (UserPromptSubmit) — polls Relay for tasks/messages; periodic workspace snapshot to the platform cache dir.
- `pre_tool` (PreToolUse on Edit/Write) — the only blocking hook: lease check + `POST /agent/action/before`; preserves the exact block→stderr+nonzero / allow→proceed exit-code contract; falls through to allow (logged) if Cortex is unreachable. On kiro (validated on kiro-cli 2.12.1, `docs/KIRO-VALIDATION.md`) the pre-edit matcher is the exact tool name `fs_write` (Claude's `Edit`/`Write` names don't exist there) remapped via `--block-exit 2`, and the block is **advisory** — kiro 2.12.1 fires the hook but does not enforce the exit-2 block (the agent-gateway before-call still runs).
- `post_tool` (PostToolUse) — `POST /agent/action/after` reconcile, keyed to `pre_tool`'s shared temp-state.

Presence registration/heartbeat/periodic snapshots/deregistration for Claude Code — and for kiro and OpenCode, which wire the same five hook cores to their own lifecycle events — are owned directly by the hook cores above: `session_start` registers, `prompt` heartbeats, `stop` deregisters. The **sidecar** (`firekeep-sidecar`, one daemon per agent identity) is the *intended* presence owner for MCP-only runtimes with no hook lifecycle at all (Codex today), but nothing currently spawns it automatically — a Codex user has no presence path unless they run `firekeep-sidecar` by hand. The retired launcher is replaced by the `FIREKEEP_AGENT_ID` env override: set it in the process environment to run differently-identified agents from one machine (it overrides the profile's `agent_id`).

**OpenCode adapter (`client/firekeep_client/adapters/opencode.py`):** renders three surfaces — the six MCP servers into `$XDG_CONFIG_HOME/opencode/opencode.json` (`mcp` key, opencode's native `{type: "local", command: [...], environment}` shape), a firekeep-owned marker-guarded JS plugin at `.../opencode/plugins/firekeep-hooks.js`, and the firekeep instruction block upserted into the user's global `.../opencode/AGENTS.md` (marker-delimited, claude-CLAUDE.md precedent). The plugin bridges opencode's hooks to the same five hook cores via the dispatcher: session_start fires from the FIRST hook seen (`ensureStarted` latch — empirical 1.14.22: in `opencode run` mode `session.created` publishes before plugins subscribe), `session.idle` (turn end)→`prompt`, `session.deleted`→`stop`, `tool.execute.before/after` (`edit`/`write` mapped to the Claude-shaped `Edit`/`Write` names the cores expect; `bash`→`Bash` on the after side)→`pre_tool`/`post_tool`. Pre-edit blocking THROWS on the dispatcher's `--block-exit 2` exit — **VALIDATED live on opencode 1.14.22 as a HARD gate** (`docs/OPENCODE-VALIDATION.md`; write to `.env` aborted with the policy reason, file untouched). Caveats: briefing/inbox text goes to opencode's console log, not model context (no systemMessage channel); `stop` fires only on session deletion (not every turn end like Claude), so hard quits rely on briefing crash detection; headless `opencode run` auto-rejects opencode's own `permission: ask` before the firekeep gate is reached. Foreign files at the plugin path (no marker) are never overwritten or deleted.

See `docs/MULTI-AGENT.md` for the full workflow guide.
Existing `relay_claim`/`relay_release` remain as backward-compatible aliases.

### Night Shift (client kit — `firekeep_client.nightshift`)
The Fleet-as-GPU drain for the `distill_session` Relay tasks the `stop` hook has enqueued since SP1b. `firekeep night-shift [--max N] [--dry-run]` runs where the free compute lives — the developer's machine, against the LOCAL model served by LM Studio (OpenAI-compatible; `FIREKEEP_NIGHTSHIFT_LLM_BASE` default `http://127.0.0.1:1234/v1`, `FIREKEEP_NIGHTSHIFT_LLM_MODEL` default `qwen/qwen3.6-35b-a3b`, identity `FIREKEEP_NIGHTSHIFT_AGENT_ID` default `night-shift`). Per task: lease `distill.<task_id>` (fencing token), reconstruct evidence (Cortex replay summary + auto-evals, best-effort, plus the task's workspace snapshot), one STRICT-JSON local-LLM distillation (single retry, then the task is marked `failed` — visible, never retried forever), then write through the EXISTING review surfaces: `memory_learn` + optional `skill_create(status="draft")`, both attributed to the ORIGINAL session's agent/session (never the worker). The `stop` hook stamps `session_id=<sid>` (from the bridge tap's session stash) into the task description (0.1.23); older tasks without a stamp are completed as `legacy` with an honest note. Counting is honest: distilled/legacy/duplicates increment only after the relay CONFIRMS the update in-band (relay tools return `{"error": ...}` with HTTP 200 — never raise); a confirmed-stored memory whose completion update fails counts as `failed`, loudly. A TRANSIENT LLM failure defers the task (stays pending) and stops the shift; only malformed model output (one retry) marks a task `failed`. One session can enqueue N per-turn tasks (Stop fires every turn) — the stop hook now dedupes via a scratch marker per session AND the worker closes same-session duplicates within a run. Personal/bypass mode is a hard no-op checked before any call; an unreachable LM Studio aborts before any task is touched; dry-run touches nothing but the task listing. Stdlib-only (`hooks._mcp` + `transport`) — the import boundary holds. POST /skills persists `X-Agent-Id`/`X-Session-Id` provenance (previously resolved-then-discarded in the MCP proxy and hardcoded null in the route).

### Personal / Bypass Mode (client kit)
An in-session escape hatch: make Firekeep go **dormant** for personal work — nothing logged, recalled, or sent to the server. A single gate, `resolver.is_bypassed()`, is consulted everywhere; it returns true when a **transient marker file** (`~/.firekeep/personal`, deliberately NOT the config — toggling it never rewrites config) is present-and-fresh, OR the `FIREKEEP_BYPASS` env var is truthy. Fails toward NOT-bypassed on any error, so a bug here can never silently stop team logging.

**Two tiers, split by process lifecycle:**
- **`/personal` (live, mid-session)** — a rendered Claude slash command (`~/.claude/commands/personal.md`, firekeep-owned + marker-guarded so `unrender` removes only our copy) that runs `firekeep personal toggle`. The **hooks** re-read the marker every event, so the toggle takes effect at once: `session_start`/`prompt` no-op and instead emit a loud "⚠ PERSONAL MODE" systemMessage; `pre_tool`/`post_tool` allow (exit 0) with no agent-gateway call; the **decision** server checks per-call and returns a "suppressed — personal mode" notice (no Cortex synth, no socket); the **sidecar** (`firekeep-sidecar`, the presence owner for hookless runtimes) gates its register/heartbeat/snapshot/deregister on the same live gate, so no presence or workspace data reaches Relay/Bridge while bypassed. `stop` is the one hook NOT short-circuited — it self-handles bypass: clears the marker (**auto-clears personal mode at session end** — can't leak into the next session) and skips its own Relay/Bridge comms. A `FIREKEEP_PERSONAL_TTL_HOURS` (default 12) backstop reaps a marker a crashed session never cleared (also bounds an active single-session personal mode to that horizon; `FIREKEEP_BYPASS` is the un-TTL'd hard tier for longer).
- **`FIREKEEP_BYPASS=1` (hard, whole-session)** — set before launch. The running **shim** can't un-list tools mid-stream, so it honors this only at startup: `shim.run()` serves an inert **zero-tool** MCP server (no config resolved, nothing proxied to the HTTP service). Use when the whole session is personal from the start.

**CLI:** `firekeep personal [on|off|status|toggle]` (default `toggle`) — flips the marker; usable in any runtime via `! firekeep personal`. **`/personal` as plain chat text works in ANY runtime** (client 0.1.16): kiro has no slash-command surface, so typing `/personal` there used to do nothing — now the hook DISPATCHER (`hooks/__main__.py::_personal_text_command`) intercepts a prompt whose text is exactly `/personal [on|off|status|toggle]` and performs the toggle itself, returning the state as a systemMessage. The intercept deliberately sits BEFORE the dispatcher's bypass gate — while personal mode is ON the prompt core is short-circuited, so an in-core intercept could never toggle OFF. Both kiro's `userPromptSubmit` and Claude's `UserPromptSubmit` deliver the message as `payload["prompt"]` (kiro shape validated empirically on kiro-cli 2.12.1; opencode's bridge maps `session.idle` to the prompt core without a prompt text, so `/personal` there still goes through `! firekeep personal` or the CLI). `firekeep doctor` shows a `personal-mode` row that WARNs when bypass is active, so it's never silently left on.

**Config (client, all optional):** `FIREKEEP_BYPASS` (truthy → hard startup bypass), `FIREKEEP_PERSONAL_TTL_HOURS` (default 12; marker staleness backstop). Concurrency caveat: the marker is machine-global, so concurrent sessions share personal mode — fine for focused personal work, coarse if several run at once.

### Session Resumption
Automatic discovery and resumption of paused or crashed sessions on conversation start. No new tools or endpoints — purely hook-layer.

**How it works:**
1. During a session, the `prompt` hook core snapshots workspace state (git branch, recent commits, diff stats) to the platform cache dir every 5th prompt
2. On clean exit, the `stop` hook core captures a final snapshot
3. On next conversation start, the `session_start` hook core (via `GET /briefing`) surfaces Bridge paused sessions and Relay-presence crash detection
4. If resumable sessions found, the briefing nudges the agent to call `ctx_resume_session` which returns the full shadow context including the workspace snapshot

**Crash detection:** An "active" session with no Relay presence entry means the previous instance crashed. The briefing treats it as resumable.

**Age thresholds:** Strong nudge for sessions < 72h old ("You have unfinished work"), neutral mention for older sessions.

### A2A Agent Card Discovery (Relay)
Minimal A2A discovery endpoint (discovery only — a former JSON-RPC gateway + SSE streaming were removed; see `docs/HISTORY-NOTES.md`).

**Endpoint:**
- `GET /.well-known/agent.json` — Agent Card listing Firekeep capabilities for discovery by external agent registries and dashboards.

### Auto-Evals
Computes quality metrics from replay traces on session completion. 10 Tier 1 metrics (directly measurable): tool_success_rate, memory_freshness_at_recall, failure_rate, claim_contention_rate, etc.

**MCP Tools:** `eval_session`, `eval_summary`
**REST Endpoints (on Cortex :8100):** `GET /evals/sessions/{sid}`, `GET /evals/summary`, `POST /evals/sessions/{sid}/compute`, `GET /evals/trends?window=N`

**Quality Trends (`cortex/app/self_diagnosis.py`):** Compares recent N sessions vs previous N to detect improving/stable/degrading trends per metric. Dashboard Evals tab shows trend arrows. `detect_regressions()` returns metrics that have degraded beyond threshold.

### Pattern Engine (`cortex/app/patterns/`)
Background analysis that discovers what strategies work across sessions. Extracts features from replay traces (tool sequences, memory usage, file paths, duration, outcome), runs 6 pattern detectors, and produces strategy cards with confidence scores.

**Pattern Detectors:** memory-first usage, file hotspots, tool sequence correlation, memory usage levels, session duration buckets, failure mode detection.

**Pattern Categories:** `procedural` ("do X before Y" — briefing eligible at trial+), `risk` ("area X has elevated failure rate" — briefing eligible at trial+), `behavioral` ("sessions with trait X succeed more" — analytics only, never shown in briefings). Detector mapping: memory_first/tool_sequence → procedural, file_hotspot/failure_mode → risk, memory_usage/duration → behavioral.

**Promotion Ladder:** `candidate` → `observed` → `trial` → `validated` → `stale` → `retired`, with `quarantined` as a side state reachable from any stage. Only `trial` and `validated` procedural/risk patterns appear in briefings (max 3 tips). Promotion criteria: observed (evidence ≥ 10, confidence ≥ 0.3), trial (evidence ≥ 15, confidence ≥ 0.5, stable), validated (evidence ≥ 25, confidence ≥ 0.65, positive tip lift). Patterns go stale after 30 days without matching sessions.

**Hard Limits:** Max 50 active patterns (candidates excluded from count). Confidence decay: -0.02/week without new supporting sessions. Auto-retirement when confidence drops below 0.2. Duplicate descriptions are deduplicated on creation.

**Quarantine:** Instant kill switch — removes a pattern from all briefings immediately. Unquarantine returns the pattern to `candidate` stage for re-evaluation.

**Feedback Loop:** When briefing shows strategy tips, records which patterns were shown. After session completion, compares outcomes of sessions with tips vs without. Effective tips get reinforced; counterproductive tips decay. A/B testing randomly assigns sessions to treatment (tips shown) or control (tips withheld) groups for causal measurement. Tips shown via `GET /briefing` are recorded under the server-minted `briefing_id` (not yet a `session_id` at that point), while outcomes are keyed by `session_id` — a disjoint key space that would otherwise silently break the join. Bridge only learns a session's `briefing_id` if the caller passes it to `ctx_start_session`; `GET /briefing`'s `instructions` field renders it into the suggested call (see Briefing Endpoint above) so a compliant agent supplies it. `GET /patterns/effectiveness` closes the join: it fetches Bridge `GET /sessions` (carrying the internal key), reconciles each session's Bridge-stored `briefing_id` field to its `session_id`, and passes that map into `compute_tip_effectiveness` so tips shown through the briefing join to their session's outcome — for sessions whose agent passed the id along. Bridge unreachable degrades to an empty map (session_id-keyed logs from `POST /patterns/tip-shown` still join; briefing-keyed logs don't) rather than failing the endpoint.

**Cross-Agent Learning:** `GET /patterns/relevant?exclude_agent=X` returns patterns from other agents' sessions. Briefing hook adds a "Cross-Agent Learnings" section for multi-agent setups.

**REST Endpoints (on Cortex :8100):** `GET /patterns/?stage=X&category=Y`, `GET /patterns/relevant?goal=X&exclude_agent=Y`, `POST /patterns/analyze`, `POST /patterns/tip-shown?group=treatment|control`, `GET /patterns/effectiveness`, `POST /patterns/{id}/quarantine`, `POST /patterns/{id}/unquarantine`

**Experiment Framework (optional):** Datasets define filtered session subsets and experiments compute chi-square / Cohen's h / CI statistics. Gated behind `PATTERN_EXPERIMENTS_ENABLED=false` (default off). When enabled, also registers: `POST /patterns/datasets`, `GET /patterns/datasets`, `GET /patterns/datasets/{id}`, `DELETE /patterns/datasets/{id}`, `POST /patterns/experiments`, `GET /patterns/experiments`, `GET /patterns/experiments/{id}`, `POST /patterns/experiments/{id}/conclude`. Models: `Dataset`, `Experiment` in `cortex/app/patterns/models.py`. Statistics: `cortex/app/patterns/statistics.py`.

### Auth (`auth/`)
Per-key API auth with scopes, enforced by a shared pure-ASGI validator (`auth/asgi.py`, `FirekeepKeyAuthMiddleware`) on **every** surface when `AUTH_ENABLED=true`: all four MCP apps (cortex-mcp :8080, bridge :8070, sentinel :8060, relay :8050 — injected via `mcp.run(..., middleware=build_auth_middleware(...))`) and Cortex REST :8100 (replaces the retired legacy `APIKeyMiddleware` / `API_KEY` env). Fail-closed: Redis DB 7 unreachable while enabled → 503 loud, never pass-through (note: compose healthchecks are TCP-only, so containers stay "healthy" during a fail-closed outage). Skip list (prefix match): `/health`, `/version`, `/.well-known/agent.json`, plus on Cortex REST `/docs`, `/redoc`, `/openapi.json`. `auth/asgi.py` also supports a separate `skip_exact_paths` (literal-match only, not a prefix) — Cortex REST uses it for exactly `/dashboard` and `/dashboard/` (`app/main.py`'s `AUTH_SKIP_EXACT_PATHS`), so the dashboard HTML shell stays keyless but everything under `/dashboard/api/*` is auth-gated like any other REST route (closes a 2026-07-26 hole where `GET /dashboard/api/memories` returned full memory content unauthenticated — a bare prefix-matched `/dashboard` exempted the whole subtree). **Default `AUTH_ENABLED=true` since 2026-07-26 (audit blocker 7)** — it was `false`, which shipped every install unauthenticated. Safe to default on because `install.sh`/`update.sh` run `deploy/bootstrap-keys.sh` BEFORE the app containers start and that script writes Redis DB 7 with `redis-cli` rather than POSTing to a now-gated `/auth/keys`, so it cannot lock itself out; the installer's health probes still pass (`/health` and `/version` are skip-listed, and the one probe hitting a gated path — `GET /mcp` on cortex-mcp — is satisfied by install.sh's `2??|401|405` accept list). Compose interpolates from `.env`, so an EXISTING `.env` carrying an explicit `AUTH_ENABLED=false` keeps winning: this changes the default for new installs, not existing ones, and an existing deployment stays unauthenticated until its owner edits that line. Paired with `BIND_ADDR` (default `127.0.0.1`) on all six published app ports — the datastore bindings stay literal `127.0.0.1` and are deliberately NOT governed by `BIND_ADDR`, so widening the app surface never publishes a passwordless Redis. `docker-compose.office.yml` pins its ports with `!override` literals, so `BIND_ADDR` cannot widen the office deployment.

**Scope checks:** `require_scope` (FastAPI Depends, Cortex REST — enabled-ness from `init_auth()`, which the Cortex lifespan calls) and `require_scope_asgi` (Starlette-level, for FastMCP `@mcp.custom_route` handlers on bridge/relay). Since 2026-07-16 `require_scope_asgi` derives enabled-ness from `AuthSettings` (the `AUTH_ENABLED` env var — the same truth `build_auth_middleware` reads), NOT from `keys._AUTH_ENABLED`: the FastMCP service processes never call `init_auth()`, so that flag was permanently False there and every custom-route scope gate passed anonymously under office auth. Tests must monkeypatch `auth.asgi.get_auth_settings`, not the keys flag.

**Keys & bootstrap:** `deploy/bootstrap-keys.sh` (idempotent, invoked by `install.sh` and `update.sh`) mints: the internal service key (`FIREKEEP_INTERNAL_KEY` in `.env` — bridge distiller via `NB_FIREKEEP_API_KEY` + workers; scopes `memory:write,session:read,eval:read,eval:write`, NOT admin), the dashboard key (`DASHBOARD_API_KEY` — nginx injects it as `X-API-Key` on the dashboard's `/api/*` proxies; admin-scoped, behind nginx basic-auth = documented second door), and an admin key (plaintext printed exactly once, never stored). Teammate keys: `deploy/firekeep-admin keys create --agent <name>` → full non-admin scope set. **Confused-deputy fix:** cortex-mcp holds no key — it forwards the caller's `X-API-Key` on proxied REST calls, so `require_scope("admin")` on vault/auth routes sees the caller's own scopes.

Under office `AUTH_ENABLED=true`, Sentinel threads `FIREKEEP_INTERNAL_KEY` (via `NS_FIREKEEP_INTERNAL_KEY`) as `X-API-Key` onto its outbound alert-broadcast (→Relay `/mcp`) and webhook (→Cortex `/webhooks/internal/fire`) calls; unset on personal VPS. Symdex threads the same `FIREKEEP_INTERNAL_KEY` (bare env var — Symdex has no Settings prefix) as `X-API-Key` on its outbound Cortex calls (SP1b §11, Task 33) — this closes the last dark integration; every outbound call to an auth-enforced surface is now keyed under `AUTH_ENABLED=true` (Symdex itself has no auth middleware and is loopback-only, so inbound calls to it need none).

**REST Endpoints (on Cortex :8100):** `POST /auth/keys`, `GET /auth/keys`, `DELETE /auth/keys/{id}`, `GET /auth/scopes`
**Scopes:** `memory:read`, `memory:write`, `session:read`, `session:write`, `replay:read`, `relay:read`, `relay:write`, `eval:read`, `eval:write`, `admin`

**Anonymous identity (the `AUTH_ENABLED=false` path):** `keys.ANONYMOUS_SCOPES` is DERIVED as `SCOPES - {"admin", "*"}` (not a literal list — a scope added later is granted automatically instead of silently withheld). Not `[]`: with auth off the product must still work for a single user who has done no key management, and an empty set 403s every `require_scope` route. Not `["*"]` (what shipped until audit blocker 7): `admin` is the whole exposure — it gates decrypted vault reads (`vault/api.py`) and key minting (`auth/api.py`), and granting it to every anonymous caller is what put 12 real secrets on the public internet. **Narrowing the list was only half the fix** — both disabled paths (`require_scope` in `auth/middleware.py`, `require_scope_asgi` in `auth/asgi.py`) previously returned the anonymous identity WITHOUT consulting the required scope at all, so the list could say anything and `admin` routes still passed. They now run `keys.scopes_allow(anon_scopes, scope, allow_wildcard=False)` and raise 403 via `keys.anonymous_denied_detail()`. `allow_wildcard=False` is specific to that path: a real key legitimately carries `["*"]` (owner + dashboard keys) and must keep passing every gate, but a caller who never presented a key gets a plain membership test so a regression in `ANONYMOUS_SCOPES` cannot re-open vault/key-minting. Net: with auth off, everything below `admin` is open and unattributed — that is a convenience mode for a loopback-bound single user, not a safe posture on a reachable stack.

**Config:** `AUTH_ENABLED=true` (default since 2026-07-26), `AUTH_REDIS_URL=redis://redis:6379/7`, `FIREKEEP_INTERNAL_KEY`, `DASHBOARD_API_KEY`, `BIND_ADDR=127.0.0.1`
**Office deployment:** `docker-compose.office.yml` override rebinds all app ports to 127.0.0.1 behind Caddy :443 (internal CA, path routing `/mcp/<svc>` + `/api/<svc>/`; symdex is client-side stdio only and has no server container to route in the office deployment). Runbook: `docs/DEPLOYMENT-OFFICE.md`.

### Build Provenance (`provenance/`) — Stage 1
Shared build-identity module (`provenance.get_version_info(service) -> {service, version, git_sha, build_time}`) so every service answers "what version are you running?" identically instead of four copies of the logic drifting. `GET /version` is registered on all four services — Cortex REST :8100 (`cortex/app/version.py` is a thin adapter preserving its pre-existing 3-key response shape for `cortex/tests/test_version.py`), and as an `@mcp.custom_route` on bridge :8070, sentinel :8060 and relay :8050 (matching the `/health` precedent). Unauthenticated on all four (in the auth skip list alongside `/health`) and probes no backends, so it answers even when the service's dependencies are down — a support call's first question. Values are injected at image build time: each Dockerfile declares `ARG GIT_SHA=unknown` / `ARG BUILD_TIME=unknown` / `ARG APP_VERSION=0.6.0` and `ENV`s them; `docker-compose.yml`'s `build.args` passes them through from the host env; `install.sh`/`update.sh` export `GIT_SHA` (`git rev-parse --short HEAD`), `BUILD_TIME` (UTC ISO8601) and `APP_VERSION` (`git describe --tags --match 'v[0-9]*' --always --dirty`) before building — the `--match` excludes this repo's `client-vX.Y.Z` client-kit release tags so a server image never reports a client version; falls back to the short SHA until server `vX.Y.Z` tags exist. **Known gap:** `docker-compose.yml`'s four cortex build blocks don't pass `APP_VERSION` (only `GIT_SHA`/`BUILD_TIME`) — a `docker compose build` deploy has cortex reporting the `0.6.0` fallback while bridge/relay/sentinel report the real value; and `.gitlab-ci.yml`'s bare `docker build --build-arg` calls for bridge/sentinel/relay pass none of the three (mirrors the pre-existing `REGISTRY` office-CI comment) — an office-CI-built image reports the Dockerfile defaults regardless of `install.sh`/`update.sh`. Neither is fixed yet.

### Secrets Vault (`vault/`)
Encrypted secret storage for infrastructure credentials, API tokens, and connection strings. Uses Fernet symmetric encryption with values encrypted at rest in Redis DB 7 (shared with auth, distinct key prefix `vault:secret:`).

**MCP Tools:** `vault_store`, `vault_retrieve`, `vault_list`, `vault_delete`
**REST Endpoints (on Cortex :8100):** `POST /vault/secrets`, `GET /vault/secrets/{key}`, `DELETE /vault/secrets/{key}`, `GET /vault/secrets` (list metadata only)
**Config:** `VAULT_ENABLED=true`, `VAULT_KEY=<fernet-key>` (required), `VAULT_REDIS_URL=redis://redis:6379/7`
**Generate key:** `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

### Corpus (`corpus/`)
Business knowledge documents chunked and stored in Qdrant for semantic recall. Chunks are naturally discoverable via `memory_recall` (tagged `source=corpus`). Source metadata tracked in Redis. Corpus stores chunks only — there is no entity/relationship graph (a former Neo4j extraction path was removed; see `docs/HISTORY-NOTES.md`).

**MCP Tools:** `corpus_ingest`, `corpus_sources`, `corpus_delete`
**REST Endpoints (on Cortex :8100):** `POST /corpus/ingest`, `GET /corpus/sources`, `DELETE /corpus/sources/{source_name}`
**Config:** `CORPUS_ENABLED=true`

**Usage:** Agents ingest docs via `corpus_ingest` (copy-paste text or fetch URL first). Corpus chunks are tagged with `source=corpus` and `tags=["corpus", source_type, source_name_slug]` so they appear in regular memory recall alongside operational memories. Re-ingesting a source with the same name automatically replaces old content via a staged swap (new chunks are written and committed first; the previous generation is deleted only afterward, so a mid-ingest failure leaves the old content intact).

### Memory Improvements (`cortex/app/`)
Phase 1–3 upgrades for storage hygiene, retrieval precision, and team continuity.

**Phase 1 — Storage Hygiene:**
- Composite eviction: `score = (age/half_life) × 1/(1+ln(access+1)) × (1-confidence) × (0.5 + (1 − owm_efficacy))` (OWM factor neutral at 1.0 when unscored/disabled). Confirmed memories are never evicted.
- Dedup threshold: `DEDUP_SIMILARITY_THRESHOLD=0.78` (replaces old fixed 0.85)

**Phase 2 — Retrieval Precision:**
- Token budget: trims results to `RECALL_TOKEN_BUDGET` tokens (always keeps ≥2), then optionally synthesizes via LLM
- `format` param on `memory_recall`: `"synthesized"` (default) runs LLM synthesis, `"raw"` skips it

**Phase 3 — Team Continuity:**
- `project` and `agent_id` fields on all memories; `project` is normalized to lowercase
- `agent_id` = contributor identity (person's name). An interactive `firekeep install` prompts for it (default: the configured value, else the OS username) and writes it to every profile it configures. `CHANGEME` remains the skeleton value only on a non-interactive install that passed no `--agent-id`. `FIREKEEP_AGENT_ID` still overrides the profile value per-process.
- `/memory/learn` reads `X-Agent-Id` / `X-Session-Id` from request headers and persists them to the Qdrant payload (top-level fields, so `/memory/contributors` and project filters can read them directly). Default is `"unknown"` when headers are absent.
- Legacy sentinel: memories created before the `agent_id` field was wired (~3.9K records) are tagged `agent_id="legacy-pre-team-continuity"` via `cortex/scripts/backfill_legacy_agent_id.py`. The backfill is idempotent and only touches records with `agent_id` missing or `"unknown"` AND `timestamp` before today's UTC date.

**MCP Tools (updated/new):**
- `memory_recall` — now accepts `token_budget=600`, `format="synthesized"`, `project=None`; top_k default 3
- `memory_handoff` — generate LLM handoff brief for a project: `memory_handoff(project, since_days=7, agent_id, session_id)`

**REST Endpoints (on Cortex :8100):**
- `GET /memory/contributors?project=X&since_days=7` — list contributors with activity stats
- `POST /memory/handoff` — body: `{project, since_days}` — returns synthesized handoff Markdown

**Config:**

| Var | Default | Purpose |
|-----|---------|---------|
| `EVICTION_THRESHOLD` | `1.5` | Composite eviction score cutoff |
| `DEDUP_SIMILARITY_THRESHOLD` | `0.78` | Memory agent dedup cosine threshold |
| `RECALL_TOKEN_BUDGET` | `600` | Max tokens per recall response |
| `RECALL_TOP_K` | `3` | Default number of results to retrieve |
| `RECALL_SYNTHESIS_ENABLED` | `true` | Enable LLM synthesis pass in recall |
| `DEDUP_ENABLED` | `false` | Master switch for the 6-hourly dedup/merge pass (default off until validated) |
| `RECALL_SCORE_FLOOR` | `0.35` | Raw-cosine floor passed to Qdrant `score_threshold`; recall returns `degraded: true` when vector search fails |
| `EMBED_RETRY_ATTEMPTS` | `3` | Write-path embedding retries before backfill enqueue |
| `BACKFILL_MAX_ATTEMPTS` | `10` | Per-entry backfill drain attempts before the entry moves to `memory:backfill:dlq` |

### Skills (`cortex/app/skills/`)
Team-shareable playbooks for recurring situations, stored as `memory_type="skill"` in Cortex.

**Skills are CLIENT-authored (the primary path).** The agent (Claude/Kiro/…) — which holds the full session context and a capable model — authors the skill itself via the `skill_create` MCP tool (→ `POST /skills`), which only runs the *embedding* model server-side (no generation LLM). The global cognitive-stack instructions and the client `stop` hook direct agents to do this after a hard-won fix / reusable technique. This is what makes skills work on a **CPU-only, GPU-less** deployment: the intelligence is on the client, the server just stores + serves. See `docs/…` and the root cognitive-stack CLAUDE.md.

**Server-side session synthesis is OFF by default (`SKILL_SYNTHESIS_ENABLED=false`).** The legacy path — a 4-signal scorer (error density 0.30, session anomaly 0.20, resolution language 0.35, manual flag) that, on `ctx_complete_session(skill_worthy=True)`, fetched the session shadow and called an LLM to *generate* a skill card — needs a fast LLM the default CPU deploy doesn't have (qwen3:4b on CPU takes >300s per card and times out). It remains behind the flag for a future fast-LLM deploy; on failure/empty it now stores NO placeholder skill. `SKILL_SCORE_THRESHOLD` still gates it when enabled.

**Injection:** Briefing hook injects top-3 active skills matching session goal. `skill_recall` MCP tool for on-demand retrieval during session.

**Staleness (aging story for active skills):** client-authored skills have no source document, so `needs_rereview` (which fires only when a backing doc changes) never catches a skill whose subject rotted. The recall hot path HSETs `memory:last_recalled` (`memory_id → ISO ts`, alongside the access-count accumulator — so skills surfaced through general RAG are covered, the high-volume signal); a memory-agent pass (`flush_last_recalled`, `cortex/app/workers/memory_agent.py`) drains it to the `last_recalled_at` Qdrant payload; then `skill_staleness_pass` (`cortex/app/skills/staleness.py`, registered in `run_memory_agent` right AFTER that flush so timestamps are current) flags active skills unrecalled past `SKILL_STALE_AFTER_DAYS` (default 90) as `stale=True` (falling back to the creation timestamp when never recalled) and un-flags any recalled since — self-healing both directions. It NEVER changes `skill_status` and NEVER deletes (a review signal, not eviction — mirrors gc's skill skip). Surfaced via `GET /skills?status=active&stale=true` and the dashboard Skills tab's "Stale (review)" filter + STALE badge + "Still valid" action (`PATCH /skills/{id}` `{stale:false}`). Because `stale` is sweep-DERIVED, a human clearing it also stamps `stale_reviewed_at=now`, which the sweep counts as freshness (`max(last_recalled_at, stale_reviewed_at, timestamp)`) — otherwise the next cycle would re-flag the just-reviewed skill. A review buys one more full window and does NOT touch `last_recalled_at` (which would falsify recall activity). Promoting a skill to active (`PATCH skill_status=active`) also stamps `stale_reviewed_at` — otherwise a Docs→Skills draft that aged past the threshold in the review queue would be flagged stale the instant it's approved (its only timestamp is the old synthesis time). Config: `SKILL_STALE_AFTER_DAYS=90`. Phase 2 (not built): a client-side Symdex `audit_skill_refs` cross-check of skill content against the code index — must run client-side since Symdex is stdio-local with no server container.

**MCP Tools:** `skill_recall(task, project, top_k)`, `skill_create(trigger, symptoms, steps, ..., status="active")`, `skill_list(status, project)`. `skill_create`'s `status` is `"active"` (default, immediately recallable) or `"draft"` (lands in the dashboard review queue, excluded from recall until a human `PATCH`es it active) — `"draft"` is for the **client-side knowledge-ingest flow** (see below): on a deploy with no server generation model, an agent classifies a document itself and calls `skill_create` once per procedure, optionally as drafts for review, giving document → skills without server-side classification (intelligence on the client, storage on the server — same principle as client-authored skills).

**REST Endpoints (on Cortex :8100):** `POST /skill/evaluate`, `GET /skills`, `GET /skills/{id}`, `POST /skills`, `PATCH /skills/{id}`, `DELETE /skills/{id}`

**Config:**

| Var | Default | Purpose |
|-----|---------|---------|
| `SKILL_SYNTHESIS_ENABLED` | `false` | Legacy server-side session→skill LLM synthesis. OFF by default — skills are client-authored via `skill_create`. Enable only on a fast-LLM deploy. |
| `SKILL_SCORE_THRESHOLD` | `0.6` | Score cutoff for synthesis (when enabled) |
| `SKILL_STALE_AFTER_DAYS` | `90` | Active skills unrecalled this long are flagged `stale=True` for review by the memory-agent staleness sweep (never deleted/status-changed; re-recall un-stales) |
| `SKILL_SYNTH_TIMEOUT_SECONDS` | `300.0` | LLM budget for server-side synthesis (when enabled); sized for slow CPU |
| `BRIDGE_URL` | `http://bridge:8070` | Bridge URL for session data |
| `SKILL_ERROR_DENSITY_WEIGHT` | `0.30` | Error signal weight |
| `SKILL_ANOMALY_WEIGHT` | `0.20` | Duration anomaly weight |
| `SKILL_RESOLUTION_WEIGHT` | `0.35` | Resolution language weight |
| `SKILL_AGENT_SCHEDULE_HOURS` | `6` | Pass 9 cadence |

### Docs→Skills (Knowledge) pipeline (`cortex/app/knowledge/`)
Front door for turning raw documents (wikis, runbooks, Jira exports, API docs) into both searchable corpus content and reviewable draft skills, in one call. Distinct from plain `corpus_ingest`: use `knowledge_ingest` when the document may contain step-by-step procedures worth auto-drafting into skills.

**Flow:** `knowledge_ingest(content, source_name, source_type)` → the full document is ingested to the corpus **synchronously** (via `corpus.pipeline.ingest_document` directly, so this works even with `CORPUS_ENABLED=false`; searchable immediately) → a `queued` ingest-status record is written (`app/knowledge/status.py`, Redis key `knowledge:ingest_status:{source_name}`) → one `classify_and_draft_from_doc` Celery task is enqueued (`app/workers/skill_synthesis.py`) and the call returns without waiting on it. Ordering invariant: corpus ingest must succeed before the status write/enqueue — a corpus-ingest failure surfaces as HTTP 500 with no status written and no task queued. Out of band, the worker runs the LLM JSON-mode classifier (`cortex/app/knowledge/classifier.py`, same call pattern as the Sleep Cycle worker) to label the document `reference` / `procedural` / `mixed` and extract procedure titles, fans out one `draft_skill_from_doc` Celery task per detected procedure title (capped at `KNOWLEDGE_MAX_PROCEDURES`, each producing a `skill_status="draft"` skill point via `synthesize_from_document` in `cortex/app/skills/synthesizer.py`), and records the terminal `classified`/`failed`/`corpus_only` status back to the same ingest-status record. `reference` documents therefore only add corpus content; `procedural`/`mixed` documents add corpus content immediately **plus** N draft skills once async classification completes. **Generation-offline degradation:** on a deploy with no generation model (e.g. the office embed-only ollama image), the classify LLM call fails; the classifier distinguishes this (`_is_backend_unavailable` — connection/timeout errors or an HTTP 404 / "model not found") from a genuine classify error and returns `unavailable: True`, so the worker records the terminal status `corpus_only` (disposition `reference`) instead of `failed`. The document is still corpus-ingested and searchable; classification/skill-drafting is simply deferred (no config or chart change needed). Once a generation backend is deployed, **new** ingests classify normally on their own; documents ingested during the offline window stay `corpus_only` until **re-ingested** (re-ingest is idempotent via the deterministic skill point IDs — `uuid5(source_name::procedure_title)` — so it produces no duplicates). The dashboard renders `corpus_only` as a neutral "Corpus-only (generation offline)" badge, not a red error.

**Deterministic-ID re-ingest:** each draft skill's Qdrant point ID is `uuid5(SKILL_NS, "source_name::procedure_title")`, so re-ingesting the same document/procedure pair upserts the same point idempotently (no duplicate drafts, no delete-then-write race). Active-skill guard: if that ID already holds a human-approved (`skill_status="active"`) point, re-ingestion does **not** overwrite its content — it only sets `needs_rereview=True` so a human knows the source document changed.

**Approval-then-recall:** drafts surface in the dashboard's Skill Draft Queue (Knowledge tab); a human approves via `PATCH /skills/{id}` (sets `skill_status="active"`), after which the skill becomes visible to `skill_recall`, the briefing's active-skills section, and general `memory_recall`. Until approved, draft skills are excluded from **all** recall paths, including the core `memory_recall`/`recall_streaming` vector search (`cortex/app/db/vector.py` applies a `must_not skill_status="draft"` filter — a back-door leak fix, since `skill_recall`/the briefing already scrolled with explicit active-only filters but the primary RAG search path didn't).

**MCP Tools:** `knowledge_ingest(content, source_name="Untitled", source_type="text")` (cortex-mcp)
**REST Endpoints (on Cortex :8100):** `POST /knowledge/ingest` → **202** `{corpus_source, status, note}` (`status="queued"`; classification + drafting happen async in the worker); `GET /knowledge/sources` → corpus sources joined with each source's pending (draft) skill count plus the latest async ingest-status fields (`status`, `disposition`, `skills_queued`, `updated_at`)
**Config:** `KNOWLEDGE_ENABLED=true` (gates router registration in `main.py`), `KNOWLEDGE_MAX_PROCEDURES=10` (caps procedure titles per document; excess is dropped with a note, not queued), `KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS=300` (bounds the worker's classify LLM call in `classifier.py`, not any request/proxy timeout — sized for CPU Ollama, where a classify runs ~150–200s on qwen3:4b; lower on GPU for faster fail-loud), `KNOWLEDGE_STATUS_TTL_SECONDS=2592000` (30d TTL on the per-source ingest-status Redis hash — orphan safety net).

**URL ingestion:** `POST /knowledge/ingest-url` crawls a URL instead of requiring pasted text — same downstream pipeline (corpus ingest → classify → draft skills), just a different front door. `cortex/app/knowledge/crawler.py` provides the SSRF-guarded crawler: `is_safe_url(url) -> (ok, reason)` validates scheme (`http`/`https` only) and resolves every A/AAAA record for the host, rejecting loopback/private/link-local/reserved/multicast/unspecified addresses (incl. the `169.254.169.254` cloud-metadata address) — checked before the start URL, before every same-site link followed, and before every redirect hop (bounded to 4 hops); DNS-rebinding TOCTOU is a documented, accepted limitation (single-owner trusted tool, endpoint is also admin-gated). `crawl(start_url, *, depth, max_pages, timeout, max_bytes) -> list[CrawledPage]` does a same-site-only BFS (subdomain-tolerant via last-two-labels matching) up to `depth` hops and `max_pages` pages, converting each fetched page to Markdown via `markdownify`; raises `ValueError` only for an unsafe **start** URL (so the endpoint can 400) — every URL after that degrades to a skipped page rather than raising. The endpoint (`app/knowledge/api.py`) clamps `depth`/`max_pages` into `[0, KNOWLEDGE_CRAWL_MAX_DEPTH]` / `[1, KNOWLEDGE_CRAWL_MAX_PAGES]` (over-limit requests are clamped, not rejected), re-checks `is_safe_url` on the start URL as defense-in-depth (400 if unsafe), then enqueues `run_url_ingest` (Celery, `app/workers/skill_synthesis.py`) and returns 202 immediately — the crawl itself runs entirely in the worker. `run_url_ingest` builds its own redis + vector clients (mirrors `classify_and_draft_from_doc`), crawls, and calls `ingest_knowledge_document` once per fetched page (source name `Web:{hostname}:{title-or-url, truncated to 120 chars}`, source_type `"web"`); one bad page is logged and skipped rather than aborting the rest of the crawl, and the task never raises (crawl-level `ValueError` included) — failures surface as a `{"status": "error", "error": ...}` return value, not a Celery failure.

**MCP Tools:** `knowledge_ingest_url(url, depth=0)` (cortex-mcp) — proxies to `POST /knowledge/ingest-url` the same way `knowledge_ingest` proxies to `POST /knowledge/ingest`.
**REST Endpoints (on Cortex :8100):** `POST /knowledge/ingest-url` → body `{url, depth=0, max_pages=25}` → **202** `{status, url, note}` (`status="queued"`), or **400** `{"detail": "URL rejected: <reason>"}` if `is_safe_url` fails the start URL.
**Config:** `KNOWLEDGE_URL_INGEST_ENABLED=true`, `KNOWLEDGE_CRAWL_MAX_DEPTH=2` (hard ceiling the endpoint clamps requested `depth` into), `KNOWLEDGE_CRAWL_MAX_PAGES=25` (hard ceiling the endpoint clamps requested `max_pages` into), `KNOWLEDGE_CRAWL_TIMEOUT_SECONDS=15` (per-request HTTP timeout used by the crawler), `KNOWLEDGE_CRAWL_MAX_PAGE_BYTES=2000000` (per-page byte cap before truncation).

### Collectors (SP3 — Living Knowledge Sync)
Scheduled collectors that pull external documentation sources into the docs→skills pipeline automatically, so a wiki/runbook edit reaches the corpus and (for procedural content) the skill-draft queue without a human manually pasting content into `knowledge_ingest`.

**Framework (`cortex/app/collectors/`):** `SourceAdapter` (`base.py`) is a minimal `Protocol` any source implements — `discover_changed(seen)` returns the list of changed `SourceItem`s (`stable_id`, `version`, `label`, `meta`), `fetch_content(item)` returns `(markdown, source_name, source_type)`, `aclose()` releases the adapter's client. `CollectorEngine` (`engine.py`) is source-agnostic orchestration run per collector name: enabled-gate first (no clients built at all if disabled) → Redis `SETNX` lock (`collector:lock:{name}`, TTL `COLLECTOR_LOCK_TTL_SECONDS`) serializes runs, which is what makes the per-run Vault bootstrap safe → PAT resolution, env-first: a truthy `pat_env_value` (from `CONFLUENCE_PAT`, i.e. a K8s Secret or `.env`) is used directly and Vault is never touched; otherwise falls back to in-run Vault init + PAT fetch, unchanged (engine-owned fail-fast: no PAT from either source → run aborts with `health="error"`, no adapter built, nothing ingested) → adapter built via a factory closure → change detection → per-item ingest via the shared `ingest_knowledge_document` core → `CollectorState.record_version` per successfully ingested item → one `CollectorState.record_run` call in `finally` → `collection.sync` replay emit → lock release. `run()` never raises: every failure path (lock error, no PAT, per-item error, unhandled exception) degrades to a status dict instead. `CollectorState` (`state.py`) is the Redis bookkeeping layer: `collector:versions:{name}` hash maps each item's `stable_id` → last-ingested `version` (change detection is a version-number comparison, not content hashing); `collector:run:{name}` hash holds the latest run's `last_run`/`pages_seen`/`pages_ingested`/`pages_skipped`/`errors`/`health`.

**Confluence adapter (`confluence.py`):** targets Confluence Server/Data-Center (PAT bearer auth, not Confluence Cloud OAuth). Two-phase fetch: phase 1 pages through `GET /rest/api/content/search` with a CQL query built from `CONFLUENCE_SPACE_KEYS` (comma-separated space keys) plus an optional `CONFLUENCE_LABEL` filter, expanding `version,space` and following `_links.next` for pagination; each result's `version.number` is compared against the last-seen version (via the engine's `seen` callback) to build the changed-item list, and the total pages scanned is recorded as `last_total_seen` for the skipped-count derivation. Phase 2 fetches `body.storage` (Confluence storage-format XHTML) per changed page and converts it to Markdown via `markdownify` (guarded import — raises `RuntimeError` at conversion time if the dependency isn't installed, rather than crashing the whole worker at import time). Ingested source name is `Confluence:{space_key}:{title}`, source_type `"wiki"`. Runs as the Celery task `run_confluence_collector`.

**Safe skill lifecycle (`cortex/app/skills/reconcile.py`):** `reconcile_source_skills(source_name, new_titles, vector)` runs after each classify pass (called from `_run_classify_and_draft` in `app/workers/skill_synthesis.py`, in its own guarded block so a Qdrant hiccup can't flip an already-successful `classified` status to `failed`). It scrolls Qdrant for that source's skill points and, for any whose `procedure_title` is no longer in the freshly classified title set: deletes it if still `draft` (stale-draft sweep); leaves it untouched if `active` (human-approved) — auto-flagging an active skill when its source procedure vanishes is deliberately **deferred**, pending classification-stability gating, so a single reclassification can't silently invalidate reviewed content. The `needs_rereview` flag (set when a re-ingest tries to overwrite an active skill's backing document) clears via the existing `PATCH /skills/{id}` route (`{"needs_rereview": false}`) — a manual human action, separate from this sweep.

**`GET /collectors`** (`cortex/app/collectors/api.py`) — per-collector status/health for the dashboard: `{collectors: [{name, enabled, last_run, pages_seen, pages_ingested, pages_skipped, errors, health}], count}`. The router is mounted only when `COLLECTORS_ENABLED=true` (same registration pattern as the knowledge router) — on a default deploy the endpoint is simply not mounted (404), it does not return a disabled-shape body. Per-collector `enabled` is `COLLECTORS_ENABLED AND <name>_COLLECTOR_ENABLED`; `health` is `ok` / `degraded` (partial per-page errors) / `error` (no PAT or run-level failure) / `unknown` (never run).

**`collection.sync` replay event:** emitted once per collector run (success or failure) via `replay.emitter.emit`, `session_id=f"collector:{name}"`, `agent_id="collector"`, payload `{seen, ingested, skipped, errors, health}` — gives every collector run a trace entry alongside normal agent activity; emit failure is swallowed and doesn't fail the run.

**Config:**

| Var | Default | Purpose |
|-----|---------|---------|
| `COLLECTORS_ENABLED` | `false` | Master switch — gates the `GET /collectors` router mount and every individual collector's enabled check |
| `CONFLUENCE_COLLECTOR_ENABLED` | `false` | Per-source switch; must be true alongside `COLLECTORS_ENABLED` for the Confluence run to do anything |
| `CONFLUENCE_BASE_URL` | `""` | Confluence Server/DC base URL |
| `CONFLUENCE_SPACE_KEYS` | `""` | Comma-separated space keys to sync; empty makes the task no-op before it even checks the enabled flags |
| `CONFLUENCE_LABEL` | `""` | Optional CQL label filter |
| `CONFLUENCE_PAT_VAULT_KEY` | `confluence_pat` | Vault key the engine reads the Personal Access Token from (fallback, used only when `CONFLUENCE_PAT` is unset) |
| `CONFLUENCE_PAT` | `""` | Direct PAT value, resolved **env-first**: a K8s Secret or `.env` value here is used as-is and Vault is skipped entirely; empty (default) falls back to `CONFLUENCE_PAT_VAULT_KEY`/Vault, unchanged |
| `CONFLUENCE_COLLECTOR_SCHEDULE_HOURS` | `24` | Celery beat interval for `run_confluence_collector` |
| `COLLECTOR_LOCK_TTL_SECONDS` | `3600` | Redis `SETNX` lock TTL serializing collector runs |

**Ops notes:** Opt-in and disabled-by-default — `COLLECTORS_ENABLED=false` and `CONFLUENCE_COLLECTOR_ENABLED=false` out of the box in `docker-compose.yml`. The `confluence-collector` Celery beat entry is registered **unconditionally** (it fires every `CONFLUENCE_COLLECTOR_SCHEDULE_HOURS` regardless of the flags), but `run_confluence_collector` no-ops before opening any external connection: first it checks `CONFLUENCE_SPACE_KEYS` (empty → `{"status": "disabled", "reason": "no space keys"}`, avoiding a malformed CQL query), then `CollectorEngine.run()`'s own enabled gate (`COLLECTORS_ENABLED and CONFLUENCE_COLLECTOR_ENABLED` → `{"status": "disabled"}`) — so a default deploy schedules the task on every tick but it does nothing until both flags are set and space keys are configured. The Confluence PAT resolves env-first: set `CONFLUENCE_PAT` (a K8s Secret in the office deployment, or `.env` on the personal VPS) to skip Vault entirely, or leave it empty and provision the token via `vault_store(key="confluence_pat", value="<PAT>")` (or whatever `CONFLUENCE_PAT_VAULT_KEY` is set to) as before; the engine fails the run cleanly (`health="error"`, nothing ingested) if neither source yields a token. `markdownify` is a `requirements.txt` dependency (SP3; swapped from the GPL-licensed `html2text` for commercial-readiness — see audit blocker 1) — an existing deployment must rebuild the cortex image (`update.sh` / `docker compose build`) before enabling the Confluence collector, or every page conversion raises `RuntimeError: markdownify not installed`.

### Runtime Policy Engine (`cortex/app/policy/`)
Compound policy evaluation for pre-edit safety checks. The policy engine runs on Cortex and is consulted by the pre-edit hook core (`firekeep_client.hooks.pre_tool`) before file edits.

**Rules:** `LeaseRule` (no-op, lease checked by hook), `FileRiskRule` (file hotspot patterns), `SessionHealthRule` (session failure rate), `PathDenyRule` (configurable deny globs), `RecentFailureRule` (recent file failure history).

**REST Endpoints (on Cortex :8100):** `POST /policy/evaluate` (body: `{file_path, agent_id, session_id}`), `GET /policy/rules`, `POST /policy/rules/{name}/toggle`, `GET /policy/decisions?limit=50&action=&agent_id=` (audit log — records `block`/`rethink` decisions only, written by the Agent Gateway, stored in Redis key `policy:decisions`, surfaced in the dashboard Policy tab)

**Operations Endpoints (on Cortex :8100):** `GET /ops/workers`, `GET /ops/queues` — Celery worker status + Redis queue depths (incl. `memory_backfill` stream + `memory_backfill_dlq`, and the bridge `distill_dlq`), served by the `cortex/app/ops.py` router and consumed by the dashboard Operations tab. Auth-gated when `AUTH_ENABLED=true` (not on the validator's skip list); open by default. `POST /ops/dlq/requeue?limit=1000` — requeue `memory:backfill:dlq` records onto the backfill stream with `attempts=0` (entries dead-lettered while the embedding backend was down have no automatic path back; the 60s drain then re-embeds them). `require_scope("admin")`; surfaced as a Requeue button on the dashboard Operations tab's `memory_backfill_dlq` row. For deployments predating the endpoint, `cortex/scripts/requeue_backfill_dlq.py` is the same logic runnable via `kubectl/docker exec -i ... python -` over stdin. `POST /ops/dlq/retry-events?limit=1000` — admin-scoped retry for the sleep-cycle event DLQ (`{REDIS_STREAM_KEY}:dlq` → rpop oldest-first → lpush back onto the event queue; its former sibling `/dashboard/api/dlq/retry` is no longer key-free — see the Auth section's dashboard note below). Note: `collect_queue_depths` reads event_stream/event_dlq from the data DB (`REDIS_URL`) — they were read from the Celery broker DB before 2026-07-16 and always showed 0. `POST /ops/distill-dlq/requeue?limit=1000` **(on Bridge :8070)** — admin-scoped requeue for `nb:distill:dlq` via `enqueue_distillation` with attempts reset; sessions whose keys hit the post-DLQ 7d TTL are dropped-with-log (counted `expired_dropped`, deliberately not restored). Every DLQ row in the dashboard Operations tab now has an action button (`QUEUE_ACTIONS` map); all requeue loops follow the guarded-restore invariant (popped record restored on write failure, logged IN FULL at CRITICAL on double failure). Note: the startup log line for an empty `LLM_API_KEY` is INFO, not WARNING — an empty key is the normal state for Ollama deployments.

**Config:** `POLICY_DENY_PATHS=.env,*.key,*.pem,*.secret` (comma-separated glob patterns, configurable via env var)

**Hook integration:** the `pre_tool` hook core (`firekeep_client.hooks.pre_tool`) calls `POST /agent/action/before` (the policy engine still runs, gated through the Agent Gateway; `/policy/evaluate` remains as a deprecated alias) after the lease check. Block exits with reason, warn emits message but allows, allow proceeds silently. Falls through to allow if Cortex is unreachable.

### Agent Gateway (`cortex/app/agent_gateway/`)
Predict-then-act surface for any agent runtime. Wraps consequential actions in a `predict → policy → execute → reconcile` flow with `allow | rethink | block` decisions.

**MCP Tools:** `action_before`, `action_after`
**REST Endpoints (on Cortex :8100):** `POST /agent/action/before`, `POST /agent/action/after`
**Legacy:** `POST /policy/evaluate` remains as a deprecated alias.
**Adapters:** Claude Code via paired Pre+Post hooks; MCP-capable agents (Codex/Kiro/Cursor) via the two new MCP tools; custom Python via two HTTP calls.
**Config:** `AGENT_GATEWAY_ENABLED`, `AGENT_PREDICTION_CONFIDENCE_THRESHOLD`, `AGENT_RECONCILE_DEADLINE_SECONDS`, `AGENT_RETHINK_MAX_LOOPS`, `AGENT_FASTPATH_MIN_SAMPLES`, `AGENT_FASTPATH_MIN_SUCCESS_RATE`, `AGENT_FASTPATH_CACHE_TTL_SECONDS`.

See `docs/superpowers/specs/2026-05-25-agent-gateway-predict-then-act-design.md`.

### FirekeepDecision (Decision Board) — SP4
Path B: a LOCAL, per-user clarification board — not a team-visible shared surface (contrast with Relay's task/bulletin board). Served by the client kit's own stdio MCP server, `firekeep-decision` (`client/firekeep_client/decision/server.py`), not cortex-mcp.

> When a clarification needs more than a couple of questions, call `decision_board(context, draft_questions)` instead of asking the questions inline.

**Trigger wiring (client 0.1.11):** that instruction only fires if it reaches the runtime's instruction layer — a tool description alone never triggers proactive use (verified 2026-07-14: the sentence lived only in this repo's CLAUDE.md, so no other project/runtime ever opened a board). `firekeep install` now renders it everywhere: the claude adapter upserts a **marker-delimited block** into the user's global `~/.claude/CLAUDE.md` (`adapters/base.py` `INSTRUCTIONS_BEGIN/END` + `upsert_marked_block`/`strip_marked_block` — only text between the markers is ever touched, user content survives byte-for-byte, `unrender` strips the block); the kiro adapter writes the firekeep-owned steering doc `~/.kiro/steering/firekeep-instructions.md` (whole-file, `STEERING_MARKER`-guarded like the `/personal` command file; deliberately NOT `firekeep.md`, which pre-kit machines carry hand-written). Shared content: `adapters/base.py` `FIREKEEP_INSTRUCTIONS` = `DECISION_INSTRUCTIONS` + `KNOWLEDGE_INGEST_INSTRUCTIONS` (the client-side knowledge-ingest flow — `corpus_ingest` the doc, then `skill_create` one skill per procedure the agent identifies, `status="draft"` for review; makes document→corpus+skills work with no server generation model). Codex has no global instruction surface wired — known gap.

**MCP Tools** (on the LOCAL `firekeep-decision` stdio server):
- `decision_board(context, draft_questions=[])` — asks Cortex to synthesize a board, opens it in the human's browser (loopback server on an ephemeral port; on macOS the opener is `/usr/bin/open` — LaunchServices directly, because `webbrowser`'s osascript carrier is TCC-fragile under app-spawned MCP processes and its `False` return used to be silently ignored, the 2026-07-18 "board does not launch" field report), and long-polls up to `DECISION_POLL_SECONDS` for the human's answers. Answered in time → returns the rendered answers (markdown). Not yet → returns `{status: "pending", board_id, board_url, next}` — `board_url` is always included so a human can open the board manually (a failed auto-open adds a `note` saying exactly that, and is hooklogged). The agent contract (tool docstrings + the rendered instruction layer) is to WAIT: loop `decision_board_check(board_id)` until answered; a board is dead only when a check returns `status: "unknown"`.
- `decision_board_check(board_id)` — resumes the bounded poll for that board. Answered → answers (board shuts down); still waiting → `pending` (same `next` hint); unknown/expired id → `{status: "unknown"}`.

**REST Endpoints (on Cortex :8100):** `POST /decision/synthesize` — body `{context (min 1 char), draft_questions: string[] = [], agent_id: string = "unknown"}` → `{questions: [{id, text, knowledge_found, evidence, suggested_answers, suggested_actions}], generated_at, degraded, note, board_id}` (`board_id` minted server-side, `uuid.uuid4().hex`). Router mounted in `main.py` only when `DECISION_ENABLED`. Module: `cortex/app/decision/` (`synthesize.py` → `synthesize_board`, `api.py` → `create_decision_router`).

**Behavior (retrieval-first + bounded best-effort LLM):** For the overall context and each draft question, runs a GLOBAL memory recall (`project=None` — spans all teammates' accumulated knowledge, not one project). `evidence` is deterministic (snippet + ref drawn straight from the surviving vector sources); `knowledge_found` is true when ≥1 vector source survives the recall score threshold. `suggested_answers`/`suggested_actions` come from a best-effort LLM pass wrapped in a hard timeout — on timeout/exception the board still returns with `degraded: true` and empty suggestions, since retrieval/evidence are never blocked by the LLM. Suggested actions are advisory/UNVERIFIED.

**Headless fallback:** when `FIREKEEP_DECISION_HEADLESS` is truthy, OR no usable browser is registered, OR (Linux only) neither `DISPLAY` nor `WAYLAND_DISPLAY` is set, the board spec is returned as inline text — no local server is started. macOS with no `DISPLAY` is NOT headless (it has a browser).

`firekeep-decision` is registered ALWAYS-ON (no opt-in flag, like `firekeep-symdex` — both are always-on client stdio servers) — console-script `firekeep-decision = firekeep_client.decision.server:main`, installed by `firekeep install` alongside the other MCP servers.

**Config:** Cortex — `DECISION_ENABLED=true` (gates router registration), `DECISION_SYNTH_TIMEOUT_SECONDS=20.0` (bounds the best-effort suggestion LLM pass), `DECISION_MAX_QUESTIONS=8` (hard cap on per-question recalls). Client (`firekeep-decision`, all optional) — `DECISION_POLL_SECONDS=24.0` (bounded long-poll ceiling per call), `DECISION_BOARD_TTL_SECONDS=1800.0` (abandoned-board reaper), `DECISION_SYNTH_TIMEOUT_SECONDS=30.0` (client's own Cortex-call timeout — same var name as the Cortex setting above but a DISTINCT, separately-read value in a different process), `DECISION_INGEST_CLIENT_TIMEOUT_SECONDS` (HTTP timeout for the Cortex synthesize call, auto-clamped strictly above the client synth timeout so it can't be misconfigured below it), `FIREKEEP_DECISION_HEADLESS` (force headless / CI opt-out).

### Agent Guidance: Secrets vs Operational Facts
- **Non-secret operational facts** (VPS IP, service URLs, hostnames, port mappings): Store in Cortex memory with `namespace="infrastructure"` via `memory_learn`.
- **Actual secrets** (passwords, API tokens, SSH keys, connection strings): Store in the vault via `vault_store`. Never store secrets in plain-text memory.

## Tech Stack

- Python 3.11+, FastAPI, FastMCP, Pydantic Settings
- Neo4j, Qdrant, Redis, Ollama
- Docker Compose (13 containers)
- tree-sitter (Symdex)

## Local Testing (v2 features)
```bash
docker compose -f docker-compose.test.yml up -d   # Redis only
python -m pytest replay/tests/ auth/tests/ vault/tests/ corpus/tests/ -v
docker compose -f docker-compose.test.yml down
```

## Agent Working Guidelines

### Change Consistency Checklist
When adding, removing, or renaming MCP tools, REST endpoints, env vars, or config settings, update ALL of these files:
- `cortex/app/mcp_server.py` — MCP tool definitions
- `cortex/app/main.py` — lifespan wiring and router registration
- Relevant `api.py` (e.g., `corpus/api.py`, `vault/api.py`) — REST routes
- `docker-compose.yml` — env vars
- `client/firekeep_client/adapters/*` + `client/firekeep_client/cli.py` — native-config render + installer next-steps output
- `dashboard/index.html` — UI references
- `CLAUDE.md` — documentation

This is non-negotiable. Stale references in setup scripts or dashboard after a tool removal are bugs.

### Use Teams for Multi-File Work
For tasks that span 3+ files or have parallelizable work streams, use Claude Code teams (`TeamCreate`). Typical split:
- **Core agent**: Implementation + tests (the critical path)
- **UI agent**: Dashboard changes (blocked by core)
- **Docs/scripts agent**: CLAUDE.md, setup scripts, docker-compose (can often run in parallel)

After all agents complete, the team lead runs the full test suite and does a manual read-through of critical-path files before calling it done. Don't trust agent summaries alone — verify the wiring.

## Design Spec

See `docs/DESIGN.md` for the full architecture design spec.
See `docs/superpowers/plans/2026-03-18-firekeep-v2-master.md` for the v2 design spec (reviewed).
See `docs/superpowers/plans/` for all implementation plans.
