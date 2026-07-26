"""Tests for FirekeepRelay MCP tool functions."""

import json
from unittest.mock import patch, AsyncMock

import pytest
import pytest_asyncio

from app.mcp_server import (
    relay_broadcast,
    relay_get_messages,
    relay_post,
    relay_read,
    relay_claim,
    relay_lease,
    relay_release,
    relay_status,
    _validate_name,
)


async def _fake_release_script(r, key: str, agent_id: str) -> int:
    """Python implementation of the Lua release script for testing."""
    holder = await r.get(key)
    if not holder:
        return 0
    data = json.loads(holder)
    if data["agent_id"] == agent_id:
        await r.delete(key)
        return 1
    return -1


@pytest_asyncio.fixture(autouse=True)
async def mock_redis(redis):
    """Patch get_redis to return fakeredis for all MCP tool tests."""
    mock_release = AsyncMock(side_effect=_fake_release_script)
    with patch("app.mcp_server.get_redis", return_value=redis), \
         patch("app.mcp_server._run_release_script", mock_release):
        yield redis


class TestInputValidation:
    """Test _validate_name and input validation in tools."""

    def test_valid_names(self):
        assert _validate_name("my-channel", "channel") == "my-channel"
        assert _validate_name("build.v2", "channel") == "build.v2"
        assert _validate_name("agent_1", "agent") == "agent_1"

    def test_invalid_names_rejected(self):
        with pytest.raises(ValueError, match="Invalid channel"):
            _validate_name("", "channel")
        with pytest.raises(ValueError, match="Invalid channel"):
            _validate_name("has spaces", "channel")
        with pytest.raises(ValueError, match="Invalid channel"):
            _validate_name("a" * 201, "channel")
        with pytest.raises(ValueError, match="Invalid channel"):
            _validate_name("bad/slash", "channel")

    @pytest.mark.asyncio
    async def test_broadcast_rejects_invalid_channel(self):
        result = await relay_broadcast(channel="bad channel!", content="hi")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_claim_rejects_invalid_resource(self):
        result = await relay_claim(resource_id="bad resource!")
        assert "error" in result


class TestContentLimits:
    """Test content size limits and limit caps."""

    @pytest.mark.asyncio
    async def test_broadcast_rejects_oversized_content(self):
        result = await relay_broadcast(channel="chan", content="x" * 65537)
        assert "error" in result
        assert "64KB" in result["error"]

    @pytest.mark.asyncio
    async def test_post_rejects_oversized_content(self):
        result = await relay_post(content="x" * 65537)
        assert "error" in result
        assert "64KB" in result["error"]

    @pytest.mark.asyncio
    async def test_get_messages_caps_limit(self):
        result = await relay_get_messages(channel="chan", limit=500)
        assert "error" not in result


class TestClaimRelease:
    """Test claim and release with atomic Lua script."""

    @pytest.mark.asyncio
    async def test_claim_acquire_and_info(self, redis):
        result = await relay_claim(resource_id="file.txt", agent_id="agent-1", ttl_minutes=5)
        assert result["claimed"] is True
        assert result["agent_id"] == "agent-1"

        # Second claim should fail
        result2 = await relay_claim(resource_id="file.txt", agent_id="agent-2", ttl_minutes=5)
        assert result2["claimed"] is False
        assert result2["held_by"] == "agent-1"

    @pytest.mark.asyncio
    async def test_claim_accepts_file_path_resource_id(self, redis):
        result = await relay_claim(resource_id="src/app/main.py", agent_id="agent-1", ttl_minutes=5)
        assert result["claimed"] is True
        assert result["resource_id"] == "src.app.main.py"

    @pytest.mark.asyncio
    async def test_release_by_owner(self, redis):
        await relay_claim(resource_id="file.txt", agent_id="agent-1", ttl_minutes=5)
        result = await relay_release(resource_id="file.txt", agent_id="agent-1")
        assert result["released"] is True

    @pytest.mark.asyncio
    async def test_release_by_non_owner(self, redis):
        await relay_claim(resource_id="file.txt", agent_id="agent-1", ttl_minutes=5)
        result = await relay_release(resource_id="file.txt", agent_id="agent-2")
        assert result["released"] is False
        assert result["reason"] == "not owner"

    @pytest.mark.asyncio
    async def test_release_nonexistent(self, redis):
        result = await relay_release(resource_id="nothing", agent_id="agent-1")
        assert result["released"] is False
        assert "no active lease or claim" in result["reason"]

    @pytest.mark.asyncio
    async def test_release_active_lease(self, redis):
        lease = await relay_lease(resource_id="src/app/main.py", agent_id="agent-1", ttl_minutes=5)
        assert lease["acquired"] is True

        result = await relay_release(
            resource_id="src/app/main.py",
            agent_id="agent-1",
            fencing_token=lease["fencing_token"],
        )
        assert result["released"] is True


class TestRelayStatus:
    """Test relay_status pipeline optimization."""

    @pytest.mark.asyncio
    async def test_status_returns_structure(self, redis):
        result = await relay_status()
        assert "channels" in result
        assert "bulletin_count" in result
        assert "active_claims" in result
        assert "claims" in result

    @pytest.mark.asyncio
    async def test_status_with_claims(self, redis):
        await relay_claim(resource_id="res-1", agent_id="agent-1")
        await relay_claim(resource_id="res-2", agent_id="agent-2")
        result = await relay_status()
        assert result["active_claims"] == 2
        assert len(result["claims"]) == 2


class TestErrorHandling:
    """Test that all tools return error dicts when Redis is unavailable."""

    @pytest.mark.asyncio
    async def test_broadcast_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_broadcast(channel="chan", content="hi")
            assert "error" in result
            assert result["status"] == "unavailable"

    @pytest.mark.asyncio
    async def test_get_messages_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_get_messages(channel="chan")
            assert "error" in result

    @pytest.mark.asyncio
    async def test_post_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_post(content="hi")
            assert "error" in result

    @pytest.mark.asyncio
    async def test_read_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_read()
            assert "error" in result

    @pytest.mark.asyncio
    async def test_claim_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_claim(resource_id="x", agent_id="a")
            assert "error" in result

    @pytest.mark.asyncio
    async def test_release_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_release(resource_id="x", agent_id="a")
            assert "error" in result

    @pytest.mark.asyncio
    async def test_status_error_handling(self):
        with patch("app.mcp_server.get_redis", side_effect=Exception("connection refused")):
            result = await relay_status()
            assert "error" in result


class TestSettingsIntegration:
    """Test that settings values are used as defaults."""

    @pytest.mark.asyncio
    async def test_claim_uses_settings_default(self, redis):
        result = await relay_claim(resource_id="file.txt", agent_id="agent-1")
        assert result["claimed"] is True
        # Default from settings is 30 minutes
        assert result["ttl_minutes"] == 30

    @pytest.mark.asyncio
    async def test_post_uses_settings_default(self, redis):
        result = await relay_post(content="hello")
        assert result["status"] == "posted"
