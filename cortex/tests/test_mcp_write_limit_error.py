"""cortex-mcp says plainly when the memory write ceiling refused a write.

Agents write memory over MCP; the REST route answers 429 with a structured
detail and Retry-After (THREAT-MODEL §5.19). The tools return error STRINGS
rather than raising, and the generic branch rendered a 429 as "API returned
429 ... check FirekeepCortex logs" -- which tells an agent nothing it can act
on and does not say the write was dropped. slowapi's per-IP 429s carry no
Retry-After and a different body, and must render too.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


@pytest.fixture(autouse=True)
def _reset_client():
    import app.mcp_server as mod

    mod._client = None
    yield
    mod._client = None


def _limited(headers: dict | None = None, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=429,
        json=body if body is not None else {"detail": {
            "error_code": "MEMORY_WRITE_LIMITED",
            "detail": "Memory write limit reached for this credential: 300 writes "
                      "per 3600 seconds. Nothing was written.",
            "credential_id": "a11ce0000000a11c",
            "limit": 300, "window_seconds": 3600, "retry_after": 1234,
        }},
        headers=headers if headers is not None else {"Retry-After": "1234"},
        request=httpx.Request("POST", "http://test/memory/learn"),
    )


@pytest.mark.asyncio
async def test_memory_learn_reports_the_write_ceiling_and_when_to_retry():
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock,
                      return_value=_limited()):
        from app.mcp_server import memory_learn

        result = await memory_learn(action="a", outcome="b")
    assert result.startswith("Error:")
    assert "write limit" in result.lower()
    assert "NOT stored" in result
    assert "1234" in result
    assert "Stored memory" not in result


@pytest.mark.asyncio
async def test_memory_stream_reports_the_write_ceiling():
    with patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock,
                      return_value=_limited()):
        from app.mcp_server import memory_stream

        result = await memory_stream(source="ci", payload={"x": 1})
    assert "write limit" in result.lower()
    assert "1234" in result


def test_a_slowapi_429_without_retry_after_still_renders():
    from app.mcp_server import _format_error

    resp = _limited(headers={}, body={
        "error_code": "RATE_LIMITED",
        "detail": "Rate limit exceeded. Please slow down."})
    message = _format_error(httpx.HTTPStatusError("429", request=resp.request,
                                                  response=resp))
    assert message.startswith("Error:")
    assert "rate limit" in message.lower()
    assert "Retry-After" not in message
