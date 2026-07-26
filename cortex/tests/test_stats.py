"""Tests for the /memory/stats endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.stats import create_stats_router


@pytest.fixture()
def stats_app():
    """Create a minimal FastAPI app with the stats router for testing."""
    mock_graph = AsyncMock(spec=Neo4jClient)
    mock_vector = AsyncMock(spec=VectorClient)
    mock_redis = AsyncMock()

    # Default return values
    mock_graph.get_stats = AsyncMock(return_value={
        "node_count": 10,
        "edge_count": 25,
        "domains": ["infrastructure", "auth"],
        "top_tags": [
            {"tag": "auth", "count": 15},
            {"tag": "database", "count": 8},
        ],
    })
    mock_vector.get_stats = AsyncMock(return_value={
        "total": 42,
        "oldest_memory": "2025-01-01T00:00:00+00:00",
        "newest_memory": "2026-03-01T12:00:00+00:00",
        "namespace_counts": {"default": 30, "agent-1": 12},
    })
    mock_redis.llen = AsyncMock(return_value=3)

    app = FastAPI()
    router = create_stats_router(
        graph=mock_graph,
        vector=mock_vector,
        redis_client=mock_redis,
    )
    app.include_router(router)

    return app, mock_graph, mock_vector, mock_redis


class TestMemoryStatsEndpoint:
    def test_stats_returns_all_fields(self, stats_app):
        """Stats endpoint returns all expected fields with correct values."""
        app, mock_graph, mock_vector, mock_redis = stats_app

        with TestClient(app) as client:
            resp = client.get("/memory/stats")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_memories"] == 42
        assert body["graph_nodes"] == 10
        assert body["graph_edges"] == 25
        assert body["domains"] == ["infrastructure", "auth"]
        assert len(body["top_tags"]) == 2
        assert body["top_tags"][0]["tag"] == "auth"
        assert body["top_tags"][0]["count"] == 15
        assert body["dlq_depth"] == 3
        assert body["oldest_memory"] == "2025-01-01T00:00:00+00:00"
        assert body["newest_memory"] == "2026-03-01T12:00:00+00:00"
        assert body["namespace_counts"]["default"] == 30
        assert body["namespace_counts"]["agent-1"] == 12

    def test_stats_with_empty_stores(self, stats_app):
        """Stats endpoint handles empty stores gracefully."""
        app, mock_graph, mock_vector, mock_redis = stats_app

        mock_graph.get_stats = AsyncMock(return_value={
            "node_count": 0,
            "edge_count": 0,
            "domains": [],
            "top_tags": [],
        })
        mock_vector.get_stats = AsyncMock(return_value={
            "total": 0,
            "oldest_memory": None,
            "newest_memory": None,
            "namespace_counts": {},
        })
        mock_redis.llen = AsyncMock(return_value=0)

        with TestClient(app) as client:
            resp = client.get("/memory/stats")

        assert resp.status_code == 200
        body = resp.json()
        assert body["total_memories"] == 0
        assert body["graph_nodes"] == 0
        assert body["graph_edges"] == 0
        assert body["domains"] == []
        assert body["top_tags"] == []
        assert body["dlq_depth"] == 0
        assert body["oldest_memory"] is None
        assert body["newest_memory"] is None
        assert body["namespace_counts"] == {}

    def test_stats_handles_redis_error(self, stats_app):
        """Stats endpoint handles Redis errors gracefully."""
        app, mock_graph, mock_vector, mock_redis = stats_app

        mock_redis.llen = AsyncMock(side_effect=RuntimeError("Redis down"))

        with TestClient(app) as client:
            resp = client.get("/memory/stats")

        assert resp.status_code == 200
        body = resp.json()
        assert body["dlq_depth"] == 0
        # Other fields should still be populated
        assert body["total_memories"] == 42
