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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from qdrant_client.models import FieldCondition, Filter, MatchValue

import corpus.api as corpus_api
from app.config import get_settings
from app.db.vector import VectorClient
from app.knowledge.crawler import is_safe_url
from app.knowledge.ingest_core import ingest_knowledge_document
from app.knowledge.status import get_ingest_status
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


def create_knowledge_router() -> APIRouter:
    """Create the knowledge ingestion REST router."""

    router = APIRouter(prefix="/knowledge", tags=["knowledge"])

    # Deferred import: app.main registers this router from inside
    # _register_feature_routers, so importing app.main at module load time
    # here would be circular. Mirrors app/skills/api.py's identical pattern.
    from app.main import get_redis, get_vector

    @router.post("/ingest", response_model=KnowledgeIngestResponse, status_code=202)
    async def ingest(
        req: KnowledgeIngestRequest,
        vector: VectorClient = Depends(get_vector),
        redis_client=Depends(get_redis),
    ) -> KnowledgeIngestResponse:
        """Corpus-ingest synchronously (doc searchable now), then queue classify+draft."""
        if not req.content.strip():
            raise HTTPException(status_code=400, detail="content must not be empty or whitespace-only")

        try:
            await ingest_knowledge_document(
                req.content, req.source_name, req.source_type, vector=vector, redis=redis_client,
            )
        except Exception as exc:
            logger.exception("Knowledge ingest failed for '%s'", req.source_name)
            raise HTTPException(status_code=500, detail=str(exc))

        return KnowledgeIngestResponse(
            corpus_source=req.source_name, status="queued",
            note="classification + skill drafting queued",
        )

    @router.post("/ingest-url", response_model=KnowledgeUrlIngestResponse, status_code=202)
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
            result.append({
                **src,
                "draft_skill_count": draft_count,
                "status": (status_rec or {}).get("status", "unknown"),
                "disposition": (status_rec or {}).get("disposition", ""),
                "skills_queued": (status_rec or {}).get("skills_queued", 0),
                "updated_at": (status_rec or {}).get("updated_at", ""),
            })

        return KnowledgeSourcesResponse(sources=result, count=len(result))

    return router
