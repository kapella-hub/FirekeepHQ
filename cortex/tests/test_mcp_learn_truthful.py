"""A1 — memory_learn MCP tool must report partial writes honestly (defect #1).

/memory/learn deliberately returns HTTP 200 with status="partial" and
vector_id=None when the embedding/Qdrant half fails (main.py:1059). The MCP
tool previously reported "Stored memory" on ANY 200 — presenting silent data
loss as success. These tests pin the truthful behavior.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test"),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests (same pattern as test_mcp_memory_tools.py)."""
    import app.mcp_server as mod

    mod._client = None
    yield
    mod._client = None


class TestTruthfulMemoryLearn:
    @pytest.mark.asyncio
    async def test_full_success_reports_stored(self):
        mock_resp = _mock_response(
            {"status": "stored", "graph_id": "g-1", "vector_id": "v-1", "namespace": "default"}
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="fixed the auth bug", outcome="tests pass")
        assert "Stored memory" in result
        assert "WARNING" not in result

    @pytest.mark.asyncio
    async def test_partial_vectorless_write_warns(self):
        """status=partial + vector_id=None => explicit not-recallable warning."""
        mock_resp = _mock_response(
            {"status": "partial", "graph_id": "g-1", "vector_id": None, "namespace": "default"}
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="fixed the auth bug", outcome="tests pass")
        assert "WARNING: partial write" in result
        assert "WITHOUT a vector" in result
        assert "NOT semantically recallable" in result
        assert "Stored memory" not in result

    @pytest.mark.asyncio
    async def test_partial_vectorless_mentions_backfill_when_queued(self):
        """When the server reports backfill_queued=True (Task 2), say so."""
        mock_resp = _mock_response(
            {
                "status": "partial",
                "graph_id": "g-1",
                "vector_id": None,
                "namespace": "default",
                "backfill_queued": True,
            }
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="a", outcome="b")
        assert "queued for automatic backfill" in result

    @pytest.mark.asyncio
    async def test_partial_vectorless_without_backfill_says_no_retry_queued(self):
        mock_resp = _mock_response(
            {"status": "partial", "graph_id": "g-1", "vector_id": None, "namespace": "default"}
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="a", outcome="b")
        assert "No backfill was queued" in result

    @pytest.mark.asyncio
    async def test_partial_graphless_write_warns(self):
        """The other partial shape: graph failed, vector succeeded."""
        mock_resp = _mock_response(
            {"status": "partial", "graph_id": None, "vector_id": "v-1", "namespace": "default"}
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="a", outcome="b")
        assert "WARNING: partial write" in result
        assert "vector store only" in result

    @pytest.mark.asyncio
    async def test_unparseable_body_reports_unknown_status(self):
        """A 200 with a non-JSON body must not be reported as success."""
        bad_resp = httpx.Response(
            status_code=200, text="not json", request=httpx.Request("POST", "http://test")
        )
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=bad_resp
        ):
            from app.mcp_server import memory_learn

            result = await memory_learn(action="a", outcome="b")
        assert "WARNING" in result
        assert "write status unknown" in result
        assert "Stored memory" not in result
