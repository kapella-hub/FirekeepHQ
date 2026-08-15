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
├── mcp_server.py      # MCP server (Streamable HTTP; 30 tools — core memory (recall/learn/stream/health/handoff/feedback) + replay/eval/vault/corpus/knowledge/skills/agent-gateway feature tools + runbook_ack)
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
├── procedures/        # Living Procedures — a skill observed as a procedure (rounds 1–2, opt-in via PROCEDURE_ENABLED)
│   ├── models.py        # StepSpec — self-contained per-step matcher (id/text/kind/pattern/load_bearing; round 2 adds kind "command"). Pydantic only, no I/O
│   ├── match.py         # Pure glob matching (file paths AND normalized command text), earlier-load-bearing-step detection, advisory text. No I/O and CANNOT raise — it runs on the blocking pre-edit path
│   ├── store.py         # Every Redis read/write; the only module that knows a proc:* key format (index, executions, warn latch, stats, proposals, bundle acks, modes, challenges/permits, pending evidence, deviation ledger)
│   ├── observe.py       # ProcedureObserver — the recognise/advise stage called from AgentGatewayService.decide(); returns a PendingObservation whose commit() runs only once the decision settles on allow (I7). Holds NO vector client (I5). Round 2: plan_command() → enforce.evaluate() for Bash steps
│   ├── enforce.py       # Enforced Runbooks (round 2) — command verdicts (advise/require_ack/block), challenge→ack→one-use permits, success-gated evidence, and the runbook_evaluated marker a block-mode client requires before honouring an allow. Fails CLOSED via an exception-tight branch; everything scoped to the VERIFIED workspace
│   ├── harden.py        # Nightly Celery pass: Tier A frequency + gated Tier B efficacy → proposals, never mutations
│   └── api.py           # GET /procedures, GET /procedures/{skill_id}/executions, POST /procedures/proposals/{id}/dismiss; round 2: GET /procedures/bundle, POST /procedures/bundle/ack, POST /procedures/ack, GET /procedures/deviations, GET+PUT /procedures/{skill_id}/mode (PUT admin-only — agents never arm modes)
├── autopilot/         # Knowledge Autopilot round 1 (docs/guides/knowledge-autopilot.md) — READ-ONLY operator surface
│   ├── inbox.py         # Section builders: draft/stale/rereview skills, LP proposals, contested pairs, eval DLQ
│   ├── digest.py        # Windowed activity counts (learned/archived/superseded/dreamed/drafted/feedback/GC)
│   ├── compliance.py    # Living Instructions round 1 — per-instruction compliance over rp:eval:* (predicates frozen to the 2026-08-11 founding measurement)
│   └── api.py           # GET /autopilot/inbox + /digest + /compliance (admin); per-section fault isolation
└── dreams/            # Automated memory consolidation + person profiles (round 1 — additive only)
    ├── select.py        # Pure candidate selection + partitioning + clustering + per-synthesis member sampling (centroid-nearest), no I/O
    ├── synthesize.py     # The one LLM call for cluster insights — via app/llm.py's chat() (native /api/chat where available, so think:false is actually honoured), JSON mode
    ├── store.py          # Dedicated write path — raw PointStruct, deterministic IDs, never /memory/learn
    ├── profile.py        # Person-profile assembly, keyed by member_id — its one LLM call also goes via app/llm.py's chat(), SCHEMA-CONSTRAINED to `{"profile": "<prose>"}` and the prose extracted back out (the stored payload is still prose; the grammar is on the wire, not in the store)
    ├── state.py          # Redis run-record + progress counters (DreamState, sync client — see task.py)
    ├── task.py           # Celery task: gate → lock → one unit of work → record
    └── api.py            # GET /dreams status endpoint
```
(Module map above omits some pre-existing packages, e.g. `knowledge/`, `skills/`, `policy/`, `agent_gateway/`, `briefing/`, `decision/` (SP4 — `synthesize.py`'s `synthesize_board` + `api.py`'s `create_decision_router`, backing `POST /decision/synthesize`) — see the API Endpoints list below and the root `CLAUDE.md` for those. Two more SP3 additions live under those existing packages: `app/knowledge/ingest_core.py` (`ingest_knowledge_document` — shared corpus-ingest + classify-enqueue core used by both `POST /knowledge/ingest` and the collectors) and `app/skills/reconcile.py` (`reconcile_source_skills` — stale-draft skill sweep on reclassification). `app/knowledge/crawler.py` (`crawl` + `is_safe_url`) is the SSRF-guarded crawler backing `POST /knowledge/ingest-url`. See the root `CLAUDE.md`'s Collectors and Docs→Skills subsections for the full picture. Dreaming's person-profile section also lives partly outside `app/dreams/`: `app/briefing/sections.py::profile_section` is the `GET /briefing` reader. See the root `CLAUDE.md`'s Dreaming subsection for the full picture.)

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
Beat schedule (`app/workers/sleep_cycle.py`) includes `confluence-collector` → `app.collectors.confluence.run_confluence_collector`, interval `CONFLUENCE_COLLECTOR_SCHEDULE_HOURS` (default 24h). This entry is registered unconditionally — the task fires on every tick regardless of feature flags — but no-ops before opening any external connection unless `COLLECTORS_ENABLED`, `CONFLUENCE_COLLECTOR_ENABLED`, and `CONFLUENCE_SPACE_KEYS` are all set. `procedure-hardening` → `app.procedures.harden.run_procedure_hardening`, interval `PROCEDURE_SCHEDULE_HOURS` (default 24h), follows the same shape: registered unconditionally, self-gates to `{"status": "disabled"}` unless `PROCEDURE_ENABLED`, and never raises.

### Run MCP server standalone
```bash
python -m app.mcp_server
```

## Reference

The endpoint list, the full configuration surface and the design-decision record
moved to `docs/guides/`. They are consulted while working on a specific area, and
this file is loaded into every session that touches `cortex/` — on top of the root
guide. Keeping them here taxed every unrelated task.

| Reference | File |
|---|---|
| Cortex REST + MCP endpoint reference | [`docs/guides/cortex-api-endpoints.md`](../docs/guides/cortex-api-endpoints.md) |
| Cortex configuration reference | [`docs/guides/cortex-configuration.md`](../docs/guides/cortex-configuration.md) |
| Cortex design decisions and their evidence | [`docs/guides/cortex-design-decisions.md`](../docs/guides/cortex-design-decisions.md) |

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
