"""Knowledge ingestion orchestration — sync corpus ingest -> async classify + per-procedure skill drafting.

Endpoints:
    POST /knowledge/ingest   -- corpus-ingest a document synchronously (searchable
                                immediately), write a "queued" ingest-status record,
                                and enqueue classify_and_draft_from_doc (Celery) to
                                classify the doc and fan out per-procedure skill-draft
                                tasks. Returns 202 with {corpus_source, status, note}.
    GET  /knowledge/sources  -- corpus sources joined with a per-source count of
                                pending (draft) skills, plus the latest async
                                classify/draft ingest status for each source.
    POST /knowledge/ingest-url -- crawl a URL (SSRF-guarded, bounded depth/pages
                                via app.knowledge.crawler) and enqueue
                                run_url_ingest (Celery) to fetch each page and
                                ingest it through the same knowledge pipeline as
                                POST /knowledge/ingest. Returns 202 with
                                {status, url, note}; 400 if the start URL fails
                                the SSRF safety check.

Design notes:
- ORDERING INVARIANT: corpus ingest runs and must succeed BEFORE any ingest-status
  write or Celery enqueue. A corpus-ingest failure surfaces as 500 with no status
  written and no task enqueued (no half-success where a queued/classifying status
  or draft task references a doc that never landed in corpus).
- The corpus write goes straight through corpus.pipeline.ingest_document
  (not corpus.api's lifespan-wired indirection) so this endpoint works
  independent of CORPUS_ENABLED; this router's own KNOWLEDGE_ENABLED gate
  (see app/main.py) controls whether it is mounted at all.
- Classification and skill drafting happen out-of-band in
  app.workers.skill_synthesis.classify_and_draft_from_doc; progress is tracked
  via app.knowledge.status (set_ingest_status / get_ingest_status).
- GET /knowledge/sources DOES reuse corpus.api's lifespan-wired
  get_corpus_sources (the existing Redis-backed source tracker) rather than
  re-implementing source listing.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from qdrant_client.models import FieldCondition, Filter, MatchValue

import corpus.api as corpus_api
from app.config import get_settings
from app.db.vector import VectorClient
from app.knowledge.crawler import is_safe_url
from app.knowledge.ingest_core import ingest_knowledge_document
from app.knowledge.status import get_ingest_status
from app.migration_gate import require_not_frozen
from app.workers.skill_synthesis import run_url_ingest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class KnowledgeIngestRequest(BaseModel):
    content: str = Field(..., description="Document text to ingest")
    source_name: str = Field(default="Untitled", max_length=500)
    source_type: str = Field(default="text", pattern=r"^(text|wiki|jira|api-doc)$")


class KnowledgeIngestResponse(BaseModel):
    corpus_source: str
    status: str
    note: str | None = None


class KnowledgeSourcesResponse(BaseModel):
    sources: list[dict]
    count: int


class KnowledgeUrlIngestRequest(BaseModel):
    url: str = Field(..., min_length=1, description="URL to crawl and ingest")
    depth: int = Field(default=0, description="Crawl depth; 0 = start page only")
    max_pages: int = Field(default=25, description="Max pages to fetch across the crawl")


class KnowledgeUrlIngestResponse(BaseModel):
    status: str
    url: str
    note: str | None = None


#: Multiplier applied to worst-case drafting wall time to get the grace window
#: after which "still in flight" stops being a credible explanation for a source
#: that has produced no drafts. Worst case is
#: ``KNOWLEDGE_MAX_PROCEDURES x SKILL_SYNTH_TIMEOUT_SECONDS`` = 10 x 300s = 50min
#: on the solo worker; x24 is 20h, and the 24h floor below dominates at defaults.
_DRAFT_GRACE_MULTIPLIER = 24
_DRAFT_GRACE_FLOOR_SECONDS = 86_400.0


def _draft_grace_seconds(settings) -> float:
    """How long a queued-but-undrafted source may stay unexplained.

    Derived rather than hardcoded so a deploy that raises
    ``KNOWLEDGE_MAX_PROCEDURES`` or ``SKILL_SYNTH_TIMEOUT_SECONDS`` cannot make
    this window too tight and start calling slow-but-healthy ingests missing.
    """
    try:
        worst_case = max(1, int(settings.KNOWLEDGE_MAX_PROCEDURES)) * max(
            1.0, float(settings.SKILL_SYNTH_TIMEOUT_SECONDS)
        )
    except Exception:
        worst_case = 0.0
    return max(_DRAFT_GRACE_FLOOR_SECONDS, worst_case * _DRAFT_GRACE_MULTIPLIER)


def _age_seconds(rec: dict, now: datetime | None = None) -> float | None:
    """Age of the record's ``updated_at``, or None if it cannot be read.

    Never raises: an unparseable stamp must leave the conservative stored
    status in place, not crash a listing endpoint.
    """
    raw = rec.get("updated_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return (ref - stamp).total_seconds()


def _effective_status(
    rec: dict,
    drafted: int,
    failed: int,
    queued: int,
    draft_points: int,
    *,
    grace_seconds: float = _DRAFT_GRACE_FLOOR_SECONDS,
    now: datetime | None = None,
) -> str:
    """What this source's ingest ACTUALLY produced, not what it was asked to do.

    ``classify_and_draft_from_doc`` stores ``classified`` the moment it has
    enqueued N ``draft_skill_from_doc`` tasks. Those tasks then reported their
    outcome to nobody, so a source whose every draft failed was served as
    ``{"status": "classified", "skills_queued": 1}`` indefinitely — measured
    live on "Runbook: Restart stuck Celery worker": queued 1, drafted 0,
    unchanged since 2026-07-12, while `GET /skills?status=draft` held nothing
    from it. The endpoint was reporting an intention as a result.

    Two derived verdicts, deliberately distinct because they rest on different
    evidence and must not be conflated:

    ``drafts_failed`` — POSITIVE evidence of failure. The classifier asked for
    drafts, at least one draft REPORTED failure, none reported success, and
    Qdrant holds no draft point for the source (the last clause means a source
    drafted successfully by an earlier ingest is never relabelled by a later
    failed one).

    ``drafts_missing`` — evidence of ABSENCE, which is the only thing available
    for a record written before ``record_draft_outcome`` existed. Such a record
    has ``skills_failed`` unset, so it can never satisfy ``failed > 0`` and
    would otherwise read as ``classified`` forever. Measured live: "Runbook:
    Restart stuck Celery worker" still resolved to ``classified`` (queued 1,
    drafted 0, no draft point) 25 days after its drafts died — the endpoint went
    on reporting an intention as a result for exactly the source that proved the
    bug. So when nothing was drafted, nothing landed in Qdrant, and the record
    has not moved for ``grace_seconds`` (24h floor vs a ~50min worst case, see
    ``_draft_grace_seconds``), we say so. We do NOT say ``drafts_failed``: no
    failure was ever observed, and claiming one would be the same overreach in
    the opposite direction.

    Anything short of both keeps the stored classify status — an in-flight
    ingest must not be reported as broken, and an unreadable ``updated_at``
    counts as in-flight. ``classify_status`` is returned alongside either way,
    so nothing is lost.
    """
    stored = rec.get("status", "unknown")
    if stored != "classified" or queued <= 0:
        return stored
    if drafted > 0 or draft_points > 0:
        return stored
    if failed > 0:
        return "drafts_failed"
    age = _age_seconds(rec, now)
    if age is not None and age >= grace_seconds:
        return "drafts_missing"
    return stored


def create_knowledge_router() -> APIRouter:
    """Create the knowledge ingestion REST router."""

    router = APIRouter(prefix="/knowledge", tags=["knowledge"])

    # Deferred import: app.main registers this router from inside
    # _register_feature_routers, so importing app.main at module load time
    # here would be circular. Mirrors app/skills/api.py's identical pattern.
    from app.main import get_redis, get_vector

    @router.post(
        "/ingest", response_model=KnowledgeIngestResponse, status_code=202,
        dependencies=[Depends(require_not_frozen)],
    )
    async def ingest(
        request: Request,
        req: KnowledgeIngestRequest,
        vector: VectorClient = Depends(get_vector),
        redis_client=Depends(get_redis),
    ) -> KnowledgeIngestResponse:
        """Corpus-ingest synchronously (doc searchable now), then queue classify+draft.

        The caller's principal is threaded into both halves of the pipeline
        (corpus chunks now, draft skills later via the Celery kwargs). Without
        it both landed with ``workspace_id=null`` and neither was reachable
        from ``memory_recall``, which filters on the caller's workspace.
        """
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="content must not be empty or whitespace-only")

        from auth.principal import request_principal

        principal = request_principal(request)

        # This is a second corpus front door. Enforce the SAME rules the
        # corpus router does (Docdex §4.3/§4.4): a reserved `docdex:` name
        # needs the dex scope, and an existing source only the caller may see
        # can be overwritten (which generation-sweeps its chunks) only by an
        # authorized principal. Reserved-prefix check is name-only and always
        # safe; the overwrite check needs the tracked listing.
        corpus_api.require_dex_scope(req.source_name, principal)
        if corpus_api.get_corpus_sources is not None:
            existing = next(
                (r for r in await corpus_api.get_corpus_sources()
                 if r.get("name") == req.source_name),
                None,
            )
            if existing is not None and not corpus_api.source_visible(existing, principal):
                raise HTTPException(
                    status_code=403,
                    detail="source belongs to another member",
                )
        try:
            await ingest_knowledge_document(
                req.content, req.source_name, req.source_type, vector=vector, redis=redis_client,
                workspace_id=principal["workspace_id"], member_id=principal["member_id"],
            )
        except Exception as exc:
            logger.exception("Knowledge ingest failed for '%s'", req.source_name)
            raise HTTPException(status_code=500, detail=str(exc))

        return KnowledgeIngestResponse(
            corpus_source=req.source_name, status="queued",
            note="classification + skill drafting queued",
        )

    @router.post(
        "/ingest-url", response_model=KnowledgeUrlIngestResponse, status_code=202,
        dependencies=[Depends(require_not_frozen)],
    )
    async def ingest_url(req: KnowledgeUrlIngestRequest) -> KnowledgeUrlIngestResponse:
        """Crawl a URL (bounded depth/pages, SSRF-guarded) and queue each fetched
        page for knowledge ingestion. Fails fast on an unsafe start URL (defense
        in depth — the crawl task re-checks every URL it touches, including this
        one, before fetching)."""
        settings = get_settings()
        depth = max(0, min(req.depth, settings.KNOWLEDGE_CRAWL_MAX_DEPTH))
        max_pages = max(1, min(req.max_pages, settings.KNOWLEDGE_CRAWL_MAX_PAGES))

        ok, reason = is_safe_url(req.url)
        if not ok:
            # Log the specific reason (may name a resolved private IP) server-side,
            # but return a generic message so the endpoint isn't an SSRF/DNS recon oracle.
            logger.warning("URL ingest rejected for %r: %s", req.url, reason)
            raise HTTPException(status_code=400, detail="URL rejected: not permitted")

        run_url_ingest.delay(req.url, depth, max_pages)

        return KnowledgeUrlIngestResponse(
            status="queued", url=req.url,
            note="crawling + ingest queued; pages appear in Sources as they land",
        )

    @router.get("/sources", response_model=KnowledgeSourcesResponse)
    async def sources(
        request: Request,
        vector: VectorClient = Depends(get_vector),
        redis_client=Depends(get_redis),
    ) -> KnowledgeSourcesResponse:
        """List corpus sources joined with their pending (draft) skill counts and ingest status."""
        if corpus_api.get_corpus_sources is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        try:
            corpus_sources = await corpus_api.get_corpus_sources()
        except Exception as exc:
            logger.exception("Failed to list corpus sources")
            raise HTTPException(status_code=500, detail=str(exc))

        # A source NAME is private data (Docdex §4.4). This is a corpus egress
        # surface, so it filters exactly like /corpus/sources: another member's
        # private source is not listed. The review found it returned every
        # record — name, member_id, workspace_id — to any member key.
        from auth.principal import request_principal

        principal = request_principal(request)
        corpus_sources = [
            r for r in corpus_sources if corpus_api.source_visible(r, principal)
        ]

        settings = get_settings()
        result: list[dict] = []
        for src in corpus_sources:
            name = src.get("name")
            draft_count = 0
            status_rec = None
            if name:
                points, _ = await vector._client.scroll(
                    collection_name=settings.QDRANT_COLLECTION,
                    scroll_filter=Filter(must=[
                        FieldCondition(key="memory_type", match=MatchValue(value="skill")),
                        FieldCondition(key="skill_status", match=MatchValue(value="draft")),
                        FieldCondition(key="source_doc", match=MatchValue(value=name)),
                    ]),
                    limit=1000, with_payload=False, with_vectors=False,
                )
                draft_count = len(points)
                status_rec = await get_ingest_status(name, redis_client=redis_client)
            rec = status_rec or {}
            drafted = int(rec.get("skills_drafted", 0) or 0)
            failed = int(rec.get("skills_failed", 0) or 0)
            queued = int(rec.get("skills_queued", 0) or 0)
            result.append({
                **src,
                "draft_skill_count": draft_count,
                "status": _effective_status(
                    rec, drafted, failed, queued, draft_count,
                    grace_seconds=_draft_grace_seconds(settings),
                ),
                "classify_status": rec.get("status", "unknown"),
                "disposition": rec.get("disposition", ""),
                "skills_queued": queued,
                "skills_drafted": drafted,
                "skills_failed": failed,
                "last_draft_error": rec.get("last_draft_error", ""),
                "updated_at": rec.get("updated_at", ""),
            })

        return KnowledgeSourcesResponse(sources=result, count=len(result))

    return router
