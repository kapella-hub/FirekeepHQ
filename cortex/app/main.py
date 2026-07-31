"""FirekeepCortex FastAPI application.

Memory-as-a-Service layer providing persistent cognitive memory for LLM agents.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
import redis.asyncio
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.contradiction import detect_and_supersede
from app.dashboard import create_dashboard_router
from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.embedding_admin import create_embedding_router
from app.engine.rag import RAGEngine
from app.lifecycle import create_lifecycle_router
from app.ops import create_ops_router
from app.exceptions import (
    GraphConnectionError,
    LLMExtractionError,
    FirekeepCortexError,
    StreamIngestionError,
    VectorStoreError,
)
from app.models import (
    ActionLog,
    ContextQuery,
    ErrorDetail,
    FeedbackRequest,
    FeedbackResponse,
    GenericEventIngest,
    HandoffRequest,
    HealthResponse,
    LearnResponse,
    RecallResponse,
    ServiceStatus,
    StreamResponse,
)
from app.engine.rag import synthesize_memories
from app.stats import create_stats_router
from app.streaming import create_streaming_router
from app.transfer import create_transfer_router
from app.version import get_version_info, VERSION
from app.webhooks import create_webhook_router, fire_webhooks

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 100

# ---------------------------------------------------------------------------
# Replay emitter (fire-and-forget trace events)
# ---------------------------------------------------------------------------

_replay_initialized = False

# One-time warning flag for the session_touched_check stub
_session_touched_check_warned = False


async def _ensure_replay() -> None:
    global _replay_initialized
    if _replay_initialized:
        return
    _replay_initialized = True
    try:
        from replay.emitter import init_emitter
        await init_emitter()
        logger.info("Replay emitter initialized for Cortex")
    except Exception as exc:
        logger.warning("Replay emitter init FAILED: %s", exc)


async def _replay_emit(event_type: str, session_id: str, agent_id: str, payload: dict, **kwargs) -> None:
    try:
        await _ensure_replay()
        from replay.emitter import emit
        await emit(event_type, session_id, agent_id, payload, **kwargs)
    except Exception as exc:
        logger.warning("Replay emit failed for %s: %s", event_type, exc)


async def _bump_untagged_counter(redis_client: redis.asyncio.Redis, session_id: str) -> None:
    """Increment the daily untagged-call counter when session_id is missing or 'unknown'.

    Surfaces session_id discipline failures via the briefing without reversing
    the multi-tenant guard in mcp_server._resolve_identity.
    """
    if session_id and session_id != "unknown":
        return
    try:
        key = f"cortex:untagged_calls:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        await redis_client.incr(key)
        await redis_client.expire(key, 86400 * 3)  # 3-day retention
    except Exception as exc:
        logger.warning("Untagged counter bump failed: %s", exc)


MAX_REQUEST_BODY_BYTES = 10 * 1024 * 1024  # 10 MB

# Health check cache (10-second TTL to reduce backend probing)
_health_cache: HealthResponse | None = None
_health_cache_time: datetime | None = None
_HEALTH_CACHE_TTL_SECONDS = 10.0
_health_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


_ENABLE_AUTH_HINT = (
    " To enable: set AUTH_ENABLED=true in .env, restart, and send the admin "
    "key as X-API-Key. deploy/bootstrap-keys.sh (run by install.sh/update.sh) "
    "mints that key and prints it exactly once."
)


def _admin_surface_disabled_router(prefix: str, tag: str, reason: str):
    """A stand-in router that 503s every path under `prefix` with `reason`.

    Mounted in place of an admin-only router when auth is disabled. It exists
    so the refusal is SELF-EXPLANATORY: a bare 404 on /vault/secrets looks like
    a broken deployment and sends the operator hunting through logs, whereas
    this says which setting turned the surface off and what to do about it.
    """
    from fastapi import APIRouter

    router = APIRouter(prefix=prefix, tags=[tag])

    @router.api_route(
        "/{_unused:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def _refuse(_unused: str) -> None:
        raise HTTPException(status_code=503, detail=reason + _ENABLE_AUTH_HINT)

    return router


def _register_admin_surface_routers(app: FastAPI) -> None:
    """Mount /auth/* and /vault/* — but only when auth enforcement is on.

    DEFENCE IN DEPTH (audit blocker 7). Both routers are admin-only via
    require_scope("admin"), and with auth off no caller can hold that scope any
    more (auth/keys.py ANONYMOUS_SCOPES). That alone closes the hole. Refusing
    to mount them as well closes it a SECOND time, so a regression in the
    anonymous scope set — the exact regression that leaked 12 real secrets —
    cannot by itself re-expose decrypted secret reads and API-key minting.

    Enabled-ness comes from AuthSettings (env var AUTH_ENABLED), the same truth
    build_auth_middleware reads — NOT keys._AUTH_ENABLED, which is process
    state only init_auth() writes. The two agree in production (the lifespan
    derives its init_auth(enabled=...) argument from this same settings object,
    a few lines earlier), but AuthSettings is the one that answers correctly for
    a caller that has not run the lifespan at all.

    Split out of _register_feature_routers so the branch is directly callable:
    a test can mount it on a bare app and observe what it actually serves,
    rather than asserting about the source text.
    """
    from auth.config import get_auth_settings as _auth_cfg

    if _auth_cfg().ENABLED:
        # Auth endpoints (/auth/*) — init happens in lifespan (async), registration here (sync)
        # Both mounts below fall back to the SAME 503 stand-in the disabled
        # branch uses, rather than logging a warning and moving on.
        #
        # The old behaviour left a bare 404 on every /auth/* and /vault/* path
        # if the router failed to construct — indistinguishable from a typo, a
        # wrong port, or an old build, with the only explanation in a log line
        # nobody reads until they already suspect the answer. Meanwhile the
        # auth-OFF branch below went to real trouble to explain itself. The
        # default is now auth-ON, so the branch with no legibility became the
        # one nearly everyone runs.
        #
        # Not a hard raise: a broken vault must not take the whole API down
        # when memory, sessions and coordination are unaffected. Fail loud on
        # the surface that is actually missing, and only there.
        try:
            from auth.api import create_auth_router
            app.include_router(create_auth_router())
            logger.info("Auth router registered at /auth/*")
        except Exception as exc:
            logger.error("Auth router FAILED to register: %s", exc, exc_info=True)
            app.include_router(_admin_surface_disabled_router(
                "/auth", "auth",
                f"API-key management (/auth/*) failed to start: {exc}. This is a "
                f"fault, not a configuration choice — auth enforcement IS enabled. "
                f"Check the cortex-api logs for the traceback.",
            ))

        # Vault endpoints (/vault/*)
        try:
            from vault.api import create_vault_router
            app.include_router(create_vault_router())
            logger.info("Vault router registered at /vault/*")
        except Exception as exc:
            logger.error("Vault router FAILED to register: %s", exc, exc_info=True)
            app.include_router(_admin_surface_disabled_router(
                "/vault", "vault",
                f"The secrets vault (/vault/*) failed to start: {exc}. This is a "
                f"fault, not a configuration choice — auth enforcement IS enabled. "
                f"Check the cortex-api logs for the traceback.",
            ))
        return

    app.include_router(_admin_surface_disabled_router(
        "/auth", "auth",
        "API-key management (/auth/*) is disabled because auth enforcement is "
        "off (AUTH_ENABLED=false). Minting and listing keys is an admin "
        "operation, and with no auth there is no admin — serving it here would "
        "hand key creation to any caller who can reach this port.",
    ))
    app.include_router(_admin_surface_disabled_router(
        "/vault", "vault",
        "The secrets vault (/vault/*) is disabled because auth enforcement is "
        "off (AUTH_ENABLED=false). These routes return DECRYPTED secret values "
        "and are admin-only; with no auth there is no admin.",
    ))
    logger.warning(
        "AUTH_ENABLED=false — /auth/* and /vault/* are NOT served (they are "
        "admin-only, and with no auth there is no admin). Set AUTH_ENABLED=true "
        "and restart to use them."
    )


def _register_feature_routers(app: FastAPI) -> None:
    """Register all feature routers with the app. Called during lifespan startup."""
    # Enrollment must exist even when AUTH_ENABLED=false so it can explain why
    # no credential can be issued. Only its exact public redeem/anchor paths
    # bypass the global key middleware; invite management remains admin-scoped.
    from app.enroll.api import create_enroll_router
    app.include_router(create_enroll_router(limiter=limiter))

    app.include_router(create_dashboard_router(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
        redis_client=app.state.redis_client,
    ))
    app.include_router(create_webhook_router(app.state.redis_client))
    app.include_router(create_stats_router(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
        redis_client=app.state.redis_client,
    ))
    app.include_router(create_transfer_router(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
    ))
    app.include_router(create_streaming_router(
        rag_engine=app.state.rag_engine,
        graph=app.state.graph_client,
        vector=app.state.vector_client,
    ))
    app.include_router(create_embedding_router(app.state.vector_client))
    app.include_router(create_lifecycle_router(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
    ))
    app.include_router(create_ops_router())

    _register_admin_surface_routers(app)

    # Corpus — Business Knowledge Graph
    try:
        from corpus.api import create_corpus_router
        app.include_router(create_corpus_router())
        logger.info("Corpus router registered at /corpus/*")
    except Exception as exc:
        logger.warning("Corpus router not registered (non-critical): %s", exc)

    # Knowledge Ingestion — docs->skills orchestration (SP2)
    if get_settings().KNOWLEDGE_ENABLED:
        try:
            from app.knowledge.api import create_knowledge_router
            app.include_router(create_knowledge_router())
            logger.info("Knowledge router registered at /knowledge/*")
        except Exception as exc:
            logger.warning("Knowledge router not registered (non-critical): %s", exc)
    else:
        logger.info("Knowledge router disabled (KNOWLEDGE_ENABLED=false)")

    # Decision Board — global-knowledge board homework (SP4)
    if get_settings().DECISION_ENABLED:
        try:
            from app.decision.api import create_decision_router
            app.include_router(create_decision_router())
            logger.info("Decision router registered at /decision")
        except Exception as exc:
            logger.warning("Decision router not registered (non-critical): %s", exc)
    else:
        logger.info("Decision router disabled (DECISION_ENABLED=false)")

    # Collectors — living-knowledge-sync status API (SP3)
    if get_settings().COLLECTORS_ENABLED:
        from app.collectors.api import create_collectors_router
        app.include_router(create_collectors_router())
        logger.info("Collectors router registered at /collectors")

    # Audit endpoints (/audit/*)
    try:
        from app.audit import get_memory_audit, get_memory_access_summary
        from fastapi import APIRouter as _AR, Query as _Q

        audit_router = _AR(prefix="/audit", tags=["audit"])

        @audit_router.get("/memory")
        async def audit_memory(
            action: str | None = _Q(default=None),
            memory_chain_id: str | None = _Q(default=None),
            agent_id: str | None = _Q(default=None),
            namespace: str | None = _Q(default=None),
            limit: int = _Q(default=50, ge=1, le=200),
        ):
            return {"events": await get_memory_audit(
                app.state.replay_redis, action=action,
                memory_chain_id=memory_chain_id, agent_id=agent_id,
                namespace=namespace, limit=limit,
            )}

        @audit_router.get("/memory/summary")
        async def audit_memory_summary():
            return await get_memory_access_summary(app.state.replay_redis)

        app.include_router(audit_router)
        logger.info("Audit router registered at /audit/*")
    except Exception as exc:
        logger.warning("Audit router not registered (non-critical): %s", exc)

    # Auto-Evals endpoints (/evals/*)
    try:
        from app.evals.api import create_evals_router

        def _get_replay_redis_for_evals():
            return app.state.replay_redis

        app.include_router(create_evals_router(_get_replay_redis_for_evals))
        logger.info("Evals router registered at /evals/*")
    except Exception as exc:
        logger.warning("Evals router not registered (non-critical): %s", exc)

    # Pattern Engine endpoints (/patterns/*)
    try:
        from app.patterns.api import create_patterns_router

        def _get_replay_redis_for_patterns():
            return app.state.replay_redis

        app.include_router(create_patterns_router(_get_replay_redis_for_patterns))
        logger.info("Patterns router registered at /patterns/*")
    except Exception as exc:
        logger.warning("Patterns router not registered: %s", exc)

    # Policy Engine endpoints (/policy/*)
    try:
        from app.policy.api import create_policy_router
        from app.policy.engine import PolicyEngine
        from app.policy.rules import (
            FileRiskRule, LeaseRule, PathDenyRule,
            PredictionConfidenceRule, RecentFailureRule, SessionHealthRule,
        )

        settings = get_settings()
        deny_patterns = [
            p.strip() for p in getattr(settings, "POLICY_DENY_PATHS", "").split(",") if p.strip()
        ] if getattr(settings, "POLICY_DENY_PATHS", "") else []

        def _get_replay_redis_for_policy():
            return app.state.replay_redis

        policy_engine = PolicyEngine(rules=[
            LeaseRule(),
            PathDenyRule(deny_patterns=deny_patterns),
            FileRiskRule(get_replay_redis=_get_replay_redis_for_policy),
            SessionHealthRule(get_replay_redis=_get_replay_redis_for_policy),
            RecentFailureRule(get_replay_redis=_get_replay_redis_for_policy),
            PredictionConfidenceRule(
                threshold=settings.AGENT_PREDICTION_CONFIDENCE_THRESHOLD,
            ),
        ])
        app.state.policy_engine = policy_engine

        def _get_gateway_service_for_policy():
            # Lazily resolve the gateway service at request time so that the
            # policy router can be registered before the gateway block runs.
            # The gateway block monkey-patches get_agent_gateway_service on the
            # module, so importing + calling it here always gets the live instance.
            try:
                import app.agent_gateway.service as _gw
                return _gw.get_agent_gateway_service()
            except Exception:
                return None

        app.include_router(create_policy_router(
            get_engine=lambda: app.state.policy_engine,
            get_gateway_service=_get_gateway_service_for_policy,
            get_decision_redis=lambda: getattr(app.state, "redis_client", None),
        ))
        logger.info("Policy router registered at /policy/*")
    except Exception as exc:
        logger.warning("Policy router not registered: %s", exc)

    # Agent Gateway endpoints (/agent/action/*)
    try:
        from app.agent_gateway.api import create_agent_gateway_router
        from app.agent_gateway.service import AgentGatewayService, RethinkCounter
        import app.agent_gateway.service as _gw_module

        # _recent_failure_check: reads pattern features from the same store that
        # RecentFailureRule uses so tier classification correctly elevates targets
        # with recent failures to full tier (rather than relying on the policy
        # engine to catch them after tier classification).
        async def _recent_failure_check(target: str) -> bool:
            """Return True if the target has recent failures in the pattern store."""
            try:
                from app.patterns.store import get_all_features
                r = getattr(app.state, "replay_redis", None) or app.state.redis_client
                features = await get_all_features(r, limit=20)
                if not features:
                    return False
                target_norm = target.replace("\\", "/")
                failure_count = 0
                total_with_file = 0
                for f in features:
                    file_match = any(
                        target_norm.endswith(fp.replace("\\", "/")) or fp.replace("\\", "/").endswith(target_norm)
                        for fp in f.file_paths
                    )
                    if file_match:
                        total_with_file += 1
                        if f.outcome == "failure":
                            failure_count += 1
                # Threshold: 3+ matching sessions with ≥50% failure rate
                return total_with_file >= 3 and failure_count / total_with_file >= 0.5
            except Exception as exc:
                logger.debug("recent_failure_check error: %s", exc)
                return False

        # Use a shared Redis client for fastpath reads in decide() and writes in record().
        def _get_fastpath_redis():
            r = getattr(app.state, "replay_redis", None)
            if r is not None:
                return r
            return app.state.redis_client

        _fastpath_redis_client = _get_fastpath_redis()

        from app.agent_gateway.fastpath import check_fastpath as _check_fastpath_fn

        async def _fastpath_check(agent_id: str, action_type: str, target: str) -> bool:
            return await _check_fastpath_fn(_fastpath_redis_client, agent_id, action_type, target)

        async def _session_touched_check(session_id: str, target: str) -> bool:
            """Stub: full session-touch tracking is deferred.

            Returns False; meaning we never demote to auto via this signal.
            """
            global _session_touched_check_warned
            if not _session_touched_check_warned:
                logger.info(
                    "session_touched_check is a stub — tier demotion via clean session touch is not yet active"
                )
                _session_touched_check_warned = True
            return False

        # Reuse the module-level _replay_emit already defined in this file.
        # It is best-effort (swallows exceptions) and wraps replay.emitter.emit.

        # Use app.state.replay_redis if available; fall back to main Redis client.
        # RethinkCounter only needs basic .incr/.expire/.delete.
        def _get_rethink_redis():
            r = getattr(app.state, "replay_redis", None)
            if r is not None:
                return r
            return app.state.redis_client

        _rethink_counter = RethinkCounter(_get_rethink_redis())

        # policy_engine is constructed in the block above and stored on app.state.
        # We reference it via app.state so the gateway always gets the live instance.
        _gateway_service = AgentGatewayService(
            policy_engine=app.state.policy_engine,
            recent_failure_check=_recent_failure_check,
            fastpath_check=_fastpath_check,
            session_touched_check=_session_touched_check,
            replay_emitter=_replay_emit,
            rethink_counter=_rethink_counter,
            prediction_redis=_get_rethink_redis(),  # Redis-backed prediction store (cross-process safe)
            fastpath_redis=_fastpath_redis_client,
            policy_decision_redis=app.state.redis_client,  # audit log of block/rethink decisions
        )

        # Override module-level DI hook so router resolves to real service.
        _gw_module.get_agent_gateway_service = lambda: _gateway_service

        app.include_router(
            create_agent_gateway_router(get_service=_gw_module.get_agent_gateway_service)
        )
        logger.info("Agent Gateway router registered with real service")
    except Exception as exc:
        logger.warning("Agent Gateway router not registered: %s", exc)

    # Replay Engine query endpoints (/replay/*)
    try:
        from replay.api import create_replay_router

        def _get_replay_redis():
            return app.state.replay_redis

        app.include_router(create_replay_router(_get_replay_redis))
        logger.info("Replay router registered at /replay/*")
    except Exception as exc:
        logger.warning("Replay router not registered (non-critical): %s", exc)

    # Skill CRUD endpoints (/skills/*, /skill/evaluate)
    try:
        from app.skills.api import create_skills_router
        app.include_router(create_skills_router())
        logger.info("Skills router registered at /skills/*")
    except Exception as exc:
        logger.warning("Skills router not registered: %s", exc)

    # Pre-flight briefing aggregator (/briefing) — SP1b-server
    try:
        from app.briefing.api import create_briefing_router
        app.include_router(create_briefing_router())
        logger.info("Briefing router registered at /briefing")
    except Exception as exc:
        logger.warning("Briefing router not registered (non-critical): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    app.state.graph_client = Neo4jClient(settings)
    app.state.vector_client = VectorClient(settings)
    app.state.redis_client = redis.asyncio.from_url(settings.REDIS_URL)
    app.state.http_client = httpx.AsyncClient(timeout=15.0)
    app.state.start_time = datetime.now(timezone.utc)

    await app.state.graph_client.connect()
    await app.state.graph_client.ensure_indexes()
    await app.state.vector_client.initialize()

    app.state.rag_engine = RAGEngine(
        graph=app.state.graph_client,
        vector=app.state.vector_client,
        settings=settings,
        http_client=app.state.http_client,
    )

    # Replay Redis client (DB 6) — separate from main Cortex Redis (DB 0)
    try:
        from replay.config import get_replay_settings
        replay_settings = get_replay_settings()
        app.state.replay_redis = redis.asyncio.from_url(
            replay_settings.REDIS_URL, decode_responses=True,
        )
        logger.info("Replay Redis connected (DB 6)")
    except Exception as exc:
        app.state.replay_redis = None
        logger.warning("Replay Redis not available (non-critical): %s", exc)

    # Auth system initialization (async).
    # Reliability Principle (SP1a §2): when AUTH_ENABLED=true an init failure
    # is FATAL-LOUD — silently falling back to pass-through would leave the
    # API open while the operator believes auth is on. No try/except here.
    from auth.config import get_auth_settings
    from auth.middleware import init_auth

    auth_settings = get_auth_settings()
    if auth_settings.ENABLED:
        app.state.auth_redis = redis.asyncio.from_url(
            auth_settings.REDIS_URL, decode_responses=True,
        )
        await init_auth(redis_client=app.state.auth_redis, enabled=True)
        logger.info("Auth initialized (enabled=True)")
    else:
        app.state.auth_redis = None
        await init_auth(enabled=False)
        logger.info("Auth initialized (enabled=False)")

    # Vault initialization (shares Redis DB 7 with auth)
    app.state.vault_redis = None
    try:
        from vault.config import get_vault_settings
        from vault.store import init_vault

        vault_settings = get_vault_settings()
        if vault_settings.ENABLED and vault_settings.KEY:
            if app.state.auth_redis is not None:
                app.state.vault_redis = app.state.auth_redis
            else:
                app.state.vault_redis = redis.asyncio.from_url(
                    vault_settings.REDIS_URL, decode_responses=True,
                )
            init_vault(app.state.vault_redis, vault_settings.KEY)
            logger.info("Vault initialized (encrypted storage on Redis DB 7)")
        else:
            logger.info("Vault disabled or VAULT_KEY not set")
    except Exception as exc:
        logger.warning("Vault init failed (non-critical): %s", exc)

    # Initialize corpus module (Qdrant chunks + Redis source tracking)
    settings = get_settings()
    if settings.CORPUS_ENABLED:
        try:
            from corpus.pipeline import ingest_document as _ingest_doc
            from corpus.store import list_sources as _list_sources
            from corpus.store import delete_source as _delete_source
            import corpus.api as corpus_api

            _vector = app.state.vector_client
            _redis = app.state.redis_client

            async def _do_ingest(content, source_name, source_type):
                return await _ingest_doc(
                    content=content,
                    source_name=source_name,
                    source_type=source_type,
                    vector_client=_vector,
                    redis_client=_redis,
                )

            async def _do_sources():
                return await _list_sources(
                    redis_client=_redis,
                )

            async def _do_delete(source_name):
                return await _delete_source(
                    source_name=source_name,
                    vector_client=_vector,
                    redis_client=_redis,
                )

            corpus_api.ingest_document = _do_ingest
            corpus_api.get_corpus_sources = _do_sources
            corpus_api.delete_corpus_source = _do_delete

            logger.info("Corpus module initialized (Qdrant chunks + Redis source tracking)")
        except Exception as exc:
            logger.warning("Corpus init failed (non-critical): %s", exc)

    _register_feature_routers(app)

    yield

    # Cleanup
    if getattr(app.state, "replay_redis", None):
        await app.state.replay_redis.aclose()
    if getattr(app.state, "auth_redis", None):
        await app.state.auth_redis.aclose()
    vault_redis = getattr(app.state, "vault_redis", None)
    if vault_redis is not None and vault_redis is not getattr(app.state, "auth_redis", None):
        await vault_redis.aclose()

    await app.state.http_client.aclose()
    await app.state.vector_client.close()
    await app.state.graph_client.close()
    await app.state.redis_client.aclose()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="FirekeepCortex",
    description="Memory-as-a-Service for LLM agents",
    version=VERSION,
    lifespan=lifespan,
)

app.state.limiter = limiter


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class _BodyTooLargeError(Exception):
    """Internal sentinel raised when chunked request body exceeds the limit."""


class RequestBodySizeLimitMiddleware:
    """Reject requests exceeding MAX_REQUEST_BODY_BYTES.

    Checks Content-Length header upfront and also tracks actual bytes
    received to guard against chunked-encoding bypass.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            content_length = headers.get(b"content-length")
            if content_length is not None and int(content_length) > self.max_bytes:
                response = JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
                await response(scope, receive, send)
                return

            # Track actual bytes for chunked/streaming requests
            total_bytes = 0
            max_bytes = self.max_bytes

            async def receive_with_limit() -> dict:
                nonlocal total_bytes
                message = await receive()
                if message.get("type") == "http.request":
                    body = message.get("body", b"")
                    total_bytes += len(body)
                    if total_bytes > max_bytes:
                        raise _BodyTooLargeError()
                return message

            try:
                await self.app(scope, receive_with_limit, send)
            except _BodyTooLargeError:
                try:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )
                    await response(scope, receive, send)
                except Exception:
                    pass
            return
        await self.app(scope, receive, send)


class RequestIDMiddleware:
    """Inject a unique X-Request-Id into each request and response.

    If the client provides an X-Request-Id header, it is preserved.
    Otherwise, a new UUID is generated.
    The ID is stored in request scope for use by exception handlers.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        existing_id = headers.get(b"x-request-id", b"").decode("utf-8", errors="replace")
        # Sanitize client-supplied request IDs: allow only safe chars, max 128 chars
        if existing_id and len(existing_id) <= 128 and all(
            c.isalnum() or c in "-_." for c in existing_id
        ):
            request_id = existing_id
        else:
            request_id = str(uuid.uuid4())

        # Store in scope for downstream access
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_with_request_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("utf-8")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_request_id)


def _get_request_id(request: Request) -> str | None:
    """Extract request ID from request state, with fallback."""
    try:
        return request.state.request_id
    except AttributeError:
        return None


# Middleware runs OUTERMOST-first, but add_middleware() PREPENDS — so the LAST
# middleware registered below is the OUTERMOST at runtime. This block is
# therefore written in REVERSE of execution order.
#
# CORS MUST be outermost: a cross-origin preflight is an OPTIONS request that
# browsers send WITHOUT the X-API-Key header. If auth wrapped CORS, that
# preflight would be 401'd before CORS could answer it — blocking every
# cross-origin browser client even when it holds a valid key. With CORS
# outermost, the preflight short-circuits at CORS; real (non-OPTIONS) requests
# still fall through to the auth gate, and auth's own 401/503 responses get
# CORS headers attached on the way out.
#
# Execution order (outer -> inner): CORS -> RequestID -> FirekeepKeyAuth -> RequestBodySize -> App
from auth.asgi import FirekeepKeyAuthMiddleware
from auth.config import get_auth_settings as _get_auth_settings

app.add_middleware(RequestBodySizeLimitMiddleware)

# Auth skip list, split into prefix vs. exact matches (auth/asgi.py:
# skip_paths does path.startswith(prefix); skip_exact_paths matches the
# literal path only). "/dashboard" MUST be exact, not prefix: a bare
# "/dashboard" prefix exempts everything under it, including
# GET /dashboard/api/memories — which returned real memory content to any
# unauthenticated caller (verified against a running instance 2026-07-26,
# fixed here). The dashboard.py router serves exactly two shell paths for
# the HTML page (GET /dashboard and GET /dashboard/, which FastAPI's
# redirect_slashes sends /dashboard -> /dashboard/ 307); everything under
# /dashboard/api/* is now auth-gated like every other REST route,
# including the previously-key-free POST /dashboard/api/dlq/retry (see the
# reversal note on that route in cortex/app/ops.py). A login-less HTML
# shell with no secrets in it is fine; a login-less data/mutation API is
# not. The nginx-fronted unified dashboard (dashboard/index.html, port
# 8040) is unaffected: nginx already injects X-API-Key: DASHBOARD_API_KEY
# on every /api/cortex/* proxy call (dashboard/nginx.conf.template), which
# is how that SPA already reaches other auth-gated routes today (e.g.
# /ops/dlq/retry-events, /patterns/, /evals/summary — none of those are on
# any skip list). Cortex's OWN embedded SPA
# (cortex/app/static/dashboard.html), served directly by this process with
# no key of its own, loses its data tabs when AUTH_ENABLED=true — that is
# the intended, not accidental, consequence of closing this hole.
AUTH_SKIP_PREFIXES: tuple[str, ...] = (
    "/health", "/version", "/docs", "/redoc", "/openapi.json",
)
AUTH_SKIP_EXACT_PATHS: tuple[str, ...] = (
    "/dashboard",
    "/dashboard/",
    "/enroll",
    "/enroll/anchor",
)

_auth_settings = _get_auth_settings()
app.add_middleware(
    FirekeepKeyAuthMiddleware,
    enabled=_auth_settings.ENABLED,
    redis_url=_auth_settings.REDIS_URL,
    skip_paths=AUTH_SKIP_PREFIXES,
    skip_exact_paths=AUTH_SKIP_EXACT_PATHS,
)
app.add_middleware(RequestIDMiddleware)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "X-API-Key", "X-Request-Id"],
)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_graph(request: Request) -> Neo4jClient:
    return request.app.state.graph_client


async def get_vector(request: Request) -> VectorClient:
    return request.app.state.vector_client


async def get_redis(request: Request) -> redis.asyncio.Redis:
    return request.app.state.redis_client


async def get_rag_engine(request: Request) -> RAGEngine:
    return request.app.state.rag_engine


# ---------------------------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------------------------


@app.exception_handler(RateLimitExceeded)
async def handle_rate_limit(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    error = ErrorDetail(
        error_code="RATE_LIMITED",
        detail="Rate limit exceeded. Please slow down.",
        request_id=_get_request_id(request),
        suggestion="Reduce request frequency or contact the administrator.",
    )
    return JSONResponse(status_code=429, content=error.model_dump(exclude_none=True))


@app.exception_handler(GraphConnectionError)
async def handle_graph_error(request: Request, exc: GraphConnectionError) -> JSONResponse:
    logger.error("Graph connection error: %s", exc)
    error = ErrorDetail(
        error_code="GRAPH_UNAVAILABLE",
        detail="Knowledge graph service unavailable",
        request_id=_get_request_id(request),
    )
    return JSONResponse(status_code=503, content=error.model_dump(exclude_none=True))


@app.exception_handler(VectorStoreError)
async def handle_vector_error(request: Request, exc: VectorStoreError) -> JSONResponse:
    logger.error("Vector store error: %s", exc)
    error = ErrorDetail(
        error_code="VECTOR_ERROR",
        detail="Vector store service error",
        request_id=_get_request_id(request),
    )
    return JSONResponse(status_code=502, content=error.model_dump(exclude_none=True))


@app.exception_handler(LLMExtractionError)
async def handle_llm_error(request: Request, exc: LLMExtractionError) -> JSONResponse:
    logger.error("LLM extraction error: %s", exc)
    error = ErrorDetail(
        error_code="LLM_ERROR",
        detail="LLM extraction service error",
        request_id=_get_request_id(request),
    )
    return JSONResponse(status_code=502, content=error.model_dump(exclude_none=True))


@app.exception_handler(StreamIngestionError)
async def handle_stream_error(request: Request, exc: StreamIngestionError) -> JSONResponse:
    logger.error("Stream ingestion error: %s", exc)
    error = ErrorDetail(
        error_code="STREAM_ERROR",
        detail="Event stream ingestion error",
        request_id=_get_request_id(request),
    )
    return JSONResponse(status_code=502, content=error.model_dump(exclude_none=True))


@app.exception_handler(FirekeepCortexError)
async def handle_firekeep_error(request: Request, exc: FirekeepCortexError) -> JSONResponse:
    logger.error("FirekeepCortex error: %s", exc)
    error = ErrorDetail(
        error_code="INTERNAL_ERROR",
        detail="Internal service error",
        request_id=_get_request_id(request),
    )
    return JSONResponse(status_code=500, content=error.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/version")
async def version() -> dict[str, str]:
    """Return build provenance (version, git SHA, build time).

    Unauthenticated and probes no backends — a cheap liveness + provenance check
    that answers "what code is actually running here?" without introspection.
    """
    return get_version_info()


@app.get("/health", response_model=HealthResponse)
async def health(
    graph: Annotated[Neo4jClient, Depends(get_graph)],
    vector: Annotated[VectorClient, Depends(get_vector)],
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
) -> HealthResponse:
    """Return service health status by probing each backend (cached for 10s)."""
    global _health_cache, _health_cache_time

    now = datetime.now(timezone.utc)
    if (
        _health_cache is not None
        and _health_cache_time is not None
        and (now - _health_cache_time).total_seconds() < _HEALTH_CACHE_TTL_SECONDS
    ):
        return _health_cache

    async with _health_lock:
        # Re-check after acquiring lock to avoid redundant probes
        now = datetime.now(timezone.utc)
        if (
            _health_cache is not None
            and _health_cache_time is not None
            and (now - _health_cache_time).total_seconds() < _HEALTH_CACHE_TTL_SECONDS
        ):
            return _health_cache

        services: dict[str, ServiceStatus] = {}

        # Redis
        try:
            await redis_client.ping()
            services["redis"] = ServiceStatus(status="connected")
        except Exception as exc:
            logger.warning("Redis health check failed: %s", exc)
            services["redis"] = ServiceStatus(status="disconnected", detail="Service unreachable")

        # Neo4j
        try:
            await graph.ping()
            services["graph"] = ServiceStatus(status="connected")
        except Exception as exc:
            logger.warning("Neo4j health check failed: %s", exc)
            services["graph"] = ServiceStatus(status="disconnected", detail="Service unreachable")

        # Qdrant
        try:
            await vector.ping()
            services["qdrant"] = ServiceStatus(status="connected")
        except Exception as exc:
            logger.warning("Qdrant health check failed: %s", exc)
            services["qdrant"] = ServiceStatus(status="disconnected", detail="Service unreachable")

        # Uptime
        uptime_seconds: float | None = None
        try:
            start_time = app.state.start_time
            uptime_seconds = (now - start_time).total_seconds()
        except AttributeError:
            pass

        # Memory count from Qdrant
        memory_count = await vector.memory_count()

        all_connected = all(s.status == "connected" for s in services.values())
        result = HealthResponse(
            status="ok" if all_connected else "degraded",
            services=services,
            version=VERSION,
            uptime_seconds=uptime_seconds,
            memory_count=memory_count,
        )

        # Replay stream monitoring
        try:
            if getattr(app.state, "replay_redis", None):
                stream_len = await app.state.replay_redis.xlen("rp:events")
                from replay.config import get_replay_settings
                rp_settings = get_replay_settings()
                result.replay_stream_length = stream_len
                result.replay_stream_utilization = round(stream_len / rp_settings.STREAM_MAXLEN, 4)
        except Exception:
            pass

        # Replay emitter backpressure metrics
        try:
            from replay.emitter import get_emitter_stats
            result.replay_emitter = get_emitter_stats()
        except Exception:
            pass

        # Backfill queue depths (SP0 A2) — memories awaiting vector backfill.
        # DLQ > 0 means memories are permanently vector-less until reprocessed.
        try:
            from app.workers.backfill import BACKFILL_DLQ_KEY, BACKFILL_STREAM_KEY

            result.backfill_queue_depth = int(await redis_client.xlen(BACKFILL_STREAM_KEY))
            result.backfill_dlq_depth = int(await redis_client.llen(BACKFILL_DLQ_KEY))
        except Exception as exc:
            logger.warning("Backfill depth probe failed: %s", exc)

        _health_cache = result
        _health_cache_time = now
        return result


@app.post("/memory/recall", response_model=RecallResponse)
@limiter.limit(lambda: get_settings().RATE_LIMIT)
async def memory_recall(
    request: Request,
    query: ContextQuery,
    engine: Annotated[RAGEngine, Depends(get_rag_engine)],
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
) -> RecallResponse:
    """Dual-retrieval memory recall: graph + vector search, merged and scored."""
    result = await engine.recall(query)
    result.request_id = _get_request_id(request)
    result.namespace = query.namespace

    # SP0 B2: access-count accumulator. Best-effort HINCRBY into a Redis hash
    # (no Qdrant write-on-read on the hot path); the memory-agent periodic
    # pass flushes deltas into Qdrant payloads and GC merges both.
    accessed_ids: list = []
    try:
        accessed_ids = [
            s.metadata.get("id") for s in result.sources if s.metadata.get("id")
        ]
        if accessed_ids:
            now_iso = datetime.now(timezone.utc).isoformat()
            pipe = redis_client.pipeline()
            for mem_id in accessed_ids:
                pipe.hincrby("memory:access_counts", mem_id, 1)
                # last-recall timestamp — feeds the skill staleness sweep; also
                # covers skills surfaced through general RAG (the high-volume
                # signal), not just skill_recall.
                pipe.hset("memory:last_recalled", mem_id, now_iso)
            await pipe.execute()
    except Exception as exc:
        logger.warning("Failed to record access counts: %s", exc)

    # Replay: trace memory read
    sid = request.headers.get("X-Session-Id", "unknown")
    aid = request.headers.get("X-Agent-Id", "unknown")
    await _bump_untagged_counter(redis_client, sid)
    await _replay_emit(
        "memory_read",
        session_id=sid,
        agent_id=aid,
        payload={
            "query": query.task[:200],
            "top_k": query.top_k,
            "result_count": len(result.sources),
            "top_score": result.score,
            "namespace": query.namespace,
            # OWM: the ids RETURNED, so a nightly pass can join which sessions
            # saw which memories to how those sessions ended (app/owm.py).
            "memory_ids": accessed_ids[:50],
        },
    )

    # Fire webhooks (best-effort)
    try:
        redis_client = request.app.state.redis_client
        asyncio.create_task(fire_webhooks(
            redis_client, "memory.recalled",
            {"query": query.task[:200], "result_count": len(result.sources),
             "top_score": result.score},
            query.namespace,
        ))
    except Exception:
        pass

    return result


@app.post("/memory/learn", response_model=LearnResponse)
@limiter.limit(lambda: get_settings().RATE_LIMIT)
async def memory_learn(
    request: Request,
    log: ActionLog,
    graph: Annotated[Neo4jClient, Depends(get_graph)],
    vector: Annotated[VectorClient, Depends(get_vector)],
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
) -> LearnResponse:
    """Store an action log in both the knowledge graph and vector store."""
    # Secret detection
    try:
        from app.secret_scan import scan_action_log
        settings = get_settings()
        scan_enabled = getattr(settings, "SECRET_SCAN_ENABLED", True)
        scan_mode = getattr(settings, "SECRET_SCAN_MODE", "warn")
        if scan_enabled:
            findings = scan_action_log(log.action, log.outcome, log.resolution)
            if findings:
                finding_types = [f["type"] for f in findings]
                if scan_mode == "block":
                    raise HTTPException(
                        status_code=422,
                        detail=f"Secret detected in memory content: {', '.join(finding_types)}. "
                               "Remove secrets before storing to memory.",
                    )
                else:
                    logger.warning(
                        "Secret scan warning: %d potential secret(s) in learn request: %s",
                        len(findings), finding_types,
                    )
    except HTTPException:
        raise
    except Exception as e:
        logger.debug("Secret scan failed (non-blocking): %s", e)

    text = f"{log.action}. The outcome was: {log.outcome}."
    if log.resolution:
        text += f" Resolution: {log.resolution}"

    # Team continuity: capture identity headers so /memory/contributors can
    # group by agent_id and project. Defaults match the recall endpoint.
    sid = request.headers.get("X-Session-Id", "unknown")
    aid = request.headers.get("X-Agent-Id", "unknown")

    graph_result, vector_result = await asyncio.gather(
        graph.merge_action_log(log, namespace=log.namespace),
        vector.upsert(
            text=text,
            metadata={
                "source": "action_log",
                "tags": log.tags,
                "domain": log.domain,
                "memory_type": log.memory_type,
                "agent_id": aid,
                "session_id": sid,
                "project": log.project,
            },
            namespace=log.namespace,
        ),
        return_exceptions=True,
    )

    graph_failed = isinstance(graph_result, BaseException)
    vector_failed = isinstance(vector_result, BaseException)

    if graph_failed and vector_failed:
        logger.error("Both stores failed: graph=%s, vector=%s", graph_result, vector_result)
        raise GraphConnectionError(f"Both stores failed during learn: {graph_result}")

    if graph_failed:
        logger.error("Graph write failed (vector succeeded): %s", graph_result)
        return LearnResponse(
            status="partial",
            graph_id=None,
            vector_id=str(vector_result),
            namespace=log.namespace,
        )

    if vector_failed:
        logger.error("Vector write failed (graph succeeded): %s", vector_result)
        # SP0 A2: never leave a memory silently vector-less — enqueue for
        # background backfill (Redis stream, drained by Celery beat).
        backfill_queued = False
        try:
            import uuid as _uuid

            from app.db.vector import FIREKEEP_UUID_NAMESPACE
            from app.workers.backfill import enqueue_backfill

            await enqueue_backfill(
                memory_id=str(_uuid.uuid5(FIREKEEP_UUID_NAMESPACE, text)),
                text=text,
                payload={
                    "source": "action_log",
                    "tags": log.tags,
                    "domain": log.domain,
                    "memory_type": log.memory_type,
                    "agent_id": aid,
                    "session_id": sid,
                    "project": log.project,
                    "namespace": log.namespace,
                },
                redis_client=redis_client,
            )
            backfill_queued = True
        except Exception as enqueue_exc:
            logger.error(
                "Backfill enqueue FAILED — memory is vector-less with NO retry "
                "queued (action=%.80s): %s",
                log.action,
                enqueue_exc,
            )
        return LearnResponse(
            status="partial",
            graph_id=str(graph_result),
            vector_id=None,
            namespace=log.namespace,
            backfill_queued=backfill_queued,
        )

    # Contradiction detection — auto-supersede similar old memories
    superseded: list[str] = []
    try:
        superseded = await detect_and_supersede(
            vector=vector,
            graph=graph,
            new_text=text,
            new_vector_id=str(vector_result),
            new_graph_id=str(graph_result),
            domain=log.domain,
            namespace=log.namespace,
        )
    except Exception:
        logger.warning("Contradiction detection failed, continuing")

    # Fire webhooks in background (best-effort)
    try:
        asyncio.create_task(fire_webhooks(
            redis_client, "memory.learned",
            {"graph_id": graph_result, "vector_id": vector_result, "action": log.action,
             "superseded": superseded},
            namespace=log.namespace,
        ))
    except Exception as e:
        logger.warning("Webhook fire failed: %s", e)

    learn_response = LearnResponse(
        status="stored",
        graph_id=graph_result,
        vector_id=vector_result,
        namespace=log.namespace,
        superseded=superseded,
    )

    # Replay: trace memory write (sid/aid extracted earlier for upsert payload)
    await _bump_untagged_counter(redis_client, sid)
    await _replay_emit(
        "memory_write",
        session_id=sid,
        agent_id=aid,
        payload={
            "action_summary": log.action[:200],
            "memory_type": log.memory_type,
            "memory_id": graph_result or "",
            "vector_id": str(vector_result) if vector_result else "",
            "namespace": log.namespace,
            "superseded_count": len(superseded),
        },
        outcome="success",
    )

    return learn_response


@app.post("/memory/stream", response_model=StreamResponse)
@limiter.limit(lambda: get_settings().RATE_LIMIT)
async def memory_stream(
    request: Request,
    events: GenericEventIngest | list[GenericEventIngest],
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
) -> StreamResponse:
    """Push event(s) onto the Redis ingestion queue for background processing."""
    settings = get_settings()

    if isinstance(events, GenericEventIngest):
        events = [events]

    if len(events) > MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=422,
            detail=f"Batch size exceeds maximum of {MAX_BATCH_SIZE}",
        )

    try:
        pipe = redis_client.pipeline()
        for event in events:
            pipe.lpush(settings.REDIS_STREAM_KEY, event.model_dump_json())
        await pipe.execute()
    except Exception as exc:
        raise StreamIngestionError(f"Failed to push events to Redis: {exc}") from exc

    # Fire webhooks in background (best-effort)
    try:
        asyncio.create_task(fire_webhooks(
            redis_client, "stream.ingested",
            {"count": len(events), "source": events[0].source},
            namespace=events[0].namespace if events else "default",
        ))
    except Exception as e:
        logger.warning("Webhook fire failed: %s", e)

    return StreamResponse(status="queued", queued=len(events))


@app.post("/memory/feedback", response_model=FeedbackResponse)
@limiter.limit(lambda: get_settings().RATE_LIMIT)
async def memory_feedback(
    request: Request,
    feedback: FeedbackRequest,
    vector: Annotated[VectorClient, Depends(get_vector)],
) -> FeedbackResponse:
    """Record relevance feedback for recalled memories.

    Updates the Qdrant payload metadata for each referenced memory point
    with the feedback signal (useful/not useful) and optional comment.
    """
    updated = 0
    timestamp = datetime.now(timezone.utc).isoformat()
    for memory_id in feedback.memory_ids:
        try:
            await vector.set_feedback(
                memory_id=memory_id,
                useful=feedback.useful,
                comment=feedback.comment,
                timestamp=timestamp,
            )
            updated += 1
        except Exception:
            logger.warning("Failed to update feedback for memory %s", memory_id)

    return FeedbackResponse(status="recorded", updated=updated)


@app.get("/admin/untagged-calls")
async def get_untagged_calls(
    redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
    days: int = 1,
) -> dict:
    """Return untagged-call counts for the past N days. Used by the session_start hook core's discipline check."""
    days = max(1, min(days, 30))  # clamp to [1, 30] to bound the Redis loop
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    total = 0
    for i in range(days):
        date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        val = await redis_client.get(f"cortex:untagged_calls:{date}")
        n = int(val) if val else 0
        counts[date] = n
        total += n
    return {"total": total, "by_day": counts}


@app.get("/memory/contributors")
async def get_memory_contributors(
    request: Request,
    project: str | None = None,
    since: str | None = None,
    limit: int = 10000,
    vector: Annotated[VectorClient, Depends(get_vector)] = None,
) -> list[dict]:
    """Return contributor stats grouped by agent_id from Qdrant payload."""
    from collections import defaultdict

    from qdrant_client.models import FieldCondition, Filter, MatchValue

    collection = get_settings().QDRANT_COLLECTION

    must_conditions = []
    if project:
        must_conditions.append(
            FieldCondition(key="project", match=MatchValue(value=project.lower()))
        )
    scroll_filter = Filter(must=must_conditions) if must_conditions else None

    all_points: list = []
    offset = None
    scrolled = 0
    cap = min(limit, 10000)
    while scrolled < cap:
        batch, offset = await vector._client.scroll(
            collection_name=collection,
            scroll_filter=scroll_filter,
            limit=min(500, cap - scrolled),
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        all_points.extend(batch)
        scrolled += len(batch)
        if offset is None or not batch:
            break

    # Client-side since filter: timestamps are stored as ISO strings
    if since:
        all_points = [
            p for p in all_points
            if (p.payload or {}).get("timestamp") is not None
            and (p.payload or {}).get("timestamp", "") >= since
        ]

    groups: dict[str, dict] = defaultdict(lambda: {
        "memory_count": 0, "projects": set(), "last_active": None, "domains": defaultdict(int)
    })
    for point in all_points:
        payload = point.payload or {}
        aid = payload.get("agent_id", "unknown")
        g = groups[aid]
        g["memory_count"] += 1
        if payload.get("project"):
            g["projects"].add(payload["project"])
        ts = payload.get("timestamp") or payload.get("created_at")
        if ts and (g["last_active"] is None or ts > g["last_active"]):
            g["last_active"] = ts
        if payload.get("domain"):
            g["domains"][payload["domain"]] += 1

    return [
        {
            "contributor_id": aid,
            "memory_count": g["memory_count"],
            "projects": sorted(g["projects"]),
            "last_active": g["last_active"],
            "top_domain": max(g["domains"], key=g["domains"].get) if g["domains"] else None,
        }
        for aid, g in sorted(groups.items(), key=lambda x: -x[1]["memory_count"])
    ]


@app.post("/memory/handoff")
async def post_memory_handoff(
    request: Request,
    req: HandoffRequest,
    engine: Annotated[RAGEngine, Depends(get_rag_engine)],
) -> dict:
    """Generate team handoff summary for a project."""
    from datetime import timedelta

    settings = get_settings()
    since_dt = datetime.now(timezone.utc) - timedelta(days=req.since_days)

    # Reuse contributor logic via the endpoint function
    vector_client = request.app.state.vector_client
    contributors_data = await get_memory_contributors(
        request=request,
        project=req.project,
        since=since_dt.isoformat(),
        vector=vector_client,
    )
    contributors_text = "\n".join(
        f"- {c['contributor_id']}: {c['memory_count']} memories, "
        f"last active {c.get('last_active', 'unknown')}, "
        f"top domain: {c.get('top_domain', 'unknown')}"
        for c in contributors_data
    ) if contributors_data else "(no contributors found)"

    recall_query = ContextQuery(
        task=f"recent work on {req.project}",
        project=req.project,
        top_k=10,
        format="raw",
    )
    recall_resp = await engine.recall(recall_query)

    combined = f"Contributors:\n{contributors_text}\n\nRecent memories:\n{recall_resp.context_block}"
    summary = await synthesize_memories(
        task=(
            "Produce a handoff summary with three sections: "
            "(1) What was done, (2) Open items or incomplete work, "
            "(3) Where to pick up. Be specific. Under 300 words."
        ),
        entries=[{"content": combined, "score": 1.0}],
        llm_base_url=settings.LLM_BASE_URL,
        llm_model=settings.LLM_MODEL,
        llm_api_key=getattr(settings, "LLM_API_KEY", ""),
    )
    return {
        "summary": summary or recall_resp.context_block,
        "project": req.project,
    }


