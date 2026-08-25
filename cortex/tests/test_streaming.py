"""Tests for SSE streaming recall (app.streaming + app.engine.rag.recall_streaming)."""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.engine.rag import RAGEngine
from app.models import ContextQuery
from replay.config import ReplaySettings
from replay.reader import get_session_timeline


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
    async def test_graph_results_streamed_as_sources(self, mock_graph, mock_vector, engine):
        """Graph search results should produce source events."""
        mock_graph.query_related.return_value = [
            {"name": "auth", "description": "Authentication module", "label": "Concept",
             "distance": 1, "memory_ids": ["m-auth"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "m-auth": {"id": "m-auth", "status": "active"}
        }

        query = ContextQuery(task="fix auth", top_k=5)
        events = []
        async for event in engine.recall_streaming(query):
            events.append(event)

        source_events = [e for e in events if e["type"] == "source"]
        graph_sources = [e for e in source_events if e["data"]["store"] == "graph"]
        assert len(graph_sources) == 1
        assert graph_sources[0]["data"]["content"] == "Authentication module"

    @pytest.mark.asyncio
    async def test_description_less_graph_node_is_skipped(self, mock_graph, mock_vector, engine):
        """A bare node (name but no description) must not stream as a source.

        Mirrors _format_graph_entries' `if not description: continue` guard —
        a bare name is not memory content and must not compete with real
        memories for slots (SP0 C5, defect #13).
        """
        mock_graph.query_related.return_value = [
            {"name": "bare-node", "description": "", "label": "Concept", "distance": 1},
            {"name": "auth", "description": "Authentication module", "label": "Concept",
             "distance": 1, "memory_ids": ["m-auth"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "m-auth": {"id": "m-auth", "status": "active"}
        }

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
            {"name": "concept", "description": "A graph node", "label": "Concept",
             "distance": 1, "memory_ids": ["g1"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "g1": {"id": "g1", "status": "active"}
        }

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
    async def test_sse_content_type(self, mock_graph, mock_vector, mock_redis):
        """Endpoint should return text/event-stream content type."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)
        test_app.state.redis_client = mock_redis

        with TestClient(test_app) as client:
            response = client.post(
                "/memory/recall/stream",
                json={"task": "test query", "top_k": 5},
            )
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_sse_events_properly_formatted(self, mock_graph, mock_vector, mock_redis):
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
        test_app.state.redis_client = mock_redis

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
    async def test_sse_empty_results(self, mock_graph, mock_vector, mock_redis):
        """Empty results should still produce context + done events."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)

        test_app = FastAPI()
        test_app.include_router(router)
        test_app.state.redis_client = mock_redis

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
    async def test_sse_data_is_valid_json(self, mock_graph, mock_vector, mock_redis):
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
        test_app.state.redis_client = mock_redis

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


# ---------------------------------------------------------------------------
# Receipt parity — memory_read replay event + access/staleness bumps (D1)
#
# Parity target: main.py:1291-1342 (the non-streaming /memory/recall handler).
# It builds `accessed_ids = [s.metadata.get("id") for s in result.sources if
# s.metadata.get("id")]`, pipelines hincrby("memory:access_counts", ...) +
# hset("memory:last_recalled", ...) per id, bumps the untagged-call counter,
# and emits ONE `memory_read` replay event. Before this fix, none of that ran
# on the SSE path — SSE-recalled memories were invisible to OWM/compliance
# and their staleness clock never advanced.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture()
def wired_replay_emitter(monkeypatch, fake_redis):
    """Point the real replay emitter at `fake_redis` for this test only.

    `app.main._replay_emit` lazily calls `replay.emitter.init_emitter()` the
    first time it runs, then flips a module-global `_replay_initialized` flag
    so every later call in the process skips re-init — which would otherwise
    clobber this test's wiring with a real (or a prior test's) connection, or
    vice versa. Patching both module globals directly, scoped by `monkeypatch`,
    makes `emit()` (called via `_replay_emit`) and `get_session_timeline()`
    read/write the exact same fake_redis instance the endpoint's own
    access-count bumps land in.
    """
    import app.main as main_mod
    import replay.emitter as emitter_mod

    monkeypatch.setattr(main_mod, "_replay_initialized", True)
    monkeypatch.setattr(emitter_mod, "_redis", fake_redis)
    monkeypatch.setattr(emitter_mod, "_settings", ReplaySettings(ENABLED=True))
    return fake_redis


def _stream_app(rag, mock_graph, mock_vector, redis_client):
    from fastapi import FastAPI

    from app.streaming import create_streaming_router

    router = create_streaming_router(rag, mock_graph, mock_vector)
    test_app = FastAPI()
    test_app.include_router(router)
    test_app.state.redis_client = redis_client
    return test_app


def _async_client(app):
    """httpx.AsyncClient over ASGITransport, not fastapi.testclient.TestClient:
    TestClient runs the app in its own thread with its own event loop, so a
    fakeredis instance touched there gets bound to that loop — the SAME
    fakeredis instance is then unusable from the outer test coroutine's loop
    (`RuntimeError: ... is bound to a different event loop`). ASGITransport
    runs the app in-process on the caller's own loop instead."""
    import httpx

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


class TestStreamReceiptParity:
    """D1: SSE recall must reach parity with POST /memory/recall — one
    memory_read replay event plus access_counts/last_recalled bumps, fired
    after the stream finishes."""

    @pytest.mark.asyncio
    async def test_stream_emits_memory_read_with_ids_and_no_top_score(
        self, mock_graph, mock_vector, wired_replay_emitter
    ):
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.85, "text": "Fix login timeout",
             "metadata": {"id": "v1", "source": "log"}},
            {"id": "v2", "score": 0.6, "text": "Auth module update",
             "metadata": {"id": "v2"}},
        ]

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        test_app = _stream_app(rag, mock_graph, mock_vector, wired_replay_emitter)

        async with _async_client(test_app) as client:
            resp = await client.post(
                "/memory/recall/stream",
                json={"task": "fix auth", "top_k": 5, "namespace": "default"},
                headers={"X-Session-Id": "sess-stream-1", "X-Agent-Id": "agent-a"},
            )
            _ = resp.text  # drain the body so the `finally` receipt fires

        timeline = await get_session_timeline(
            wired_replay_emitter, "sess-stream-1", event_type="memory_read"
        )
        events = timeline["events"]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert set(payload["memory_ids"]) == {"v1", "v2"}
        assert payload["result_count"] == 2
        assert "top_score" not in payload
        assert payload["trigger"] is None
        assert payload["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_stream_bumps_access_counts_and_last_recalled(
        self, mock_graph, mock_vector, wired_replay_emitter
    ):
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.9, "text": "Fix login timeout",
             "metadata": {"id": "v1"}},
        ]

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        test_app = _stream_app(rag, mock_graph, mock_vector, wired_replay_emitter)

        async with _async_client(test_app) as client:
            resp = await client.post(
                "/memory/recall/stream",
                json={"task": "fix auth", "top_k": 5},
                headers={"X-Session-Id": "sess-stream-2", "X-Agent-Id": "agent-a"},
            )
            _ = resp.text

        assert await wired_replay_emitter.hget("memory:access_counts", "v1") == "1"
        last_recalled = await wired_replay_emitter.hget("memory:last_recalled", "v1")
        assert last_recalled is not None
        datetime.fromisoformat(last_recalled)  # a real ISO-8601 timestamp

    @pytest.mark.asyncio
    async def test_graph_only_sources_carry_no_id_same_as_non_streaming(
        self, mock_graph, mock_vector, wired_replay_emitter
    ):
        """Parity, not a new bug: graph-sourced entries have no
        `metadata["id"]` in EITHER path (rag.py's graph branch in
        `recall_streaming` never sets one, same as `_format_graph_entries`
        for the non-streaming path), so they're silently excluded from
        accessed_ids on both. This pins that the SSE fix does not change that
        pre-existing behavior."""
        mock_graph.query_related.return_value = [
            {"name": "auth", "description": "Authentication module", "label": "Concept",
             "distance": 1, "memory_ids": ["m-auth"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "m-auth": {"id": "m-auth", "status": "active"}
        }

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        test_app = _stream_app(rag, mock_graph, mock_vector, wired_replay_emitter)

        async with _async_client(test_app) as client:
            resp = await client.post(
                "/memory/recall/stream",
                json={"task": "fix auth", "top_k": 5},
                headers={"X-Session-Id": "sess-stream-3", "X-Agent-Id": "agent-a"},
            )
            _ = resp.text

        timeline = await get_session_timeline(
            wired_replay_emitter, "sess-stream-3", event_type="memory_read"
        )
        payload = timeline["events"][0]["payload"]
        assert payload["memory_ids"] == []
        assert payload["result_count"] == 0
        assert await wired_replay_emitter.hlen("memory:access_counts") == 0
