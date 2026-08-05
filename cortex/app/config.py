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
    # is honoured. It exists as a SEPARATELY TUNABLE budget — an operator who
    # wants fail-fast on a known-fast backend can lower it — but it DEFAULTS TO
    # THE SAME 300, deliberately. It was 120 for one review cycle; that was
    # wrong, and the deployment it would have broken is the office one.
    #
    # The trap: "native" does not imply "fast". The native path is faster only
    # because it disables THINKING, so a non-thinking model gains nothing from
    # it while still being routed down it — the probe confirms ollama, not a
    # thinking model. The office deploy runs llama3.2:3b, which has no thinking
    # to disable, at a recorded ~56s per classify. Measured 2026-08-04, a
    # non-thinking model also ACCEPTS the flag cleanly rather than 4xx-ing, so
    # the demote-and-retry escape hatch never fires for it either:
    #     llama3:latest + think:false -> OK 3.10s, keys=['content','role']
    #     llama3:latest without flag  -> OK 0.36s, keys=['content','role']
    #     gemma3:4b     + think:false -> OK 1.94s, keys=['content','role']
    # So at 120 the office keeps its unchanged ~56s and loses headroom from
    # 5.4x to 2.1x, for zero speedup. `classify_document` sends the WHOLE
    # document untruncated and the crawler admits 2MB pages, so a document
    # ~2.2x the measured one would newly time out. The office helm chart (a
    # separate config repo) sets none of these vars, so it would have inherited
    # that with nobody deciding.
    #
    # Against that: a native classify measures ~6s (8.59s end-to-end through
    # the worker), so 120 vs 300 only changes how fast a BROKEN call gives up.
    # Tiny upside, real regression risk on a deployment we cannot measure.
    # If you lower this, lower it for a backend you have measured.
    KNOWLEDGE_CLASSIFY_TIMEOUT_SECONDS: float = 300.0
    KNOWLEDGE_CLASSIFY_NATIVE_TIMEOUT_SECONDS: float = 300.0
    KNOWLEDGE_STATUS_TTL_SECONDS: int = 2592000  # 30d orphan safety-net for per-source ingest status

    # URL ingestion (SSRF-guarded crawler -> knowledge pipeline)
    KNOWLEDGE_URL_INGEST_ENABLED: bool = True
    KNOWLEDGE_CRAWL_MAX_DEPTH: int = 2
    KNOWLEDGE_CRAWL_MAX_PAGES: int = 25
    KNOWLEDGE_CRAWL_TIMEOUT_SECONDS: float = 15.0
    KNOWLEDGE_CRAWL_MAX_PAGE_BYTES: int = 2_000_000

    # Decision Board (SP4)
    DECISION_ENABLED: bool = True
    # Bounds the best-effort suggestion LLM pass in app/decision/synthesize.py.
    # Retrieval runs BEFORE this call and outside its try/except, so evidence
    # and knowledge_found are returned whatever it does; only
    # suggested_answers/suggested_actions are at stake.
    #
    # 20.0 -> 30.0 (2026-08-04, LLM endpoint phase 2). Three reasons, none taste:
    #
    # 1. THE CLIENT ALREADY ASSUMED 30. client/firekeep_client/decision/server.py
    #    sets `_DEFAULT_SYNTH_TIMEOUT = 30.0` under the comment "Kept env-tunable
    #    to mirror the server default". It did not mirror it; the two processes
    #    have disagreed since SP4 shipped.
    # 2. 30 IS ALSO THE CLIENT'S CEILING. That same file derives its HTTP timeout
    #    for POST /decision/synthesize as synth + _INGEST_TIMEOUT_HEADROOM (15.0)
    #    = 45s, so this endpoint must answer inside 45s or be hung up on. Going
    #    past 30 needs a coordinated client release, not an env change.
    #
    #    Be clear about what the raise COSTS, because the 45s is fixed: the
    #    margin left for everything outside this budget — the up-to-9 recalls —
    #    went 25s -> 15s. That is the right trade (at 20 the LLM pass never
    #    completed at all, so the 25s bought nothing), but it is a REDUCTION,
    #    not the restoration it can read as. What lives in that margin: the
    #    recalls issue `ContextQuery(format="raw")`, which `RAGEngine.recall`
    #    uses to skip the synthesis LLM pass entirely (engine/rag.py:302), so
    #    there is no GENERATION on that path — but there IS one embed per recall
    #    (`VectorClient._embed` -> `POST {LLM_BASE_URL}/embeddings`), LRU-cached
    #    by content hash and therefore ALWAYS COLD for distinct question texts.
    #    Sub-second each on this hardware, not free, and nobody has timed nine of
    #    them; 15s absorbs them with room, and that is the honest claim.
    #    OPS NOTE: `RERANK_ENABLED=true` adds `top_k x
    #    RERANK_CANDIDATES_MULTIPLIER` LLM calls per recall, times 9 questions.
    #    That already blew through the old 25s, so it is not a new regression —
    #    but the margin is thinner now. Do not enable it on a CPU backend and
    #    expect this endpoint to answer inside the client's ceiling.
    # 3. 20 WAS BELOW THE FLOOR EVEN FOR THE FAST PATH. Native generation on the
    #    VPS measured 5.9-7.2 tok/s (see SKILL_SYNTH_TIMEOUT_SECONDS below), so
    #    20s buys ~120-145 output tokens — less than a suggestion JSON for three
    #    questions. On `/v1` it never had a chance at all: ollama ignores
    #    `think:false` there, so a thinking model generates its full reasoning
    #    first (83.19s on a comparable call, app/llm.py probe E).
    #
    # DELIBERATELY NO NATIVE SIBLING, unlike KNOWLEDGE_CLASSIFY_* above. The
    # asymmetry runs the wrong way here: a native budget could only be LOWER
    # than this one, and phase 1 measured that lowering the native budget
    # strands non-thinking-model deploys — the probe confirms ollama, not a
    # thinking model, so such a backend takes the native path and gains nothing
    # from `think:false`. Raising the /v1 budget is capped by the client at 45s.
    # One number for both endpoints.
    #
    # HONEST CEILING — measured live on the VPS 2026-08-04 (qwen3:4b, native
    # /api/chat with think:false), not extrapolated:
    #     1 question    20.98s    57 output tokens    FITS
    #     3 questions   16.31s   111 output tokens    FITS
    #     8 questions   37.28s   239 output tokens    EXCEEDS the 30s budget
    # So a small board now gets suggestions and a FULL board still degrades to
    # retrieval-only. The binding constraint is WALL CLOCK at ~6.5 tok/s, not
    # output size: 239 tokens is small — an earlier estimate of ~400-550 was
    # high — and still costs 37s. Read these as three data points, not a
    # function of question count: the 1-question run is SLOWER than the
    # 3-question one, so per-call overhead and warmness dominate at this size.
    # The remaining lever is therefore the PROMPT (cap suggestions per question
    # and their length, which cuts tokens actually generated), not a larger
    # timeout the client will cut off and not `max_tokens`, which in JSON mode
    # can only truncate valid output into invalid — see decision/synthesize.py.
    DECISION_SYNTH_TIMEOUT_SECONDS: float = 30.0
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
    # How many of a cluster's members are actually SENT to the model for one
    # cluster synthesis. This caps the PROMPT, not the cluster: the resulting
    # dream still covers the whole cluster, and store.build_dream_payload
    # records both numbers (dream_cluster_size / dream_sampled_count) so a
    # reader can tell "summarised from 5 of 23" from "5 of 5".
    #
    # Before this existed there was no cap at all, and on the live production
    # store that made most of the pass unrunnable. Measured on the VPS
    # 2026-08-04 (qwen3:4b, 4 vCPU, native /api/chat, budget
    # DREAM_SYNTH_TIMEOUT_SECONDS=45s) against 526 real candidates forming 20
    # clusters of sizes [23,16,10,10,10,9,8,8,7,6,6,5]:
    #
    # a single probe per size gave 38.1s (4 members), 52.0s (5), 65.9s (10) and
    # a timeout on the 23-member cluster the real tick actually hit — the last
    # of which is why dreaming wrote zero insights in production.
    #
    # DO NOT TUNE THIS OFF SINGLE PROBES. Re-measuring the SAME capped prompt
    # three times each showed the box's latency is dominated by variance, not
    # by prompt size:
    #
    #     cap  prompt chars   runs (s)            median   worst   vs 45s
    #      4      3,159       42.9, 16.8, 12.0     16.8s   42.9s   fits
    #      5      3,764       35.8, 29.8, 30.3     30.3s   35.8s   fits
    #      6      4,369       35.8, 25.1, 31.3     31.3s   35.8s   fits
    #
    # Identical input at cap=4 spanned 12.0-42.9s, a 3.6x spread. So the
    # single-probe table above measured scheduling noise on a shared 4-vCPU box
    # as much as it measured tokens, and its apparent "5 EXCEEDS, 6 fits at 93%
    # utilisation" ordering does not survive repetition. What DOES survive: an
    # uncapped 23-member cluster never completes, and every cap in 4..6 does.
    #
    # Capping also makes the OUTPUT BETTER, which is the durable finding. Same
    # 23-member cluster: capped to 4 -> 1 insight; capped to 6 -> 3 specific,
    # usable insights; uncapped -> 0. Cluster members are near-duplicates BY
    # CONSTRUCTION (cosine >= DREAM_CLUSTER_THRESHOLD), so members past the
    # first handful are redundant tokens that dilute the signal and eat the
    # generation budget that writing the answer needs.
    #
    # 5 is chosen for headroom under that variance, not for a winning time: it
    # is >= the 2 episodes the prompt requires per insight, produced multiple
    # insights in the quality probe, and its worst observed run (35.8s) leaves
    # ~20% of the budget spare on an otherwise-idle box. 4 has a lower median
    # but yielded a single hedged generality; 6 is not measurably slower than 5
    # and is a defensible alternative on a machine that has been measured.
    #
    # <= 0 means "no cap" (send the whole cluster) — the pre-change behaviour,
    # available for a fast-GPU deploy that measured it. Values below 2 are
    # legal but pointless: the system prompt requires each insight to be
    # supported by at least 2 episodes.
    DREAM_MAX_CLUSTER_MEMBERS_PER_SYNTHESIS: int = 5
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
    # /api/chat. On /v1/chat/completions ollama IGNORES the flag, the reasoning
    # runs anyway, and the completion budget (synthesize._MAX_COMPLETION_TOKENS,
    # raised to 4000 to stop that reasoning starving the answer) has to be
    # generated before any JSON appears. Measured that day on the VPS with the
    # SMALLEST cluster the pass ever attempts (4 members, 2,595-char prompt):
    # >400s on /v1 without completing, versus 22.5s native. Against this 45s
    # budget that is not slow, it is inoperable — and it was the live state,
    # because synthesize() built its own /v1 body.
    #
    # RESOLVED for BOTH dreams LLM calls: synthesize() and synthesize_profile()
    # now call app/llm.py's chat(), which selects the native endpoint whenever
    # the backend confirms as ollama and falls back to /v1 otherwise. This value
    # is passed straight through as each call's budget, for BOTH endpoints —
    # there is deliberately no native sibling (see synthesize()'s docstring: a
    # lower native budget strands a non-thinking-model ollama deploy, which
    # takes the native path and gains nothing from think:false).
    #
    # The /v1 regime above is therefore no longer the live state, but it is not
    # gone: any backend that does not confirm as ollama, and any ollama demoted
    # by a pre-generation 4xx, still lands there — where this timeout can fire,
    # synthesize()/synthesize_profile() return []/None, and the run reports
    # health="degraded" at GET /dreams. The remedy for such a deploy is NOT
    # repointing LLM_BASE_URL at /api: three embedding call sites concatenate
    # /embeddings onto that same variable, so it would break every memory write.
    # Any increase here should carry its own measurement, as this 45s does.
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
