"""Regular and streaming recall share one verified-workspace vector path."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import FieldCondition

from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery


@pytest.mark.asyncio
async def test_regular_and_streaming_recall_use_shared_workspace_search(
    mock_graph, mock_vector
):
    engine = RAGEngine(graph=mock_graph, vector=mock_vector)
    engine._search_vector = AsyncMock(return_value=[])
    query = ContextQuery(task="workspace isolation")

    await engine.recall(query, workspace_id="workspace-a")
    _ = [
        event
        async for event in engine.recall_streaming(
            query, workspace_id="workspace-a"
        )
    ]

    assert engine._search_vector.await_count == 2
    assert all(
        call.kwargs["workspace_id"] == "workspace-a"
        for call in engine._search_vector.await_args_list
    )


@pytest.mark.asyncio
async def test_vector_search_builds_workspace_payload_filter(test_settings):
    vector = VectorClient(test_settings)
    vector._embed = AsyncMock(return_value=[0.1] * test_settings.EMBEDDING_DIM)
    vector._client = AsyncMock()
    vector._client.query_points = AsyncMock(return_value=SimpleNamespace(points=[]))
    try:
        await vector.search("query", workspace_id="workspace-a")
        query_filter = vector._client.query_points.call_args.kwargs["query_filter"]
        workspace_conditions = [
            condition
            for condition in (query_filter.must or [])
            if isinstance(condition, FieldCondition)
            and condition.key == "workspace_id"
        ]
        assert len(workspace_conditions) == 1
        assert workspace_conditions[0].match.value == "workspace-a"
    finally:
        await vector._http_client.aclose()
