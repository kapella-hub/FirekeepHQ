"""Tests for the FirekeepCortex MCP server tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.mcp_server import (
    _get_client,
    memory_health,
    memory_learn,
    memory_recall,
    memory_stream,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    """Create a mock httpx.Response with the given JSON body."""
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test"),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client between tests."""
    import app.mcp_server as mod

    mod._client = None
    yield
    if mod._client and not mod._client.is_closed:
        # Synchronously close isn't available; just reset
        mod._client = None


# ---------------------------------------------------------------------------
# memory_recall
# ---------------------------------------------------------------------------


class TestMemoryRecall:
    @pytest.mark.asyncio
    async def test_basic_recall(self):
        mock_resp = _mock_response(
            {"context_block": "## Memories\n- Fix auth timeout", "sources": [], "score": 0.85}
        )
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_recall(task="fix auth bug")
        assert result == "## Memories\n- Fix auth timeout"

    @pytest.mark.asyncio
    async def test_recall_with_tags_and_top_k(self):
        mock_resp = _mock_response({"context_block": "context", "sources": [], "score": 0.5})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            result = await memory_recall(task="deploy", tags=["infra"], top_k=3)
        call_json = mock_post.call_args[1]["json"]
        assert call_json["task"] == "deploy"
        assert call_json["tags"] == ["infra"]
        assert call_json["top_k"] == 3
        assert result == "context"

    @pytest.mark.asyncio
    async def test_recall_no_tags_omits_key(self):
        mock_resp = _mock_response({"context_block": "ctx", "sources": [], "score": 0.0})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_recall(task="test")
        call_json = mock_post.call_args[1]["json"]
        assert "tags" not in call_json

    @pytest.mark.asyncio
    async def test_recall_api_error_returns_message(self):
        mock_resp = httpx.Response(
            status_code=503,
            json={"detail": "unavailable"},
            request=httpx.Request("POST", "http://test"),
        )
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_recall(task="fail")
        assert "Error:" in result
        assert "unavailable" in result.lower() or "503" in result
        assert "Suggestion:" in result

    @pytest.mark.asyncio
    async def test_recall_connection_error_returns_message(self):
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock,
            side_effect=httpx.ConnectError("Connection refused"),
        ):
            result = await memory_recall(task="fail")
        assert "Error:" in result
        assert "Cannot connect" in result
        assert "Suggestion:" in result

    @pytest.mark.asyncio
    async def test_recall_422_returns_detail(self):
        mock_resp = httpx.Response(
            status_code=422,
            json={"detail": "top_k must be >= 1"},
            request=httpx.Request("POST", "http://test"),
        )
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_recall(task="fail", top_k=0)
        assert "Error:" in result
        assert "Invalid input" in result
        assert "top_k must be >= 1" in result

    @pytest.mark.asyncio
    async def test_recall_does_not_reuse_identity_from_previous_call(self):
        mock_resp = _mock_response({"context_block": "ctx", "sources": [], "score": 0.0})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_recall(task="first", session_id="sess-1", agent_id="agent-1")
            await memory_recall(task="second")

        first_headers = mock_post.call_args_list[0].kwargs["headers"]
        second_headers = mock_post.call_args_list[1].kwargs["headers"]

        assert first_headers["X-Session-Id"] == "sess-1"
        assert first_headers["X-Agent-Id"] == "agent-1"
        assert second_headers == {}


# ---------------------------------------------------------------------------
# memory_learn
# ---------------------------------------------------------------------------


class TestMemoryLearn:
    @pytest.mark.asyncio
    async def test_basic_learn(self):
        mock_resp = _mock_response({"status": "stored", "graph_id": "g-1", "vector_id": "v-1"})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_learn(action="fixed bug", outcome="tests pass")
        assert "Stored memory" in result
        assert "fixed bug" in result
        assert "general" in result
        assert "future recall" in result

    @pytest.mark.asyncio
    async def test_learn_with_resolution_and_tags(self):
        mock_resp = _mock_response({"status": "stored", "graph_id": "g-2", "vector_id": "v-2"})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_learn(
                action="scaled DB pool",
                outcome="timeouts gone",
                resolution="set pool=20",
                tags=["db"],
                domain="infra",
            )
        call_json = mock_post.call_args[1]["json"]
        assert call_json["resolution"] == "set pool=20"
        assert call_json["tags"] == ["db"]
        assert call_json["domain"] == "infra"

    @pytest.mark.asyncio
    async def test_learn_omits_none_resolution(self):
        mock_resp = _mock_response({"status": "stored", "graph_id": "g-3", "vector_id": "v-3"})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_learn(action="test", outcome="ok")
        call_json = mock_post.call_args[1]["json"]
        assert "resolution" not in call_json


# ---------------------------------------------------------------------------
# memory_stream
# ---------------------------------------------------------------------------


class TestMemoryStream:
    @pytest.mark.asyncio
    async def test_basic_stream(self):
        mock_resp = _mock_response({"status": "queued", "queued": 1})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_stream(source="ci", payload={"build": "123"})
        assert "1 event(s)" in result

    @pytest.mark.asyncio
    async def test_stream_with_tags(self):
        mock_resp = _mock_response({"status": "queued", "queued": 1})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_stream(source="ci", payload={"x": 1}, tags=["ci"])
        call_json = mock_post.call_args[1]["json"]
        assert call_json["tags"] == ["ci"]

    @pytest.mark.asyncio
    async def test_stream_no_tags_omits_key(self):
        mock_resp = _mock_response({"status": "queued", "queued": 1})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await memory_stream(source="ide", payload={"file": "main.py"})
        call_json = mock_post.call_args[1]["json"]
        assert "tags" not in call_json


# ---------------------------------------------------------------------------
# memory_health
# ---------------------------------------------------------------------------


class TestMemoryHealth:
    @pytest.mark.asyncio
    async def test_all_healthy(self):
        mock_resp = _mock_response(
            {
                "status": "ok",
                "services": {
                    "redis": {"status": "connected"},
                    "graph": {"status": "connected"},
                    "qdrant": {"status": "connected"},
                },
            }
        )
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_health()
        assert "Status: ok" in result
        assert "redis: connected" in result
        assert "graph: connected" in result
        assert "qdrant: connected" in result

    @pytest.mark.asyncio
    async def test_degraded_with_detail(self):
        mock_resp = _mock_response(
            {
                "status": "degraded",
                "services": {
                    "redis": {"status": "connected"},
                    "graph": {"status": "disconnected", "detail": "timeout"},
                    "qdrant": {"status": "connected"},
                },
            }
        )
        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_health()
        assert "Status: degraded" in result
        assert "graph: disconnected (timeout)" in result


# ---------------------------------------------------------------------------
# _get_client
# ---------------------------------------------------------------------------


class TestGetClient:
    @pytest.mark.asyncio
    async def test_returns_client(self):
        client = await _get_client()
        assert isinstance(client, httpx.AsyncClient)
        assert not client.is_closed

    @pytest.mark.asyncio
    async def test_reuses_client(self):
        c1 = await _get_client()
        c2 = await _get_client()
        assert c1 is c2

    @pytest.mark.asyncio
    async def test_client_has_no_static_api_key_header(self):
        """SP1a §4.2: the proxy holds no key of its own — the caller's key is
        attached per-request by _CallerKeyAuth (confused deputy eliminated)."""
        import app.mcp_server as mod
        from unittest.mock import MagicMock, patch

        mod._client = None
        mock_settings = MagicMock()
        mock_settings.FIREKEEP_API_URL = "http://localhost:8000"
        mock_settings.MCP_CLIENT_TIMEOUT = 90.0
        try:
            with patch("app.mcp_server.get_settings", return_value=mock_settings):
                client = await _get_client()
                assert "x-api-key" not in client.headers
                assert isinstance(client.auth, mod._CallerKeyAuth)
        finally:
            mod._client = None


# ---------------------------------------------------------------------------
# Error handling on learn/stream/health
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_learn_http_error(self):
        mock_resp = httpx.Response(
            status_code=401,
            json={"detail": "unauthorized"},
            request=httpx.Request("POST", "http://test"),
        )
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_learn(action="test", outcome="ok")
        assert "Error:" in result
        assert "Authentication failed" in result

    @pytest.mark.asyncio
    async def test_learn_connection_error(self):
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            result = await memory_learn(action="test", outcome="ok")
        assert "Cannot connect" in result

    @pytest.mark.asyncio
    async def test_stream_http_error(self):
        mock_resp = httpx.Response(
            status_code=500,
            text="internal error",
            request=httpx.Request("POST", "http://test"),
        )
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_stream(source="ci", payload={"x": 1})
        assert "Error:" in result
        assert "500" in result

    @pytest.mark.asyncio
    async def test_health_connection_error(self):
        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock,
            side_effect=httpx.ConnectError("refused"),
        ):
            result = await memory_health()
        assert "Cannot connect" in result

    @pytest.mark.asyncio
    async def test_learn_natural_language_response(self):
        mock_resp = _mock_response({"status": "stored", "graph_id": "g-1", "vector_id": "v-1"})
        with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp):
            result = await memory_learn(
                action="a" * 100, outcome="ok", domain="infra",
            )
        assert "Stored memory in domain 'infra'" in result
        assert "a" * 80 in result
        assert "..." in result
        assert "future recall" in result
