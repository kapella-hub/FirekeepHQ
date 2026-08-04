from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # API
    APP_NAME: str = "FirekeepCortex"
    DEBUG: bool = False
    CORS_ORIGINS: list[str] = ["http://localhost:3000", "http://localhost:8080"]
    RATE_LIMIT: str = "60/minute"
    ENROLL_TICKET_TTL_HOURS: int = 24
    ENROLL_TOMBSTONE_DAYS: int = 7
    ENROLL_KEY_EXPIRES_DAYS: int = 90
    ENROLL_RATE_LIMIT: str = "10/minute"
    ENROLL_MAX_ATTEMPTS_PER_HOUR: int = 60

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

    # Chat-endpoint selection (app/llm.py). Ollama honours `think:false` on its
    # NATIVE /api/chat and silently IGNORES it on /v1/chat/completions, so a
    # thinking model generates its full reasoning on /v1 no matter what is sent.
    # Measured 2026-08-04 on the VPS (ollama 0.32.4, qwen3:4b, 4 vCPU), same
    # document both ways: 83.19s on /v1 vs 4.00s native. See app/llm.py's
    # docstring for all five probes.
    #
    # `auto` derives a native root from LLM_BASE_URL (which must end in /v1) and
    # confirms it with one cached GET {root}/api/version; anything unconfirmed
    # falls back to /v1, so a vLLM/LiteLLM/OpenAI backend is unaffected.
    # `always` skips the probe, `never` disables the native path entirely — the
    # escape hatch if something non-ollama ever answers /api/version.
    #
    # NOT a substitute for this: repointing LLM_BASE_URL at http://host:11434.
    # Eleven chat sites concatenate `/chat/completions` onto it and THREE
    # EMBEDDING sites concatenate `/embeddings` (db/vector.py, workers/reembed.py),
    # so repointing breaks every memory write.
    LLM_NATIVE_CHAT: str = "auto"
    LLM_NATIVE_PROBE_TTL_SECONDS: float = 600.0
    # Only for a backend that speaks ollama's native API at a root this cannot
    # derive (LLM_BASE_URL not ending in /v1). Normally empty.
    LLM_NATIVE_BASE_URL: str = ""

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

    # Garbage Collection — ARCHIVE-FIRST. Defaults per docs/CONFIGURATION.md, which
    # survived the 2026-08-02 loss of app/ and is the authority for these values.
    MAX_MEMORY_AGE_DAYS: int = 180
    PRUNE_SCORE_THRESHOLD: float = 0.3
    GC_SCHEDULE_HOURS: int = 24
    GC_ENABLED: bool = True
    # Evaluate and audit without changing Qdrant or Neo4j.
    GC_DRY_RUN: bool = False
    # Recovery window recorded on GC-origin archives before they become purge-eligible.
    GC_ARCHIVE_GRACE_DAYS: int = 90
    # Hard deletion is OFF by default: it permits deleting only records the task
    # archived ITSELF whose grace window has elapsed — manual and legacy archives are
    # never guessed at. Also gates destructive Neo4j orphan cleanup.
    GC_PURGE_ENABLED: bool = False

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
    # Classify LLM call timeout, per endpoint. TWO values because the two
    # endpoints have genuinely different latency regimes and one number cannot
    # be both a safe ceiling for the slow one and a useful bound on the fast one.
    #
    # ..._TIMEOUT_SECONDS bounds /v1/chat/completions and STAYS 300. On a
    # thinking model /v1 generates the full reasoning regardless of flags: the
    # audit measured 288.9s against this 300s budget — 3.7% headroom — and the
    # VPS measured 83.19s on a smaller document. Cutting this to 120 would
    # convert today's slow successes into guaranteed timeouts on any deployment
    # the native path does NOT engage on (a vLLM/LiteLLM backend serving a
    # reasoning model), which is a regression the endpoint fix does not earn.
    #
    # ..._NATIVE_TIMEOUT_SECONDS bounds ollama's /api/chat, where `think:false`
    # is honoured. 120 is 30x the VPS measurement (4.00s) and ~2.1x the binding
    # constraint, which is NOT the VPS: the office deploy runs llama3.2:3b at
    # ~56s per classify, and llama3.2:3b is not a thinking model, so this fix
    # saves it nothing and its 56s stays 56s. 60s would look defensible from the
    # VPS number alone and would break the office deploy. The upper bound is the
    # --pool=solo worker: a 300s classify blocks sleep-cycle consolidation,
    # backfill drain, gateway sweep and dream-tick for five minutes, and
    # post-fix a 300s native classify can only mean something is already wrong.
    KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS: float = 300.0
    KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS: float = 120.0
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

    # Dreaming (app/dreams/) — automated consolidation + person profiles.
    # Opt-in: round 1 is additive (nothing is archived).
    DREAM_ENABLED: bool = False
    DREAM_TICK_MINUTES: int = 5
    DREAM_IDLE_MINUTES: int = 30
    DREAM_MIN_NEW_MEMORIES: int = 25
    DREAM_MIN_AGE_DAYS: int = 2
    DREAM_MIN_CLUSTER: int = 4
    DREAM_CLUSTER_THRESHOLD: float = 0.72
    DREAM_MAX_CLUSTERS_PER_RUN: int = 20
    DREAM_MAX_INSIGHT_CHARS: int = 800
    DREAM_OWM_FLOOR: float = 0.35
    # This is the ONLY timeout that actually binds on the dream tick. The
    # Celery worker runs --pool=solo (docker-compose.yml:437), and Celery's
    # solo pool silently IGNORES soft_time_limit/time_limit — they're
    # declared on the task for correctness under a future prefork pool, but
    # do nothing today. httpx's timeout, enforced inside synthesize()/
    # synthesize_profile(), is the real control. Measured real synthesis on
    # the production VPS is 22.5s (qwen3:4b, 4 vCPU) — 45s is 2x headroom;
    # the prior 120s was 5.3x with no measured basis. 45s also sits BELOW
    # this module's own documented failure mode: without think:false, qwen3
    # returns EMPTY after ~101s, which parses as JSONDecodeError and
    # triggers synthesize()'s one retry -> ~202s total. At 45s that
    # regression fails fast (returns [] with a WARNING) in well under a
    # minute instead of stalling the solo worker for ~3.5 minutes. Do not
    # raise time_limit to compensate for a bigger value here — it's a no-op
    # under --pool=solo, so it would look like a boost with no real effect.
    #
    # CAVEAT added after live validation (2026-08-04): the 22.5s measurement
    # above holds where think:false is HONOURED, i.e. ollama's native
    # /api/chat. On /v1/chat/completions — where LLM_BASE_URL points by
    # default — ollama IGNORES the flag, the reasoning runs anyway, and the
    # completion budget (synthesize._MAX_COMPLETION_TOKENS, raised to 4000 to
    # stop that reasoning starving the answer) has to be generated before any
    # JSON appears. On slow CPU inference that can exceed 45s, in which case
    # this timeout fires, synthesize() returns [] with a WARNING and the run
    # reports health="degraded" at GET /dreams. Raising this number is NOT the
    # blind fix: pointing LLM_BASE_URL at /api restores the measured 22.5s
    # path. Any increase here should carry its own measurement, as this 45s
    # does.
    DREAM_SYNTH_TIMEOUT_SECONDS: float = 45.0
    DREAM_LOCK_TTL_SECONDS: int = 1800
    DREAM_PROFILES_ENABLED: bool = True

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
    # LLM generation budget for skill synthesis (session + doc drafting).
    #
    # DELIBERATELY NOT REDUCED, and deliberately WITHOUT a tighter native
    # sibling — unlike KNOWLEDGE_CLASSIFY_* above. Skill drafting is
    # GENERATION-bound, not reasoning-bound, so the endpoint fix does not buy a
    # timeout reduction here the way it does for classify. Measured on the VPS
    # 2026-08-04 (qwen3:4b, 4 vCPU) on ollama's NATIVE endpoint with think:false
    # already in effect: generation runs at 5.9-7.2 tokens/sec, and the model
    # runs to the cap every time (done_reason=length at 300/400/500/800), so the
    # 800-token bound below costs 112-135s of pure output generation. A 120s
    # native budget was tried and FAILED ALL THREE DRAFTS of a real ingest at
    # exactly 120.13s each. 300 leaves ~2.2x over the worst observed 135s.
    #
    # A design estimate of "~25-45s native" for a card did not survive contact
    # with the hardware; it extrapolated from Dreaming's 22.5s, which is a
    # ~145-token JSON at this token rate — a fifth of a skill card. If you
    # reduce this, measure a real draft on YOUR hardware first: at ~6 tok/s the
    # floor is (SKILL_SYNTH_MAX_TOKENS / 6) seconds plus prompt eval.
    SKILL_SYNTH_TIMEOUT_SECONDS: float = 300.0
    # Output bound for a skill card. Both synthesis calls previously sent NO
    # limit at all, so a thinking model could generate reasoning until the
    # timeout with no card ever emitted. This is also the real cost control:
    # per the measurement above the model does NOT stop on its own, so this
    # number multiplied by the token rate IS the wall-clock cost of a draft.
    # 800 yields a complete card (all four sections) with the tail truncated;
    # lowering it lowers latency proportionally and truncates more.
    SKILL_SYNTH_MAX_TOKENS: int = 800

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
