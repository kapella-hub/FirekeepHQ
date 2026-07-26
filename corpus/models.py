"""Pydantic models for the Corpus module."""

from __future__ import annotations

from pydantic import BaseModel


class ChunkMetadata(BaseModel):
    source_name: str
    source_type: str = "text"
    chunk_index: int
    total_chunks: int


class Chunk(BaseModel):
    content: str
    metadata: ChunkMetadata


class IngestionResult(BaseModel):
    source_name: str
    chunks_stored: int
    entities_extracted: int
    relationships_extracted: int
    entity_types_discovered: list[str]
