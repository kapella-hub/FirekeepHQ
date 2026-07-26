"""Integration test: full ingest cycle with mocked I/O."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from corpus.pipeline import ingest_document


class TestFullCycle:
    """Ingest a document and verify chunk storage."""

    @pytest.fixture()
    def mock_vector(self):
        v = AsyncMock()
        v.upsert = AsyncMock(return_value="point-id")
        v.delete_by_filter = AsyncMock()
        return v

    @pytest.fixture()
    def mock_redis(self):
        r = AsyncMock()
        r.set = AsyncMock()
        r.zadd = AsyncMock()
        return r

    @pytest.mark.asyncio
    async def test_ingest_stores_cable_chunks(self, mock_vector, mock_redis):
        """Ingest a cable company doc and verify chunks are stored."""
        result = await ingest_document(
            content=(
                "The HFC plant uses DOCSIS 3.1 technology. "
                "The CMTS manages traffic across the network. "
                "When a node becomes overloaded, a node split divides "
                "the segment to reduce subscriber density."
            ),
            source_name="Network Architecture Wiki",
            source_type="wiki",
            vector_client=mock_vector,
            redis_client=mock_redis,
        )

        # Chunks stored, no entity extraction
        assert result["chunks_stored"] >= 1
        assert result["entities_extracted"] == 0
        assert result["extraction_status"] == "skipped"
        mock_vector.upsert.assert_called()

    @pytest.mark.asyncio
    async def test_reingest_replaces_old_generation(self, mock_vector, mock_redis):
        """Re-ingestion should replace old data with the new generation.

        The pipeline is staged (SP0 A4): new chunks are stored first, and the
        previous generation is deleted only after the new one is fully
        committed — see corpus/pipeline.py.
        """
        await ingest_document(
            content="The HFC plant uses DOCSIS 3.1.",
            source_name="Network Wiki",
            source_type="wiki",
            vector_client=mock_vector,
            redis_client=mock_redis,
        )

        # Verify delete_by_filter was called on the VectorClient (delete_source_chunks)
        mock_vector.delete_by_filter.assert_called_once()
