"""Tests for FastAPI API endpoints and exception handlers."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from app.db.vector import FIREKEEP_UUID_NAMESPACE
from app.exceptions import (
    GraphConnectionError,
    VectorStoreError,
)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_ok(self, test_client, mock_graph, mock_vector, mock_redis):
        """Health endpoint with all services connected returns 'ok'."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=42)
        mock_redis.ping = AsyncMock()

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "services" in body
        assert body["services"]["graph"]["status"] == "connected"
        assert body["services"]["qdrant"]["status"] == "connected"
        assert body["services"]["redis"]["status"] == "connected"

    def test_health_degraded_when_graph_down(self, test_client, mock_graph, mock_vector, mock_redis):
        """Health returns 'degraded' when Neo4j is unreachable."""
        mock_graph.ping = AsyncMock(side_effect=RuntimeError("Neo4j down"))
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=None)
        mock_redis.ping = AsyncMock()

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["services"]["graph"]["status"] == "disconnected"
        assert body["services"]["qdrant"]["status"] == "connected"
        assert body["services"]["redis"]["status"] == "connected"

    def test_health_degraded_when_qdrant_down(self, test_client, mock_graph, mock_vector, mock_redis):
        """Health returns 'degraded' when Qdrant is unreachable."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock(side_effect=RuntimeError("Qdrant down"))
        mock_vector.memory_count = AsyncMock(return_value=None)
        mock_redis.ping = AsyncMock()

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["services"]["qdrant"]["status"] == "disconnected"

    def test_health_degraded_when_redis_down(self, test_client, mock_graph, mock_vector, mock_redis):
        """Health returns 'degraded' when Redis is unreachable."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=None)
        mock_redis.ping = AsyncMock(side_effect=RuntimeError("Redis down"))

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["services"]["redis"]["status"] == "disconnected"

    def test_health_caches_response(self, test_client, mock_graph, mock_vector, mock_redis):
        """Second health call within TTL should not re-probe backends."""
        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=42)
        mock_redis.ping = AsyncMock()

        # First call — probes everything
        resp1 = test_client.get("/health")
        assert resp1.status_code == 200

        # Reset mocks to track second call
        mock_graph.ping.reset_mock()
        mock_vector.ping.reset_mock()
        mock_vector.memory_count.reset_mock()
        mock_redis.ping.reset_mock()

        # Second call — should use cache, no backend calls
        resp2 = test_client.get("/health")
        assert resp2.status_code == 200
        assert resp2.json() == resp1.json()

        mock_graph.ping.assert_not_called()
        mock_vector.ping.assert_not_called()
        mock_redis.ping.assert_not_called()

    def test_health_cache_expires(self, test_client, mock_graph, mock_vector, mock_redis):
        """Health cache should expire after TTL."""
        from datetime import datetime, timezone, timedelta

        mock_graph.ping = AsyncMock()
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=42)
        mock_redis.ping = AsyncMock()

        # First call
        test_client.get("/health")

        # Simulate time passing beyond TTL
        from app import main as main_module
        main_module._health_cache_time = datetime.now(timezone.utc) - timedelta(seconds=30)

        mock_graph.ping.reset_mock()

        # Call after cache expired — should re-probe
        resp = test_client.get("/health")
        assert resp.status_code == 200
        mock_graph.ping.assert_called_once()

    def test_health_degraded_includes_error_detail(self, test_client, mock_graph, mock_vector, mock_redis):
        """Disconnected services include generic detail (no internal info leak)."""
        mock_graph.ping = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_vector.ping = AsyncMock()
        mock_vector.memory_count = AsyncMock(return_value=None)
        mock_redis.ping = AsyncMock()

        resp = test_client.get("/health")
        body = resp.json()
        assert body["services"]["graph"]["detail"] is not None
        # Detail should be generic — no internal host/port/driver info leaked
        assert body["services"]["graph"]["detail"] == "Service unreachable"


# ---------------------------------------------------------------------------
# POST /memory/recall
# ---------------------------------------------------------------------------


class TestMemoryRecall:
    def test_recall_returns_recall_response(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.query_related.return_value = [
            {
                "name": "auth",
                "description": "Authentication module",
                "label": "Concept",
                "distance": 1,
            }
        ]
        mock_vector.search.return_value = [
            {
                "id": "v1",
                "score": 0.85,
                "text": "Fix login timeout",
                "metadata": {"source": "action_log", "tags": ["auth"], "domain": "general"},
            }
        ]

        resp = test_client.post(
            "/memory/recall",
            json={"task": "Fix auth bug", "tags": ["auth"], "top_k": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "context_block" in body
        assert "sources" in body
        assert "score" in body
        assert isinstance(body["sources"], list)

    def test_recall_empty_stores(self, test_client, mock_graph, mock_vector):
        """Both stores return nothing -- should still produce a valid response."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []

        resp = test_client.post(
            "/memory/recall", json={"task": "unknown topic"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["sources"] == []
        assert body["score"] == 0.0
        assert "No relevant memories found" in body["context_block"]

    def test_recall_missing_task_field(self, test_client):
        resp = test_client.post("/memory/recall", json={})
        assert resp.status_code == 422  # validation error


# ---------------------------------------------------------------------------
# POST /memory/learn
# ---------------------------------------------------------------------------


class TestMemoryLearn:
    def test_learn_stores_and_returns_ids(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={
                "action": "Increased pool size",
                "outcome": "Timeout resolved",
                "resolution": "Updated config",
                "tags": ["db"],
                "domain": "infra",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "stored"
        assert body["graph_id"] == "graph-id-1"
        assert body["vector_id"] == "vector-id-1"

    def test_learn_without_resolution(
        self, test_client, mock_graph, mock_vector
    ):
        resp = test_client.post(
            "/memory/learn",
            json={"action": "a", "outcome": "o"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "stored"

    def test_learn_constructs_correct_text_for_vector(
        self, test_client, mock_graph, mock_vector
    ):
        """Verify the text sent to vector.upsert includes action|outcome|resolution."""
        mock_graph.merge_action_log.return_value = "id"
        mock_vector.upsert.return_value = "vid"

        test_client.post(
            "/memory/learn",
            json={
                "action": "Rebuild index",
                "outcome": "Search speed improved",
                "resolution": "Ran REINDEX",
                "tags": ["search"],
                "domain": "db",
            },
        )

        call_args = mock_vector.upsert.call_args
        text_arg = call_args.kwargs.get("text") or call_args[1].get("text") or call_args[0][0]
        assert "Rebuild index" in text_arg
        assert "Search speed improved" in text_arg
        assert "Resolution: Ran REINDEX" in text_arg
        expected_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, text_arg))
        assert mock_graph.merge_action_log.call_args.kwargs["memory_id"] == expected_id

    def test_learn_missing_required_fields(self, test_client):
        resp = test_client.post("/memory/learn", json={"action": "only action"})
        assert resp.status_code == 422

    def test_learn_graph_failure_returns_partial(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.side_effect = RuntimeError("neo4j down")
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={"action": "test", "outcome": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["graph_id"] is None
        assert body["vector_id"] == "vector-id-1"

    def test_learn_vector_failure_returns_partial(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.side_effect = RuntimeError("qdrant down")

        resp = test_client.post(
            "/memory/learn",
            json={"action": "test", "outcome": "test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["graph_id"] == "graph-id-1"
        assert body["vector_id"] is None

    def test_learn_both_fail_returns_503(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.side_effect = RuntimeError("neo4j down")
        mock_vector.upsert.side_effect = RuntimeError("qdrant down")

        resp = test_client.post(
            "/memory/learn",
            json={"action": "test", "outcome": "test"},
        )
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# POST /memory/stream
# ---------------------------------------------------------------------------


class TestMemoryStream:
    def test_stream_single_event(self, test_client, mock_redis):
        resp = test_client.post(
            "/memory/stream",
            json={
                "source": "ci",
                "payload": {"build": "123", "status": "failed"},
                "tags": ["ci"],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert body["queued"] == 1
        mock_redis.pipeline.assert_called_once()
        mock_redis._pipeline.lpush.assert_called_once()
        mock_redis._pipeline.execute.assert_called_once()

    def test_stream_batch_events(self, test_client, mock_redis):
        events = [
            {"source": "ci", "payload": {"id": "1"}, "tags": ["a"]},
            {"source": "ci", "payload": {"id": "2"}, "tags": ["b"]},
            {"source": "ci", "payload": {"id": "3"}, "tags": ["c"]},
        ]
        resp = test_client.post("/memory/stream", json=events)
        assert resp.status_code == 200
        body = resp.json()
        assert body["queued"] == 3
        assert mock_redis._pipeline.lpush.call_count == 3

    def test_stream_batch_exceeds_max_returns_422(self, test_client, mock_redis):
        """Sending more than MAX_BATCH_SIZE (100) events returns 422."""
        events = [
            {"source": "ci", "payload": {"id": str(i)}}
            for i in range(101)
        ]
        resp = test_client.post("/memory/stream", json=events)
        assert resp.status_code == 422
        assert "Batch size exceeds maximum" in resp.json()["detail"]

    def test_stream_redis_failure(self, test_client, mock_redis):
        """Redis pipeline failure should surface as a 502."""
        mock_redis._pipeline.execute = AsyncMock(side_effect=Exception("connection refused"))

        resp = test_client.post(
            "/memory/stream",
            json={"source": "ci", "payload": {"x": 1}},
        )
        assert resp.status_code == 502

    def test_stream_missing_payload(self, test_client):
        resp = test_client.post(
            "/memory/stream", json={"source": "ci"}
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Exception handler mapping
# ---------------------------------------------------------------------------


class TestExceptionHandlers:
    def test_graph_error_on_recall_graceful_degradation(
        self, test_client, mock_graph, mock_vector
    ):
        """When graph fails during recall, RAG engine catches it and returns
        vector-only results (200, not 503)."""
        mock_graph.query_related.side_effect = GraphConnectionError("down")
        mock_vector.search.return_value = [
            {
                "id": "v1",
                "score": 0.7,
                "text": "Vector result survives graph failure",
                "metadata": {"source": "action_log", "tags": [], "domain": "general"},
            }
        ]

        resp = test_client.post(
            "/memory/recall", json={"task": "test graceful degradation"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["sources"]) == 1
        assert body["sources"][0]["store"] == "vector"

    def test_graph_error_on_learn_returns_partial(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.side_effect = GraphConnectionError(
            "Neo4j unreachable"
        )
        resp = test_client.post(
            "/memory/learn",
            json={"action": "a", "outcome": "o"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["graph_id"] is None

    def test_vector_error_on_learn_returns_partial(
        self, test_client, mock_graph, mock_vector
    ):
        mock_graph.merge_action_log.return_value = "id"
        mock_vector.upsert.side_effect = VectorStoreError("Qdrant down")

        resp = test_client.post(
            "/memory/learn",
            json={"action": "a", "outcome": "o"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["vector_id"] is None

    def test_stream_ingestion_error_returns_502(
        self, test_client, mock_redis
    ):
        mock_redis._pipeline.execute = AsyncMock(side_effect=Exception("Redis down"))
        resp = test_client.post(
            "/memory/stream",
            json={"source": "s", "payload": {"k": "v"}},
        )
        assert resp.status_code == 502


# ---------------------------------------------------------------------------
# POST /memory/feedback
# ---------------------------------------------------------------------------


class TestMemoryFeedback:
    def test_feedback_success(self, test_client, mock_vector):
        mock_vector.set_feedback = AsyncMock()

        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": ["id-1", "id-2"], "useful": True, "comment": "helpful"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "recorded"
        assert body["updated"] == 2
        assert mock_vector.set_feedback.call_count == 2

    def test_feedback_partial_failure(self, test_client, mock_vector):
        call_count = 0

        async def set_feedback_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("Qdrant error")

        mock_vector.set_feedback = AsyncMock(side_effect=set_feedback_side_effect)

        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": ["id-1", "id-2", "id-3"], "useful": False},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "recorded"
        assert body["updated"] == 2  # 1 of 3 failed

    def test_feedback_empty_ids_rejected(self, test_client):
        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": [], "useful": True},
        )
        assert resp.status_code == 422

    def test_feedback_missing_useful_rejected(self, test_client):
        resp = test_client.post(
            "/memory/feedback",
            json={"memory_ids": ["id-1"]},
        )
        assert resp.status_code == 422

    def test_feedback_emits_replay_receipt(self, test_client, mock_vector):
        """POST /memory/feedback emits a memory_feedback replay event carrying
        the ids/useful bit -- the APPLIED stage of memory_read -> memory_feedback
        -> session grade. The comment body must never appear in the payload."""
        mock_vector.set_feedback = AsyncMock()

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/feedback",
                json={"memory_ids": ["m1", "m2"], "useful": True, "comment": "spot on"},
                headers={"X-Session-Id": "sess-fb", "X-Agent-Id": "agent-x"},
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call = mock_emit.call_args
            assert call.args[0] == "memory_feedback"
            assert call.kwargs.get("session_id") == "sess-fb"
            assert call.kwargs.get("agent_id") == "agent-x"
            p = call.kwargs.get("payload")
            assert p["memory_ids"] == ["m1", "m2"]
            assert p["useful"] is True
            assert p["comment_present"] is True
            assert "comment" not in p  # comment body never leaves
            assert p["updated"] == 2


# ---------------------------------------------------------------------------
# Session Context Propagation (X-Session-Id / X-Agent-Id → replay emit)
# ---------------------------------------------------------------------------


class TestSessionContextPropagation:
    """Verify that X-Session-Id and X-Agent-Id headers reach _replay_emit."""

    def test_recall_propagates_session_id(
        self, test_client, mock_graph, mock_vector
    ):
        """Recall endpoint passes X-Session-Id header to _replay_emit."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/recall",
                json={"task": "test session propagation"},
                headers={"X-Session-Id": "sess-abc-123"},
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs.kwargs.get("session_id") == "sess-abc-123"

    def test_recall_propagates_agent_id(
        self, test_client, mock_graph, mock_vector
    ):
        """Recall endpoint passes X-Agent-Id header to _replay_emit."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/recall",
                json={"task": "test agent propagation"},
                headers={"X-Agent-Id": "agent-007"},
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs.kwargs.get("agent_id") == "agent-007"

    def test_recall_defaults_to_unknown_without_headers(
        self, test_client, mock_graph, mock_vector
    ):
        """Without session headers, _replay_emit receives 'unknown'."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/recall",
                json={"task": "no headers test"},
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs.kwargs.get("session_id") == "unknown"
            assert call_kwargs.kwargs.get("agent_id") == "unknown"

    def test_learn_propagates_session_and_agent_id(
        self, test_client, mock_graph, mock_vector
    ):
        """Learn endpoint passes both X-Session-Id and X-Agent-Id to _replay_emit."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/learn",
                json={"action": "tested headers", "outcome": "propagated"},
                headers={
                    "X-Session-Id": "sess-xyz-789",
                    "X-Agent-Id": "coder-1",
                },
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs.kwargs.get("session_id") == "sess-xyz-789"
            assert call_kwargs.kwargs.get("agent_id") == "coder-1"

    def test_learn_defaults_to_unknown_without_headers(
        self, test_client, mock_graph, mock_vector
    ):
        """Learn without session headers defaults to 'unknown'."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
            resp = test_client.post(
                "/memory/learn",
                json={"action": "no headers", "outcome": "defaults"},
            )
            assert resp.status_code == 200
            mock_emit.assert_called_once()
            call_kwargs = mock_emit.call_args
            assert call_kwargs.kwargs.get("session_id") == "unknown"
            assert call_kwargs.kwargs.get("agent_id") == "unknown"

    def test_recall_without_session_header_increments_counter(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Recall with no X-Session-Id increments the daily untagged counter."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []
        mock_redis.incr = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)

        with patch("app.main._replay_emit", new_callable=AsyncMock):
            resp = test_client.post(
                "/memory/recall",
                json={"task": "test query", "top_k": 1},
            )
            assert resp.status_code == 200

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expected_key = f"cortex:untagged_calls:{today}"
        incr_calls = [c.args[0] for c in mock_redis.incr.await_args_list]
        assert expected_key in incr_calls

    def test_recall_with_session_header_does_not_increment_counter(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Recall WITH X-Session-Id does not increment the counter."""
        mock_graph.query_related.return_value = []
        mock_vector.search.return_value = []
        mock_redis.incr = AsyncMock(return_value=1)

        with patch("app.main._replay_emit", new_callable=AsyncMock):
            resp = test_client.post(
                "/memory/recall",
                json={"task": "test query", "top_k": 1},
                headers={"X-Session-Id": "abc-123"},
            )
            assert resp.status_code == 200

        assert mock_redis.incr.await_count == 0

    def test_learn_without_session_header_increments_counter(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Learn with no X-Session-Id increments the daily untagged counter."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"
        mock_redis.incr = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)

        with patch("app.main._replay_emit", new_callable=AsyncMock):
            resp = test_client.post(
                "/memory/learn",
                json={"action": "no headers", "outcome": "defaults"},
            )
            assert resp.status_code == 200

        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        expected_key = f"cortex:untagged_calls:{today}"
        incr_calls = [c.args[0] for c in mock_redis.incr.await_args_list]
        assert expected_key in incr_calls

    def test_untagged_calls_admin_endpoint(
        self, test_client, mock_redis
    ):
        """GET /admin/untagged-calls returns counts per day with total."""
        mock_redis.get = AsyncMock(return_value="7")
        resp = test_client.get("/admin/untagged-calls?days=2")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 14  # 7 per day x 2 days
        assert len(body["by_day"]) == 2


# ---------------------------------------------------------------------------
# agent_id persistence on /memory/learn (Qdrant payload)
# ---------------------------------------------------------------------------


class TestAgentIdPersistence:
    """X-Agent-Id / X-Session-Id headers must flow into the Qdrant payload so
    /memory/contributors can group by them. ActionLog.project also belongs in
    the metadata dict passed to vector.upsert.
    """

    def test_learn_persists_agent_id_from_header(
        self, test_client, mock_graph, mock_vector
    ):
        """When X-Agent-Id is sent, it's stored in the metadata dict passed to upsert."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={"action": "did X", "outcome": "got Y", "domain": "test"},
            headers={"X-Agent-Id": "alex"},
        )
        assert resp.status_code == 200

        call_kwargs = mock_vector.upsert.call_args.kwargs
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("agent_id") == "alex"

    def test_learn_without_agent_id_header_stores_unknown(
        self, test_client, mock_graph, mock_vector
    ):
        """No X-Agent-Id header -> metadata.agent_id == 'unknown' (not absent)."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={"action": "did X", "outcome": "got Y", "domain": "test"},
        )
        assert resp.status_code == 200

        call_kwargs = mock_vector.upsert.call_args.kwargs
        metadata = call_kwargs.get("metadata") or {}
        assert metadata.get("agent_id") == "unknown"

    def test_learn_persists_session_id_from_header(
        self, test_client, mock_graph, mock_vector
    ):
        """X-Session-Id flows into metadata too (for future correlation)."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={"action": "a", "outcome": "o"},
            headers={"X-Session-Id": "sess-xyz", "X-Agent-Id": "ada"},
        )
        assert resp.status_code == 200

        metadata = mock_vector.upsert.call_args.kwargs.get("metadata") or {}
        assert metadata.get("session_id") == "sess-xyz"
        assert metadata.get("agent_id") == "ada"

    def test_learn_persists_project_from_body(
        self, test_client, mock_graph, mock_vector
    ):
        """ActionLog.project flows into the metadata dict (contributors endpoint reads it)."""
        mock_graph.merge_action_log.return_value = "graph-id-1"
        mock_vector.upsert.return_value = "vector-id-1"

        resp = test_client.post(
            "/memory/learn",
            json={"action": "a", "outcome": "o", "project": "Firekeep"},
        )
        assert resp.status_code == 200

        metadata = mock_vector.upsert.call_args.kwargs.get("metadata") or {}
        # field_validator lowercases project
        assert metadata.get("project") == "firekeep"


# ---------------------------------------------------------------------------
# SP0 B2 — recall bumps the access-count accumulator (best-effort HINCRBY)
# ---------------------------------------------------------------------------


class TestRecallAccessCounts:
    def test_recall_increments_access_counts_hash(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        mock_graph.query_related.return_value = []
        mock_graph.query_related_multihop.return_value = []
        mock_vector.search.return_value = [
            {
                "id": "v1",
                "score": 0.85,
                "text": "Fix login timeout",
                "metadata": {"id": "v1", "source": "action_log", "tags": [],
                             "domain": "general"},
            }
        ]

        resp = test_client.post(
            "/memory/recall",
            json={"task": "Fix auth bug", "top_k": 5, "format": "raw"},
        )
        assert resp.status_code == 200
        mock_redis._pipeline.hincrby.assert_any_call("memory:access_counts", "v1", 1)
        # last-recall timestamp recorded alongside the access count (feeds the
        # skill staleness sweep); value is an ISO timestamp, so match on key.
        hset_keys = [c.args[:2] for c in mock_redis._pipeline.hset.call_args_list]
        assert ("memory:last_recalled", "v1") in hset_keys
        mock_redis._pipeline.execute.assert_called()

    def test_recall_skips_hincrby_for_graph_only_results(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Graph entries have no point id — nothing to increment; recall must
        not error."""
        mock_graph.query_related.return_value = [
            {"name": "auth", "description": "Authentication module",
             "label": "Concept", "distance": 1}
        ]
        mock_graph.query_related_multihop.return_value = [
            {"name": "auth", "description": "Authentication module",
             "label": "Concept", "distance": 1}
        ]
        mock_vector.search.return_value = []

        resp = test_client.post(
            "/memory/recall", json={"task": "Fix auth bug", "format": "raw"}
        )
        assert resp.status_code == 200
        mock_redis._pipeline.hincrby.assert_not_called()


class TestRecallTrigger:
    """Proactive Recall (2026-08-18): the optional `trigger` field marks pushed
    recalls so the exposure change is attributable in replay — and its absence
    must behave exactly as before (the existing recall suite passes unedited)."""

    def test_trigger_absent_is_none(self):
        from app.models import ContextQuery
        q = ContextQuery(task="how do we deploy")
        assert q.trigger is None

    def test_trigger_accepted_and_bounded(self):
        import pydantic
        import pytest

        from app.models import ContextQuery
        assert ContextQuery(task="t", trigger="prompt-hook").trigger == "prompt-hook"
        with pytest.raises(pydantic.ValidationError):
            ContextQuery(task="t", trigger="x" * 33)
