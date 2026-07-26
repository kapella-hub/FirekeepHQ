"""Tests for the knowledge_ingest MCP tool (Task 7, SP2 docs->skills).

knowledge_ingest forwards to POST /knowledge/ingest (Task 6's REST route) and
returns its envelope. It must use the shared _get_client() proxy so the
caller's X-API-Key is forwarded per the confused-deputy pattern
(_CallerKeyAuth) — see test_confused_deputy.py for the generic proof that
_get_client()-based tools inherit that behavior; this file only asserts
knowledge_ingest is wired through _get_client() like its siblings
(corpus_ingest, skill_recall, skill_create).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_knowledge_ingest_calls_knowledge_ingest_route():
    """knowledge_ingest POSTs to /knowledge/ingest with content/source_name/source_type."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "corpus_source": "Provisioning Wiki",
        "status": "queued",
        "note": "classification + skill drafting queued",
    })
    mock_resp.raise_for_status = MagicMock()

    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client

        from app.mcp_server import knowledge_ingest
        await knowledge_ingest(
            content="1. Do the thing. 2. Do the other thing.",
            source_name="Provisioning Wiki",
            source_type="wiki",
        )

    mock_client.post.assert_awaited_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "/knowledge/ingest"
    body = kwargs["json"]
    assert body == {
        "content": "1. Do the thing. 2. Do the other thing.",
        "source_name": "Provisioning Wiki",
        "source_type": "wiki",
    }


@pytest.mark.asyncio
async def test_knowledge_ingest_returns_envelope_fields():
    """knowledge_ingest surfaces the async-202 REST envelope (corpus_source, status, note)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "corpus_source": "Provisioning Wiki",
        "status": "queued",
        "note": "classification + skill drafting queued",
    })
    mock_resp.raise_for_status = MagicMock()

    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client

        from app.mcp_server import knowledge_ingest
        result = await knowledge_ingest(content="text", source_name="Doc A")

    assert isinstance(result, str)
    assert "Provisioning Wiki" in result
    assert "queued" in result.lower()


@pytest.mark.asyncio
async def test_knowledge_ingest_default_source_name_and_type():
    """source_name/source_type default to Untitled/text, mirroring corpus_ingest."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "disposition": "reference",
        "corpus_source": "Untitled",
        "skills_queued": 0,
        "classify_ok": True,
        "note": None,
    })
    mock_resp.raise_for_status = MagicMock()

    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client

        from app.mcp_server import knowledge_ingest
        await knowledge_ingest(content="some text")

    body = mock_client.post.call_args[1]["json"]
    assert body["source_name"] == "Untitled"
    assert body["source_type"] == "text"


@pytest.mark.asyncio
async def test_knowledge_ingest_uses_shared_client_for_caller_key_forwarding():
    """knowledge_ingest must go through _get_client() (confused-deputy fix,
    SP1a): that's what attaches _CallerKeyAuth so the caller's X-API-Key
    (not a server-held key) reaches the REST layer. See
    test_confused_deputy.py for the end-to-end proof of that forwarding."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={
        "disposition": "reference",
        "corpus_source": "Doc",
        "skills_queued": 0,
        "classify_ok": True,
        "note": None,
    })
    mock_resp.raise_for_status = MagicMock()

    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_get.return_value = mock_client

        from app.mcp_server import knowledge_ingest
        await knowledge_ingest(content="some text", source_name="Doc")

    mock_get.assert_awaited_once()


@pytest.mark.asyncio
async def test_knowledge_ingest_http_error():
    """knowledge_ingest surfaces HTTP errors via the shared _format_error helper."""
    import httpx

    request = httpx.Request("POST", "http://cortex-api/knowledge/ingest")
    response = httpx.Response(status_code=500, request=request, text="boom")

    with patch("app.mcp_server._get_client") as mock_get:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=request, response=response)
        )
        mock_get.return_value = mock_client

        from app.mcp_server import knowledge_ingest
        result = await knowledge_ingest(content="some text", source_name="Doc")

    assert "500" in result
