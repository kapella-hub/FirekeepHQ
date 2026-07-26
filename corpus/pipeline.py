"""Ingestion pipeline: chunk -> store in Qdrant (sync), track in Redis.

The pipeline is staged (SP0 A4, defect #7):
1. Chunk the content
2. Store new chunks in Qdrant, tagged with a per-run ingest_id
3. Delete the previous generation (everything for this source EXCEPT the
   new ingest_id) — only after every new chunk is committed
4. Record source metadata in Redis, last

A mid-ingest failure therefore leaves the old generation fully live and the
source metadata unchanged; the partial new generation is swept by the next
successful ingest of the same source. The exception propagates to the caller
(a boundary may retry or fail loudly — never silently).

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
    # Legacy params accepted but ignored (no longer used)
    neo4j_driver=None,
    llm_base_url: str = "",
    llm_api_key: str | None = None,
    llm_model: str = "",
) -> dict[str, Any]:
    """Ingest a document: stage chunks + swap generations in Qdrant, track in Redis.

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

    # 2. Stage new chunks FIRST (SP0 A4). Old chunks stay live until the
    #    full new generation is committed. Identical chunk text upserts over
    #    the old point (same uuid5 id) and inherits its lifecycle via
    #    _merge_lifecycle, gaining this run's ingest_id.
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
                chunk_objects, vector_client, ingest_id=ingest_id
            )
        except Exception:
            # Fail loudly. Old chunks are still intact and source metadata is
            # unchanged; the partial new generation (tagged with this
            # ingest_id) is swept by the next successful ingest of this source.
            logger.exception(
                "Staged ingest of '%s' failed — old content preserved, "
                "source metadata unchanged (ingest_id=%s)",
                source_name,
                ingest_id,
            )
            raise

        # 3. Delete the previous generation only now that every new chunk is
        #    committed. The filter excludes points carrying this ingest_id.
        await delete_source_chunks(
            source_name, vector_client, exclude_ingest_id=ingest_id
        )

    # 4. Track source in Redis — last, only after the swap completed.
    await track_source(
        source_name, source_type,
        chunk_count=len(raw_chunks),
        redis_client=redis_client,
    )

    return {
        "source_name": source_name,
        "chunks_stored": chunks_stored,
        "entities_extracted": 0,
        "relationships_extracted": 0,
        "entity_types_discovered": [],
        "extraction_status": "skipped",
    }
