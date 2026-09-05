# Firekeep

Unified cognitive stack for AI agents. Consolidates four server services (Cortex, Bridge, Sentinel, Relay) plus a dashboard into one deployable unit; the dexes — the domain indexes the Keep understands (Symdex for code, Docdex for documents) — ship client-side in the kit behind a registry ([`docs/guides/dexes.md`](docs/guides/dexes.md)). Firekeep Studio is a separate optional Electron desktop client under `studio/`, not a server container or a Client Kit replacement.

## Architecture

| Service | Directory | Port | Purpose |
|---------|-----------|------|---------|
| FirekeepCortex | `cortex/` | 8100 (API), 8080 (MCP) | Long-term memory (semantic + graph RAG) |
| FirekeepBridge | `bridge/` | 8070 | Session context persistence across compressions |
| FirekeepSentinel | `sentinel/` | 8060 | Environment observer (collectors + webhook intake). **Docker collector is opt-in** — `NS_DOCKER_COLLECTOR_ENABLED=false` by default, and `docker-compose.yml` no longer bind-mounts `/var/run/docker.sock` or the repo root (`./:/watch:ro`). Reaching the Docker API is root on the host: a caller can `POST /containers/create` with a host bind mount, and `:ro` restricts the socket *file*, not the API. The old repo-root mount also put `.env` — `NEO4J_PASSWORD`, `VAULT_KEY`, minted API keys — inside a service with a published, unauthenticated port. Neither mount did anything by default (the collector makes one call, `GET /containers/json`; git/file watches come from Redis + `NS_WATCH_PATHS`, both empty). To opt in, set the flag and restore the mount per the comments in the compose `sentinel:` block — preferably behind a read-only socket proxy. Guarded by `sentinel/tests/test_docker_collector_optin.py`. |
| FirekeepRelay | `relay/` | 8050 | Agent-to-agent communication (pub/sub + bulletin board) |
| FirekeepSymdex | `symdex/` | stdio (local, client-installed) | Code intelligence (tree-sitter AST parsing). **CLIENT-SIDE ONLY** — it must be local to the codebase it indexes, so it ships as the standalone `firekeep-symdex` stdio MCP server (`firekeep_symdex.server:main`). The wheel is still **always installed** — bundled and checksum-verified by the bootstrap, or from the local `symdex/` dir on a checkout install — but **MOUNTING is dex-registry-driven**: the gateway starts it only when the registry has it (`firekeep dex add symdex`). Since client 1.2.0 an absent registry seeds symdex+docdex automatically (default-on); an existing dexes.json is never touched, so removals stick. `firekeep-decision` is NOT a dex (it indexes nothing) and stays core and unconditional; the old `firekeep install --with-symdex` flag is retired. See [`docs/guides/dexes.md`](docs/guides/dexes.md). The server-side HTTP container was removed from both `docker-compose.yml` and `docker-compose.office.yml` (a VPS/K8s box has no developer working tree to index — it was vestigial). 8 analytics tools (`get_evolution_timeline`, `get_code_churn`, `get_contributors`, `get_change_summary`, `detect_patterns`, `get_complexity_metrics`, `get_hotspots`, `compare_repos`) require indexed repos and are hidden by default (`SYMDEX_ANALYTICS_ENABLED=false`). Per-index file ceiling via `FIREKEEP_SYMDEX_MAX_FILES` (default 1500). `list_repos` exposes indexed-repo inventory. (Sentinel's git collector still best-effort POSTs to `SYMDEX_URL` on commit activity; with no server symdex it fails fast into a swallowed debug log — harmless, and the seam a team would reuse if it ever re-adds a server symdex.) |
| FirekeepDocdex | `docdex/` | none (client-side ingest client) | Documents dex. **CLIENT-SIDE ONLY, and NO MCP server** — manifest `kind: ingest-client`, so the gateway mounts nothing for it; the registry entry gates its session-start background sync and the doctor row instead. A human registers folders (`firekeep docdex add ~/Notes`, `--shared` for the workspace, default private to the member); a sync extracts `.md`/`.txt`/`.pdf`/`.docx`/`.html`/`.eml`/conversation-shaped `.json` (no OCR; role-labeled turns, not typed provenance) and ingests into the EXISTING corpus — no new server component. Ships as the `firekeep-docdex` wheel, bundled and checksum-verified exactly like symdex; registered by default since 1.2.0; `firekeep dex remove` is the off-switch. Disclosed caps, deletion semantics (a completed walk is the only source of deletions), the member-private threat boundary and the per-runtime sync coverage are all in [`docs/guides/dexes.md`](docs/guides/dexes.md). |
| FirekeepMaildex | `maildex/` | none (client-side ingest client) | Email dex, registry consumer #3. **CLIENT-SIDE, NO MCP server, pure stdlib.** IMAP read-only (every open is `EXAMINE`, every fetch `PEEK`; no mutating verb or SMTP exists in the wheel), app password member-owned in the server vault (`maildex.<id>` — never on client disk), always member-private, 90-day backfill then incremental on the docdex chassis. Round-1 gap disclosed everywhere: provider-side deletions are not mirrored until `remove`/re-`add`. See [`docs/guides/dexes.md`](docs/guides/dexes.md). |
| FirekeepHands | `hands/` | none (client-side MCP server, opt-in) | Desktop operator. **CLIENT-SIDE, OPT-IN, NEVER BUNDLED and NEVER SEEDED** — `firekeep hands enable --from <checkout>/hands` installs the `firekeep-hands` wheel into the kit venv and registers it as a `role: capability` entry the gateway mounts like any `mcp-stdio` dex (a PyPI install is refused until the name is published — squat guard `HANDS_PYPI_PUBLISHED`). A separate approval broker (`firekeep-hands-broker`, per-user logon `Run` value / LaunchAgent) is the only thing that can approve a protected step, and only from real, non-injected keyboard input — or a dashboard tap, which is **off by default** because relay records no completing principal. See [`docs/guides/hands.md`](docs/guides/hands.md). |
| Dashboard | `dashboard/` | 8040 | Unified web UI (static SPA) |
| Firekeep Studio | `studio/` | none (local desktop) | Runtime-neutral primary/reviewer console and Mission harness over Codex App Server, Claude stream JSON, Kiro ACP, and the Grok Responses API; one persisted workspace is passed to every runtime, live provider models/reasoning, cache-aware usage, sanitized Mermaid diagrams, and native Decision Boards are surfaced locally, while the Python Client Kit remains the Keep and dashboard connection source. |

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

### Install (firekeep.ai is the single source)
`https://firekeep.ai/docs.html` is the install documentation. In-repo copies are
links, not walkthroughs — the one command it hands a user is:
```bash
curl -fsSL https://firekeep.ai/latest/install | sh      # macOS / Linux
irm https://firekeep.ai/latest/install.ps1 | iex           # Windows
```
It asks two things: agent identity, then where the server is (set one up here /
join code / already running / not yet). "Set one up here" runs `firekeep init`,
which provisions the server and self-enrols the machine, so `firekeep doctor` is
green with no dashboard or pasted key.

### Deploy the server from this checkout
```bash
bash install.sh          # build from source
bash install.sh --pull   # published images
bash update.sh           # update an existing deployment
```
`install.sh` prompts for nothing — host address detected, Neo4j password
generated (`--ip` / `--neo4j-password`, `FIREKEEP_VPS_IP` /
`FIREKEEP_NEO4J_PASSWORD` override). It returns before the ~3.3GB model pull
finishes: until then memory writes return `status="partial"` and `firekeep
doctor` shows an `embeddings` WARN. `bash install.sh --wait-for-models` blocks.

### Install the client kit on a workstation
The kit installs to `~/.firekeep` (standalone CPython + config, `0600`) and renders the
adapters for every runtime — Claude Code, Codex, kiro, OpenCode, Claude Desktop (the consumer
app: auto-mounted into `claude_desktop_config.json` when its config dir exists — the generic
tier with the friction removed, no hooks), ChatGPT (server-side: OpenAI Secure MCP Tunnel →
`firekeep gateway --runtime chatgpt` under `FIREKEEP_TOOLSET=chat`, a curated 12-tool surface
enforced at the gateway's routing layer — see `deploy/chatgpt-tunnel/` and
[`docs/guides/client-kit.md`](docs/guides/client-kit.md) "Gateway toolsets"), plus a `generic` "any MCP
client" tier (`--runtime generic --agents-md <path>`, or one skippable wizard question) that
prints a paste-in gateway snippet and delivers the MCP tools + on-connect instructions but no
hook-driven lifecycle (no auto-briefing / pre-edit-block / stop→learn / checkpoint / presence).
Teammates on a bare machine use the one-liner above.
From a checkout: `cd client && ./install` (`.\install.ps1` on Windows), then
`firekeep install` to re-render adapters only. `firekeep update` re-execs the bootstrap,
which installs each release side-by-side at `~/.firekeep/venvs/<version>` and flips the
`~/.firekeep/current` link (NTFS junction / POSIX symlink) that every rendered surface
routes through — so since client 0.1.35 updates land without closing any agent session;
running sessions keep their old venv until GC proves nothing holds it.

Everything else about the kit — the five hook cores, night shift, personal mode, symdex
auto-index, PATH handling, release signing — is in
[`docs/guides/client-kit.md`](docs/guides/client-kit.md); the dex registry
(`firekeep dex list/add/remove`, the grandfathering rule) and docdex
(`firekeep docdex add <folder>`) are in [`docs/guides/dexes.md`](docs/guides/dexes.md).
`firekeep hands enable|disable|status|allow|chord|config|evidence` is the opt-in
desktop operator — the registry's first `role: capability` entry, never seeded and
never bundled, with a `hands` doctor row that warns when nothing can approve; see
[`docs/guides/hands.md`](docs/guides/hands.md).

A separate, ongoing field-failure reporting channel exists alongside `doctor --report`
(`client/firekeep_client/report.py`): consent is tri-state (unset = not enrolled, never
mirrors autoupdate's on-by-default), and an enrolled machine sends only closed-enum
category codes on install/connectivity/runtime failures, never paths or messages. See
[`docs/guides/client-kit.md`](docs/guides/client-kit.md) "Field failure reporting".
Doctor and the session-start briefing also now surface a Keep running behind the
latest published server release — cortex's version compared against
`server/latest/server.json`, silenced per-version via
`[dist] server_update_ack = vX.Y.Z` — see
[`docs/guides/client-kit.md`](docs/guides/client-kit.md) "Server update visibility".

Night Shift is now the drain for a **fleet job catalog** (`distill_session`, `reauthor_stale_skill`, `propose_contested_verdict`): cortex's nightly `fleet_enqueue_pass` posts the latter two through relay's `POST /tasks`, `session_start` spawns `firekeep night-shift` in the background when a local model port answers (`FIREKEEP_NO_AUTO_NIGHTSHIFT=1` to stop), every output is a draft skill or a verdict *proposal* behind human review, and an approval-rate ledger per job type shows on the dashboard's Autopilot tab — see [`docs/guides/client-kit.md`](docs/guides/client-kit.md) "Night Shift and the fleet job catalog" and [`docs/guides/knowledge-autopilot.md`](docs/guides/knowledge-autopilot.md) §8. A separate, shadow-mode skill ladder (`SKILL_LADDER_ENABLED`, default on) nightly proposes draft→trial→active promotions and trial→draft demotions on independent graded evidence without changing any `skill_status` yet — see [`docs/guides/knowledge-autopilot.md`](docs/guides/knowledge-autopilot.md) §9.

### Run Firekeep Studio from this checkout

```bash
cd studio
npm install
npm test
npm run typecheck
npm run smoke:runtimes   # installed auth/model/effort/Firekeep probes; no agent turn
npm run smoke:tasks      # opt-in real disposable missions; spends provider tokens
npm run smoke:package    # after package/dist; confirms app.asar renderer over CDP
npm start
npm run dist             # current-platform installer under studio/release/
```

Studio versioning and packaging are independent of the Python Client Kit. Runtime/UI
behavior and slash commands are documented in [`studio/README.md`](studio/README.md).

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
cd docdex && pytest tests/ -v
cd studio && npm test && npm run typecheck
```

## Agent Guidance: Secrets vs Operational Facts
- **Non-secret operational facts** (VPS IP, service URLs, hostnames, port mappings): Store in Cortex memory with `namespace="infrastructure"` via `memory_learn`.
- **Actual secrets** (passwords, API tokens, SSH keys, connection strings): Store in the vault via `vault_store`. Never store secrets in plain-text memory.
- **`namespace` is a CATEGORY, not a partition, and this advice depends on that.** A `memory_recall` that names no namespace searches every namespace inside the caller's `workspace_id` — which is the actual tenancy boundary, derived from the verified principal and unforgeable — so filing a fact under `infrastructure` makes it easier to find, never harder. Naming a namespace on a recall scopes it to exactly that one, `"default"` included. This is load-bearing: when recall scoped to the literal `"default"` while this guidance sent writes to `infrastructure`, **146 memories on the live store (129 active) were unreachable by every recall the product makes** — the advice and the retrieval have to agree, and the place they agree is here. See `cortex/CLAUDE.md`'s "Namespace is a CATEGORY, not a tenant".

## Tech Stack

- Python 3.11+, FastAPI, FastMCP, Pydantic Settings
- Neo4j, Qdrant, Redis, Ollama
- Docker Compose (13 containers)
- tree-sitter (Symdex)
- TypeScript, Electron, React (Firekeep Studio)

## Local Testing (v2 features)
```bash
docker compose -f docker-compose.test.yml up -d   # Redis only
python -m pytest replay/tests/ auth/tests/ vault/tests/ corpus/tests/ -v
docker compose -f docker-compose.test.yml down
```

## Feature guides

The detail for each subsystem — design rationale, measured numbers, config
tables and the failures that shaped it — lives in `docs/guides/`. It is kept out
of this file deliberately: this file is loaded into every session's prompt, and
reference material does not need to be.

| Area | Guide |
|---|---|
| Office Kubernetes deployment | [`docs/guides/deployment-office-kubernetes.md`](docs/guides/deployment-office-kubernetes.md) |
| The client kit — install, hooks, night shift, personal mode | [`docs/guides/client-kit.md`](docs/guides/client-kit.md) |
| Dexes — the registry, symdex, docdex | [`docs/guides/dexes.md`](docs/guides/dexes.md) |
| Hands — the desktop operator, its broker and its limits | [`docs/guides/hands.md`](docs/guides/hands.md) |
| Backup and restore — nightly snapshots, `firekeep backup`, the disaster runbook | [`docs/guides/backup-and-restore.md`](docs/guides/backup-and-restore.md) |
| Memory, recall, corpus and vault | [`docs/guides/memory-and-recall.md`](docs/guides/memory-and-recall.md) |
| Skills, docs→skills and collectors | [`docs/guides/knowledge-and-skills.md`](docs/guides/knowledge-and-skills.md) |
| Knowledge Autopilot — feedback, reaper, contested, inbox, the fleet ledger | [`docs/guides/knowledge-autopilot.md`](docs/guides/knowledge-autopilot.md) |
| LLM endpoint selection | [`docs/guides/llm-endpoint-selection.md`](docs/guides/llm-endpoint-selection.md) |
| Dreaming — consolidation and person profiles | [`docs/guides/dreaming.md`](docs/guides/dreaming.md) |
| Living Procedures — observed runbooks | [`docs/guides/living-procedures.md`](docs/guides/living-procedures.md) |
| Agent gateway, policy engine and decision board | [`docs/guides/agent-gateway-and-policy.md`](docs/guides/agent-gateway-and-policy.md) |
| Relay — leases, tasks, presence, scope, DMs | [`docs/guides/relay-coordination.md`](docs/guides/relay-coordination.md) |
| Replay, auto-evals and the pattern engine | [`docs/guides/replay-evals-patterns.md`](docs/guides/replay-evals-patterns.md) |
| Auth, scopes and build provenance | [`docs/guides/auth-and-provenance.md`](docs/guides/auth-and-provenance.md) |
| Bridge shadow residency and the briefing endpoint | [`docs/guides/bridge-context-and-briefing.md`](docs/guides/bridge-context-and-briefing.md) |

## Agent Working Guidelines

### Change Consistency Checklist
When adding, removing, or renaming MCP tools, REST endpoints, env vars, or config settings, update ALL of these files:
- `cortex/app/mcp_server.py` — MCP tool definitions
- `cortex/app/main.py` — lifespan wiring and router registration
- Relevant `api.py` (e.g., `corpus/api.py`, `vault/api.py`) — REST routes
- `docker-compose.yml` — env vars
- `docs/guides/<area>.md` — the feature guide for the area you changed (config tables and
  behaviour notes moved there; several tests assert the documented default matches the code)
- `client/firekeep_client/adapters/*` + `client/firekeep_client/cli.py` — native-config render + installer next-steps output (runtimes: claude, codex, kiro, opencode, claude-desktop, and the `generic` "any MCP client" tier; keep the per-runtime degradation columns in `client/firekeep_client/contract/matrix.py` honest)
- `dashboard/index.html` — UI references
- `studio/src/core/`, `studio/src/main/`, `studio/src/renderer/`, and `studio/README.md` — desktop runtime contracts, secure IPC, UI, and slash-command references
- `CLAUDE.md` — documentation

This is non-negotiable. Stale references in setup scripts or dashboard after a tool removal are bugs.

### Use Teams for Multi-File Work
For tasks that span 3+ files or have parallelizable work streams, use Claude Code teams (`TeamCreate`). Typical split:
- **Core agent**: Implementation + tests (the critical path)
- **UI agent**: Dashboard changes (blocked by core)
- **Docs/scripts agent**: CLAUDE.md, setup scripts, docker-compose (can often run in parallel)

After all agents complete, the team lead runs the full test suite and does a manual read-through of critical-path files before calling it done. Don't trust agent summaries alone — verify the wiring.

## Dependency locking

`<svc>/requirements.txt` holds loose ranges and is the **input**;
`<svc>/requirements.lock` is the resolved, hash-pinned **output**, and it is what
`cortex|bridge|sentinel|relay/Dockerfile` installs. Before this, two builds of the same
commit produced different images — a customer reporting against a git SHA was not
describing a knowable artifact, and the CVE gate and SBOM described whatever resolved on
the day they ran. It surfaced as three tests passing on a dev box holding `fastapi` 0.128
and failing in a clean venv resolving 0.140, both allowed by `fastapi>=0.115,<1`.

Regenerate after editing any `requirements.txt` (CI fails if they drift):

```bash
uv pip compile <svc>/requirements.txt --python-platform linux --python-version 3.11   --generate-hashes --output-file <svc>/requirements.lock
```

`--python-platform linux` matters: the lock is generated on whatever machine you are on
but must install in the pinned `python:3.11.15-slim` base (see "Image pinning" below —
a base bump is also a lock-compatibility question). Verified by building all four images and
importing each service's modules.

Hashes put pip in `--require-hashes` mode implicitly — every requirement must be pinned
and hashed, and a re-uploaded artifact fails the build rather than installing.

**`client/`, `symdex/` and `docdex/` are deliberately NOT locked.** They ship as wheels
into a user's virtualenv; pinning a library's transitive dependencies forces them on every
consumer and fights the bootstrap's own resolution. `tests/test_requirements_lock.py`
asserts they stay unlocked.

Guards in `tests/test_requirements_lock.py`: every direct requirement is present in the
lock, at a version its specifier allows; the lock is fully hash-pinned; and the
Dockerfile installs the lock rather than the `.txt`. It deliberately does **not**
regenerate-and-diff — that fails whenever any upstream publishes, which is not drift.

## Image pinning

The same job as dependency locking, one layer down: the images those locks install
*into*. Every `image:` in a compose file and every `FROM` in a Dockerfile is pinned by
**tag and digest**:

```yaml
image: redis:7.4.10-alpine@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2
```

The digest is what makes it immutable; the tag is what lets a human read the line and
know what it is. **Both, never a bare digest** — `redis@sha256:e7723ff7…` is equally
immutable and tells nobody it is 7.4.10, which is the difference between RSALv2/SSPLv1
and the old BSD terms.

Floating tags cost three things, in descending order of how much they matter for
software being sold. First, **the licence of what ships can change with no commit** —
`redis:7-alpine` is the proven case, not a hypothetical: Redis relicensed at 7.4 and
that tag carried Firekeep across the boundary on its own schedule, unnoticed. Second,
**one-way data migrations** — `neo4j:5-community` floats across 5.x minors and Neo4j
store-format upgrades are irreversible, so a customer's `docker compose pull` could
upgrade their database into a state they cannot roll back. Third, reproducibility.

### Updating a pin

```bash
docker buildx imagetools inspect redis:7.5.0-alpine
```

1. **Take the TOP-LEVEL `Digest:`** — the manifest list (OCI image index), not any of
   the per-platform digests listed underneath it under `Manifests:`. This is the single
   most likely way to get this wrong, and it fails on somebody else's machine, not
   yours: a platform manifest pins one architecture and breaks every other. Verify by
   re-inspecting what you pinned — `docker buildx imagetools inspect <ref>@<digest>`
   must report `application/vnd.oci.image.index.v1+json` (or the Docker
   `manifest.list.v2+json`) and list more than one platform. A single-platform
   manifest means you captured the wrong one.
2. **Where the tag floats on a major/minor** (`redis:7-alpine`, `nginx:alpine`,
   `ollama/ollama:latest`), first resolve what concrete version it *is* — inspect the
   candidate concrete tag and confirm its index digest matches the floating tag's, then
   pin the concrete one. Ask the artifact, not your memory: a plausible-but-wrong
   digest is worse than an obviously-wrong one.
3. **Update the tag and the digest together.** A bumped digest under a stale tag is a
   line that lies about what it runs.
4. **Re-check the licence row.** For a datastore, the licence is stated per exact
   version in `docs/THIRD-PARTY-DATASTORES.md`; a version bump can move it across a
   licence boundary (Redis 7.2→7.4 did exactly that). The pin is what keeps that file
   true between reviews — bumping one without revisiting the row is how the analysis
   goes stale, and it is the one thing pinning cannot protect you from.
5. **Rebuild and run the guard:** `pytest tests/test_image_pins.py` (CI job
   `repo-scripts`).

Constraints the guard enforces beyond "has a digest": all five Python bases
(root, cortex, bridge, sentinel, relay — `python:3.11.15-slim` today) share one digest
and one tag, and that tag stays on the 3.11 line the locks are compiled for
(`uv pip compile --python-version 3.11`); the two compose
`ollama/ollama` services share one digest (`ollama-pull` populates the model store the
`ollama` service reads); any tag appearing in more than one file resolves to the same
digest; and the datastore versions named in `docs/THIRD-PARTY-DATASTORES.md`'s summary
table are the versions actually pinned.

`${REGISTRY}` prefixes are deliberately kept in the Dockerfiles. A digest is
content-addressed, so a pull-through mirror serves the identical manifest; a mirror that
**re-pushes** rather than proxies may not carry the digest at all, which fails loudly at
build time — the right outcome, and the reason not to hard-code `docker.io` there.

## Security

- **`SECURITY.md`** (root) — disclosure policy, SLA targets, in/out-of-scope, supported
  versions. The contact address `security@firekeep.ai` is a live, monitored mailbox
  (confirmed 2026-08-09; `sales@firekeep.ai` exists too). Remaining before-first-sale
  items are tracked in the file itself.
- **`docs/THREAT-MODEL.md`** (2026-07-26) — all four services, the dashboard, the client
  kit and the URL crawler. Supersedes `cortex/docs/SECURITY_REVIEW.md`, which covers
  Cortex v0.1.0 as of 2026-03-02 and predates auth, the vault, the agent gateway and the
  crawler; that file is kept as a record of what was reviewed then, not as current state.
  Findings marked **OPEN** in the threat model are not mitigated — the largest remaining
  is memory poisoning by a compromised agent holding a valid non-admin key. The client
  update path is signed as of 2026-08-12: keys minted (ID 7D6D83D1240D4A61), the public
  key pinned in `client/firekeep_client/signing.py`, `FIREKEEP_SIGNING_KEY` set in
  Actions — releases from client 0.1.42 on publish a verified `SHA256SUMS.minisig`.
  Residuals stay honest: TOFU first install, `require_signed` still default-false (flip
  planned one release after signing proves itself in production), unsigned `latest.json`
  downgrade window — see `docs/THREAT-MODEL.md` §5.6 and `docs/RELEASE-SIGNING.md`.
- **CI gates** (`.github/workflows/ci.yml`): `security` runs `pip-audit --strict` over
  each shipped dependency set in its own clean venv and uploads CycloneDX SBOMs;
  `secrets` runs the gitleaks binary over the working tree and full history. Both are
  blocking. The CVE gate starts from zero — test frameworks were removed from
  `cortex/requirements.txt` and `bridge/requirements.txt`, where they had been shipping
  inside the production images and were the only CVEs in the shipped set.
  `secrets` uses the gitleaks BINARY, not `gitleaks/gitleaks-action@v2`, which requires a
  paid licence for GitHub organization accounts.

## Design Spec

See `docs/ROADMAP.md` for the two published roadmap promises (linked instances,
domain profiles) and the profiles-not-clients decision record behind rung 06.
See `docs/DESIGN.md` for the full architecture design spec.
See `docs/superpowers/plans/2026-03-18-firekeep-v2-master.md` for the v2 design spec (reviewed).
See `docs/superpowers/plans/` for all implementation plans.
