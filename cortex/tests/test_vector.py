"""Tests for VectorClient (app.db.vector)."""

from __future__ import annotations

import hashlib
import uuid
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.db.vector import FIREKEEP_UUID_NAMESPACE, VectorClient, _EMBED_CACHE_MAX_SIZE
from app.exceptions import VectorStoreError


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_collection",
        EMBEDDING_DIM=768,
        LLM_BASE_URL="http://localhost:11434/v1",
        LLM_API_KEY="test-api-key",
        EMBEDDING_MODEL="test-embed",
    )


@pytest.fixture()
def mock_qdrant_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_http_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def vector_client(settings: Settings, mock_qdrant_client: AsyncMock, mock_http_client: AsyncMock) -> VectorClient:
    client = VectorClient(settings)
    client._client = mock_qdrant_client
    client._http_client = mock_http_client
    return client


def _embed_response(dim: int = 768) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"data": [{"index": 0, "embedding": [0.1] * dim}]})
    return resp


class TestEmbedTruncation:
    """Long inputs must be capped to the model's context window, else the embed
    endpoint 400s ('input length exceeds the context length') and the memory is
    stranded vector-less in the backfill DLQ."""

    @pytest.mark.asyncio
    async def test_embed_truncates_long_input(self, vector_client, mock_http_client):
        mock_http_client.post = AsyncMock(return_value=_embed_response())
        long_text = "word " * 5000  # ~25000 chars, far over the cap
        await vector_client._embed(long_text)
        sent = mock_http_client.post.call_args.kwargs["json"]["input"]
        assert len(sent) == vector_client._embed_max_chars == 2000

    @pytest.mark.asyncio
    async def test_embed_leaves_short_input_untouched(self, vector_client, mock_http_client):
        mock_http_client.post = AsyncMock(return_value=_embed_response())
        short = "a short memory"
        await vector_client._embed(short)
        assert mock_http_client.post.call_args.kwargs["json"]["input"] == short

    @pytest.mark.asyncio
    async def test_batch_embed_truncates_each_text(self, vector_client, mock_http_client):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"data": [{"index": 0, "embedding": [0.1] * 768}]})
        mock_http_client.post = AsyncMock(return_value=resp)
        await vector_client._batch_embed_api(["x" * 9000])
        sent = mock_http_client.post.call_args.kwargs["json"]["input"]
        assert all(len(t) <= vector_client._embed_max_chars for t in sent)

    @pytest.mark.asyncio
    async def test_embed_shrinks_on_input_too_long_400(self, vector_client, mock_http_client):
        """Dense text can be over the TOKEN limit even under the char cap; a 400
        'input length exceeds the context length' must trigger halve-and-retry,
        not strand the memory."""
        req = httpx.Request("POST", "http://embed/embeddings")
        too_long = httpx.Response(
            400, request=req,
            text='{"error":{"message":"the input length exceeds the context length"}}',
        )
        ok = httpx.Response(200, request=req, json={"data": [{"index": 0, "embedding": [0.2] * 768}]})
        mock_http_client.post = AsyncMock(side_effect=[too_long, ok])

        # 1900 chars < the 2000 char cap, so no up-front truncation — the 400 is
        # what forces the shrink.
        result = await vector_client._embed("d" * 1900)
        assert result == [0.2] * 768
        assert mock_http_client.post.call_count == 2
        first = mock_http_client.post.call_args_list[0].kwargs["json"]["input"]
        second = mock_http_client.post.call_args_list[1].kwargs["json"]["input"]
        assert len(second) < len(first)  # it shrank


# ---------------------------------------------------------------------------
# initialize
# ---------------------------------------------------------------------------


class TestInitialize:
    @pytest.mark.asyncio
    async def test_creates_collection_when_missing(
        self, vector_client, mock_qdrant_client
    ):
        # Simulate no existing collection
        collections_resp = MagicMock()
        collections_resp.collections = []
        mock_qdrant_client.get_collections.return_value = collections_resp

        await vector_client.initialize()

        mock_qdrant_client.create_collection.assert_called_once()
        call_kwargs = mock_qdrant_client.create_collection.call_args
        assert call_kwargs.kwargs["collection_name"] == "test_collection"

    @pytest.mark.asyncio
    async def test_skips_creation_when_exists(
        self, vector_client, mock_qdrant_client
    ):
        existing = MagicMock()
        existing.name = "test_collection"
        collections_resp = MagicMock()
        collections_resp.collections = [existing]
        mock_qdrant_client.get_collections.return_value = collections_resp

        await vector_client.initialize()

        mock_qdrant_client.create_collection.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_error_raises_vector_store_error(
        self, vector_client, mock_qdrant_client
    ):
        mock_qdrant_client.get_collections.side_effect = RuntimeError("down")

        with pytest.raises(VectorStoreError, match="Failed to initialize"):
            await vector_client.initialize()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    @pytest.mark.asyncio
    async def test_close_closes_both_clients(self, vector_client, mock_qdrant_client, mock_http_client):
        """close() should close both the httpx client and the qdrant client."""
        await vector_client.close()

        mock_http_client.aclose.assert_called_once()
        mock_qdrant_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    @pytest.mark.asyncio
    async def test_upsert_embeds_and_stores(self, vector_client, mock_qdrant_client):
        embed_vector = [0.1] * 768

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=embed_vector
        ):
            point_id = await vector_client.upsert(
                text="Fix auth bug",
                metadata={
                    "source": "action_log",
                    "tags": ["auth"],
                    "domain": "security",
                },
            )

        assert isinstance(point_id, str)
        assert len(point_id) == 36  # UUID format

        mock_qdrant_client.upsert.assert_called_once()
        call_kwargs = mock_qdrant_client.upsert.call_args
        assert call_kwargs.kwargs["collection_name"] == "test_collection"

        points = call_kwargs.kwargs["points"]
        assert len(points) == 1
        assert points[0].payload["text"] == "Fix auth bug"
        assert points[0].payload["source"] == "action_log"
        assert points[0].payload["tags"] == ["auth"]
        assert points[0].payload["domain"] == "security"

    @pytest.mark.asyncio
    async def test_upsert_uses_deterministic_uuid(self, vector_client, mock_qdrant_client):
        """Same text should produce the same point ID (uuid5)."""
        embed_vector = [0.1] * 768

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=embed_vector
        ):
            id1 = await vector_client.upsert("same text", {"source": "test"})
            id2 = await vector_client.upsert("same text", {"source": "test"})

        assert id1 == id2
        expected = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, "same text"))
        assert id1 == expected

    @pytest.mark.asyncio
    async def test_upsert_embedding_failure(self, vector_client):
        with patch.object(
            vector_client,
            "_embed",
            new_callable=AsyncMock,
            side_effect=VectorStoreError("embed failed"),
        ):
            with pytest.raises(VectorStoreError, match="embed failed"):
                await vector_client.upsert("text", {"source": "test"})

    @pytest.mark.asyncio
    async def test_upsert_qdrant_failure(self, vector_client, mock_qdrant_client):
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            mock_qdrant_client.upsert.side_effect = RuntimeError("qdrant down")
            with pytest.raises(VectorStoreError, match="Failed to upsert"):
                await vector_client.upsert("text", {"source": "test"})

    @pytest.mark.asyncio
    async def test_upsert_promotes_agent_id_session_id_and_project(
        self, vector_client, mock_qdrant_client
    ):
        """agent_id, session_id, project land at TOP-level payload, NOT nested.

        This guards the contract between /memory/learn (which sets these in
        the metadata dict) and /memory/contributors (which reads them from
        the top-level Qdrant payload). Without promotion they'd be buried
        under payload.metadata and contributors would silently see 'unknown'.
        """
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="test memory content",
                metadata={
                    "source": "action_log",
                    "tags": ["tag1"],
                    "domain": "test",
                    "agent_id": "alex",
                    "session_id": "sid-123",
                    "project": "firekeep",
                },
            )

        points = mock_qdrant_client.upsert.call_args.kwargs["points"]
        payload = points[0].payload

        # Top-level promotion
        assert payload["agent_id"] == "alex"
        assert payload["session_id"] == "sid-123"
        assert payload["project"] == "firekeep"

        # NOT duplicated under nested metadata
        nested = payload.get("metadata", {})
        assert "agent_id" not in nested
        assert "session_id" not in nested
        assert "project" not in nested

    @pytest.mark.asyncio
    async def test_upsert_defaults_for_missing_promoted_keys(
        self, vector_client, mock_qdrant_client
    ):
        """When metadata omits the promoted keys, defaults are written.

        agent_id / session_id default to 'unknown' (sentinel preferred over
        absent so contributors endpoint groups consistently); project
        defaults to None.
        """
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="content with no team-continuity keys",
                metadata={"source": "action_log", "tags": [], "domain": "general"},
            )

        points = mock_qdrant_client.upsert.call_args.kwargs["points"]
        payload = points[0].payload

        assert payload["agent_id"] == "unknown"
        assert payload["session_id"] == "unknown"
        assert payload["project"] is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_without_filters(self, vector_client, mock_qdrant_client):
        point = MagicMock()
        point.id = "p1"
        point.score = 0.92
        point.payload = {
            "text": "result text",
            "source": "action_log",
            "tags": ["auth"],
            "domain": "security",
            "timestamp": "2025-01-01T00:00:00Z",
            "metadata": {"extra": "val"},
        }
        result_obj = MagicMock()
        result_obj.points = [point]
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("auth bug", top_k=3)

        assert len(results) == 1
        assert results[0]["id"] == "p1"
        assert results[0]["score"] == 0.92
        assert results[0]["text"] == "result text"
        assert results[0]["metadata"]["source"] == "action_log"
        assert results[0]["metadata"]["extra"] == "val"

        # Verify only the archived exclusion filter was passed (no must conditions)
        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert query_filter.must is None
        assert query_filter.must_not is not None
        assert call_kwargs["limit"] == 3

    @pytest.mark.asyncio
    async def test_search_with_tag_filter(self, vector_client, mock_qdrant_client):
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search(
                "auth bug", top_k=5, filter_tags=["auth", "login"]
            )

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        # Filter should have a FieldCondition on "tags"
        assert len(query_filter.must) == 1
        assert query_filter.must[0].key == "tags"

    @pytest.mark.asyncio
    async def test_search_failure(self, vector_client, mock_qdrant_client):
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            mock_qdrant_client.query_points.side_effect = RuntimeError("fail")
            with pytest.raises(VectorStoreError, match="Failed to search"):
                await vector_client.search("query")

    @pytest.mark.asyncio
    async def test_search_with_empty_payload(self, vector_client, mock_qdrant_client):
        """Points with None payload should produce empty metadata dicts."""
        point = MagicMock()
        point.id = "p2"
        point.score = 0.5
        point.payload = None
        result_obj = MagicMock()
        result_obj.points = [point]
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("q")

        assert results[0]["text"] == ""
        assert results[0]["metadata"] == {}


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------


class TestEmbed:
    @pytest.mark.asyncio
    async def test_correct_api_call_format(self, vector_client, mock_http_client):
        """Verify _embed calls the persistent _http_client with correct params."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1, 0.2, 0.3]}]
        }
        mock_http_client.post = AsyncMock(return_value=mock_response)

        result = await vector_client._embed("test text")

        assert result == [0.1, 0.2, 0.3]

        # Verify the API call
        mock_http_client.post.assert_called_once()
        call_args = mock_http_client.post.call_args
        url = call_args[0][0]
        assert "embeddings" in url
        json_body = call_args.kwargs.get("json") or call_args[1].get("json")
        assert json_body["model"] == "test-embed"
        assert json_body["input"] == "test text"

    @pytest.mark.asyncio
    async def test_embed_sends_auth_header(self, vector_client, mock_http_client):
        """Verify _embed sends Authorization header when API key is set."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1]}]
        }
        mock_http_client.post = AsyncMock(return_value=mock_response)

        await vector_client._embed("test")

        call_args = mock_http_client.post.call_args
        headers = call_args.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer test-api-key"

    @pytest.mark.asyncio
    async def test_embed_http_error(self, vector_client, mock_http_client):
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        exc = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=mock_response
        )
        mock_response.raise_for_status.side_effect = exc
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(VectorStoreError, match="Embedding endpoint returned"):
            await vector_client._embed("test")

    @pytest.mark.asyncio
    async def test_embed_malformed_response(self, vector_client, mock_http_client):
        """Missing 'data' key in response should raise VectorStoreError."""
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"unexpected": "format"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(VectorStoreError, match="Failed to generate embedding"):
            await vector_client._embed("test")

    @pytest.mark.asyncio
    async def test_embed_no_auth_header_when_key_empty(self, settings, mock_qdrant_client, mock_http_client):
        """When LLM_API_KEY is empty, no Authorization header should be sent."""
        settings_no_key = Settings(
            QDRANT_HOST="localhost",
            QDRANT_PORT=6333,
            QDRANT_COLLECTION="test_collection",
            EMBEDDING_DIM=768,
            LLM_BASE_URL="http://localhost:11434/v1",
            LLM_API_KEY="",
            EMBEDDING_MODEL="test-embed",
        )
        client = VectorClient(settings_no_key)
        client._client = mock_qdrant_client
        client._http_client = mock_http_client

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "data": [{"embedding": [0.1]}]
        }
        mock_http_client.post = AsyncMock(return_value=mock_response)

        await client._embed("test")

        call_args = mock_http_client.post.call_args
        headers = call_args.kwargs.get("headers", {})
        assert "Authorization" not in headers


# ---------------------------------------------------------------------------
# batch_embed LRU cache behaviour
# ---------------------------------------------------------------------------


class TestBatchEmbedLRU:
    @pytest.mark.asyncio
    async def test_batch_embed_promotes_cache_hits(self, vector_client):
        """batch_embed should call move_to_end on cache hits to maintain LRU order."""
        # Pre-populate cache with two entries; "aaa" is oldest (first inserted)
        key_a = hashlib.sha256(b"aaa").hexdigest()
        key_b = hashlib.sha256(b"bbb").hexdigest()
        vector_client._embed_cache[key_a] = [0.1, 0.2]
        vector_client._embed_cache[key_b] = [0.3, 0.4]

        # Before batch_embed, "aaa" is the oldest (first to be evicted)
        assert list(vector_client._embed_cache.keys())[0] == key_a

        with patch.object(
            vector_client, "_batch_embed_api", new_callable=AsyncMock
        ) as mock_api:
            # Request "aaa" via batch_embed — it should be promoted (moved to end)
            result = await vector_client.batch_embed(["aaa"])

        assert result == [[0.1, 0.2]]
        mock_api.assert_not_called()  # everything came from cache

        # After promotion, "aaa" should now be the newest (last), "bbb" oldest (first)
        keys = list(vector_client._embed_cache.keys())
        assert keys[0] == key_b
        assert keys[-1] == key_a

    @pytest.mark.asyncio
    async def test_lru_eviction_when_cache_full(self, vector_client):
        """When the cache is full, the least-recently-used entry should be evicted."""
        # Fill cache to max capacity
        for i in range(_EMBED_CACHE_MAX_SIZE):
            key = hashlib.sha256(f"item-{i}".encode()).hexdigest()
            vector_client._embed_cache[key] = [float(i)]

        assert len(vector_client._embed_cache) == _EMBED_CACHE_MAX_SIZE
        oldest_key = list(vector_client._embed_cache.keys())[0]  # "item-0"

        # Insert one more via _cache_put — should evict the oldest
        new_key = hashlib.sha256(b"new-item").hexdigest()
        vector_client._cache_put(new_key, [99.0])

        assert len(vector_client._embed_cache) == _EMBED_CACHE_MAX_SIZE
        assert oldest_key not in vector_client._embed_cache
        assert new_key in vector_client._embed_cache

    @pytest.mark.asyncio
    async def test_batch_embed_cache_hit_prevents_eviction(self, vector_client):
        """Accessing an item via batch_embed should protect it from LRU eviction."""
        # Fill cache to max - 1
        for i in range(_EMBED_CACHE_MAX_SIZE - 1):
            key = hashlib.sha256(f"fill-{i}".encode()).hexdigest()
            vector_client._embed_cache[key] = [float(i)]

        # Add a target entry as the oldest
        target_key = hashlib.sha256(b"target").hexdigest()
        # Insert at the beginning by rebuilding — target is oldest
        old_cache = vector_client._embed_cache
        vector_client._embed_cache = OrderedDict()
        vector_client._embed_cache[target_key] = [1.0, 2.0]
        vector_client._embed_cache.update(old_cache)

        assert len(vector_client._embed_cache) == _EMBED_CACHE_MAX_SIZE
        assert list(vector_client._embed_cache.keys())[0] == target_key

        # Access "target" via batch_embed — promotes it to end
        with patch.object(vector_client, "_batch_embed_api", new_callable=AsyncMock):
            await vector_client.batch_embed(["target"])

        # Now target should be last (newest), not first
        assert list(vector_client._embed_cache.keys())[-1] == target_key

        # Insert a new item — should evict the current oldest, NOT target
        new_key = hashlib.sha256(b"newcomer").hexdigest()
        vector_client._cache_put(new_key, [42.0])

        assert target_key in vector_client._embed_cache
        assert new_key in vector_client._embed_cache


# ---------------------------------------------------------------------------
# set_feedback
# ---------------------------------------------------------------------------


class TestSetFeedback:
    @pytest.mark.asyncio
    async def test_set_feedback_calls_qdrant(self, vector_client, mock_qdrant_client):
        """set_feedback should call set_payload on the Qdrant client."""
        mock_qdrant_client.set_payload = AsyncMock()

        await vector_client.set_feedback(
            memory_id="point-123",
            useful=True,
            comment="Very helpful",
            timestamp="2026-03-05T12:00:00+00:00",
        )

        mock_qdrant_client.set_payload.assert_called_once_with(
            collection_name="test_collection",
            payload={
                "feedback_useful": True,
                "feedback_comment": "Very helpful",
                "feedback_timestamp": "2026-03-05T12:00:00+00:00",
            },
            points=["point-123"],
        )

    @pytest.mark.asyncio
    async def test_set_feedback_with_no_comment(self, vector_client, mock_qdrant_client):
        """set_feedback with comment=None should store None in payload."""
        mock_qdrant_client.set_payload = AsyncMock()

        await vector_client.set_feedback(
            memory_id="point-456",
            useful=False,
            comment=None,
            timestamp="2026-03-05T12:00:00+00:00",
        )

        call_kwargs = mock_qdrant_client.set_payload.call_args.kwargs
        assert call_kwargs["payload"]["feedback_useful"] is False
        assert call_kwargs["payload"]["feedback_comment"] is None

    @pytest.mark.asyncio
    async def test_set_feedback_propagates_errors(self, vector_client, mock_qdrant_client):
        """Errors from Qdrant should propagate to the caller."""
        mock_qdrant_client.set_payload = AsyncMock(
            side_effect=RuntimeError("Qdrant unavailable")
        )

        with pytest.raises(RuntimeError, match="Qdrant unavailable"):
            await vector_client.set_feedback(
                memory_id="point-789",
                useful=True,
                comment=None,
                timestamp="2026-03-05T12:00:00+00:00",
            )


# ---------------------------------------------------------------------------
# Namespace support
# ---------------------------------------------------------------------------


class TestNamespaceSupport:
    @pytest.mark.asyncio
    async def test_upsert_stores_namespace_in_payload(self, vector_client, mock_qdrant_client):
        """upsert should store the namespace in the Qdrant payload."""
        embed_vector = [0.1] * 768

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=embed_vector
        ):
            await vector_client.upsert(
                text="Fix auth bug",
                metadata={"source": "action_log", "tags": ["auth"], "domain": "security"},
                namespace="agent-1",
            )

        call_kwargs = mock_qdrant_client.upsert.call_args.kwargs
        points = call_kwargs["points"]
        assert points[0].payload["namespace"] == "agent-1"

    @pytest.mark.asyncio
    async def test_upsert_default_namespace_in_payload(self, vector_client, mock_qdrant_client):
        """upsert without namespace should store 'default' in payload."""
        embed_vector = [0.1] * 768

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=embed_vector
        ):
            await vector_client.upsert(
                text="Fix auth bug",
                metadata={"source": "action_log", "tags": [], "domain": "general"},
            )

        call_kwargs = mock_qdrant_client.upsert.call_args.kwargs
        points = call_kwargs["points"]
        assert points[0].payload["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_search_filters_by_namespace(self, vector_client, mock_qdrant_client):
        """search with non-default namespace should add a FieldCondition filter."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search("auth bug", top_k=5, namespace="agent-1")

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        # Should have a FieldCondition on "namespace"
        ns_conditions = [c for c in query_filter.must if c.key == "namespace"]
        assert len(ns_conditions) == 1
        assert ns_conditions[0].match.value == "agent-1"

    @pytest.mark.asyncio
    async def test_search_default_namespace_no_filter(self, vector_client, mock_qdrant_client):
        """search with default namespace should NOT add namespace filter (only archived exclusion)."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search("auth bug", top_k=5, namespace="default")

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        # Should have must_not for archived but no must conditions for namespace
        assert query_filter is not None
        assert query_filter.must is None
        ns_conditions = [c for c in (query_filter.must or []) if c.key == "namespace"]
        assert len(ns_conditions) == 0

    @pytest.mark.asyncio
    async def test_search_namespace_combined_with_tags(self, vector_client, mock_qdrant_client):
        """search with both namespace and tags should include both filters."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search(
                "auth bug", top_k=5, filter_tags=["auth"], namespace="agent-1"
            )

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert len(query_filter.must) == 2
        keys = {c.key for c in query_filter.must}
        assert keys == {"tags", "namespace"}


# ---------------------------------------------------------------------------
# Lifecycle: update_status
# ---------------------------------------------------------------------------


class TestUpdateStatus:
    @pytest.mark.asyncio
    async def test_update_status_sets_status(self, vector_client, mock_qdrant_client):
        """update_status should set the status field on the point."""
        mock_qdrant_client.set_payload = AsyncMock()

        await vector_client.update_status("point-1", "deprecated")

        mock_qdrant_client.set_payload.assert_called_once_with(
            collection_name="test_collection",
            payload={"status": "deprecated"},
            points=["point-1"],
        )

    @pytest.mark.asyncio
    async def test_update_status_sets_superseded_by(self, vector_client, mock_qdrant_client):
        """update_status with superseded_by should include it in the payload."""
        mock_qdrant_client.set_payload = AsyncMock()
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])

        await vector_client.update_status("point-1", "archived", superseded_by="point-2")

        call_kwargs = mock_qdrant_client.set_payload.call_args.kwargs
        assert call_kwargs["payload"]["status"] == "archived"
        assert call_kwargs["payload"]["superseded_by"] == "point-2"

    @pytest.mark.asyncio
    async def test_update_status_superseded_increments_contradicted_count(self, vector_client, mock_qdrant_client):
        """update_status with status=superseded should increment contradicted_count."""
        mock_point = MagicMock()
        mock_point.payload = {"contradicted_count": 3}
        mock_qdrant_client.retrieve = AsyncMock(return_value=[mock_point])
        mock_qdrant_client.set_payload = AsyncMock()

        await vector_client.update_status("point-1", "superseded", superseded_by="point-2")

        call_kwargs = mock_qdrant_client.set_payload.call_args.kwargs
        assert call_kwargs["payload"]["contradicted_count"] == 4
        assert call_kwargs["payload"]["status"] == "superseded"
        assert call_kwargs["payload"]["superseded_by"] == "point-2"


# ---------------------------------------------------------------------------
# Lifecycle: confirm_memory
# ---------------------------------------------------------------------------


class TestConfirmMemory:
    @pytest.mark.asyncio
    async def test_confirm_memory_increments_count_and_sets_timestamp(self, vector_client, mock_qdrant_client):
        """confirm_memory should bump confirmed_count and update last_confirmed_at."""
        mock_point = MagicMock()
        mock_point.payload = {"confirmed_count": 2}
        mock_qdrant_client.retrieve = AsyncMock(return_value=[mock_point])
        mock_qdrant_client.set_payload = AsyncMock()

        result = await vector_client.confirm_memory("point-1")

        assert result is True
        call_kwargs = mock_qdrant_client.set_payload.call_args.kwargs
        assert call_kwargs["payload"]["confirmed_count"] == 3
        assert "last_confirmed_at" in call_kwargs["payload"]
        assert call_kwargs["payload"]["last_confirmed_at"] is not None

    @pytest.mark.asyncio
    async def test_confirm_memory_returns_false_for_missing(self, vector_client, mock_qdrant_client):
        """confirm_memory should return False when the point doesn't exist."""
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])

        result = await vector_client.confirm_memory("nonexistent")

        assert result is False
        mock_qdrant_client.set_payload.assert_not_called()


# ---------------------------------------------------------------------------
# Lifecycle: get_memory
# ---------------------------------------------------------------------------


class TestGetMemory:
    @pytest.mark.asyncio
    async def test_get_memory_returns_correct_fields(self, vector_client, mock_qdrant_client):
        """get_memory should return a dict with lifecycle fields."""
        mock_point = MagicMock()
        mock_point.id = "point-1"
        mock_point.payload = {
            "text": "some text",
            "status": "active",
            "confirmed_count": 5,
            "contradicted_count": 1,
            "last_confirmed_at": "2026-03-05T12:00:00Z",
            "superseded_by": None,
            "domain": "general",
            "tags": ["auth"],
        }
        mock_qdrant_client.retrieve = AsyncMock(return_value=[mock_point])

        result = await vector_client.get_memory("point-1")

        assert result is not None
        assert result["id"] == "point-1"
        assert result["text"] == "some text"
        assert result["status"] == "active"
        assert result["confirmed_count"] == 5
        assert result["contradicted_count"] == 1
        assert result["last_confirmed_at"] == "2026-03-05T12:00:00Z"
        assert result["superseded_by"] is None
        assert "domain" in result["metadata"]
        assert "tags" in result["metadata"]

    @pytest.mark.asyncio
    async def test_get_memory_returns_none_for_missing(self, vector_client, mock_qdrant_client):
        """get_memory should return None when the point doesn't exist."""
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])

        result = await vector_client.get_memory("nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# Lifecycle: find_similar
# ---------------------------------------------------------------------------


class TestFindSimilar:
    @pytest.mark.asyncio
    async def test_find_similar_filters_by_status_active_and_threshold(self, vector_client, mock_qdrant_client):
        """find_similar should filter by status=active and respect threshold."""
        point_high = MagicMock()
        point_high.id = "p1"
        point_high.score = 0.90
        point_high.payload = {"text": "high match", "domain": "general", "namespace": "default"}

        point_low = MagicMock()
        point_low.id = "p2"
        point_low.score = 0.50
        point_low.payload = {"text": "low match", "domain": "general", "namespace": "default"}

        result_obj = MagicMock()
        result_obj.points = [point_high, point_low]
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.find_similar("test text", threshold=0.85)

        assert len(results) == 1
        assert results[0]["id"] == "p1"
        assert results[0]["score"] == 0.90

        # Verify filter includes status=active
        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        status_conditions = [c for c in query_filter.must if c.key == "status"]
        assert len(status_conditions) == 1
        assert status_conditions[0].match.value == "active"

    @pytest.mark.asyncio
    async def test_find_similar_filters_by_domain(self, vector_client, mock_qdrant_client):
        """find_similar with domain should include domain filter."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.find_similar("test", domain="infra")

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        domain_conditions = [c for c in query_filter.must if c.key == "domain"]
        assert len(domain_conditions) == 1
        assert domain_conditions[0].match.value == "infra"


# ---------------------------------------------------------------------------
# Lifecycle fields in upsert
# ---------------------------------------------------------------------------


class TestUpsertLifecycleFields:
    @pytest.mark.asyncio
    async def test_upsert_includes_lifecycle_fields(self, vector_client, mock_qdrant_client):
        """upsert should include status, confirmed_count, contradicted_count, etc."""
        embed_vector = [0.1] * 768

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=embed_vector
        ):
            await vector_client.upsert(
                text="Test memory",
                metadata={"source": "test", "tags": [], "domain": "general"},
            )

        call_kwargs = mock_qdrant_client.upsert.call_args.kwargs
        payload = call_kwargs["points"][0].payload
        assert payload["status"] == "active"
        assert payload["confirmed_count"] == 0
        assert payload["contradicted_count"] == 0
        assert payload["last_confirmed_at"] is None
        assert payload["superseded_by"] is None


# ---------------------------------------------------------------------------
# Search excludes archived
# ---------------------------------------------------------------------------


class TestSearchExcludesArchived:
    @pytest.mark.asyncio
    async def test_search_excludes_archived_by_default(self, vector_client, mock_qdrant_client):
        """search should add must_not filter for status=archived by default."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search("test query")

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert query_filter.must_not is not None
        archived_conditions = [c for c in query_filter.must_not if c.key == "status"]
        assert len(archived_conditions) == 1
        assert archived_conditions[0].match.value == "archived"

    @pytest.mark.asyncio
    async def test_search_includes_archived_when_requested(self, vector_client, mock_qdrant_client):
        """search with include_archived=True should not exclude archived."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search("test query", include_archived=True)

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        # is_valid filter is always applied; no status/archived filter added
        must_not = query_filter.must_not if query_filter else []
        status_keys = [c.key for c in must_not if hasattr(c, "key") and c.key == "status"]
        assert status_keys == [], "include_archived=True should not add a status must_not filter"


# ---------------------------------------------------------------------------
# SP2 Task 2b — draft skills must not leak through memory_recall/search()
#
# skill_recall / skills_section (briefing) correctly exclude drafts because
# they query via scroll() with an explicit skill_status="active" filter.
# But VectorClient.search() — the function backing /memory/recall and
# /memory/recall/stream, the primary RAG interface — only excluded
# status=archived and is_valid=False. Draft skills (synthesizer.py writes
# skill_status="draft" with real embeddings into the SAME Qdrant collection)
# could surface through memory_recall despite never having been approved,
# defeating SP2's human-in-the-loop gate through the back door.
# ---------------------------------------------------------------------------

_MISSING = object()


def _field_matches(cond, payload: dict) -> bool:
    """Evaluate a single Qdrant FieldCondition against a payload dict."""
    value = payload.get(cond.key, _MISSING)
    match = cond.match
    if hasattr(match, "value"):
        return value == match.value
    if hasattr(match, "any"):
        if value is _MISSING:
            return False
        if isinstance(value, list):
            return any(v in match.any for v in value)
        return value in match.any
    return False


def _filtering_query_points(points):
    """A query_points fake that actually evaluates the Qdrant `must`/`must_not`
    FieldConditions built by VectorClient.search(), unlike the other fixtures
    in this file which return a canned points list regardless of the filter.
    Needed to prove the skill_status=draft exclusion actually filters results
    out of the returned set — not just that the Filter object happens to
    contain the right FieldCondition (which would be true even if the
    condition were silently ignored downstream).
    """

    async def _query_points(*, query_filter, limit, **_kwargs):
        must = (query_filter.must or []) if query_filter else []
        must_not = (query_filter.must_not or []) if query_filter else []
        matched = []
        for p in points:
            payload = p.payload or {}
            if not all(_field_matches(c, payload) for c in must):
                continue
            if any(_field_matches(c, payload) for c in must_not):
                continue
            matched.append(p)
        result_obj = MagicMock()
        result_obj.points = matched[:limit]
        return result_obj

    return _query_points


def _make_point(point_id: str, payload: dict) -> MagicMock:
    point = MagicMock()
    point.id = point_id
    point.score = 0.95
    point.payload = payload
    return point


class TestSearchExcludesDraftSkills:
    @pytest.mark.asyncio
    async def test_search_excludes_draft_skill_that_would_otherwise_match(
        self, vector_client, mock_qdrant_client
    ):
        """A semantically-relevant DRAFT skill must not surface via search()."""
        draft_skill = _make_point(
            "draft-1",
            {
                "text": "how to fix the auth timeout bug",
                "memory_type": "skill",
                "skill_status": "draft",
                "source": "skill_synthesis",
                "tags": [],
                "domain": "auth",
            },
        )

        mock_qdrant_client.query_points = _filtering_query_points([draft_skill])

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("auth timeout bug", top_k=10)

        ids = {r["id"] for r in results}
        assert "draft-1" not in ids
        assert results == []

    @pytest.mark.asyncio
    async def test_search_does_not_exclude_active_skill_or_regular_memory(
        self, vector_client, mock_qdrant_client
    ):
        """The fix must be surgical: active skills and ordinary memories
        (which have no skill_status field at all) are unaffected."""
        active_skill = _make_point(
            "active-1",
            {
                "text": "verified fix for the auth timeout bug",
                "memory_type": "skill",
                "skill_status": "active",
                "source": "skill_synthesis",
                "tags": [],
                "domain": "auth",
            },
        )
        regular_memory = _make_point(
            "mem-1",
            {
                "text": "auth timeout is caused by pool exhaustion",
                "source": "action_log",
                "tags": ["auth"],
                "domain": "auth",
                # No skill_status field at all — must not be excluded.
            },
        )
        draft_skill = _make_point(
            "draft-1",
            {
                "text": "draft note about auth timeout",
                "memory_type": "skill",
                "skill_status": "draft",
                "source": "skill_synthesis",
                "tags": [],
                "domain": "auth",
            },
        )

        mock_qdrant_client.query_points = _filtering_query_points(
            [active_skill, regular_memory, draft_skill]
        )

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("auth timeout bug", top_k=10)

        ids = {r["id"] for r in results}
        assert ids == {"active-1", "mem-1"}
        assert "draft-1" not in ids

    @pytest.mark.asyncio
    async def test_search_adds_skill_status_draft_must_not_condition(
        self, vector_client, mock_qdrant_client
    ):
        """search() should build a must_not FieldCondition on skill_status=draft
        unconditionally (mirrors the existing is_valid must_not, which is
        always applied regardless of other params)."""
        result_obj = MagicMock()
        result_obj.points = []
        mock_qdrant_client.query_points.return_value = result_obj

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.search("test query")

        call_kwargs = mock_qdrant_client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        assert query_filter is not None
        assert query_filter.must_not is not None
        draft_conditions = [
            c for c in query_filter.must_not if c.key == "skill_status"
        ]
        assert len(draft_conditions) == 1
        assert draft_conditions[0].match.value == "draft"


# ---------------------------------------------------------------------------
# SP0 B2 — confidence persisted on confirm / contradict
# ---------------------------------------------------------------------------


class TestConfidencePersistence:
    @pytest.mark.asyncio
    async def test_confirm_memory_persists_confidence(self, vector_client, mock_qdrant_client):
        from app.confidence import compute_confidence

        point = MagicMock()
        point.payload = {"confirmed_count": 1, "contradicted_count": 0}
        mock_qdrant_client.retrieve.return_value = [point]

        ok = await vector_client.confirm_memory("mem-1")

        assert ok is True
        payload = mock_qdrant_client.set_payload.call_args.kwargs["payload"]
        assert payload["confirmed_count"] == 2
        assert payload["confidence"] == compute_confidence(
            confirmed_count=2, contradicted_count=0
        )

    @pytest.mark.asyncio
    async def test_supersede_persists_confidence(self, vector_client, mock_qdrant_client):
        from app.confidence import compute_confidence

        point = MagicMock()
        point.payload = {"confirmed_count": 0, "contradicted_count": 0}
        mock_qdrant_client.retrieve.return_value = [point]

        await vector_client.update_status("mem-1", "superseded", superseded_by="mem-2")

        payload = mock_qdrant_client.set_payload.call_args.kwargs["payload"]
        assert payload["contradicted_count"] == 1
        assert payload["confidence"] == compute_confidence(
            confirmed_count=0, contradicted_count=1
        )


# ---------------------------------------------------------------------------
# SP0 B2 — memory_type promoted to top-level payload at write time
# ---------------------------------------------------------------------------


class TestMemoryTypeTopLevel:
    @pytest.mark.asyncio
    async def test_upsert_writes_top_level_memory_type(self, vector_client, mock_qdrant_client):
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="Neo4j pool size is 50",
                metadata={"source": "action_log", "tags": [], "domain": "infra",
                          "memory_type": "reference"},
            )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["memory_type"] == "reference"          # top-level: GC reads this
        assert payload["metadata"]["memory_type"] == "reference"  # nested copy kept for rag.py decay back-compat

    @pytest.mark.asyncio
    async def test_upsert_defaults_memory_type_episodic(self, vector_client, mock_qdrant_client):
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="some text",
                metadata={"source": "action_log", "tags": [], "domain": "general"},
            )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["memory_type"] == "episodic"

    @pytest.mark.asyncio
    async def test_search_metadata_includes_point_id(self, vector_client, mock_qdrant_client):
        point = MagicMock()
        point.id = "point-abc"
        point.score = 0.9
        point.payload = {"text": "hit", "source": "action_log", "tags": [],
                         "domain": "general", "timestamp": "2026-01-01T00:00:00+00:00",
                         "metadata": {}}
        results = MagicMock()
        results.points = [point]
        mock_qdrant_client.query_points.return_value = results

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            out = await vector_client.search("query")

        assert out[0]["metadata"]["id"] == "point-abc"
