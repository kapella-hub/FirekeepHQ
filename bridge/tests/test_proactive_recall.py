"""Tests for proactive recall — memory fetching from FirekeepCortex."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.proactive_recall import fetch_relevant_memories


class TestFetchRelevantMemories:
    """Tests for fetch_relevant_memories."""

    @pytest.mark.asyncio
    async def test_returns_memories_on_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "sources": [
                {"content": "Fixed Redis connection leak", "score": 1.0,
                 "metadata": {"raw_score": 0.85}},
                {"content": "Used batch pipeline for speed", "score": 0.0,
                 "metadata": {"raw_score": 0.62}},
            ],
            "context_block": "...",
            "degraded": False,
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_relevant_memories(
                "implementing proactive recall feature",
                api_url="http://localhost:8100",
            )

        assert len(result) == 2
        assert result[0]["content"] == "Fixed Redis connection leak"
        assert result[0]["score"] == 0.85  # raw cosine, not normalized
        assert result[1]["score"] == 0.62

    @pytest.mark.asyncio
    async def test_filters_by_min_score_on_raw_cosine(self):
        """Defect #16: the floor operates on raw cosine, not on relative
        decay-mangled scores. A normalized-1.0 entry with low raw cosine is
        dropped; a normalized-0.0 entry with high raw cosine is kept."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "sources": [
                {"content": "noise pinned to 1.0", "score": 1.0,
                 "metadata": {"raw_score": 0.10}},
                {"content": "real match", "score": 0.0,
                 "metadata": {"raw_score": 0.90}},
            ],
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_relevant_memories(
                "some context for recall",
                api_url="http://localhost:8100",
                min_score=0.35,
            )

        assert len(result) == 1
        assert result[0]["content"] == "real match"

    @pytest.mark.asyncio
    async def test_skips_sources_without_raw_score(self):
        """Graph-only bare entries carry no cosine score and cannot be
        honestly ranked — they must not be injected."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "sources": [{"content": "bare graph node", "score": 1.0, "metadata": {}}],
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_relevant_memories(
                "some context for recall", api_url="http://localhost:8100"
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_skips_injection_when_degraded(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "sources": [{"content": "graph-only guess", "score": 1.0,
                         "metadata": {"raw_score": 0.9}}],
            "degraded": True,
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_relevant_memories(
                "some context for recall", api_url="http://localhost:8100"
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_http_error(self):
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_relevant_memories(
                "some context for recall",
                api_url="http://localhost:8100",
            )

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_if_context_too_short(self):
        result = await fetch_relevant_memories(
            "short",
            api_url="http://localhost:8100",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_empty_context(self):
        result = await fetch_relevant_memories(
            "",
            api_url="http://localhost:8100",
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_truncates_context_to_500_chars(self):
        long_context = "x" * 1000

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"sources": []}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            await fetch_relevant_memories(
                long_context,
                api_url="http://localhost:8100",
            )

        call_args = mock_client.post.call_args
        sent_payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert len(sent_payload["task"]) == 500

    @pytest.mark.asyncio
    async def test_sends_api_key_header_when_provided(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"sources": []}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            await fetch_relevant_memories(
                "some context for recall",
                api_url="http://localhost:8100",
                api_key="test-key-123",
            )

        call_args = mock_client.post.call_args
        sent_headers = call_args.kwargs.get("headers") or call_args[1].get("headers")
        assert sent_headers["X-API-Key"] == "test-key-123"

    @pytest.mark.asyncio
    async def test_requests_raw_format(self):
        """Defect #11: the hot path must skip LLM synthesis."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"sources": []}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("app.proactive_recall.httpx.AsyncClient", return_value=mock_client):
            await fetch_relevant_memories(
                "some context for recall", api_url="http://localhost:8100"
            )

        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert sent_payload["format"] == "raw"

    @pytest.mark.asyncio
    async def test_default_timeout_matches_server_budget(self):
        """Defect #11: 10s client timeout vs 30s+ server work = silent loss."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"sources": []}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "app.proactive_recall.httpx.AsyncClient", return_value=mock_client
        ) as mock_ctor:
            await fetch_relevant_memories(
                "some context for recall", api_url="http://localhost:8100"
            )

        assert mock_ctor.call_args.kwargs["timeout"] == 30.0
