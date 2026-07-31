from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # API
    APP_NAME: str = "FirekeepCortex"
    DEBUG: bool = False
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]
    RATE_LIMIT: str = "60/minute"

    # Neo4j
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = ""  # Must be set via environment / .env
    NEO4J_POOL_SIZE: int = 50
    # Bounded retry for the lifespan connect. Cortex aborts startup if the graph
    # is unreachable, so a dependency that is merely slow to come up used to take
    # the API down permanently (observed in production 2026-07-26 during a
    # rolling restart). Set attempts to 1 to restore single-shot behaviour.
    NEO4J_CONNECT_ATTEMPTS: int = 6
    NEO4J_CONNECT_BACKOFF_SECONDS: float = 1.0

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "firekeep_memory"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_STREAM_KEY: str = "firekeep:event_stream"
    REDIS_BATCH_SIZE: int = 50
    DLQ_MAX_SIZE: int = 10_000

    # Celery
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"

    # LLM
    LLM_BASE_URL: str = "http://localhost:11434/v1"
    LLM_MODEL: str = "llama3"
    LLM_API_KEY: str = ""  # Must be set via environment / .env

    # Embedding — defaults MUST match the deployed model + the Qdrant collection's
    # vector size, or a deploy without an .env override embeds at the wrong dimension
    # into the existing collection and every write fails. The stack ships
    # mxbai-embed-large (1024-dim); .env.example + the ollama-pull service agree.
    EMBEDDING_MODEL: str = "mxbai-embed-large"
    EMBEDDING_DIM: int = 1024

    # RAG Engine
    BOOST_FACTOR: float = 1.5
    GRAPH_RELEVANCE_WEIGHT: float = 0.4
    CONTENT_HASH_LENGTH: int = 32

    # Re-ranking (LLM-based; the ML GradientBoosting ranker was removed — its
    # training bridge was never built, so it never functioned).
    RERANK_ENABLED: bool = False
    RERANK_CANDIDATES_MULTIPLIER: int = 2

    # Re-embed
    REEMBED_BATCH_SIZE: int = 50

    # Memory Decay
    MEMORY_DECAY_HALF_LIFE_DAYS: int = 90
    DECAY_REFERENCE_DAYS: int = 0
    DECAY_PROCEDURAL_DAYS: int = 180
    DECAY_EPISODIC_DAYS: int = 90
    DECAY_TRANSIENT_DAYS: int = 14

    # Garbage Collection
    MAX_MEMORY_AGE_DAYS: int = 180
    PRUNE_SCORE_THRESHOLD: float = 0.3
    GC_SCHEDULE_HOURS: int = 24

    # Namespace
    DEFAULT_NAMESPACE: str = "default"

    # Memory Agent
    AGENT_ENABLED: bool = True
    AGENT_SCHEDULE_HOURS: int = 6
    AGENT_DUPLICATE_THRESHOLD: float = 0.9
    AGENT_CONFIDENCE_DECAY_DAYS: int = 180
    AGENT_BATCH_LIMIT: int = 100

    # Multi-hop Graph Traversal
    MULTIHOP_ENABLED: bool = True
    MULTIHOP_MAX_HOPS: int = 3
    MULTIHOP_DECAY_PER_HOP: float = 0.5

    # Secret Scanning
    SECRET_SCAN_ENABLED: bool = True
    SECRET_SCAN_MODE: str = "warn"  # "warn" or "block"

    # Corpus (business knowledge graph)
    CORPUS_ENABLED: bool = True

    # Knowledge Ingestion (docs -> skills orchestration, SP2)
    KNOWLEDGE_ENABLED: bool = True
    KNOWLEDGE_MAX_PROCEDURES: int = 10
    # Synchronous classify LLM call timeout (POST /knowledge/ingest). Defaults
    # sized for CPU Ollama, where a single classify runs ~150-200s (measured on
    # qwen3:4b); lower it on GPU deployments for faster fail-loud.
    KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS: float = 300.0
    KNOWLEDGE_STATUS_TTL_SECONDS: int = 2592000  # 30d orphan safety-net for per-source ingest status

    # URL ingestion (SSRF-guarded crawler -> knowledge pipeline)
    KNOWLEDGE_URL_INGEST_ENABLED: bool = True
    KNOWLEDGE_CRAWL_MAX_DEPTH: int = 2
    KNOWLEDGE_CRAWL_MAX_PAGES: int = 25
    KNOWLEDGE_CRAWL_TIMEOUT_SECONDS: float = 15.0
    KNOWLEDGE_CRAWL_MAX_PAGE_BYTES: int = 2_000_000

    # Decision Board (SP4)
    DECISION_ENABLED: bool = True
    DECISION_SYNTH_TIMEOUT_SECONDS: float = 20.0  # bound the best-effort suggestion LLM pass
    DECISION_MAX_QUESTIONS: int = 8               # hard cap on per-question recalls

    # Collectors (SP3 — Living Knowledge Sync)
    COLLECTORS_ENABLED: bool = False
    CONFLUENCE_COLLECTOR_ENABLED: bool = False
    CONFLUENCE_BASE_URL: str = ""
    CONFLUENCE_SPACE_KEYS: str = ""
    CONFLUENCE_LABEL: str = ""
    CONFLUENCE_PAT_VAULT_KEY: str = "confluence_pat"
    CONFLUENCE_PAT: str = ""  # Direct token (env / K8s Secret). Empty = fall back to Vault.
    CONFLUENCE_COLLECTOR_SCHEDULE_HOURS: float = 24.0
    COLLECTOR_LOCK_TTL_SECONDS: int = 3600

    # Phase 1 — Storage Hygiene
    EVICTION_THRESHOLD: float = 1.5
    DEDUP_SIMILARITY_THRESHOLD: float = 0.78
    # SP0 B1: LLM dedup/merge pass is opt-in. A team store must not have an
    # LLM silently rewriting memories on a timer — re-enable deliberately
    # after solo-use validation.
    DEDUP_ENABLED: bool = False

    # Phase 2 — Retrieval Precision
    RECALL_TOKEN_BUDGET: int = 600
    RECALL_TOP_K: int = 3
    # Default OFF. Measured 2026-07-26 on the shipped CPU-Ollama profile: the synthesis
    # call hits the hardcoded 30s timeout in engine/rag.py on EVERY request and recall
    # falls back to the raw context block -- so leaving this on costs 30s per call and
    # returns byte-identical output to format="raw" at 9.8ms. Turn it on only where a
    # fast generation backend actually exists.
    RECALL_SYNTHESIS_ENABLED: bool = False
    RECALL_SCORE_FLOOR: float = 0.35  # raw-cosine floor passed to Qdrant score_threshold (SP0 C4)

    # Outcome-Weighted Memory (app/owm.py): recall ranked by whether the
    # sessions that recalled a memory SUCCEEDED. Neutral-by-construction at
    # small N (Beta shrinkage), so enabling it is safe: unscored memories rank
    # bit-identically to pre-OWM.
    OWM_ENABLED: bool = True
    OWM_WEIGHT: float = 0.15          # max +/- multiplier the efficacy term can apply
    OWM_PRIOR_N: int = 5              # pseudo-observations pulling toward neutral 0.5
    OWM_WINDOW_DAYS: int = 30         # join window; evals expire at 30d, larger is dead weight
    OWM_SCHEDULE_HOURS: int = 24      # Celery beat cadence for the scoring pass
    OWM_AGENT_CAP: int = 5            # max observations one agent contributes per memory

    # Memory Reliability (SP0 — WS-A)
    EMBED_RETRY_ATTEMPTS: int = 3
    BACKFILL_MAX_ATTEMPTS: int = 10
    # Cap embed input to the model's context window. Long memories (session
    # outcomes) otherwise exceed the embedding model's token limit and the
    # endpoint 400s ("input length exceeds the context length") — a non-retryable
    # error that leaves the memory permanently vector-less (backfill DLQ). 2000
    # chars is a safe margin under mxbai-embed-large's ~512-token context
    # (measured: it accepts ~2500 prose chars, rejects ~3000).
    EMBED_MAX_CHARS: int = 2000

    # Policy Engine
    POLICY_DENY_PATHS: str = ".env,*.key,*.pem,*.secret"  # Comma-separated glob patterns

    # Pattern Experiments (datasets + statistical framework)
    PATTERN_EXPERIMENTS_ENABLED: bool = False

    # Validation (promotion ladder + A/B tip effectiveness) needs session volume a
    # small team won't hit for months; freeze it until then. Detectors + the N=1
    # "observed" briefing surface are unaffected. See docs/STRATEGY.md.
    PATTERN_VALIDATION_ENABLED: bool = False

    # Agent Gateway
    AGENT_GATEWAY_ENABLED: bool = True
    AGENT_PREDICTION_CONFIDENCE_THRESHOLD: float = 0.6
    AGENT_RECONCILE_DEADLINE_SECONDS: int = 300
    AGENT_RETHINK_MAX_LOOPS: int = 3
    AGENT_FASTPATH_MIN_SAMPLES: int = 20
    AGENT_FASTPATH_MIN_SUCCESS_RATE: float = 0.9
    AGENT_FASTPATH_CACHE_TTL_SECONDS: int = 86400

    # Replay Engine (Redis DB 6)
    RP_REDIS_URL: str = "redis://localhost:6379/6"

    # Skill Synthesis
    BRIDGE_URL: str = "http://bridge:8070"
    # Server-side session -> skill synthesis is OFF by default: it needs an LLM to
    # generate a skill card, which the CPU-only default deploy can't do in a workable
    # time. Skills are authored CLIENT-SIDE by the agent (which has the context and a
    # capable model) via skill_create / POST /skills. Re-enable only on a deploy with
    # a fast LLM. (skill_recall, briefing injection, and manual skill_create are
    # unaffected — this only gates the ctx_complete_session(skill_worthy=True) job.)
    SKILL_SYNTHESIS_ENABLED: bool = False
    SKILL_SCORE_THRESHOLD: float = 0.6
    # Active skills unrecalled for this many days are flagged stale=True for
    # human review by the memory-agent staleness sweep (never deleted, never
    # status-changed). A re-recalled skill un-stales on the next run.
    SKILL_STALE_AFTER_DAYS: int = 90
    # Raw-cosine floor for SEMANTIC skill matching (GET /skills?q=, briefing skills
    # section). Deliberately NOT RECALL_SCORE_FLOOR (0.35, above): that was tuned for
    # prose memory bodies on mxbai-embed-large/1024-dim, while a skill embeds a terse
    # composite (trigger + symptoms + domain + steps + gotchas) and the office deploy
    # embeds with granite-embedding:30m/384-dim. Sweep against a real corpus after ship.
    # A too-high value cannot regress below the legacy behaviour: when nothing clears
    # the floor the matcher falls back to the pre-existing scroll + substring path.
    SKILL_MATCH_SCORE_FLOOR: float = 0.30
    # Bounds the query embed on the skill-match path. Sized to fit inside the
    # briefing's 2.0s per-section budget (briefing/api.py `_run_section`) with room for
    # the Qdrant round trip AND the scroll fallback — without it, a hung embeddings
    # backend costs 3 x 30s httpx timeouts plus backoff before failing.
    SKILL_MATCH_EMBED_TIMEOUT_SECONDS: float = 1.2
    SKILL_ERROR_DENSITY_WEIGHT: float = 0.30
    SKILL_ANOMALY_WEIGHT: float = 0.20
    SKILL_RESOLUTION_WEIGHT: float = 0.35
    SKILL_AGENT_SCHEDULE_HOURS: int = 6
    # LLM generation budget for skill synthesis (session + doc drafting). Sized for
    # CPU Ollama, where a qwen3:4b generation runs ~150-200s — the old hardcoded 60s
    # timed out every draft, so docs->skills never produced a skill. Lower on GPU.
    SKILL_SYNTH_TIMEOUT_SECONDS: float = 300.0

    # MCP Server
    FIREKEEP_API_URL: str = "http://localhost:8000"
    # Internal service key (nxs_, scoped memory:write/session:read/eval:read/eval:write —
    # NOT admin). Fallback identity for server-initiated proxy calls only;
    # caller keys are forwarded per-request (SP1a §4.2). Minted by
    # deploy/bootstrap-keys.sh. Replaces the retired FIREKEEP_API_KEY deputy key.
    FIREKEEP_INTERNAL_KEY: str | None = None
    MCP_HOST: str = "0.0.0.0"
    MCP_PORT: int = 8080
    MCP_CLIENT_TIMEOUT: float = 90.0  # Ollama CPU synthesis can take 30s+

    # Briefing aggregator upstreams (SP1b) — the GET /briefing router fans out
    # to Relay + Sentinel REST for the environment / tasks / bulletins sections.
    # All outbound calls attach internal_key_headers(FIREKEEP_INTERNAL_KEY).
    RELAY_URL: str = "http://relay:8050"
    SENTINEL_URL: str = "http://sentinel:8060"

    @field_validator("CONTENT_HASH_LENGTH")
    @classmethod
    def validate_hash_length(cls, v: int) -> int:
        if v < 16:
            raise ValueError("CONTENT_HASH_LENGTH must be >= 16 to avoid hash collisions")
        return v

    def model_post_init(self, __context):
        import logging as _logging
        _log = _logging.getLogger("app.config")
        if not self.NEO4J_PASSWORD:
            _log.warning("NEO4J_PASSWORD is empty — set via environment or .env")
        if not self.LLM_API_KEY:
            # Not a misconfiguration: local Ollama needs no key (every call
            # site sends no Authorization header when empty). Only hosted
            # OpenAI-compatible backends require one.
            _log.info(
                "LLM_API_KEY is empty — expected for local Ollama; required "
                "for hosted OpenAI-compatible backends"
            )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
