"""Tests for corpus storage layer (Qdrant + Redis)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from corpus.models import Chunk, ChunkMetadata
from corpus.store import (
    _slug,
    delete_source,
    delete_source_chunks,
    delete_source_tracking,
    store_chunks,
    track_source,
    list_sources,
)


class TestHelpers:
    def test_slug(self):
        assert _slug("Network Architecture Wiki") == "network-architecture-wiki"
        assert _slug("  Billing ") == "billing"
        assert _slug("api-doc!@#") == "api-doc"


class TestStoreChunks:
    @pytest.mark.asyncio
    async def test_stores_via_vector_client(self):
        mock_vector = AsyncMock()
        mock_vector.upsert = AsyncMock(return_value="point-id")

        chunks = [
            Chunk(
                content="CSG handles billing for all products.",
                metadata=ChunkMetadata(
                    source_name="Billing Wiki",
                    source_type="wiki",
                    chunk_index=0,
                    total_chunks=1,
                ),
            ),
        ]

        count = await store_chunks(chunks, mock_vector)

        assert count == 1
        mock_vector.upsert.assert_called_once()
        call_kwargs = mock_vector.upsert.call_args
        metadata = call_kwargs.kwargs["metadata"]
        assert metadata["source"] == "corpus"
        assert metadata["domain"] == "wiki"
        assert "corpus" in metadata["tags"]
        assert "wiki" in metadata["tags"]
        assert "billing-wiki" in metadata["tags"]

    @pytest.mark.asyncio
    async def test_empty_chunks_returns_zero(self):
        mock_vector = AsyncMock()
        count = await store_chunks([], mock_vector)
        assert count == 0
        mock_vector.upsert.assert_not_called()


class TestTrackSource:
    @pytest.mark.asyncio
    async def test_stores_in_redis(self):
        mock_redis = AsyncMock()
        mock_redis.set = AsyncMock()
        mock_redis.zadd = AsyncMock()

        await track_source("Billing Wiki", "wiki", chunk_count=3, redis_client=mock_redis)

        mock_redis.set.assert_called_once()
        mock_redis.zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_redis_noop(self):
        # Should not raise when redis_client is None
        await track_source("Billing Wiki", "wiki", chunk_count=3, redis_client=None)


class TestListSources:
    @pytest.mark.asyncio
    async def test_returns_sources_from_redis(self):
        import json
        source1 = json.dumps({
            "name": "Billing Wiki", "source_type": "wiki",
            "chunks": 5, "last_ingested": "2026-01-01T00:00:00+00:00",
        })
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=["Billing Wiki"])
        mock_redis.get = AsyncMock(return_value=source1)

        results = await list_sources(redis_client=mock_redis)

        assert len(results) == 1
        assert results[0]["name"] == "Billing Wiki"
        assert results[0]["chunks"] == 5

    @pytest.mark.asyncio
    async def test_returns_empty_without_redis(self):
        results = await list_sources(redis_client=None)
        assert results == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_sources(self):
        mock_redis = AsyncMock()
        mock_redis.zrevrange = AsyncMock(return_value=[])

        results = await list_sources(redis_client=mock_redis)
        assert results == []

    @pytest.mark.asyncio
    async def test_roundtrip_with_non_decoding_client(self):
        """Regression (SP2 deploy validation): the shared corpus module must
        not assume its caller's redis client has decode_responses=True.

        Cortex's app.state.redis_client (app/main.py) is created WITHOUT
        decode_responses=True, so zrevrange returns bytes. Before the fix,
        list_sources interpolated the bytes repr into the key
        (f"corpus:source:{b'Name'}" -> "corpus:source:b'Name'"), read a
        non-existent key, and returned [] — making /corpus/sources and
        /knowledge/sources permanently empty even though the source was
        tracked. This drives a real round-trip through a non-decoding
        fakeredis to pin the behavior.
        """
        import fakeredis.aioredis

        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await track_source("Billing Wiki", "wiki", chunk_count=5, redis_client=r)

        results = await list_sources(redis_client=r)

        assert len(results) == 1
        assert results[0]["name"] == "Billing Wiki"
        assert results[0]["chunks"] == 5


class TestDeleteSourceChunks:
    @pytest.mark.asyncio
    async def test_calls_delete_by_filter(self):
        mock_vector = AsyncMock()
        mock_vector.delete_by_filter = AsyncMock()

        await delete_source_chunks("Billing Wiki", mock_vector)

        mock_vector.delete_by_filter.assert_called_once()
        filt = mock_vector.delete_by_filter.call_args[0][0]
        # Verify filter has the right conditions
        assert len(filt.must) == 2
        filter_keys = [cond.key for cond in filt.must]
        assert "source" in filter_keys
        assert "metadata.source_name" in filter_keys


class TestDeleteSourceTracking:
    @pytest.mark.asyncio
    async def test_removes_from_redis(self):
        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock()
        mock_redis.zrem = AsyncMock()

        await delete_source_tracking("Billing Wiki", redis_client=mock_redis)

        mock_redis.delete.assert_called_once()
        mock_redis.zrem.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_redis_noop(self):
        await delete_source_tracking("Billing Wiki", redis_client=None)


class TestDeleteSource:
    @pytest.mark.asyncio
    async def test_full_delete(self):
        mock_vector = AsyncMock()
        mock_vector.delete_by_filter = AsyncMock()

        mock_redis = AsyncMock()
        mock_redis.delete = AsyncMock()
        mock_redis.zrem = AsyncMock()

        result = await delete_source("Test Source", mock_vector, redis_client=mock_redis)

        assert result["source_name"] == "Test Source"
        assert result["chunks_deleted"] == "all"
        assert result["entities_deleted"] == "all"
        mock_vector.delete_by_filter.assert_called_once()
        mock_redis.delete.assert_called_once()
        mock_redis.zrem.assert_called_once()
