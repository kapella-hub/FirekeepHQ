"""Ingestion pipeline: chunk -> store in Qdrant (sync), track in Redis.

The pipeline is staged (SP0 A4, defect #7):
1. Chunk the content
2. Store new chunks in Qdrant, written gated (``committed: False``) and
   tagged with a per-run ingest_id
3. Commit the generation — one payload flip recall's GENERATION_GUARD
   honors (Docdex spec §4.5 option (a))
4. Delete the previous generation (everything for this source EXCEPT the
   new ingest_id) — only after the new generation is live
5. Record source metadata in Redis, last

A mid-ingest failure therefore leaves the old generation fully live and the
source metadata unchanged; the partial new generation is uncommitted —
invisible to recall — until the next successful ingest of the same source
sweeps it. The exception propagates to the caller (a boundary may retry or
fail loudly — never silently).

The async LLM entity extraction path was removed (2026-05-27) — audit
found 0 entities ever extracted in production. Qdrant chunk path is
unaffected and corpus content surfaces correctly in /memory/recall.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from corpus.chunker import chunk_content
from corpus.config import get_corpus_settings
from corpus.models import Chunk, ChunkMetadata
from corpus.store import (
    commit_generation,
    delete_source_chunks,
    store_chunks,
    track_source,
)

logger = logging.getLogger(__name__)


async def ingest_document(
    content: str,
    source_name: str = "Untitled",
    source_type: str = "text",
    vector_client=None,
    redis_client=None,
    workspace_id: str | None = None,
    member_id: str | None = None,
    visibility: str = "workspace",
    metadata: dict[str, str] | None = None,
    # Legacy params accepted but ignored (no longer used)
    neo4j_driver=None,
    llm_base_url: str = "",
    llm_api_key: str | None = None,
    llm_model: str = "",
) -> dict[str, Any]:
    """Ingest a document: stage chunks + swap generations in Qdrant, track in Redis.

    ``workspace_id`` / ``member_id`` are the ingesting principal's tenancy and
    are stamped onto every chunk. This function had NO workspace parameter at
    all, so every corpus and knowledge ingest wrote ``workspace_id=null`` and
    the chunks were invisible to ``memory_recall``, which filters on the
    caller's workspace — see ``corpus.store.store_chunks`` for the measurement.

    ``visibility`` scopes every chunk ("workspace" default, "member" =
    ingesting member only) and ``metadata`` is the API-bounded client dict;
    both ride the principal's path into ``store_chunks`` and ``visibility``
    also lands on the Redis source record (Docdex §4.1).

    Returns a dict matching IngestionResult fields. The entities_extracted
    field is always 0 — entity extraction was removed.
    """
    settings = get_corpus_settings()

    # 1. Chunk
    raw_chunks = chunk_content(
        content,
        source_type=source_type,
        chunk_size=settings.CHUNK_SIZE,
        overlap=settings.CHUNK_OVERLAP,
    )
    logger.info(
        "Chunked '%s' (%s) into %d chunks", source_name, source_type, len(raw_chunks),
    )

    if not raw_chunks:
        # Nothing to stage — leave any existing content and metadata untouched.
        return {
            "source_name": source_name,
            "chunks_stored": 0,
            "entities_extracted": 0,
            "relationships_extracted": 0,
            "entity_types_discovered": [],
            "extraction_status": "skipped",
        }

    # 2. Stage new chunks FIRST (SP0 A4), written gated (committed: False).
    #    Old chunks stay live until the new generation is committed. Point ids
    #    are source+run scoped (corpus_point_id — Docdex §4.2), so staging
    #    never overwrites the previous generation's points; the sweep below
    #    removes them.
    ingest_id = str(uuid.uuid4())
    chunks_stored = 0
    if vector_client:
        chunk_objects = [
            Chunk(
                content=text,
                metadata=ChunkMetadata(
                    source_name=source_name,
                    source_type=source_type,
                    chunk_index=i,
                    total_chunks=len(raw_chunks),
                ),
            )
            for i, text in enumerate(raw_chunks)
        ]
        try:
            chunks_stored = await store_chunks(
                chunk_objects, vector_client, ingest_id=ingest_id,
                workspace_id=workspace_id, member_id=member_id,
                visibility=visibility, client_metadata=metadata,
            )
            # 3. Commit: the staged generation goes live in ONE payload flip
            #    (spec §4.5 option (a)). Recall's GENERATION_GUARD excludes
            #    committed: False, so a failure anywhere before this line
            #    leaves the new generation unrecallable, not half-visible.
            await commit_generation(vector_client, source_name, ingest_id)
        except Exception:
            # Fail loudly. Old chunks are still intact and source metadata is
            # unchanged; the partial new generation (tagged with this
            # ingest_id) stays uncommitted and is swept by the next successful
            # ingest of this source.
            logger.exception(
                "Staged ingest of '%s' failed — old content preserved, "
                "source metadata unchanged (ingest_id=%s)",
                source_name,
                ingest_id,
            )
            raise

        # 4. Delete the previous generation only now that the new one is
        #    committed and live. The filter excludes points carrying this
        #    ingest_id.
        await delete_source_chunks(
            source_name, vector_client, exclude_ingest_id=ingest_id
        )

    # 5. Track source in Redis — last, only after the swap completed. The
    #    principal's tenancy is stamped on the record too: corpus/api.py's
    #    list/delete authz reads it (Docdex §4.3).
    await track_source(
        source_name, source_type,
        chunk_count=len(raw_chunks),
        redis_client=redis_client,
        visibility=visibility,
        workspace_id=workspace_id,
        member_id=member_id,
    )

    return {
        "source_name": source_name,
        "chunks_stored": chunks_stored,
        "entities_extracted": 0,
        "relationships_extracted": 0,
        "entity_types_discovered": [],
        "extraction_status": "skipped",
    }
