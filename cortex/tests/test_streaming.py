"""Tests for SSE streaming recall (app.streaming + app.engine.rag.recall_streaming)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.engine.rag import RAGEngine
from app.models import ContextQuery


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_graph() -> AsyncMock:
    g = AsyncMock()
    g.query_related = AsyncMock(return_value=[])
    return g


@pytest.fixture()
def mock_vector() -> AsyncMock:
    v = AsyncMock()
    v.search = AsyncMock(return_value=[])
    return v


@pytest.fixture()
def engine(mock_graph, mock_vector) -> RAGEngine:
    return RAGEngine(graph=mock_graph, vector=mock_vector)


# ---------------------------------------------------------------------------
# recall_streaming — unit tests on the RAGEngine method
# ---------------------------------------------------------------------------


class TestRecallStreaming:
    @pytest.mark.asyncio
    async def test_empty_results_produce_done_event(self, engine):
        """When both stores return empty, we should still get context + done events."""
        query = ContextQuery(task="test query", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        types = [e["type"] for e in events]
        assert "context" in types
        assert "done" in types

        done_event = next(e for e in events if e["type"] == "done")
        assert done_event["data"]["total_sources"] == 0

    @pytest.mark.asyncio
    async def test_vector_results_streamed_as_sources(self, mock_vector, engine):
        """Vector search results should produce source events."""
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.85, "text": "Fix login timeout", "metadata": {"source": "log"}},
            {"id": "v2", "score": 0.6, "text": "Auth module update", "metadata": {}},
        ]

        query = ContextQuery(task="fix auth", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        assert len(source_events) >= 2

        vector_sources = [e for e in source_events if e["data"]["store"] == "vector"]
        assert len(vector_sources) == 2
        assert vector_sources[0]["data"]["content"] == "Fix login timeout"
        assert vector_sources[0]["data"]["score"] == 0.85

    @pytest.mark.asyncio
    async def test_graph_results_streamed_as_sources(self, mock_graph, engine):
        """Graph search results should produce source events."""
        mock_graph.query_related.return_value = [
            {"name": "auth", "description": "Authentication module", "label": "Concept", "distance": 1},
        ]

        query = ContextQuery(task="fix auth", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        graph_sources = [e for e in source_events if e["data"]["store"] == "graph"]
        assert len(graph_sources) == 1
        assert graph_sources[0]["data"]["content"] == "Authentication module"

    @pytest.mark.asyncio
    async def test_description_less_graph_node_is_skipped(self, mock_graph, engine):
        """A bare node (name but no description) must not stream as a source.

        Mirrors _format_graph_entries' `if not description: continue` guard —
        a bare name is not memory content and must not compete with real
        memories for slots (SP0 C5, defect #13).
        """
        mock_graph.query_related.return_value = [
            {"name": "bare-node", "description": "", "label": "Concept", "distance": 1},
            {"name": "auth", "description": "Authentication module", "label": "Concept", "distance": 1},
        ]

        query = ContextQuery(task="fix auth", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        graph_sources = [e for e in source_events if e["data"]["store"] == "graph"]
        assert len(graph_sources) == 1
        assert graph_sources[0]["data"]["content"] == "Authentication module"
        assert all(s["data"]["content"] != "bare-node" for s in graph_sources)

    @pytest.mark.asyncio
    async def test_vector_search_forwards_project_and_score_floor(self, mock_vector, engine):
        """Streaming recall must forward project + score_threshold, mirroring
        _safe_vector in the non-streaming path — otherwise scoping/relevance
        floor is silently dropped on the SSE path (final-review fix)."""
        query = ContextQuery(task="fix auth", top_k=5, project="firekeep")
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        mock_vector.search.assert_awaited_once()
        _, kwargs = mock_vector.search.call_args
        assert kwargs["project"] == "firekeep"
        assert kwargs["score_threshold"] == engine._settings.RECALL_SCORE_FLOOR

    @pytest.mark.asyncio
    async def test_context_event_contains_markdown(self, mock_vector, engine):
        """Context event should contain a markdown context block."""
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.9, "text": "Important memory", "metadata": {}},
        ]

        query = ContextQuery(task="test", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        context_event = next(e for e in events if e["type"] == "context")
        assert "Memory Recall" in context_event["data"]["context_block"]
        assert context_event["data"]["score"] > 0

    @pytest.mark.asyncio
    async def test_done_event_has_request_id(self, engine):
        """Done event should include a request_id."""
        query = ContextQuery(task="test", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        done_event = next(e for e in events if e["type"] == "done")
        assert "request_id" in done_event["data"]
        assert len(done_event["data"]["request_id"]) > 0

    @pytest.mark.asyncio
    async def test_partial_failure_still_yields_results(self, mock_graph, mock_vector, engine):
        """If one store fails, the other should still yield source events."""
        mock_vector.search.side_effect = RuntimeError("vector down")
        mock_graph.query_related.return_value = [
            {"name": "concept", "description": "A graph node", "label": "Concept", "distance": 1},
        ]

        query = ContextQuery(task="test", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        assert len(source_events) == 1
        assert source_events[0]["data"]["store"] == "graph"

        done_event = next(e for e in events if e["type"] == "done")
        assert done_event["data"]["total_sources"] == 1

    @pytest.mark.asyncio
    async def test_both_stores_fail_gracefully(self, mock_graph, mock_vector, engine):
        """If both stores fail, we should still get context + done events."""
        mock_vector.search.side_effect = RuntimeError("vector down")
        mock_graph.query_related.side_effect = RuntimeError("graph down")

        query = ContextQuery(task="test", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        assert len(source_events) == 0

        done_event = next(e for e in events if e["type"] == "done")
        assert done_event["data"]["total_sources"] == 0

    @pytest.mark.asyncio
    async def test_event_order(self, mock_vector, mock_graph, engine):
        """Events should be: sources first, then context, then done."""
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.8, "text": "vector result", "metadata": {}},
        ]
        mock_graph.query_related.return_value = [
            {"name": "g1", "description": "graph result", "label": "C", "distance": 1},
        ]

        query = ContextQuery(task="test", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        types = [e["type"] for e in events]
        # context and done should be at the end
        context_idx = types.index("context")
        done_idx = types.index("done")
        source_indices = [i for i, t in enumerate(types) if t == "source"]

        for si in source_indices:
            assert si < context_idx
        assert context_idx < done_idx


# ---------------------------------------------------------------------------
# SSE formatting — test the streaming router
# ---------------------------------------------------------------------------


class TestSSEEndpoint:
    """Test the SSE endpoint via the streaming router."""

    @pytest.mark.asyncio
    async def test_sse_content_type(self, mock_graph, mock_vector):
        """Endpoint should return text/event-stream content type."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)

        with TestClient(test_app) as client:
            response = client.post(
                "/memory/recall/stream",
                json={"task": "test query", "top_k": 5},
            )
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_sse_events_properly_formatted(self, mock_graph, mock_vector):
        """SSE events should follow the event/data format."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.85, "text": "Fix login timeout", "metadata": {}},
        ]

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)

        with TestClient(test_app) as client:
            response = client.post(
                "/memory/recall/stream",
                json={"task": "test query", "top_k": 5},
            )
            body = response.text

            # Should contain properly formatted SSE events
            assert "event: sources\n" in body
            assert "event: context\n" in body
            assert "event: done\n" in body
            assert "data: " in body

            # Each event block should end with double newline
            blocks = body.strip().split("\n\n")
            assert len(blocks) >= 3  # at least source + context + done

    @pytest.mark.asyncio
    async def test_sse_empty_results(self, mock_graph, mock_vector):
        """Empty results should still produce context + done events."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)

        with TestClient(test_app) as client:
            response = client.post(
                "/memory/recall/stream",
                json={"task": "nothing here", "top_k": 5},
            )
            body = response.text

            assert "event: context\n" in body
            assert "event: done\n" in body

            # Parse done event data
            for block in body.strip().split("\n\n"):
                lines = block.strip().split("\n")
                if len(lines) >= 2 and lines[0] == "event: done":
                    data_line = lines[1].replace("data: ", "")
                    done_data = json.loads(data_line)
                    assert done_data["total_sources"] == 0

    @pytest.mark.asyncio
    async def test_sse_data_is_valid_json(self, mock_graph, mock_vector):
        """All data lines in SSE events should be valid JSON."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.7, "text": "Some result", "metadata": {}},
        ]

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)

        with TestClient(test_app) as client:
            response = client.post(
                "/memory/recall/stream",
                json={"task": "test", "top_k": 5},
            )
            body = response.text

            for block in body.strip().split("\n\n"):
                lines = block.strip().split("\n")
                for line in lines:
                    if line.startswith("data: "):
                        data_str = line[6:]
                        # Should be valid JSON
                        parsed = json.loads(data_str)
                        assert isinstance(parsed, dict)
