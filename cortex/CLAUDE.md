# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FirekeepCortex is a domain-agnostic Memory-as-a-Service (MaaS) layer that provides persistent cognitive memory for LLM agents. It tracks procedural memory, declarative memory, and mistake resolution across arbitrary software projects and automated workflows.

## Tech Stack

- **API**: FastAPI (Python 3.11+)
- **Graph DB**: Neo4j 5.x (knowledge graph)
- **Vector DB**: Qdrant (semantic embeddings)
- **Queue/Cache**: Redis 7+ (event stream)
- **Background Worker**: Celery (Sleep Cycle consolidation)
- **LLM**: OpenAI-compatible API (Ollama/vLLM/OpenAI)
- **Embeddings**: mxbai-embed-large (1024-dim, configurable via `EMBEDDING_MODEL`/`EMBEDDING_DIM`; embed input is capped and shrink-to-fit at `EMBED_MAX_CHARS=2000` so long memories still embed)

## Architecture

```
app/
├── config.py          # Pydantic BaseSettings, env var loading
├── llm.py             # Chat endpoint selection (ollama native /api/chat vs /v1), body build + response normalise, optional JSON-schema structured outputs
├── models.py          # Request/response Pydantic models
├── exceptions.py      # FirekeepCortexError hierarchy
├── main.py            # FastAPI app, lifespan, routes, DI, router integration
├── mcp_server.py      # MCP server (Streamable HTTP; ~27 tools — 4 core memory + replay/eval/vault/corpus/knowledge/skills/agent-gateway feature tools)
├── dashboard.py       # Web dashboard router (memory browser, graph viz, DLQ)
├── webhooks.py        # Webhook registration and event firing
├── stats.py           # Memory statistics endpoint
├── transfer.py        # Export/import API (JSONL streaming)
├── streaming.py       # SSE streaming recall endpoint
├── embedding_admin.py # Embedding model admin (status, re-embed)
├── lifecycle.py       # Knowledge lifecycle (deprecate, confirm, history, backlinks)
├── contradiction.py   # Automatic contradiction detection & supersession
├── static/
│   └── dashboard.html # Self-contained dashboard SPA (zero dependencies)
├── db/
│   ├── graph.py       # Neo4j async client, Cypher queries
│   └── vector.py      # Qdrant async client, embedding + search
├── engine/
│   └── rag.py         # Dual-retrieval RAG engine (vector + graph merge)
├── workers/
│   ├── sleep_cycle.py # Celery worker: Redis → LLM extraction → Neo4j
│   ├── gc.py          # Celery task: memory expiry & garbage collection
│   ├── reembed.py     # Celery task: re-embed all vectors with new model
│   └── memory_agent.py # Celery task: autonomous knowledge custodian (5 passes)
├── collectors/        # Scheduled external-source collectors (SP3 — Living Knowledge Sync)
│   ├── base.py         # SourceAdapter protocol + SourceItem
│   ├── engine.py        # CollectorEngine — source-agnostic run orchestration
│   ├── state.py         # Redis-backed version map + run record (CollectorState)
│   ├── confluence.py     # Confluence Server/DC adapter + run_confluence_collector Celery task
│   └── api.py           # GET /collectors status endpoint
└── dreams/            # Automated memory consolidation + person profiles (round 1 — additive only)
    ├── select.py        # Pure candidate selection + partitioning + clustering, no I/O
    ├── synthesize.py     # The one LLM call for cluster insights — via app/llm.py's chat() (native /api/chat where available, so think:false is actually honoured), JSON mode
    ├── store.py          # Dedicated write path — raw PointStruct, deterministic IDs, never /memory/learn
    ├── profile.py        # Person-profile assembly, keyed by member_id — its one LLM call also goes via app/llm.py's chat(), but json_mode=False (a profile is prose, not JSON)
    ├── state.py          # Redis run-record + progress counters (DreamState, sync client — see task.py)
    ├── task.py           # Celery task: gate → lock → one unit of work → record
    └── api.py            # GET /dreams status endpoint
```
(Module map above omits some pre-existing packages, e.g. `knowledge/`, `skills/`, `policy/`, `agent_gateway/`, `briefing/`, `decision/` (SP4 — `synthesize.py`'s `synthesize_board` + `api.py`'s `create_decision_router`, backing `POST /decision/synthesize`) — see the API Endpoints list below and the root `CLAUDE.md` for those. Two more SP3 additions live under those existing packages: `app/knowledge/ingest_core.py` (`ingest_knowledge_document` — shared corpus-ingest + classify-enqueue core used by both `POST /knowledge/ingest` and the collectors) and `app/skills/reconcile.py` (`reconcile_source_skills` — stale-draft skill sweep on reclassification). `app/knowledge/crawler.py` (`crawl` + `is_safe_url`) is the SSRF-guarded crawler backing `POST /knowledge/ingest-url`. See the root `CLAUDE.md`'s Collectors and Docs→Skills subsections for the full picture. Dreaming's person-profile section also lives partly outside `app/dreams/`: `app/briefing/sections.py::profile_section` is the `GET /briefing` reader. See the root `CLAUDE.md`'s Dreaming subsection for the full picture.)

### API Endpoints
- `POST /memory/recall` — Dual-retrieval RAG query, returns Markdown for LLM injection
- `POST /memory/recall/stream` — SSE streaming recall (progressive results). Honors `project` scoping and the `RECALL_SCORE_FLOOR` score threshold on the vector search leg, and skips description-less graph nodes, mirroring the non-streaming `/memory/recall` path.
- `POST /memory/learn` — Logs actions/outcomes/resolutions to both graph + vector (parallel writes)
- `POST /memory/stream` — High-volume event ingest → Redis queue (pipeline batching)
- `POST /memory/feedback` — Submit feedback on memory usefulness
- `GET /memory/stats` — Memory statistics (counts, domains, tags, DLQ depth)
- `GET /memory/export` — Export all memories as JSONL stream. **`require_scope("admin")`** — it streams every memory in the deployment in one request, so `memory:read` (held by every agent key) is not a sufficient gate.
- `POST /memory/import` — Bulk import memories from JSONL. **`require_scope("admin")`** — besides memories it writes arbitrary Neo4j structure (`label` and `rel_type` pass through to `merge_knowledge_nodes` verbatim), so it can author graph labels and relationship types no other route exposes. Both gates added 2026-08-02: `create_transfer_router` previously declared no dependency and `main.py` registered it without `dependencies=`, leaving both routes open to any caller that could reach the port. No production caller exists — these are operator backup/migration functions.
- `GET /health` — Service connectivity status with version, uptime, memory count
- `GET /version` — Build provenance `{version, git_sha, build_time}`. Unauthenticated, no backend probes. Values injected at image build via Dockerfile ARGs (`GIT_SHA`/`BUILD_TIME`/`APP_VERSION`) and `docker-compose` build args; `update.sh`/`install.sh` export them from `git rev-parse`. Single source of truth: `app/version.py` (the old hardcoded `0.6.0` in `main.py`/`models.py` is gone — only the fallback remains).
- `GET /dashboard/` — Web dashboard (memory browser, graph visualization, DLQ manager). Only the HTML shell (`GET /dashboard` / `/dashboard/`) is auth-exempt (`app/main.py`'s `AUTH_SKIP_EXACT_PATHS`, an exact match, not a prefix); every `/dashboard/api/*` route requires `X-API-Key` when `AUTH_ENABLED=true`, same as any other REST route. This dashboard's own JS (`app/static/dashboard.html`) has no key of its own, so its data tabs go dark under `AUTH_ENABLED=true` — that's expected, not a bug; the nginx-fronted unified dashboard (`dashboard/index.html`, port 8040) is the supported UI under auth, since nginx injects `DASHBOARD_API_KEY` on its proxied calls.
- `POST /webhooks/` — Register webhook callbacks for memory events
- `GET /admin/embeddings/status` — Embedding model info and cache stats
- `POST /admin/embeddings/reembed` — Trigger re-embedding with progress tracking
- `GET /admin/untagged-calls?days=N` — Count of `/memory/recall` and `/memory/learn` calls (last N days, max 30) that arrived without `X-Session-Id`. Surfaced by the `session_start` hook core (via `GET /briefing`) as a discipline reminder; reflects `mcp_server._resolve_identity`'s resolution order (explicit param > per-connection header > `"unknown"`).
- `POST /memory/deprecate` — Change memory status (deprecated, superseded, archived)
- `POST /memory/restore` — Restore one or more archived memories to their pre-archive status (or active for legacy archives); requires `memory:write`
- `GET /dashboard/api/memory-gc` — Archive/purge settings plus recent memory-maintenance audit entries; requires `memory:read`
- `POST /dashboard/api/memory-gc/preview?limit=50` — No-write preview of the next archive/purge candidates; requires `memory:read`
- `GET /ops/workers` — Celery worker status via inspect (served by `app/ops.py` router; consumed by dashboard Operations tab)
- `GET /ops/queues` — Redis queue depths `{queues:{celery, event_stream, event_dlq, memory_backfill, memory_backfill_dlq, distill_dlq}}` (served by `app/ops.py` router; backfill depths read from the data DB `REDIS_URL`, distill DLQ from bridge DB 3)
- `POST /ops/dlq/requeue?limit=1000` — Requeue `memory:backfill:dlq` records onto the `memory:backfill` stream with `attempts=0` for re-embedding (`app/workers/backfill.py::requeue_dlq`; dead-lettered entries otherwise have no path back). Backfill DLQ only — event_dlq: `POST /ops/dlq/retry-events` below; distill_dlq: Bridge `POST /ops/distill-dlq/requeue`. `require_scope("admin")`; dashboard Operations tab shows an action button on every DLQ row (`QUEUE_ACTIONS` map). One-off equivalent for deployments without the endpoint: `cortex/scripts/requeue_backfill_dlq.py` (pipe over stdin into a cortex container)
- `POST /ops/dlq/retry-events?limit=1000` — Retry dead-lettered sleep-cycle event batches (`app/ops.py::retry_event_dlq`: rpop oldest-first from `{REDIS_STREAM_KEY}:dlq` → lpush onto `{REDIS_STREAM_KEY}`, both on the data DB `REDIS_URL`). `require_scope("admin")`. `POST /dashboard/api/dlq/retry` used to be a documented key-free exception "for the embedded SPA" — reversed 2026-07-26 (see the note on that route in `app/ops.py`): it is now auth-gated like every other `/dashboard/api/*` route. Note: `collect_queue_depths` reads event depths from the data DB (fixed 2026-07-16 — the broker-DB read always returned 0 and hid the row)
- `GET /policy/decisions?limit=50&action=&agent_id=` — Recent policy decision audit log. Only `block`/`rethink` outcomes are recorded (allows are skipped to avoid evicting the interesting records under the 500-entry cap). Returns `{decisions, summary}`. Auth-gated when `AUTH_ENABLED=true` (not skip-listed, same as `/ops/*`); open by default. Written by `AgentGatewayService.decide()`; stored in Redis key `policy:decisions` (see `app/policy/store.py`).
- `GET /briefing?agent_id=&goal=&project=` — Server-side pre-flight aggregator (11 sections: 7 in-process + 4 outbound fan-in to Sentinel/Relay/Bridge), gated by `require_scope("session:read")` at the aggregator level (sub-sections don't re-check their own scope). Always returns HTTP 200 while the briefing host is up; per-section failures set `status: unavailable` and `degraded: true` on the envelope rather than failing the whole request. `vault` section is populated only for `admin`/`*` scoped callers. Replaces the retired `briefing.sh` bash assembly; the `session_start` hook core is a thin fetch of this endpoint. Router: `app/briefing/`.
- `POST /memory/confirm` — Confirm memories are still valid (bumps confidence)
- `GET /memory/{id}/history` — Get supersession chain for a memory
- `GET /memory/{id}/backlinks` — Get automatically discovered related memories
- `POST /knowledge/ingest` — Docs→skills front door (SP2/SP2.1): ingest a document to the corpus synchronously (searchable immediately), write a `queued` ingest-status record, and enqueue one `classify_and_draft_from_doc` Celery task that classifies the document (`reference`/`procedural`/`mixed`) and fans out one `draft_skill_from_doc` task per detected procedure title. Body: `{content, source_name, source_type}`. Returns **202** `{corpus_source, status, note}`. See `app/knowledge/api.py`, `app/workers/skill_synthesis.py`.
- `GET /knowledge/sources` — Corpus sources joined with each source's pending (draft) skill count plus the latest async classify/draft ingest `status`/`disposition` (and `skills_queued`/`updated_at`). Returns `{sources, count}`.
- `POST /knowledge/ingest-url` — Same docs→skills pipeline, sourced from a crawl instead of pasted text. Body: `{url, depth=0, max_pages=25}` — `depth`/`max_pages` are clamped (not rejected) into `[0, KNOWLEDGE_CRAWL_MAX_DEPTH]` / `[1, KNOWLEDGE_CRAWL_MAX_PAGES]`. The start URL is checked with `is_safe_url` (`app/knowledge/crawler.py`) before enqueueing → **400** `{"detail": "URL rejected: <reason>"}` if unsafe (defense-in-depth; `run_url_ingest`, the Celery task that actually crawls, re-checks every URL it touches). On success, enqueues `run_url_ingest` (`app/workers/skill_synthesis.py`) and returns **202** `{status, url, note}` immediately — the crawl (SSRF-guarded, same-site-only BFS) and per-page `ingest_knowledge_document` calls (source name `Web:{hostname}:{title}`, source_type `"web"`) happen entirely in the worker; one bad page is skipped, not fatal, and the task itself never raises.
- `GET /collectors` — Per-collector status/health for the dashboard (SP3 — Living Knowledge Sync): `{collectors: [{name, enabled, last_run, pages_seen, pages_ingested, pages_skipped, errors, health}], count}`. Router mounted only when `COLLECTORS_ENABLED=true` (same registration pattern as the knowledge router) — on a default deploy the endpoint is not mounted at all (404), rather than returning a disabled-shape body. See `app/collectors/api.py` and the root `CLAUDE.md`'s Collectors subsection.
- `POST /decision/synthesize` — SP4 Decision Board homework: body `{context (min 1 char), draft_questions: string[] = [], agent_id: string = "unknown"}`. Retrieval-first + bounded best-effort LLM — runs a GLOBAL (`project=None`) memory recall for the context and each draft question; `evidence`/`knowledge_found` come straight from the surviving vector sources, while `suggested_answers`/`suggested_actions` are a best-effort LLM pass through `app/llm.py` under `DECISION_SYNTH_TIMEOUT_SECONDS` (**any** failure — timeout, transport, HTTP status, unparseable or non-object completion → `degraded: true`, empty suggestions, retrieval unaffected; before LLM-endpoint phase 2 only the timeout set `degraded`, and every other failure reported a healthy board). The suggestion call is **schema-constrained** (LLM-endpoint phase 3): `_suggestion_schema` names every `q0..qN` id in the JSON Schema's `properties` **and** `required`, passed as `llm.chat(json_schema=...)`. Without it, `json_mode` constrained syntax only and qwen3:4b answered by mirroring the user message's own shape back — measured on the VPS as a 15.07s, `degraded: False` board with `answers=0 actions=0` on every question; with it, 3/3 on both runs. A successful call whose payload grounds **nothing** now also sets `degraded: true` with `note` `suggestions-unusable` (payload named none of the board's ids) or `suggestions-empty` (ids matched, all lists empty) — the phase-2 "healthy while producing nothing" shape, one level down. Returns `{questions: [{id, text, knowledge_found, evidence, suggested_answers, suggested_actions}], generated_at, degraded, note, board_id}` (`board_id` minted server-side via `uuid.uuid4().hex`). Gated by `DECISION_ENABLED`. See `app/decision/api.py`, `app/decision/synthesize.py`, and the root `CLAUDE.md`'s FirekeepDecision subsection (the human-facing board UI and its `decision_board`/`decision_board_check` MCP tools live on the client kit's LOCAL `firekeep-decision` server, not here).
- `GET /dreams` — Status endpoint for the Dreaming pass (automated memory consolidation + person profiles, round 1 additive-only — see the root `CLAUDE.md`'s Dreaming subsection): `{enabled, last_run, clusters_done, profiles_done, insights_written, errors, health}`, `health` one of `ok`/`degraded`/`unavailable`/`error`/`unknown`. `insights_written` is the cumulative per-run insight total (read from the `dreams:run` hash, which `record_run` mirrors from the per-run counter on every working tick — the counter itself is cleared by `reset_progress` at completion). A completed run that attempted ≥1 cluster and wrote **zero** insights reports `health="degraded"`, not `ok`: without these two, a run that wrote 6 dreams and one that wrote none returned an identical body. Router mounted only when `DREAM_ENABLED=true` (same registration pattern as `/collectors` — a disabled deploy 404s rather than returning a disabled-shape body). Reads the `dreams:run` Redis hash directly via the async `get_redis` dependency rather than through `DreamState` (`app/dreams/state.py`), whose methods are synchronous and built for the Celery task's own sync `redis.Redis` client. See `app/dreams/api.py`.

### MCP Server (port 8080)
Exposes MCP tools via Streamable HTTP (`/mcp` endpoint). Core memory tools:
- `memory_recall` — Recall relevant memories for a task (returns Markdown)
- `memory_learn` — Store action/outcome pairs in long-term memory
- `memory_stream` — Ingest raw events into the processing queue
- `memory_health` — Check service health status

Later feature areas (corpus, vault, skills, policy, agent gateway, knowledge, etc.) add further tools on this same server — not enumerated here; see the root `CLAUDE.md` for the full per-feature tool inventory. Notably: `knowledge_ingest(content, source_name="Untitled", source_type="text")` — the docs→skills front door backing `POST /knowledge/ingest` above; distinct from `corpus_ingest` (corpus-only, no skill drafting). `knowledge_ingest_url(url, depth=0)` — same pipeline, backing `POST /knowledge/ingest-url` above; proxies exactly like `knowledge_ingest`.

### Data Flow
- `/recall` → RAG engine queries Qdrant (semantic) + Neo4j (graph) concurrently → merges/boosts scores → Markdown
- `/learn` → writes action chain to Neo4j (Domain→Action→Outcome→Resolution) + embeds to Qdrant → contradiction detection auto-supersedes stale memories
- `/stream` → LPUSH to Redis → Celery Beat (60s) → LLM extraction → Neo4j MERGE

### Graph Schema
- **Nodes**: Namespace, Domain, Concept, Action, Outcome, Resolution, EventStream
- **Edges**: CONTAINS, RELATES_TO, CAUSED, RESOLVED_BY, UTILIZES, SUPERSEDES, BACKLINK (legacy — no new edges created; ~161K pre-existing edges remain in Neo4j pending a cleanup step)

## Commands

### Run the API server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Run all services via Docker
```bash
docker-compose up --build
```

### Run tests
```bash
# Host: install runtime + dev deps first.
pip install -r requirements.txt -r requirements-dev.txt
pytest tests/ -v
pytest tests/test_rag.py -v          # single test file
pytest tests/test_models.py -k "test_context_query" -v  # single test
```
CI runs the full suite per-service + shared modules (all fakeredis-based) on every push/PR
via `.github/workflows/ci.yml`. A non-blocking `ruff` lint job runs alongside.

### Run Celery worker + beat (separate processes)
```bash
celery -A app.workers.sleep_cycle worker --loglevel=info
celery -A app.workers.sleep_cycle beat --loglevel=info
```
Beat schedule (`app/workers/sleep_cycle.py`) includes `confluence-collector` → `app.collectors.confluence.run_confluence_collector`, interval `CONFLUENCE_COLLECTOR_SCHEDULE_HOURS` (default 24h). This entry is registered unconditionally — the task fires on every tick regardless of feature flags — but no-ops before opening any external connection unless `COLLECTORS_ENABLED`, `CONFLUENCE_COLLECTOR_ENABLED`, and `CONFLUENCE_SPACE_KEYS` are all set.

### Run MCP server standalone
```bash
python -m app.mcp_server
```

## Configuration

All configuration via environment variables or `.env` file. See `.env.example` for all options. Key settings:
- `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_POOL_SIZE`
- `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_COLLECTION`
- `REDIS_URL`, `REDIS_STREAM_KEY`
- `LLM_BASE_URL`, `LLM_MODEL`, `EMBEDDING_MODEL`
- `LLM_NATIVE_CHAT` (default `auto`), `LLM_NATIVE_PROBE_TTL_SECONDS` (default `600.0`), `LLM_NATIVE_BASE_URL` (default `""`) — chat-endpoint selection (`app/llm.py`). Ollama honours `think:false` on its native `/api/chat` and IGNORES it on `/v1/chat/completions`; measured 2026-08-04 on the VPS (qwen3:4b, 4 vCPU), same document: **83.19s on `/v1` vs 4.00s native**. `auto` derives a root by stripping a required `/v1` suffix and confirms it with one cached `GET {root}/api/version` (2xx **and** a JSON object with a `version` key), failing toward `/v1` on anything unconfirmed. Do NOT "fix" this by repointing `LLM_BASE_URL` at the native root: three embedding call sites concatenate `/embeddings` onto the same variable. `llm.chat` also takes an optional `json_schema=`/`json_schema_name=` (phase 3) threaded to ollama's native `format` and `/v1`'s standard `response_format: {"type":"json_schema", ...}` — **`json_mode` alone constrains syntax, not shape**; only `decision/synthesize.py` passes one, and a caller that does not gets byte-identical bodies. There is no env var: a backend that rejects the schema pre-generation gets one automatic retry without it (measured on the VPS: an unusable schema is refused in **0.27s**, before generation). See the root `CLAUDE.md`'s "LLM endpoint selection" subsection for the full write-up, the phase plan (which call sites are converted and which are deliberately not), and the timeout reasoning.
- `AUTH_ENABLED`, `AUTH_REDIS_URL`, `CORS_ORIGINS`, `RATE_LIMIT` (security)
- `BOOST_FACTOR`, `GRAPH_RELEVANCE_WEIGHT`, `CONTENT_HASH_LENGTH` (RAG tuning)
- `RERANK_ENABLED`, `RERANK_CANDIDATES_MULTIPLIER`, `MEMORY_DECAY_HALF_LIFE_DAYS` (advanced; the latter is the fallback for unknown legacy memory types)
- `DECAY_REFERENCE_DAYS`, `DECAY_PROCEDURAL_DAYS`, `DECAY_EPISODIC_DAYS`, `DECAY_TRANSIENT_DAYS` (type-specific recall + aging half-lives; non-positive means no age decay/archive)
- `FIREKEEP_API_URL`, `MCP_HOST`, `MCP_PORT` (MCP server — no key of its own; forwards the caller's X-API-Key)
- `RELAY_URL`, `SENTINEL_URL` (briefing aggregator outbound upstreams — `GET /briefing` fans out to Relay + Sentinel REST; every call carries `FIREKEEP_INTERNAL_KEY`)
- `DEFAULT_NAMESPACE` (multi-tenant default)
- `GC_ENABLED`, `GC_DRY_RUN`, `GC_SCHEDULE_HOURS`, `GC_ARCHIVE_GRACE_DAYS`, `GC_PURGE_ENABLED`, `EVICTION_THRESHOLD` (archive-first maintenance; hard purge off by default)
- `DLQ_MAX_SIZE`, `REEMBED_BATCH_SIZE` (operations)
- `AGENT_ENABLED`, `AGENT_SCHEDULE_HOURS`, `AGENT_DUPLICATE_THRESHOLD`, `AGENT_BATCH_LIMIT` (memory agent)
- `KNOWLEDGE_ENABLED` (default `true`), `KNOWLEDGE_MAX_PROCEDURES` (default `10`) — docs→skills pipeline: gates `/knowledge/*` router registration and caps how many procedure titles (and therefore draft-skill Celery tasks) a single classified document can queue
- `KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS` (default `300`) — bounds the classify LLM call in `classifier.py` **on the `/v1` endpoint**, invoked from the async `classify_and_draft_from_doc` worker task (not any request/proxy timeout — `POST /knowledge/ingest` returns before classification runs). Sized for CPU Ollama on `/v1`, where a thinking model measured 288.9s. `KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS` (default `300.0`) is the separately tunable budget when `app/llm.py` selects ollama's native `/api/chat`, where a real end-to-end classify measured **8.59s** through the worker. It defaults to the SAME 300 deliberately: **"native" does not imply "fast"** — the native path only disables THINKING, and the probe confirms ollama, not a thinking model, so a non-thinking backend (the office deploy's llama3.2:3b, ~56s per classify) is routed down it and gains nothing while losing headroom. Non-thinking models also accept `think:false` cleanly rather than 4xx-ing (measured: `llama3:latest` 3.10s, `gemma3:4b` 1.94s), so the demote-and-retry hatch never rescues them. Lower it only for a backend you have measured. Note `SKILL_SYNTH_TIMEOUT_SECONDS` deliberately has **no** native sibling: skill drafting is generation-bound (5.9–7.2 tok/s × `SKILL_SYNTH_MAX_TOKENS`), so 800 tokens costs 112–135s natively and a 120s budget failed every draft of a real ingest. `KNOWLEDGE_STATUS_TTL_SECONDS` (default `2592000` = 30d) — TTL on the per-source ingest-status Redis hash (`app/knowledge/status.py`), an orphan safety-net
- `KNOWLEDGE_URL_INGEST_ENABLED` (default `true`), `KNOWLEDGE_CRAWL_MAX_DEPTH` (default `2`), `KNOWLEDGE_CRAWL_MAX_PAGES` (default `25`) — `POST /knowledge/ingest-url` clamps requested `depth`/`max_pages` into these ceilings rather than rejecting over-limit requests. `KNOWLEDGE_CRAWL_TIMEOUT_SECONDS` (default `15`) — per-request HTTP timeout passed to `crawl()`. `KNOWLEDGE_CRAWL_MAX_PAGE_BYTES` (default `2000000`) — per-page byte cap before truncation. See `app/knowledge/crawler.py`.
- `COLLECTORS_ENABLED` (default `false`), `CONFLUENCE_COLLECTOR_ENABLED` (default `false`), `CONFLUENCE_BASE_URL`, `CONFLUENCE_SPACE_KEYS`, `CONFLUENCE_LABEL`, `CONFLUENCE_PAT_VAULT_KEY` (default `confluence_pat`), `CONFLUENCE_COLLECTOR_SCHEDULE_HOURS` (default `24`), `COLLECTOR_LOCK_TTL_SECONDS` (default `3600`) — SP3 scheduled collectors (Living Knowledge Sync), opt-in and disabled by default; see root `CLAUDE.md`'s Collectors subsection for the full framework/adapter/skill-lifecycle writeup. The Confluence PAT resolves env-first: `CONFLUENCE_PAT` (default `""` — a K8s Secret or `.env` value) is used directly when set, skipping Vault entirely; empty falls back to the existing `CONFLUENCE_PAT_VAULT_KEY`/Vault path, unchanged.
- `OWM_ENABLED` (default `true`), `OWM_WEIGHT` (default `0.15`), `OWM_PRIOR_N` (default `5`), `OWM_WINDOW_DAYS` (default `30` — matches the 30d eval TTL), `OWM_AGENT_CAP` (default `5`), `OWM_SCHEDULE_HOURS` (default `24`) — Outcome-Weighted Memory (`app/owm.py`): nightly join of replay `memory_read` events (payloads now carry the returned `memory_ids`) to session outcomes, Beta-shrunk per-memory `owm_efficacy` on the Qdrant payload, multiplied into lifecycle recall scoring and the GC eviction composite. Neutral-by-construction at small N — safe on by default.
- `SKILL_MATCH_SCORE_FLOOR` (default `0.30`), `SKILL_MATCH_EMBED_TIMEOUT_SECONDS` (default `1.2`) — semantic skill matching (`app/skills/search.py`). The floor is a RAW cosine threshold and is deliberately distinct from `RECALL_SCORE_FLOOR` (0.35): that was tuned for prose memory bodies on mxbai-embed-large/1024-dim, while a skill embeds a terse composite and the office deploy embeds with granite-embedding:30m/384-dim. Raising it cannot regress matching below the legacy path (empty results fall back to scroll + substring). The timeout bounds the query embed so the briefing's 2.0s per-section budget still fits the Qdrant round trip and the fallback.
- `DECISION_ENABLED` (default `true`) — gates `POST /decision/synthesize` router registration (SP4 Decision Board). `DECISION_SYNTH_TIMEOUT_SECONDS` (default `30.0`, raised from `20.0` in LLM-endpoint phase 2) — bounds the best-effort suggestion LLM pass in `app/decision/synthesize.py`; retrieval/evidence are produced before that call and returned regardless of it. The value is a **contract with a second process**, not a preference: the client-side `firekeep-decision` MCP server has its own, distinct `DECISION_SYNTH_TIMEOUT_SECONDS` (a different process) defaulting to `30.0`, and derives its HTTP timeout for this endpoint as that + 15s = **45s**, after which it hangs up — so raising past 30 needs a coordinated client release. 20.0 was below the floor even for the fast native path (~120–145 output tokens at the measured 5.9–7.2 tok/s). Note the raise **halves the margin for the recalls** (the client's 45s is fixed, so `45 − synth` went 25s → 15s): those run `format="raw"` so there is no generation on that path, but each does one always-cold embed, and `RERANK_ENABLED=true` (`top_k × multiplier` LLM calls per recall × 9 questions) will not fit — it did not fit in 25s either. Measured ceiling on the VPS after the change: 1 question 20.98s / 3 questions 16.31s (both fit), 8 questions 37.28s (degrades) — binding constraint is wall clock at ~6.5 tok/s, not output size. Deliberately **no** native sibling (a lower native budget is what strands non-thinking-model deploys) and deliberately **no** `max_tokens` (JSON mode self-terminates, so a cap could only truncate valid output into invalid). `DECISION_MAX_QUESTIONS` (default `8`) — hard cap on per-question recalls per board. See the root `CLAUDE.md`'s "LLM endpoint selection" and FirekeepDecision subsections.
- `DREAM_ENABLED` (default `false`) — master switch for Dreaming (automated memory consolidation + person profiles, `app/dreams/`); gates both `run_dream_tick`'s self-gate and the `GET /dreams` router mount. `DREAM_TICK_MINUTES` (default `5`) — beat interval; one unit of work per tick. `DREAM_IDLE_MINUTES` (default `30`), `DREAM_MIN_NEW_MEMORIES` (default `25`) — the idle + work-available gate halves. `DREAM_MIN_AGE_DAYS` (default `2`), `DREAM_MIN_CLUSTER` (default `4`), `DREAM_CLUSTER_THRESHOLD` (default `0.72`), `DREAM_MAX_CLUSTERS_PER_RUN` (default `20`) — candidate selection and clustering (`app/dreams/select.py`). `DREAM_MAX_INSIGHT_CHARS` (default `800`) — per-insight cap, load-bearing against `RECALL_TOKEN_BUDGET`. `DREAM_OWM_FLOOR` (default `0.35`) — below this at `owm_n >= OWM_PRIOR_N` a memory is excluded from candidacy. `DREAM_SYNTH_TIMEOUT_SECONDS` (default `45.0`) — the only binding timeout on the dream tick, since the worker's `--pool=solo` silently ignores Celery's `soft_time_limit`/`time_limit`. **Both** `synthesize()` and `synthesize_profile()` read it off `Settings` themselves (their caller no longer passes a timeout) and hand it to `llm.chat` as ONE budget for both endpoints — deliberately **no** native sibling, unlike `KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS`: 45s already clears the measured 22.5s native latency, and a lower native budget is what strands a non-thinking-model ollama deploy. Both read it as `getattr(settings, ..., 45.0)`; that literal is a duck-typed-stub fallback production never reaches, and `tests/test_dreams_synthesize.py` pins it to this default so the drift cannot be silent. See the root `CLAUDE.md`'s Dreaming subsection. `DREAM_LOCK_TTL_SECONDS` (default `1800`) — Redis `SETNX` lock TTL. `DREAM_PROFILES_ENABLED` (default `true`) — sub-flag for the person-profile half of the feature.

## Key Design Decisions

- **No APOC dependency**: Neo4j Cypher uses standard MERGE with dynamic labels grouped by type and sanitized against injection
- **Dual-store scoring**: Items found in both Qdrant and Neo4j get a configurable boost (default 1.5x) via substring + Jaccard similarity matching (threshold 0.3)
- **Score normalization**: Min-max normalization per source to [0,1] before merging, with composite graph scoring (text relevance + distance)
- **Graceful degradation**: If one store fails during recall, the other still contributes results; `/learn` returns partial success when one store fails
- **Sleep Cycle DLQ**: Failed batches go to `firekeep:event_stream:dlq` Redis key for later reprocessing
- **Sleep Cycle transactions**: All Neo4j writes in the Sleep Cycle worker are wrapped in explicit transactions
- **Cypher injection prevention**: Dynamic labels/relationship types are sanitized to alphanumeric + underscore only
- **Fulltext index search**: Neo4j Lucene-backed fulltext index for relevance-scored graph queries (falls back to CONTAINS on `ClientError` only)
- **Case-insensitive resolution queries**: `query_resolutions` uses `toLower()` on both sides for reliable matching
- **API key authentication**: shared per-key ASGI validator (auth/asgi.py FirekeepKeyAuthMiddleware) over the whole app when AUTH_ENABLED=true; prefix-skips /health, /version, /docs, /redoc, /openapi.json; exact-skips only /dashboard and /dashboard/ (the HTML shell — everything under /dashboard/api/* is gated, not exempt); fail-closed 503 when Redis DB 7 is unreachable while enabled
- **Rate limiting**: slowapi-based rate limiting (configurable, default 60/min)
- **Memory decay**: Type-specific exponential recall decay (reference=off, procedural=180d, episodic=90d, transient=14d by default); decay changes rank, not lifecycle state
- **LLM re-ranking**: Optional re-ranking pass via LLM (gated behind RERANK_ENABLED config), shared httpx client, robust score parsing with word-boundary regex
- **Embedding cache**: OrderedDict-based LRU cache (512 entries) with O(1) eviction for vector embeddings
- **Entity canonicalization**: Lowercase + normalize names before MERGE to reduce duplicates
- **Redis pipeline batching**: Pipeline pattern for both event ingest and Sleep Cycle rpop
- **Singleton RAGEngine**: Created once in lifespan with shared httpx client, injected via FastAPI DI
- **Request body limit**: Content-Length pre-check + chunked-encoding guard with proper 413 responses
- **Config validation**: CONTENT_HASH_LENGTH >= 16 enforced, startup warnings for empty secrets
- **Port security**: Backend ports (Neo4j, Qdrant, Redis) bound to 127.0.0.1; API/MCP on 0.0.0.0
- **Celery beat separation**: Worker and beat run as independent Docker services to prevent task storms
- **Public health methods**: `ping()` and `memory_count()` on clients — health endpoint uses public API only
- **Multi-tenant namespaces**: Optional `namespace` field on all requests (default "default"), filters in both Neo4j and Qdrant
- **Namespace graph**: `Namespace` nodes linked to `Domain` via `CONTAINS` edges for scoped queries
- **Web dashboard**: Self-contained SPA, zero external dependencies
- **Webhook system**: HMAC-SHA256 signed callbacks, event-type + namespace filtering, stored in Redis. Current maintenance events: `memory.learned`, `memory.recalled`, `stream.ingested`, `gc.pruned`, `agent.merged`, `agent.orphan_cleaned`, `agent.contradiction_found`, `agent.reclassified`. The legacy `agent.confidence_decayed` type remains accepted for compatibility but the current memory agent has no confidence-decay pass.
- **Memory GC**: Scheduled archive-first scoring based on age × access × confidence × outcome efficacy. Confirmed memories, skills and corpus chunks are protected. Preview, audit and restore are dashboard-visible; hard purge and the GC task's Neo4j orphan cleanup require `GC_PURGE_ENABLED=true`, and purge applies only to expired GC-origin archives.
- **DLQ cap**: `LTRIM` after every DLQ push prevents unbounded Redis growth (configurable max 10K)
- **Export/Import**: JSONL streaming for backup/migration, includes both vector and graph data
- **SSE streaming recall**: Progressive results via Server-Sent Events, no extra dependencies
- **Embedding hot-swap**: Re-embed all vectors with progress tracking via Celery task state
- **MCP client lock**: `asyncio.Lock` guards lazy client initialization against TOCTOU races
- **Lucene phrase search**: Bigrams wrapped in double quotes for correct Lucene phrase matching
- **Knowledge lifecycle**: Memory states (active/superseded/deprecated/archived) with status multipliers in recall scoring. Qdrant is authoritative for Neo4j rows linked to exact vector memory IDs; those fail closed when the vector is missing/archived. Unlinked rows remain recallable as `lifecycle_verified=false` because sleep-cycle extraction intentionally creates graph-owned knowledge.
- **Contradiction detection**: Automatic on `/learn` — embeds new action, finds >0.85 similarity matches, auto-supersedes stale memories
- **Confidence scoring**: `(1 + confirmed_count) / (1 + contradicted_count)` factor in recall scoring
- **Supersession chains**: `SUPERSEDES` edges in Neo4j track knowledge evolution history
- **Lifecycle scoring**: `final_score = base_score * status_multiplier * confidence_factor` (active=1.0, superseded=0.5, deprecated=0.1, archived=0.0)
- **Automatic backlinks** (removed): BACKLINK edge creation has been disabled. The `/memory/{id}/backlinks` GET endpoint still exists and can query the ~161K pre-existing edges in Neo4j; no new edges are created. `MemoryRef` nodes and `BACKLINK` edges remain in Neo4j pending a separate cleanup step.
- **Namespace normalization**: All incoming namespaces are normalized (lowercase, hyphens to underscores) via Pydantic field validators. One-time migration task (`migrate_namespaces`) consolidates pre-existing data in Qdrant and Neo4j.
- **Health check caching**: `/health` response is cached for 10 seconds to reduce backend probing (~35K fewer calls/day). Stale uptime/status for up to 10s is acceptable.
- **Skill staleness**: `memory_recall` HSETs `memory:last_recalled`; `flush_last_recalled` drains it to the `last_recalled_at` Qdrant payload; `skill_staleness_pass` (`app/skills/staleness.py`, run in `run_memory_agent` after that flush) flags active skills unrecalled past `SKILL_STALE_AFTER_DAYS` (default 90) as `stale=True` and un-flags re-recalled ones — never changes `skill_status`, never deletes. Surfaced via `GET /skills?stale=true` + the dashboard "Stale (review)" filter. **Known gap (2026-07-30):** `GET /skills` performs no Redis access, so a skill returned by `skill_recall` never advances `last_recalled_at` — now that semantic matching actually works (below), the sweep will flag genuinely-used skills stale. Tracked follow-up; wiring `get_redis` into the skills router changes what the sweep flags and ~20 tests build the app without it.
- **Semantic skill matching** (`app/skills/search.py`): `GET /skills` is two paths behind one endpoint — cosine query via `search_skill_points` when `q` is supplied (floored at `SKILL_MATCH_SCORE_FLOOR`), ID-ordered `scroll` otherwise. Replaces a `scroll`-then-**literal-substring** filter that was duplicated inline in `app/briefing/sections.py`, and which essentially never matched (`skill_recall` sent only the task's first five words and required them verbatim inside a trigger). Deliberately does NOT use `VectorClient.search()`: its `must_not skill_status="draft"` is unconditional and would permanently empty the dashboard review queue, it cannot express the skill filter, and its projection drops every skill payload field. Embed is fail-soft (degrades to scroll), Qdrant is fail-loud; an empty-result fallback makes the change a strict superset of the old behaviour. The no-`q` listing path gains no embedding-backend dependency.
- **Memory Agent**: Autonomous knowledge custodian — Celery periodic task (default 6h) performing 5 self-healing passes: (1) duplicate detection & LLM-synthesized merge with fallback, (2) orphan node cleanup, (3) deep contradiction scan across domains, (4) confidence decay on stale unconfirmed memories, (5) cluster coherence with domain reclassification. Pass 4 (backlink reinforcement) was removed — it was write-only machinery never queried by recall. Redis SETNX lock prevents overlapping runs. Per-pass error isolation ensures one failure doesn't stop others. Reports via webhook events: `agent.merged`, `agent.orphan_cleaned`, `agent.contradiction_found`, `agent.confidence_decayed`, `agent.reclassified`. **Human confirmation is protection, not a ranking input** (2026-08-04): both passes read `confirmed_count` only through the `(1 + confirmed) / (1 + contradicted)` ratio, so a memory confirmed once still lost to one confirmed three times (and a confirmed-but-contradicted memory lost to a never-confirmed one) and was written `status="superseded"` — a permanent 0.5 recall multiplier; dedup could additionally fold a confirmed memory into an LLM merge, replacing its wording AND carrying its `confirmed_count` forward onto the synthesized text via `_merge_lifecycle`'s max fold. The two fixes are deliberately different shapes: dedup excludes confirmed memories from scope (`_dedup_scope_filter`, derived from `_active_non_corpus_filter` so the corpus/dream guards can't drift — no merge outcome preserves a confirmed memory's own text, and a write-time refusal is insufficient because a refused member is still a CLUSTER member: `_merge_lifecycle` would still launder its `confirmed_count` onto the synthesis and its text would still go to the merge model. Note the argument is NOT "a refusal leaves a residual duplicate" — exclusion leaves one too whenever ≥2 unconfirmed members remain, as the guard tests themselves show), while `deep_contradiction_pass` keeps full scope and refuses the write, because the shared filter also scopes the similarity QUERY and excluding confirmed memories there would stop one being FOUND — a confirmed memory must still be able to supersede a stale rival. `cluster_coherence_pass` is deliberately untouched: it rewrites `domain` only, never status or text, and excluding confirmed memories would drop them out of the per-domain centroids, changing outlier detection for the unconfirmed memories around them. Precedent: `gc.py::_scan_candidates` and `vector.py::_similarity_filter`. Guards: `cortex/tests/test_memory_agent_confirmed.py` (11 tests against a filter-HONOURING Qdrant double — every other fake in that suite ignores the filter, so only an object-shaped assertion is possible against them). Full write-up: `docs/superpowers/plans/2026-08-04-confirmed-memory-protection.md`.
- **Docs→skills draft-exclusion + deterministic re-ingest** (SP2): draft skills produced by `POST /knowledge/ingest` (`skill_status="draft"`) are excluded from every recall path — not just `skill_recall`'s and the briefing's existing scroll-filtered queries, but also the core `VectorClient.search()` behind `memory_recall`/`recall_streaming`, which gained an explicit `must_not skill_status="draft"` filter (`app/db/vector.py`) to close a back-door leak (a semantically-relevant draft could otherwise surface in plain memory recall before human approval). Separately, each draft skill's Qdrant point ID is deterministic — `uuid5(SKILL_NS, "source_name::procedure_title")` (`app/skills/synthesizer.py`) — so re-ingesting the same document/procedure pair idempotently upserts the same point rather than creating a duplicate; if that ID already holds a human-approved (`skill_status="active"`) point, the upsert is refused and the point is flagged `needs_rereview=True` instead of being silently overwritten.

## Development Practices

- Claude maintains detailed documentation of all changes and decisions
- CLAUDE.md is kept up-to-date as the project evolves
- All significant architectural decisions and progress are logged
- Teams workflow: architect → implement → test → security review

## Documentation

- `docs/ARCHITECTURE.md` — Full architecture specification with module contracts
- `docs/SECURITY_REVIEW.md` — Security review findings and fixes
- `docs/plans/2026-03-05-major-improvements.md` — v0.5.0 implementation plan (15 fixes)
- `docs/plans/2026-03-06-memory-agent-design.md` — Memory Agent autonomous custodian design
- `docs/plans/2026-03-05-knowledge-lifecycle-design.md` — Knowledge lifecycle design document
