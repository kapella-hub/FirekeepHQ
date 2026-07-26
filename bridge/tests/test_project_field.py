"""Tests for session-level project field + attributed distillates (SP0 D2)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.distiller import Distiller
from app.session import SessionManager


@pytest.fixture
def distiller():
    d = Distiller(Settings())
    d._client = AsyncMock()
    return d


def _ok_response():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "stored", "vector_id": "v-1"}
    mock_response.raise_for_status = MagicMock()
    return mock_response


class TestSessionProjectField:
    @pytest.mark.asyncio
    async def test_start_session_stores_project(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice", project="firekeep")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["project"] == "firekeep"

    @pytest.mark.asyncio
    async def test_start_session_defaults_project_empty(self, mock_redis):
        mgr = SessionManager(mock_redis, Settings())
        await mgr.start_session("goal", agent_id="alice")
        mapping = mock_redis.hset.call_args_list[0].kwargs["mapping"]
        assert mapping["project"] == ""


class TestCtxStartSessionProject:
    @pytest.mark.asyncio
    async def test_ctx_start_session_forwards_project(self):
        from app.mcp_server import ctx_start_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
        ):
            mgr = AsyncMock()
            mgr.start_session = AsyncMock(
                return_value={"session_id": "abc", "created_at": "now"}
            )
            mock_get.return_value = mgr
            await ctx_start_session("goal", project="firekeep")

        assert mgr.start_session.call_args.kwargs["project"] == "firekeep"


class TestDistillerAttribution:
    @pytest.mark.asyncio
    async def test_distill_sends_identity_headers_and_project(self, distiller):
        distiller._client.post = AsyncMock(return_value=_ok_response())
        data = {
            "goal": "t", "plan": "", "decisions": [], "progress": [], "tags": [],
            "agent_id": "alice", "project": "firekeep",
        }
        result = await distiller.distill(data, outcome="done", session_id="sess-1")
        assert result["status"] == "success"
        kwargs = distiller._client.post.call_args.kwargs
        assert kwargs["headers"]["X-Session-Id"] == "sess-1"
        assert kwargs["headers"]["X-Agent-Id"] == "alice"
        assert kwargs["json"]["project"] == "firekeep"

    @pytest.mark.asyncio
    async def test_distill_omits_project_and_ids_when_absent(self, distiller):
        """When the session declared no project, the distiller must NOT
        fabricate one (spec D2)."""
        distiller._client.post = AsyncMock(return_value=_ok_response())
        data = {"goal": "t", "plan": "", "decisions": [], "progress": [], "tags": []}
        await distiller.distill(data)
        kwargs = distiller._client.post.call_args.kwargs
        assert "project" not in kwargs["json"]
        assert "X-Session-Id" not in kwargs["headers"]
        assert "X-Agent-Id" not in kwargs["headers"]
