"""FastAPI router for the Corpus module.

Endpoints:
    POST   /corpus/ingest              -- Ingest a document (chunk + store in Qdrant)
    GET    /corpus/sources             -- List ingested sources
    DELETE /corpus/sources/{source_name} -- Delete a source and all its data
    DELETE /corpus/dex-sources/{source_id} -- Bulk-delete one dex client source

The /corpus/entities endpoint was removed (2026-05-27) — the Neo4j entity
graph was write-only in production.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from corpus.store import KNOWN_DEX_IDS, delete_dex_source, dex_source_prefix

logger = logging.getLogger(__name__)

# Dex-reserved source-name prefixes (Docdex §4.3): a `<dex>:` name is writable
# and deletable only by a credential carrying that dex's scope (or admin) — a
# generic corpus credential cannot claim, overwrite, or delete a reserved
# source. Derived from the store's dex ids so the record's `dex` field and
# this gate can never disagree.
_DEX_SCOPE_PREFIXES = {f"{dex}:": f"dex:{dex}" for dex in KNOWN_DEX_IDS}

# Server-controlled payload keys (Docdex §4.1). A client `metadata` carrying
# any of these could re-tenant a chunk (workspace_id/member_id), re-scope it
# (visibility), detach it from its generation (ingest_id/committed) or break
# source addressing (source_name/chunk_index/total_chunks) — rejected with
# 422 rather than silently overridden.
RESERVED_METADATA_KEYS = frozenset({
    "workspace_id", "member_id", "visibility", "ingest_id",
    "source_name", "chunk_index", "total_chunks", "committed",
})
_METADATA_MAX_KEYS = 16
_METADATA_MAX_JSON_CHARS = 2048


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class IngestRequest(BaseModel):
    content: str = Field(..., min_length=1, description="Document text to ingest")
    source_name: str = Field(default="Untitled", max_length=500)
    source_type: str = Field(default="text", pattern=r"^(text|wiki|jira|api-doc|document)$")
    visibility: Literal["workspace", "member"] = "workspace"
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _bounded_metadata(cls, v: dict[str, str]) -> dict[str, str]:
        reserved = sorted(RESERVED_METADATA_KEYS.intersection(v))
        if reserved:
            raise ValueError(
                "metadata keys are server-controlled: " + ", ".join(reserved)
            )
        if len(v) > _METADATA_MAX_KEYS:
            raise ValueError(
                f"metadata is capped at {_METADATA_MAX_KEYS} keys, got {len(v)}"
            )
        serialized = len(json.dumps(v, ensure_ascii=False))
        if serialized > _METADATA_MAX_JSON_CHARS:
            raise ValueError(
                f"metadata is capped at {_METADATA_MAX_JSON_CHARS} chars "
                f"JSON-serialized, got {serialized}"
            )
        return v


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


class DexSourceDeleteResponse(BaseModel):
    deleted_sources: int
    # A real count only when the store returns one; "unknown" otherwise —
    # never fabricated (today's delete_source reports "all", not a number).
    deleted_chunks: int | str


# ---------------------------------------------------------------------------
# Principal-aware authorization (Docdex §4.3)
# ---------------------------------------------------------------------------


def _is_admin(principal: dict) -> bool:
    from auth.keys import scopes_allow

    return scopes_allow(principal.get("scopes", []), "admin")


def _require_dex_scope(source_name: str, principal: dict) -> None:
    """403 unless the caller may write/delete this (possibly reserved) name.

    Name-based only — consults no record, so it can run before any existence
    check without becoming an oracle. Applies to the OWNER too: the prefix is
    the dex's namespace, and the dex client holds the scoped credential.
    """
    from auth.keys import scopes_allow

    for prefix, needed in _DEX_SCOPE_PREFIXES.items():
        if source_name.startswith(prefix):
            scopes = principal.get("scopes", [])
            if not (scopes_allow(scopes, needed) or _is_admin(principal)):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"source names under '{prefix}' are reserved: "
                        f"requires scope '{needed}' or 'admin'"
                    ),
                )


def _in_caller_workspace(record: dict, principal: dict) -> bool:
    """An absent/empty record workspace is a pre-ownership legacy record from
    the single-workspace world — visible to every caller."""
    record_ws = record.get("workspace_id") or ""
    return not record_ws or record_ws == principal.get("workspace_id")


def _owner_or_admin_if_private(record: dict, principal: dict) -> bool:
    """visibility=member records belong to their owner; admin overrides. An
    ownerless private record (empty member_id) is admin-only — fail closed."""
    if (record.get("visibility") or "workspace") != "member":
        return True
    owner = record.get("member_id") or ""
    return _is_admin(principal) or (
        bool(owner) and owner == principal.get("member_id")
    )


def _source_visible(record: dict, principal: dict) -> bool:
    return _in_caller_workspace(record, principal) and _owner_or_admin_if_private(
        record, principal
    )


async def _tracked_record(source_name: str) -> dict | None:
    """The tracked record for a source, via the wired listing callable.

    Callers must skip record-based authz entirely when `get_corpus_sources`
    is unwired (partial init) — check it before calling this.
    """
    for record in await get_corpus_sources():
        if record.get("name") == source_name:
            return record
    return None


# Public aliases. The knowledge router (`/knowledge/ingest`, `/knowledge/sources`)
# is a SECOND corpus front door — the review found it ungated, so a member key
# could enumerate, overwrite (generation-sweep) and hijack another member's
# private source, and a generic key could claim a reserved `docdex:` name. It
# now enforces the SAME reserved-prefix and visibility rules through these two
# helpers (Docdex §4.3/§4.4). Pure functions on (name/record, principal) — no
# router state — so they compose cleanly across both doors.
require_dex_scope = _require_dex_scope
source_visible = _source_visible


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
        _require_dex_scope(req.source_name, principal)
        # Re-ingest replaces the record and sweeps the old generation's
        # chunks — it is mutation, gated like delete. Skipped when the
        # listing callable is unwired (partial init): no records to check.
        if get_corpus_sources is not None:
            existing = await _tracked_record(req.source_name)
            if existing is not None and not _source_visible(existing, principal):
                # One generic detail for the cross-workspace and
                # not-the-owner cases alike.
                raise HTTPException(
                    status_code=403,
                    detail="not permitted to overwrite this source",
                )
        try:
            result = await ingest_document(
                content=req.content,
                source_name=req.source_name,
                source_type=req.source_type,
                workspace_id=principal["workspace_id"],
                member_id=principal["member_id"],
                visibility=req.visibility,
                metadata=req.metadata,
            )
            return IngestResponse(**result)
        except Exception as e:
            logger.exception("Ingestion failed for '%s'", req.source_name)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/sources", response_model=SourcesResponse)
    async def sources(request: Request) -> SourcesResponse:
        """List the ingested corpus sources the caller may see.

        Workspace-scoped, and a private (visibility=member) source — whose
        NAME is itself private data (spec I1) — appears only to its owner or
        an admin-scoped key.
        """
        if get_corpus_sources is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        from auth.principal import request_principal

        principal = request_principal(request)
        try:
            result = await get_corpus_sources()
            visible = [r for r in result if _source_visible(r, principal)]
            return SourcesResponse(sources=visible, count=len(visible))
        except Exception as e:
            logger.exception("Failed to list corpus sources")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/sources/{source_name}", response_model=DeleteResponse)
    async def delete_source(request: Request, source_name: str) -> DeleteResponse:
        """Delete a source and all its chunks and tracking data.

        Caller must be in the source's workspace — a cross-workspace name and
        a nonexistent one answer identically (404, no existence oracle).
        Private sources additionally require owner-or-admin, dex-reserved
        names the dex scope. This handler previously resolved NO principal at
        all (spec §9 finding 1).
        """
        if delete_corpus_source is None:
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        from auth.principal import request_principal

        principal = request_principal(request)
        _require_dex_scope(source_name, principal)
        if get_corpus_sources is not None:
            record = await _tracked_record(source_name)
            if record is None or not _in_caller_workspace(record, principal):
                raise HTTPException(status_code=404, detail="Unknown source")
            if not _owner_or_admin_if_private(record, principal):
                raise HTTPException(
                    status_code=403,
                    detail="not permitted to delete this source",
                )

        try:
            result = await delete_corpus_source(source_name=source_name)
            return DeleteResponse(**result)
        except Exception as e:
            logger.exception("Delete failed for '%s'", source_name)
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete(
        "/dex-sources/{source_id}", response_model=DexSourceDeleteResponse
    )
    async def delete_dex_source_bulk(
        request: Request, source_id: str
    ) -> DexSourceDeleteResponse:
        """Bulk-remove one dex client source (Docdex §3): every tracked source
        in the caller's workspace named ``docdex:<source_id>:<file-hash>``.

        Parses NO client filter — the deletion set is derived entirely from
        the tracked source records, then deleted per-source by exact name:
        bounded by what was actually ingested, never a Qdrant prefix query.
        Authz is the single-source delete's, applied to the whole set
        atomically: any record the caller may not delete refuses the call
        before anything is removed.
        """
        if get_corpus_sources is None or delete_corpus_source is None:
            # Unlike the single-source delete, the records ARE the deletion
            # driver — without the listing there is nothing safe to delete.
            raise HTTPException(status_code=503, detail="Corpus module not initialized")

        from auth.principal import request_principal

        principal = request_principal(request)
        prefix = dex_source_prefix(source_id)
        _require_dex_scope(prefix, principal)

        records = [
            r for r in await get_corpus_sources()
            if (r.get("name") or "").startswith(prefix)
        ]
        mine = [r for r in records if _in_caller_workspace(r, principal)]
        if not mine:
            # Cross-workspace and nonexistent answer identically — the
            # single-source delete's no-existence-oracle rule.
            raise HTTPException(status_code=404, detail="Unknown source")
        if any(not _owner_or_admin_if_private(r, principal) for r in mine):
            raise HTTPException(
                status_code=403,
                detail="not permitted to delete this source",
            )

        try:
            result = await delete_dex_source(
                source_id, [r["name"] for r in mine], delete_corpus_source
            )
            return DexSourceDeleteResponse(**result)
        except Exception as e:
            logger.exception("Bulk delete failed for dex source '%s'", source_id)
            raise HTTPException(status_code=500, detail=str(e))

    return router
