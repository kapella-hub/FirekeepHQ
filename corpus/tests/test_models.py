"""Tests for corpus Pydantic models."""

from corpus.models import (
    Chunk,
    ChunkMetadata,
    IngestionResult,
)


class TestChunk:
    def test_with_metadata(self):
        c = Chunk(
            content="The provisioning system handles...",
            metadata=ChunkMetadata(
                source_name="Provisioning Wiki",
                source_type="wiki",
                chunk_index=0,
                total_chunks=3,
            ),
        )
        assert c.metadata.source_type == "wiki"
        assert c.metadata.chunk_index == 0


class TestIngestionResult:
    def test_fields(self):
        r = IngestionResult(
            source_name="Test Doc",
            chunks_stored=5,
            entities_extracted=0,
            relationships_extracted=0,
            entity_types_discovered=[],
        )
        assert r.chunks_stored == 5
        assert r.entities_extracted == 0
