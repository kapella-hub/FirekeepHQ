"""Tests for Neo4jClient (app.db.graph)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import neo4j
import pytest

from app.config import Settings
from app.db.graph import Neo4jClient
from app.db.vector import mem2_seed
from app.exceptions import GraphConnectionError
from app.models import ActionLog


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        NEO4J_URI="bolt://localhost:7687",
        NEO4J_USER="neo4j",
        NEO4J_PASSWORD="test",
    )


@pytest.fixture()
def client_with_driver(settings: Settings) -> Neo4jClient:
    """Neo4jClient with a mocked async driver already assigned.

    The mock supports two patterns used in the source code:

    1. ``session.run(...)`` — used by query_related, query_resolutions,
       ensure_indexes (simple reads / DDL).
    2. ``session.begin_transaction() -> tx -> tx.run(...)`` — used by
       merge_action_log and merge_knowledge_nodes (explicit transactions).

    Both ``session`` and ``tx`` are stored on the client instance for
    convenient assertion access in tests.
    """
    client = Neo4jClient(settings)

    # --- Transaction mock (for merge_action_log / merge_knowledge_nodes) ---
    mock_tx = AsyncMock()
    mock_tx.run = AsyncMock()
    mock_tx.commit = AsyncMock()
    mock_tx.rollback = AsyncMock()

    # --- Session mock ---
    # begin_transaction() is a coroutine that returns the tx directly
    mock_session = AsyncMock()
    mock_session.run = AsyncMock()
    mock_session.begin_transaction = AsyncMock(return_value=mock_tx)

    # Build async context manager for driver.session()
    mock_sess_ctx = AsyncMock()
    mock_sess_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    mock_sess_ctx.__aexit__ = AsyncMock(return_value=False)

    # Driver — session() is a regular (sync) call returning the async CM
    mock_driver = MagicMock()
    mock_driver.session.return_value = mock_sess_ctx

    client._driver = mock_driver
    client._mock_session = mock_session
    client._mock_tx = mock_tx
    return client


# ---------------------------------------------------------------------------
# _ensure_driver / connection guard
# ---------------------------------------------------------------------------


class TestEnsureDriver:
    def test_raises_when_driver_not_connected(self, settings):
        client = Neo4jClient(settings)
        assert client._driver is None
        with pytest.raises(GraphConnectionError, match="not connected"):
            client._ensure_driver()

    def test_returns_driver_when_connected(self, client_with_driver):
        driver = client_with_driver._ensure_driver()
        assert driver is not None


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_deterministic(self, settings):
        client = Neo4jClient(settings)
        h1 = client._content_hash("hello")
        h2 = client._content_hash("hello")
        assert h1 == h2

    def test_different_inputs_different_hashes(self, settings):
        client = Neo4jClient(settings)
        h1 = client._content_hash("hello")
        h2 = client._content_hash("world")
        assert h1 != h2

    def test_returns_configurable_hex_chars(self, settings):
        client = Neo4jClient(settings)
        h = client._content_hash("test")
        assert len(h) == settings.CONTENT_HASH_LENGTH
        assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# _extract_keywords
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_extracts_meaningful_words(self):
        kws = Neo4jClient._extract_keywords("Fix the authentication timeout bug")
        assert "authentication" in kws
        assert "timeout" in kws
        assert "the" not in kws  # stopword

    def test_respects_max_keywords(self):
        kws = Neo4jClient._extract_keywords("a very long sentence with many meaningful words here", max_keywords=2)
        assert len(kws) <= 2

    def test_removes_short_tokens(self):
        kws = Neo4jClient._extract_keywords("go do it or be an ox")
        # All 2-char or shorter tokens + stopwords should be filtered
        for kw in kws:
            assert len(kw) >= 3

    def test_empty_input(self):
        assert Neo4jClient._extract_keywords("") == []

    def test_only_stopwords(self):
        assert Neo4jClient._extract_keywords("the is are was") == []

    def test_deduplicates_tokens_for_bigrams(self):
        """Repeated tokens should not produce duplicate bigrams like 'auth auth'."""
        kws = Neo4jClient._extract_keywords("auth auth timeout auth")
        for kw in kws:
            if " " in kw:
                parts = kw.split()
                assert parts[0] != parts[1], f"Duplicate bigram found: {kw}"
        # Should contain "auth timeout" bigram but not "auth auth"
        assert "auth auth" not in kws


# ---------------------------------------------------------------------------
# merge_action_log
# ---------------------------------------------------------------------------


class TestMergeActionLog:
    @pytest.mark.asyncio
    async def test_merge_action_log_with_resolution(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-id-123"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(
            action="Increased pool",
            outcome="Timeout fixed",
            resolution="Changed config",
            tags=["db", "perf"],
            domain="infra",
        )
        result_id = await client_with_driver.merge_action_log(
            log, memory_id="qdrant-memory-1"
        )

        assert result_id == "elem-id-123"
        tx.run.assert_called_once()
        tx.commit.assert_called_once()

        # Verify Cypher parameters include content hashes
        call_kwargs = tx.run.call_args.kwargs
        assert call_kwargs["domain"] == "infra"
        assert call_kwargs["action"] == "Increased pool"
        assert call_kwargs["outcome"] == "Timeout fixed"
        assert call_kwargs["resolution"] == "Changed config"
        assert call_kwargs["tags"] == ["db", "perf"]
        # identity-v2 D4: the hash input is the canonical mem2 scope encoding,
        # not the bare text — no workspace_id/namespace were passed here, so
        # they default to None/"default", exactly as merge_action_log's own
        # signature defaults do.
        assert call_kwargs["action_id"] == client_with_driver._content_hash(
            mem2_seed(None, "default", "Increased pool")
        )
        assert call_kwargs["outcome_id"] == client_with_driver._content_hash(
            mem2_seed(None, "default", "Timeout fixed")
        )
        assert call_kwargs["resolution_id"] == client_with_driver._content_hash(
            mem2_seed(None, "default", "Changed config")
        )
        assert call_kwargs["memory_id"] == "qdrant-memory-1"

        query = tx.run.call_args.args[0]
        assert query.count("coalesce(a.memory_ids, []) + [$memory_id]") == 1
        assert query.count("coalesce(o.memory_ids, []) + [$memory_id]") == 1
        assert query.count("coalesce(r.memory_ids, []) + [$memory_id]") == 1
        assert "$memory_id IN coalesce(a.memory_ids, [])" in query
        assert "$memory_id IN coalesce(o.memory_ids, [])" in query
        assert "$memory_id IN coalesce(r.memory_ids, [])" in query

    @pytest.mark.asyncio
    async def test_merge_action_log_without_resolution(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-id-456"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="a", outcome="o")
        result_id = await client_with_driver.merge_action_log(log)
        assert result_id == "elem-id-456"

        call_kwargs = tx.run.call_args.kwargs
        assert call_kwargs["resolution"] is None
        assert call_kwargs["resolution_id"] is None
        assert call_kwargs["memory_id"] is None

    @pytest.mark.asyncio
    async def test_merge_action_log_no_result_raises(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value=None)
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="a", outcome="o")
        with pytest.raises(GraphConnectionError, match="returned no result"):
            await client_with_driver.merge_action_log(log)

    @pytest.mark.asyncio
    async def test_merge_action_log_canonicalizes_domain_and_tags(self, client_with_driver):
        """Domain and tags should be canonicalized (lowercased etc.) before Cypher params."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-id-789"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(
            action="Deploy service",
            outcome="Success",
            tags=["Infra", "DB-Pool", "  Auth "],
            domain="  Infra ",
        )
        await client_with_driver.merge_action_log(log)

        call_kwargs = tx.run.call_args.kwargs
        assert call_kwargs["domain"] == "infra"
        assert call_kwargs["tags"] == ["infra", "db_pool", "auth"]

    @pytest.mark.asyncio
    async def test_merge_action_log_driver_error(self, client_with_driver):
        tx = client_with_driver._mock_tx
        tx.run = AsyncMock(side_effect=RuntimeError("connection lost"))

        log = ActionLog(action="a", outcome="o")
        with pytest.raises(GraphConnectionError, match="Failed to merge action log"):
            await client_with_driver.merge_action_log(log)


# ---------------------------------------------------------------------------
# Scoped chain identity (identity-v2 D4)
# ---------------------------------------------------------------------------


class TestScopedChainIdentity:
    """Chain-node identity is now the canonical mem2 encoding, not bare text.

    Previously Action/Outcome/Resolution nodes were MERGEd on
    ``_content_hash(text)`` alone — identical text in two workspaces MERGEd
    onto the SAME node, silently folding one workspace's chain into another's.
    """

    @pytest.mark.asyncio
    async def test_action_and_outcome_ids_match_canonical_mem2_encoding(
        self, client_with_driver
    ):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-1"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(
            action="Restarted the service",
            outcome="Service came back up",
            resolution="Bumped the memory limit",
        )
        await client_with_driver.merge_action_log(
            log, namespace="infra", workspace_id="ws-a"
        )

        call_kwargs = tx.run.call_args.kwargs
        assert call_kwargs["action_id"] == client_with_driver._content_hash(
            mem2_seed("ws-a", "infra", "Restarted the service")
        )
        assert call_kwargs["outcome_id"] == client_with_driver._content_hash(
            mem2_seed("ws-a", "infra", "Service came back up")
        )
        assert call_kwargs["resolution_id"] == client_with_driver._content_hash(
            mem2_seed("ws-a", "infra", "Bumped the memory limit")
        )

    @pytest.mark.asyncio
    async def test_same_text_two_workspaces_produce_different_action_ids(
        self, client_with_driver
    ):
        """The MERGE key itself must differ, or the two workspaces' Action
        nodes collapse into one — exactly the defect D4 closes."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-1"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="Restarted the service", outcome="Service came back up")

        await client_with_driver.merge_action_log(log, workspace_id="workspace-a")
        call_a = tx.run.call_args.kwargs

        await client_with_driver.merge_action_log(log, workspace_id="workspace-b")
        call_b = tx.run.call_args.kwargs

        assert call_a["action_id"] != call_b["action_id"]
        assert call_a["outcome_id"] != call_b["outcome_id"]

    @pytest.mark.asyncio
    async def test_same_text_two_namespaces_produce_different_action_ids(
        self, client_with_driver
    ):
        """Namespace is part of the scope encoding too — the same workspace's
        two categories must not collapse onto one chain node either."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-1"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="Rotated the credentials", outcome="Access restored")

        await client_with_driver.merge_action_log(
            log, namespace="infrastructure", workspace_id="ws-a"
        )
        call_default = tx.run.call_args.kwargs

        await client_with_driver.merge_action_log(
            log, namespace="security", workspace_id="ws-a"
        )
        call_security = tx.run.call_args.kwargs

        assert call_default["action_id"] != call_security["action_id"]

    @pytest.mark.asyncio
    async def test_content_hash_length_unchanged(self, client_with_driver):
        """D4 changes the hash INPUT, not the hash function or its truncation."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-1"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="a", outcome="o")
        await client_with_driver.merge_action_log(log, workspace_id="ws-a")

        call_kwargs = tx.run.call_args.kwargs
        assert len(call_kwargs["action_id"]) == client_with_driver._settings.CONTENT_HASH_LENGTH


# ---------------------------------------------------------------------------
# merge_knowledge_nodes
# ---------------------------------------------------------------------------


class TestMergeKnowledgeNodes:
    @pytest.mark.asyncio
    async def test_label_sanitization(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"cnt": 1})
        tx.run = AsyncMock(return_value=mock_result)

        nodes = [
            {
                "id": "n1",
                "label": "Drop;Table--",
                "properties": {"desc": "malicious label"},
            }
        ]
        await client_with_driver.merge_knowledge_nodes(nodes, [])

        call_args = tx.run.call_args
        query = call_args[0][0]
        # The sanitized label should only keep alphanumerics/underscores
        assert "DropTable" in query
        assert ";" not in query
        assert "--" not in query

    @pytest.mark.asyncio
    async def test_empty_label_defaults_to_entity(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"cnt": 1})
        tx.run = AsyncMock(return_value=mock_result)

        nodes = [{"id": "n1", "label": "!@#$", "properties": {}}]
        await client_with_driver.merge_knowledge_nodes(nodes, [])

        query = tx.run.call_args[0][0]
        assert "Entity" in query

    @pytest.mark.asyncio
    async def test_node_and_edge_grouping(self, client_with_driver):
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"cnt": 2})
        tx.run = AsyncMock(return_value=mock_result)

        nodes = [
            {"id": "n1", "label": "Concept", "properties": {"name": "a"}},
            {"id": "n2", "label": "Concept", "properties": {"name": "b"}},
            {"id": "n3", "label": "Action", "properties": {"name": "c"}},
        ]
        edges = [
            {"source": "n1", "target": "n2", "type": "RELATES_TO"},
            {"source": "n2", "target": "n3", "type": "CAUSED"},
        ]

        count = await client_with_driver.merge_knowledge_nodes(nodes, edges)

        # 2 node label groups (Concept, Action) + 2 edge type groups (RELATES_TO, CAUSED) = 4 calls
        assert tx.run.call_count == 4
        # count = 2 per call * 4 = 8
        assert count == 8
        tx.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_nodes_and_edges(self, client_with_driver):
        count = await client_with_driver.merge_knowledge_nodes([], [])
        assert count == 0


# ---------------------------------------------------------------------------
# query_related
# ---------------------------------------------------------------------------


class TestQueryRelated:
    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self, client_with_driver):
        session = client_with_driver._mock_session

        record1 = {"name": "auth", "description": "Auth module", "label": "Concept", "distance": 1}
        record2 = {"name": "login", "description": "Login flow", "label": "Action", "distance": 2}
        records = [record1, record2]

        mock_result = MagicMock()

        async def _aiter():
            for r in records:
                yield r

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        results = await client_with_driver.query_related("authentication module", limit=10)
        assert len(results) == 2
        assert results[0]["name"] == "auth"
        assert results[1]["label"] == "Action"

    @pytest.mark.asyncio
    async def test_query_related_driver_error(self, client_with_driver):
        session = client_with_driver._mock_session
        # First call (fulltext) raises ClientError → triggers fallback.
        # Second call (CONTAINS fallback) raises RuntimeError → wrapped as GraphConnectionError.
        session.run = AsyncMock(
            side_effect=[
                neo4j.exceptions.ClientError("index not found"),
                RuntimeError("timeout"),
            ]
        )

        with pytest.raises(GraphConnectionError, match="Failed to query related"):
            await client_with_driver.query_related("anything meaningful")

    @pytest.mark.asyncio
    async def test_query_related_empty_keywords(self, client_with_driver):
        """If only stopwords are provided, should return empty list without querying."""
        results = await client_with_driver.query_related("the is a")
        assert results == []

    @pytest.mark.asyncio
    async def test_query_related_builds_fulltext_query(self, client_with_driver):
        """Verify the Cypher query uses fulltext index search."""
        session = client_with_driver._mock_session

        mock_result = MagicMock()

        async def _aiter():
            return
            yield  # empty async generator

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver.query_related("authentication timeout", limit=5)

        # Verify the query was called with fulltext search
        call_args = session.run.call_args
        query = call_args[0][0]
        assert "fulltext.queryNodes" in query
        # Should have search_terms parameter
        call_kwargs = call_args.kwargs
        assert "search_terms" in call_kwargs
        assert "coalesce(result.memory_ids, []) AS memory_ids" in query


    @pytest.mark.asyncio
    async def test_fulltext_query_wraps_bigrams_in_quotes(self, client_with_driver):
        """Bigrams (terms with spaces) should be wrapped in double quotes for Lucene."""
        session = client_with_driver._mock_session

        mock_result = MagicMock()

        async def _aiter():
            return
            yield  # empty async generator

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver.query_related("authentication timeout problem", limit=5)

        call_kwargs = session.run.call_args.kwargs
        search_terms = call_kwargs["search_terms"]
        # Any bigram (term with space) should be wrapped in quotes
        for part in search_terms.split(" OR "):
            part = part.strip()
            if " " in part.strip('"'):
                assert part.startswith('"') and part.endswith('"'), (
                    f"Bigram not quoted: {part}"
                )

    @pytest.mark.asyncio
    async def test_contains_fallback_projects_memory_ids(self, client_with_driver):
        session = client_with_driver._mock_session
        mock_result = MagicMock()

        async def _aiter():
            return
            yield

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver._query_related_contains(["authentication"], 5)

        query = session.run.call_args.args[0]
        assert "coalesce(related.memory_ids, []) AS memory_ids" in query

    @pytest.mark.asyncio
    async def test_multihop_fulltext_projects_memory_ids(self, client_with_driver):
        session = client_with_driver._mock_session
        mock_result = MagicMock()

        async def _aiter():
            return
            yield

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver._query_related_multihop_fulltext(
            ["authentication"], 5, "default", 3, 0.5
        )

        query = session.run.call_args.args[0]
        assert "coalesce(result.memory_ids, []) AS memory_ids" in query


# ---------------------------------------------------------------------------
# query_resolutions
# ---------------------------------------------------------------------------


class TestQueryResolutions:
    @pytest.mark.asyncio
    async def test_returns_matching_resolutions(self, client_with_driver):
        session = client_with_driver._mock_session
        record = {
            "resolution": "Restart service",
            "error": "OOM killed",
            "id": "r1",
        }

        mock_result = MagicMock()

        async def _aiter():
            yield record

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        results = await client_with_driver.query_resolutions("OOM")
        assert len(results) == 1
        assert results[0]["resolution"] == "Restart service"

        query = session.run.call_args.args[0]
        assert "coalesce(r.memory_ids, [])" in query
        assert "coalesce(o.memory_ids, [])" in query
        assert "AS memory_ids" in query

    @pytest.mark.asyncio
    async def test_error_propagation(self, client_with_driver):
        session = client_with_driver._mock_session
        session.run = AsyncMock(side_effect=RuntimeError("network error"))

        with pytest.raises(GraphConnectionError, match="Failed to query resolutions"):
            await client_with_driver.query_resolutions("error")


# ---------------------------------------------------------------------------
# Namespace support
# ---------------------------------------------------------------------------


class TestNamespaceSupport:
    @pytest.mark.asyncio
    async def test_merge_action_log_creates_namespace_node(self, client_with_driver):
        """merge_action_log should include Namespace MERGE in the Cypher query."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-ns-1"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="a", outcome="o", domain="infra")
        await client_with_driver.merge_action_log(log, namespace="agent-1")

        call_args = tx.run.call_args
        query = call_args[0][0]
        # Cypher should contain Namespace MERGE
        assert "MERGE (ns:Namespace {name: $namespace})" in query
        assert "MERGE (ns)-[:CONTAINS]->(d)" in query
        # Parameter should be passed
        call_kwargs = call_args.kwargs
        assert call_kwargs["namespace"] == "agent-1"

    @pytest.mark.asyncio
    async def test_merge_action_log_default_namespace(self, client_with_driver):
        """merge_action_log without explicit namespace should use 'default'."""
        tx = client_with_driver._mock_tx
        mock_result = AsyncMock()
        mock_result.single = AsyncMock(return_value={"id": "elem-ns-2"})
        tx.run = AsyncMock(return_value=mock_result)

        log = ActionLog(action="a", outcome="o")
        await client_with_driver.merge_action_log(log)

        call_kwargs = tx.run.call_args.kwargs
        assert call_kwargs["namespace"] == "default"

    @pytest.mark.asyncio
    async def test_query_related_filters_by_namespace(self, client_with_driver):
        """query_related with non-default namespace should add namespace filter."""
        session = client_with_driver._mock_session

        mock_result = MagicMock()

        async def _aiter():
            return
            yield

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver.query_related(
            "authentication timeout", limit=5, namespace="agent-1"
        )

        call_args = session.run.call_args
        query = call_args[0][0]
        # Should contain namespace filter
        assert "Namespace" in query
        assert "namespace" in (call_args.kwargs or {})

    @pytest.mark.asyncio
    async def test_query_related_default_namespace_no_filter(self, client_with_driver):
        """query_related with default namespace should NOT add namespace filter."""
        session = client_with_driver._mock_session

        mock_result = MagicMock()

        async def _aiter():
            return
            yield

        mock_result.__aiter__ = lambda self: _aiter()
        session.run = AsyncMock(return_value=mock_result)

        await client_with_driver.query_related(
            "authentication timeout", limit=5, namespace="default"
        )

        call_args = session.run.call_args
        query = call_args[0][0]
        # Should NOT contain namespace filter for default
        assert "Namespace {name:" not in query


# ---------------------------------------------------------------------------
# Supersession
# ---------------------------------------------------------------------------


class TestCreateSupersession:
    @pytest.mark.asyncio
    async def test_creates_supersedes_edge(self, client_with_driver):
        """create_supersession should run a MERGE for the SUPERSEDES edge."""
        session = client_with_driver._mock_session
        session.run = AsyncMock()

        await client_with_driver.create_supersession(
            newer_id="elem-new",
            older_id="elem-old",
            reason="Newer approach replaces old one",
            detected="manual",
        )

        session.run.assert_called_once()
        call_args = session.run.call_args
        query = call_args[0][0]
        assert "SUPERSEDES" in query
        assert "MERGE" in query
        call_kwargs = call_args.kwargs
        assert call_kwargs["newer_id"] == "elem-new"
        assert call_kwargs["older_id"] == "elem-old"
        assert call_kwargs["reason"] == "Newer approach replaces old one"
        assert call_kwargs["detected"] == "manual"

    @pytest.mark.asyncio
    async def test_create_supersession_default_detected(self, client_with_driver):
        """create_supersession should default detected to 'auto'."""
        session = client_with_driver._mock_session
        session.run = AsyncMock()

        await client_with_driver.create_supersession(
            newer_id="elem-new",
            older_id="elem-old",
            reason="Auto-detected contradiction",
        )

        call_kwargs = session.run.call_args.kwargs
        assert call_kwargs["detected"] == "auto"

    @pytest.mark.asyncio
    async def test_create_supersession_vector_uuid_path(self, client_with_driver):
        """create_supersession should match older_id via MemoryRef.vector_id when it's a Qdrant UUID."""
        session = client_with_driver._mock_session
        session.run = AsyncMock()

        vector_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        await client_with_driver.create_supersession(
            newer_id="elem-new",
            older_id=vector_uuid,
            reason="Qdrant UUID supersession",
        )

        session.run.assert_called_once()
        call_args = session.run.call_args
        query = call_args[0][0]
        # Should use OPTIONAL MATCH for both element ID and MemoryRef vector_id
        assert "OPTIONAL MATCH (older_by_eid)" in query
        assert "OPTIONAL MATCH (older_by_ref:MemoryRef {vector_id: $older_id})" in query
        assert "COALESCE(older_by_eid, older_by_ref)" in query
        call_kwargs = call_args.kwargs
        assert call_kwargs["older_id"] == vector_uuid

    @pytest.mark.asyncio
    async def test_create_supersession_query_structure(self, client_with_driver):
        """create_supersession query should guard against null older node."""
        session = client_with_driver._mock_session
        session.run = AsyncMock()

        await client_with_driver.create_supersession(
            newer_id="elem-new",
            older_id="elem-old",
            reason="test",
        )

        query = session.run.call_args[0][0]
        # Should have a WHERE older IS NOT NULL guard
        assert "WHERE older IS NOT NULL" in query


class TestGetSupersessionHistory:
    @pytest.mark.asyncio
    async def test_returns_correct_chain(self, client_with_driver):
        """get_supersession_history should return supersedes list and superseded_by dict."""
        session = client_with_driver._mock_session

        # First call: outgoing SUPERSEDES
        supersedes_record = {"id": "elem-old", "text": "old action", "reason": "outdated", "detected": "auto"}
        mock_result1 = MagicMock()

        async def _aiter1():
            yield supersedes_record

        mock_result1.__aiter__ = lambda self: _aiter1()

        # Second call: incoming SUPERSEDES
        superseded_by_record = {"id": "elem-newer", "text": "newer action", "reason": "improvement", "detected": "manual"}
        mock_result2 = MagicMock()

        async def _aiter2():
            yield superseded_by_record

        mock_result2.__aiter__ = lambda self: _aiter2()

        session.run = AsyncMock(side_effect=[mock_result1, mock_result2])

        result = await client_with_driver.get_supersession_history("elem-current")

        assert len(result["supersedes"]) == 1
        assert result["supersedes"][0]["id"] == "elem-old"
        assert result["superseded_by"] is not None
        assert result["superseded_by"]["id"] == "elem-newer"

    @pytest.mark.asyncio
    async def test_returns_none_superseded_by_when_no_incoming(self, client_with_driver):
        """get_supersession_history should return None for superseded_by when no incoming edges."""
        session = client_with_driver._mock_session

        mock_result1 = MagicMock()

        async def _aiter1():
            return
            yield

        mock_result1.__aiter__ = lambda self: _aiter1()

        mock_result2 = MagicMock()

        async def _aiter2():
            return
            yield

        mock_result2.__aiter__ = lambda self: _aiter2()

        session.run = AsyncMock(side_effect=[mock_result1, mock_result2])

        result = await client_with_driver.get_supersession_history("elem-leaf")

        assert result["supersedes"] == []
        assert result["superseded_by"] is None
