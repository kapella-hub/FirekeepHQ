"""SP0 A2 surfacing — /health and memory_health report backfill queue + DLQ depths."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _reset_mcp_client():
    import app.mcp_server as mod

    mod._client = None
    yield
    mod._client = None


def _healthy_backends(mock_graph, mock_vector, mock_redis):
    mock_graph.ping = AsyncMock()
    mock_vector.ping = AsyncMock()
    mock_vector.memory_count = AsyncMock(return_value=1)
    mock_redis.ping = AsyncMock()


class TestHealthBackfillDepths:
    def test_health_reports_backfill_depths(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        _healthy_backends(mock_graph, mock_vector, mock_redis)
        mock_redis.xlen = AsyncMock(return_value=3)
        mock_redis.llen = AsyncMock(return_value=2)

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["backfill_queue_depth"] == 3
        assert body["backfill_dlq_depth"] == 2
        mock_redis.xlen.assert_awaited_with("memory:backfill")
        mock_redis.llen.assert_awaited_with("memory:backfill:dlq")

    def test_health_survives_backfill_probe_failure(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        _healthy_backends(mock_graph, mock_vector, mock_redis)
        mock_redis.xlen = AsyncMock(side_effect=RuntimeError("no streams"))
        mock_redis.llen = AsyncMock(return_value=0)

        resp = test_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["backfill_queue_depth"] is None


def _health_json(queue_depth, dlq_depth) -> httpx.Response:
    return httpx.Response(
        status_code=200,
        json={
            "status": "ok",
            "services": {"redis": {"status": "connected", "detail": None}},
            "backfill_queue_depth": queue_depth,
            "backfill_dlq_depth": dlq_depth,
        },
        request=httpx.Request("GET", "http://test"),
    )


class TestMcpMemoryHealth:
    @pytest.mark.asyncio
    async def test_shows_backfill_depths(self):
        with patch.object(
            httpx.AsyncClient,
            "get",
            new_callable=AsyncMock,
            return_value=_health_json(4, 1),
        ):
            from app.mcp_server import memory_health

            result = await memory_health()
        assert "backfill queue: 4 pending" in result
        assert "backfill DLQ: 1" in result
        assert "ATTENTION" in result

    @pytest.mark.asyncio
    async def test_zero_dlq_has_no_attention_marker(self):
        with patch.object(
            httpx.AsyncClient,
            "get",
            new_callable=AsyncMock,
            return_value=_health_json(0, 0),
        ):
            from app.mcp_server import memory_health

            result = await memory_health()
        assert "backfill DLQ: 0" in result
        assert "ATTENTION" not in result

    @pytest.mark.asyncio
    async def test_omits_lines_when_server_lacks_fields(self):
        """Older server body without the new fields: no backfill lines, no crash."""
        resp = httpx.Response(
            status_code=200,
            json={"status": "ok", "services": {}},
            request=httpx.Request("GET", "http://test"),
        )
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=resp
        ):
            from app.mcp_server import memory_health

            result = await memory_health()
        assert "backfill" not in result
