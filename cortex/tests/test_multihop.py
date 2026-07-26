"""Tests for multi-hop graph reasoning (query_related_multihop)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import neo4j
import pytest

from app.config import Settings
from app.db.graph import Neo4jClient


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        NEO4J_URI="bolt://localhost:7687",
        NEO4J_USER="neo4j",
        NEO4J_PASSWORD="test",
    )


@pytest.fixture()
def client_with_driver(settings: Settings) -> Neo4jClient:
    """Neo4jClient with a mocked async driver."""
    client = Neo4jClient(settings)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock()

    mock_sess_ctx = AsyncMock()
    mock_sess_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_sess_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session.return_value = mock_sess_ctx

    client._driver = mock_driver
    client._mock_session = mock_session
    return client


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestMultihopConfig:
    def test_default_multihop_enabled(self, settings):
        assert settings.MULTIHOP_ENABLED is True

    def test_default_max_hops(self, settings):
        assert settings.MULTIHOP_MAX_HOPS == 3

    def test_default_decay_per_hop(self, settings):
        assert settings.MULTIHOP_DECAY_PER_HOP == 0.5

    def test_custom_values(self):
        s = Settings(
            NEO4J_PASSWORD="test",
            MULTIHOP_ENABLED=False,
            MULTIHOP_MAX_HOPS=5,
            MULTIHOP_DECAY_PER_HOP=0.7,
        )
        assert s.MULTIHOP_ENABLED is False
        assert s.MULTIHOP_MAX_HOPS == 5
        assert s.MULTIHOP_DECAY_PER_HOP == 0.7


# ---------------------------------------------------------------------------
# Method existence and signature
# ---------------------------------------------------------------------------


class TestMultihopMethodExists:
    def test_query_related_multihop_exists(self, settings):
        client = Neo4jClient(settings)
        assert hasattr(client, "query_related_multihop")
        assert callable(client.query_related_multihop)

    def test_private_fulltext_method_exists(self, settings):
        client = Neo4jClient(settings)
        assert hasattr(client, "_query_related_multihop_fulltext")


# ---------------------------------------------------------------------------
# Empty keywords
# ---------------------------------------------------------------------------


class TestMultihopEmptyKeywords:
    @pytest.mark.asyncio
    async def test_empty_concept_returns_empty(self, client_with_driver):
        result = await client_with_driver.query_related_multihop("a an the")
        assert result == []


# ---------------------------------------------------------------------------
# Fulltext query delegation
# ---------------------------------------------------------------------------


class TestMultihopFulltext:
    @pytest.mark.asyncio
    async def test_calls_session_run_with_cypher(self, client_with_driver):
        """Verify the method executes a Cypher query via the session."""
        mock_result = AsyncMock()
        mock_result.__aiter__ = lambda self: aiter([])

        async def aiter(items):
            for item in items:
                yield item

        client_with_driver._mock_session.run.return_value = mock_result

        result = await client_with_driver.query_related_multihop(
            "database performance tuning", limit=10
        )

        # Should have called session.run at least once
        assert client_with_driver._mock_session.run.called
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_returns_results_from_neo4j(self, client_with_driver):
        """Verify results are returned as list of dicts."""
        record1 = {
            "name": "database",
            "description": "Database system",
            "label": "Concept",
            "distance": 1,
            "score": 0.8,
        }

        async def mock_aiter(items):
            for item in items:
                yield item

        mock_result = AsyncMock()
        mock_result.__aiter__ = lambda self: mock_aiter([record1])

        client_with_driver._mock_session.run.return_value = mock_result

        results = await client_with_driver.query_related_multihop(
            "database performance", limit=5
        )
        assert len(results) == 1
        assert results[0]["name"] == "database"
        assert results[0]["score"] == 0.8

    @pytest.mark.asyncio
    async def test_fallback_on_client_error(self, client_with_driver):
        """When fulltext index doesn't exist, should fall back to query_related."""
        client_with_driver._mock_session.run.side_effect = neo4j.exceptions.ClientError(
            "no such index"
        )

        # Patch query_related to return a known result
        fallback_result = [{"name": "fallback", "description": "fb", "label": "Concept", "distance": 1}]
        client_with_driver.query_related = AsyncMock(return_value=fallback_result)

        results = await client_with_driver.query_related_multihop(
            "database performance", limit=5
        )
        assert results == fallback_result
        client_with_driver.query_related.assert_called_once()


# ---------------------------------------------------------------------------
# Decay calculation verification
# ---------------------------------------------------------------------------


class TestMultihopDecay:
    @pytest.mark.asyncio
    async def test_cypher_contains_case_when_decay(self, client_with_driver):
        """Verify the Cypher query includes CASE WHEN for distance-based decay."""
        mock_result = AsyncMock()

        async def empty_aiter(items):
            for item in items:
                yield item

        mock_result.__aiter__ = lambda self: empty_aiter([])
        client_with_driver._mock_session.run.return_value = mock_result

        await client_with_driver.query_related_multihop(
            "database config", limit=10, max_hops=3, decay_per_hop=0.5,
        )

        call_args = client_with_driver._mock_session.run.call_args
        cypher_query = call_args[0][0]

        # Should contain CASE WHEN clauses for each hop
        assert "WHEN 0 THEN 1.0" in cypher_query
        assert "WHEN 1 THEN 0.5" in cypher_query
        assert "WHEN 2 THEN 0.25" in cypher_query
        assert "WHEN 3 THEN 0.125" in cypher_query

    @pytest.mark.asyncio
    async def test_custom_decay_rate(self, client_with_driver):
        """Verify custom decay rate is reflected in the Cypher query."""
        mock_result = AsyncMock()

        async def empty_aiter(items):
            for item in items:
                yield item

        mock_result.__aiter__ = lambda self: empty_aiter([])
        client_with_driver._mock_session.run.return_value = mock_result

        await client_with_driver.query_related_multihop(
            "database config", limit=10, max_hops=2, decay_per_hop=0.7,
        )

        call_args = client_with_driver._mock_session.run.call_args
        cypher_query = call_args[0][0]

        assert "WHEN 0 THEN 1.0" in cypher_query
        assert "WHEN 1 THEN 0.7" in cypher_query
        # 0.7^2 = 0.48999... rounds to 0.48999999999999994 in float
        assert "WHEN 2 THEN" in cypher_query

    @pytest.mark.asyncio
    async def test_start_limit_param(self, client_with_driver):
        """Verify the start_limit parameter is passed to cap starting nodes."""
        mock_result = AsyncMock()

        async def empty_aiter(items):
            for item in items:
                yield item

        mock_result.__aiter__ = lambda self: empty_aiter([])
        client_with_driver._mock_session.run.return_value = mock_result

        await client_with_driver.query_related_multihop(
            "database config", limit=10,
        )

        call_args = client_with_driver._mock_session.run.call_args
        kwargs = call_args[1]  # keyword arguments passed to session.run

        assert kwargs.get("start_limit") == 20


# ---------------------------------------------------------------------------
# Namespace filtering
# ---------------------------------------------------------------------------


class TestMultihopNamespace:
    @pytest.mark.asyncio
    async def test_namespace_filter_included(self, client_with_driver):
        """Non-default namespace should inject a namespace filter clause."""
        mock_result = AsyncMock()

        async def empty_aiter(items):
            for item in items:
                yield item

        mock_result.__aiter__ = lambda self: empty_aiter([])
        client_with_driver._mock_session.run.return_value = mock_result

        await client_with_driver.query_related_multihop(
            "database config", limit=10, namespace="myproject",
        )

        call_args = client_with_driver._mock_session.run.call_args
        cypher_query = call_args[0][0]
        kwargs = call_args[1]

        assert "Namespace" in cypher_query
        assert kwargs.get("namespace") == "myproject"

    @pytest.mark.asyncio
    async def test_default_namespace_no_filter(self, client_with_driver):
        """Default namespace should not inject a namespace filter."""
        mock_result = AsyncMock()

        async def empty_aiter(items):
            for item in items:
                yield item

        mock_result.__aiter__ = lambda self: empty_aiter([])
        client_with_driver._mock_session.run.return_value = mock_result

        await client_with_driver.query_related_multihop(
            "database config", limit=10, namespace="default",
        )

        call_args = client_with_driver._mock_session.run.call_args
        kwargs = call_args[1]

        assert "namespace" not in kwargs
