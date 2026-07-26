"""End-to-end ingestion pipeline tests (all I/O mocked)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from corpus.pipeline import ingest_document


class TestIngestDocument:
    @pytest.fixture()
    def mock_deps(self):
        """Mock all external dependencies."""
        vector_client = AsyncMock()
        vector_client.upsert = AsyncMock(return_value="point-id")
        vector_client._client = AsyncMock()
        vector_client._collection = "firekeep_memory"

        redis_client = AsyncMock()
        redis_client.set = AsyncMock()
        redis_client.zadd = AsyncMock()

        return {
            "vector_client": vector_client,
            "redis_client": redis_client,
        }

    @pytest.mark.asyncio
    async def test_full_pipeline(self, mock_deps):
        result = await ingest_document(
            content="CSG handles billing. Provisioning triggers CSG.",
            source_name="Test Doc",
            source_type="text",
            **mock_deps,
        )

        assert result["source_name"] == "Test Doc"
        assert "namespace" not in result
        assert result["chunks_stored"] >= 1
        assert result["entities_extracted"] == 0
        assert result["extraction_status"] == "skipped"

    @pytest.mark.asyncio
    async def test_empty_content_returns_zero(self, mock_deps):
        result = await ingest_document(
            content="",
            source_name="Empty",
            **mock_deps,
        )

        assert result["chunks_stored"] == 0
        assert result["entities_extracted"] == 0

    @pytest.mark.asyncio
    async def test_old_generation_deleted_exactly_once(self, mock_deps):
        """Re-ingest still cleans up the previous generation (now AFTER staging)."""
        mock_deps["vector_client"].delete_by_filter = AsyncMock()

        await ingest_document(
            content="X is a system that does things.",
            source_name="ReIngest",
            source_type="text",
            **mock_deps,
        )

        mock_deps["vector_client"].delete_by_filter.assert_called_once()

    @pytest.mark.asyncio
    async def test_tracks_source_in_redis(self, mock_deps):
        """Verify source metadata is stored in Redis after ingest."""
        await ingest_document(
            content="Some document content that is long enough to produce a chunk.",
            source_name="Tracked Source",
            source_type="wiki",
            **mock_deps,
        )

        mock_deps["redis_client"].set.assert_called_once()
        mock_deps["redis_client"].zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_legacy_neo4j_param_ignored(self, mock_deps):
        """neo4j_driver is a legacy param and should be silently ignored."""
        from unittest.mock import MagicMock
        fake_neo4j = MagicMock()

        result = await ingest_document(
            content="Content for neo4j compat test.",
            source_name="Compat",
            source_type="text",
            neo4j_driver=fake_neo4j,
            **mock_deps,
        )
        # Should complete without error and NOT call any neo4j methods
        fake_neo4j.session.assert_not_called()
        assert result["chunks_stored"] >= 0
