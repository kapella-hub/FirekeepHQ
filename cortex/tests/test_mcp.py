"""Tests for _get_client() concurrency safety in the MCP server."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

import app.mcp_server as mod
from app.mcp_server import _get_client


@pytest.fixture(autouse=True)
def _reset_client():
    """Reset the shared httpx client and lock between tests."""
    mod._client = None
    mod._client_lock = asyncio.Lock()
    yield
    mod._client = None


class TestGetClientConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_calls_create_only_one_client(self):
        """Concurrent _get_client() calls must only construct one AsyncClient."""
        call_count = 0
        original_init = httpx.AsyncClient.__init__

        def tracking_init(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_init(self, *args, **kwargs)

        with patch.object(httpx.AsyncClient, "__init__", tracking_init):
            clients = await asyncio.gather(
                _get_client(),
                _get_client(),
                _get_client(),
                _get_client(),
                _get_client(),
            )

        assert call_count == 1, f"Expected 1 client creation, got {call_count}"
        # All coroutines should return the exact same client instance
        for c in clients[1:]:
            assert c is clients[0]
