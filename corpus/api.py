"""FastAPI router for the Corpus module.

Endpoints:
    POST   /corpus/ingest              -- Ingest a document (chunk + store in Qdrant)
    GET    /corpus/sources             -- List ingested sources
    DELETE /corpus/sources/{source_name} -- Delete a source and all its data

The /corpus/entities endpoint was removed (2026-05-27) — the Neo4j entity
graph was write-only in production.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Document text to ingest")
    source_name: str = Field(default="Untitled", max_length=500)
    source_type: str = Field(default="text", pattern=r"^(text|wiki|jira|api-doc)$")


class IngestResponse(BaseModel):
    source_name: str
    chunks_stored: int
    entities_extracted: int
    relationships_extracted: int
    entity_types_discovered: list[str]
    extraction_status: str = "skipped"


class SourcesResponse(BaseModel):
    sources: list[dict]
    count: int


class DeleteResponse(BaseModel):
    source_name: str
    chunks_deleted: str
    entities_deleted: str


# ---------------------------------------------------------------------------
# Pipeline functions (wired up during router creation)
# ---------------------------------------------------------------------------

# These are module-level callables set by the Cortex lifespan hook
# when it initializes the corpus module with real dependencies.
ingest_document = None
get_corpus_sources = None
delete_corpus_source = None


def create_corpus_router() -> APIRouter:
    """Create the corpus REST router."""

    router = APIRouter(prefix="/corpus", tags=["corpus"])

    @router.post("/ingest", response_model=IngestResponse)
    async def ingest(request: Request, req: IngestRequest) -> IngestResponse:
        """Ingest a document: chunk -> store in Qdrant vector store.

        The caller's principal is threaded down to the chunk payloads. Without
        it every chunk landed with ``workspace_id=null`` and was unreachable
        from ``memory_recall``, which filters on the caller's workspace — the
        exact opposite of what this endpoint's contract promises.
        """
        if ingest_document is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        from auth.principal import request_principal

        principal = request_principal(request)
        try:
            result = await ingest_document(
                content=req.content,
                source_name=req.source_name,
                source_type=req.source_type,
                workspace_id=principal["workspace_id"],
                member_id=principal["member_id"],
            )
            return IngestResponse(**result)
        except Exception as e:
            logger.exception("Ingestion failed for '%s'", req.source_name)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/sources", response_model=SourcesResponse)
    async def sources() -> SourcesResponse:
        """List all ingested corpus sources."""
        if get_corpus_sources is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        try:
            result = await get_corpus_sources()
            return SourcesResponse(sources=result, count=len(result))
        except Exception as e:
            logger.exception("Failed to list corpus sources")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/sources/{source_name}", response_model=DeleteResponse)
    async def delete_source(source_name: str) -> DeleteResponse:
        """Delete a source and all its chunks and tracking data."""
        if delete_corpus_source is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        try:
            result = await delete_corpus_source(source_name=source_name)
            return DeleteResponse(**result)
        except Exception as e:
            logger.exception("Delete failed for '%s'", source_name)
            raise HTTPException(status_code=500, detail=str(e))

    return router
