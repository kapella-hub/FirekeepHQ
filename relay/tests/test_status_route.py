"""Tests for Relay /status REST endpoint handler."""

import json

import pytest
import pytest_asyncio
import fakeredis.aioredis


@pytest_asyncio.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class TestStatusRoute:
    @pytest.mark.asyncio
    async def test_status_empty(self, redis):
        """Status returns zeroed counts when relay has no activity."""
        from app.routes import handle_get_status
        result = await handle_get_status(redis)
        assert result["channel_count"] == 0
        assert result["bulletin_count"] == 0
        assert result["active_claims"] == 0
        assert result["claims"] == []
        assert isinstance(result["channels"], list)

    @pytest.mark.asyncio
    async def test_status_with_claims(self, redis):
        """Status returns claim data that the briefing aggregator expects."""
        from app.routes import handle_get_status

        # Create a claim directly in Redis (same format as relay_claim)
        claim_data = json.dumps({"agent_id": "agent-alpha", "timestamp": 1234567890})
        await redis.set("nr:claim:src/main.py", claim_data, ex=600)

        result = await handle_get_status(redis)
        assert result["active_claims"] == 1
        assert len(result["claims"]) == 1
        claim = result["claims"][0]
        assert claim["resource"] == "src/main.py"
        assert claim["agent_id"] == "agent-alpha"
        assert claim["expires_in"] > 0

    @pytest.mark.asyncio
    async def test_status_with_leases(self, redis):
        """Status includes lease data in claims list."""
        from app.routes import handle_get_status

        lease_data = json.dumps({
            "holder_id": "agent-beta",
            "fencing_token": 42,
            "timestamp": 1234567890,
        })
        await redis.set("nr:lease:config.yaml", lease_data, ex=300)

        result = await handle_get_status(redis)
        assert result["active_claims"] == 1
        claim = result["claims"][0]
        assert claim["resource"] == "config.yaml"
        assert claim["agent_id"] == "agent-beta"
        assert claim["fencing_token"] == 42
        assert claim.get("type") == "lease"

    @pytest.mark.asyncio
    async def test_status_with_channels(self, redis):
        """Status reflects active channels."""
        from app.pubsub import broadcast
        from app.routes import handle_get_status

        await broadcast(redis, "build", "test msg", "sender", [], backlog_size=10, backlog_ttl_seconds=3600)
        result = await handle_get_status(redis)
        assert "build" in result["channels"]
        assert result["channel_count"] >= 1

    @pytest.mark.asyncio
    async def test_status_mixed_claims_and_leases(self, redis):
        """Status combines both claims and leases in the claims list."""
        from app.routes import handle_get_status

        await redis.set("nr:claim:file1", json.dumps({"agent_id": "a1"}), ex=600)
        await redis.set("nr:lease:file2", json.dumps({"holder_id": "a2", "fencing_token": 1}), ex=300)

        result = await handle_get_status(redis)
        assert result["active_claims"] == 2
        resources = {c["resource"] for c in result["claims"]}
        assert "file1" in resources
        assert "file2" in resources
