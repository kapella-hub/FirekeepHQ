"""Regression tests for defects #10/#16 — degraded-recall honesty + score floor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import Settings
from app.engine.rag import RAGEngine
from app.exceptions import VectorStoreError
from app.models import ContextQuery, RecallResponse


def _engine(mock_vector: AsyncMock) -> RAGEngine:
    mock_graph = AsyncMock()
    mock_graph.query_related = AsyncMock(return_value=[])
    mock_graph.query_related_multihop = AsyncMock(return_value=[])
    return RAGEngine(graph=mock_graph, vector=mock_vector)


def test_recall_score_floor_default():
    assert Settings().RECALL_SCORE_FLOOR == 0.35


def test_recall_response_has_degraded_field():
    resp = RecallResponse(context_block="x", sources=[], score=0.0)
    assert resp.degraded is False


class TestVectorSearchScoreThreshold:
    @pytest.mark.asyncio
    async def test_score_threshold_forwarded_to_qdrant(self):
        from app.db.vector import VectorClient

        client = VectorClient.__new__(VectorClient)
        client._client = AsyncMock()
        client._collection = "firekeep_memory"
        client._embed = AsyncMock(return_value=[0.1] * 768)
        client._client.query_points = AsyncMock(return_value=MagicMock(points=[]))

        await client.search("query", top_k=3, score_threshold=0.35)

        call_kwargs = client._client.query_points.call_args.kwargs
        assert call_kwargs.get("score_threshold") == 0.35


class TestDegradedRecall:
    @pytest.mark.asyncio
    async def test_vector_failure_retries_once_then_degrades(self):
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(
            side_effect=[VectorStoreError("embed down"), VectorStoreError("still down")]
        )
        engine = _engine(mock_vector)

        resp = await engine.recall(ContextQuery(task="anything at all", format="raw"))

        assert mock_vector.search.call_count == 2
        assert resp.degraded is True

    @pytest.mark.asyncio
    async def test_vector_recovers_on_retry_not_degraded(self):
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(side_effect=[VectorStoreError("blip"), []])
        engine = _engine(mock_vector)

        resp = await engine.recall(ContextQuery(task="anything at all", format="raw"))

        assert mock_vector.search.call_count == 2
        assert resp.degraded is False

    @pytest.mark.asyncio
    async def test_recall_passes_score_floor_to_search(self):
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(return_value=[])
        engine = _engine(mock_vector)

        await engine.recall(ContextQuery(task="anything at all", format="raw"))

        call_kwargs = mock_vector.search.call_args.kwargs
        assert call_kwargs.get("score_threshold") == 0.35

    @pytest.mark.asyncio
    async def test_vector_entries_carry_raw_score(self):
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(
            return_value=[
                {"id": "a", "score": 0.72, "text": "memory a", "metadata": {"timestamp": ""}},
                {"id": "b", "score": 0.41, "text": "memory b", "metadata": {"timestamp": ""}},
            ]
        )
        engine = _engine(mock_vector)

        resp = await engine.recall(ContextQuery(task="anything at all", format="raw"))

        raw_scores = {s.content: s.metadata.get("raw_score") for s in resp.sources}
        assert raw_scores["memory a"] == 0.72
        assert raw_scores["memory b"] == 0.41


def _mock_response(json_data: dict) -> httpx.Response:
    return httpx.Response(
        status_code=200, json=json_data, request=httpx.Request("POST", "http://test")
    )


@pytest.fixture(autouse=True)
def _reset_client():
    import app.mcp_server as mod

    mod._client = None
    yield
    mod._client = None


class TestMcpDegradedWarning:
    @pytest.mark.asyncio
    async def test_memory_recall_prefixes_degraded_warning(self):
        mock_resp = _mock_response({"context_block": "## Memory Recall", "degraded": True})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_recall
            result = await memory_recall(task="anything")

        assert result.startswith(
            "WARNING: vector search unavailable — results are graph-only"
        )

    @pytest.mark.asyncio
    async def test_memory_recall_no_prefix_when_healthy(self):
        mock_resp = _mock_response({
            "context_block": "## Memory Recall",
            "degraded": False,
            "memory_ids": ["memory-one", "memory-two"],
        })
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_recall
            result = await memory_recall(task="anything")

        assert result.startswith("## Memory Recall")
        assert 'memory_feedback(memory_ids=["memory-one", "memory-two"]' in result

    @pytest.mark.asyncio
    async def test_memory_recall_without_feedback_ids_keeps_legacy_text(self):
        mock_resp = _mock_response({"context_block": "## Memory Recall", "degraded": False})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_recall
            result = await memory_recall(task="anything")

        assert result == "## Memory Recall"
