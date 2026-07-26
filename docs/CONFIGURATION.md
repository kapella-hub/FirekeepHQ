# Configuration Reference

All configuration is via environment variables in `.env`. Copy the example and edit:

```bash
cp .env.example .env
```

## Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `NEO4J_PASSWORD` | (required) | Neo4j database password |
| `CORS_ORIGINS` | `["*"]` | Allowed CORS origins (set to dashboard URL) |
| `LLM_MODEL` | `qwen3:4b` | Ollama model for LLM inference |
| `EMBEDDING_MODEL` | `mxbai-embed-large` | Embedding model |
| `CORTEX_INSTALL_FINETUNE_DEPS` | `False` | Build-time toggle for optional CPU-only embedding fine-tuning deps on `cortex-worker` |
| `MULTIHOP_ENABLED` | `True` | Enable multi-hop graph traversal |
| `NB_PROACTIVE_RECALL_ENABLED` | `True` | Auto-inject memories on ctx_update |
| `RP_ENABLED` | `True` | Enable replay trace event recording |
| `RP_RETENTION_DAYS` | `30` | How long replay events are retained |
| `AUTH_ENABLED` | `False` | Enforce API key authentication on all endpoints |
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

Infrastructure ports are bound to `127.0.0.1` only. Application ports are on `0.0.0.0`.

Two MCP servers ship in the client kit as **stdio-local** processes and bind no port: `firekeep-symdex` (code intelligence, always installed) and `firekeep-decision` (the Decision Board, always installed — backed by Cortex `POST /decision/synthesize`).

## Intelligence Features

### Memory Type Classification
Every memory is typed as `reference` (no decay), `procedural` (180-day half-life), `episodic` (90-day, default), or `transient` (14-day). The sleep cycle LLM auto-classifies new memories.

### Multi-hop Graph Reasoning
Graph queries traverse up to 3 hops with 0.5x score decay per hop, finding indirect connections that single-hop queries miss. Enabled by default (`MULTIHOP_ENABLED=True`).

### User Profile Model
The memory agent automatically extracts skills, preferences, and goals from your memory corpus into Person nodes in Neo4j. Recall results are boosted for domains matching your expertise.

### Proactive Recall
When you call `ctx_update` with a plan or progress update, FirekeepBridge automatically queries FirekeepCortex for relevant past experience and injects it into your session shadow.

### Embedding Fine-tuning (optional)
Generate training triplets and fine-tune the embedding model with `POST /admin/embeddings/finetune`. The default Cortex images do not install the training stack. Set `CORTEX_INSTALL_FINETUNE_DEPS=true` and rebuild `cortex-worker` to enable the CPU-only fine-tuning dependencies.
