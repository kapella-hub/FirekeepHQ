"""Tests for RAGEngine (app.engine.rag) -- the most critical component."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.engine.rag import RAGEngine
from app.models import ContextQuery


@pytest.fixture()
def mock_graph() -> AsyncMock:
    g = AsyncMock()
    g.query_related = AsyncMock(return_value=[])
    return g


@pytest.fixture()
def mock_vector() -> AsyncMock:
    v = AsyncMock()
    v.search = AsyncMock(return_value=[])
    return v


@pytest.fixture()
def engine(mock_graph, mock_vector) -> RAGEngine:
    return RAGEngine(graph=mock_graph, vector=mock_vector)


# ---------------------------------------------------------------------------
# _normalize_vector
# ---------------------------------------------------------------------------


class TestNormalizeVector:
    def test_preserves_scores(self, engine):
        results = [
            {"text": "Fix bug", "score": 0.95, "metadata": {"source": "ci"}},
            {"text": "Add feature", "score": 0.6, "metadata": {}},
        ]
        entries = engine._normalize_vector(results)
        assert len(entries) == 2
        assert entries[0]["score"] == 0.95
        assert entries[1]["score"] == 0.6

    def test_assigns_vector_store(self, engine):
        entries = engine._normalize_vector(
            [{"text": "x", "score": 0.5, "metadata": {}}]
        )
        assert entries[0]["store"] == "vector"

    def test_skips_empty_text(self, engine):
        results = [
            {"text": "", "score": 0.9, "metadata": {}},
            {"text": "real content", "score": 0.8, "metadata": {}},
        ]
        entries = engine._normalize_vector(results)
        assert len(entries) == 1
        assert entries[0]["content"] == "real content"

    def test_handles_empty_results(self, engine):
        assert engine._normalize_vector([]) == []

    def test_missing_score_defaults_zero(self, engine):
        entries = engine._normalize_vector([{"text": "t", "metadata": {}}])
        assert entries[0]["score"] == 0.0


# ---------------------------------------------------------------------------
# _normalize_graph
# ---------------------------------------------------------------------------


class TestFormatGraphEntries:
    """Tests for _format_graph_entries (formerly _normalize_graph).

    Scoring uses: weight * text_sim + (1 - weight) * dist_score
    where dist_score = 1/max(distance, 1) or 0.5 if no distance.
    Default GRAPH_RELEVANCE_WEIGHT = 0.4.
    """

    def test_distance_based_scoring(self, engine):
        query = "desc a desc b"
        results = [
            {"name": "a", "description": "desc a", "label": "Concept", "distance": 1},
            {"name": "b", "description": "desc b", "label": "Action", "distance": 3},
        ]
        entries = engine._format_graph_entries(results, query)
        assert len(entries) == 2
        # Both have scores > 0 (composite of text_sim and distance)
        assert entries[0]["score"] > 0
        assert entries[1]["score"] > 0
        # distance=1 entry should have higher distance component than distance=3
        # (1/1 vs 1/3), so if text similarity is equal, entry 0 scores higher
        # distance component: entry 0 = 0.6 * 1.0, entry 1 = 0.6 * (1/3)

    def test_missing_distance_defaults_half(self, engine):
        results = [{"name": "x", "description": "desc", "label": "Concept", "distance": None}]
        entries = engine._format_graph_entries(results, "unrelated query")
        # dist_score defaults to 0.5 when distance is None
        # text_sim will be low for unrelated query, so score ~ 0.6 * 0.5 = 0.3
        assert entries[0]["score"] > 0
        assert entries[0]["score"] < 1.0

    def test_excludes_nodes_without_description(self, engine):
        """Bare node names (no description) are excluded from recall (SP0 C5)."""
        results = [{"name": "auth", "description": None, "label": "Concept", "distance": 1}]
        entries = engine._format_graph_entries(results, "auth")
        assert entries == []

    def test_skips_nodes_without_name_or_description(self, engine):
        results = [
            {"name": "", "description": "", "label": "Concept", "distance": 1},
            {"name": None, "description": None, "label": "Concept", "distance": 1},
        ]
        entries = engine._format_graph_entries(results, "test")
        assert len(entries) == 0

    def test_description_preferred_over_name(self, engine):
        results = [{"name": "N", "description": "Full description", "label": "X", "distance": 2}]
        entries = engine._format_graph_entries(results, "test")
        assert entries[0]["content"] == "Full description"

    def test_assigns_graph_store(self, engine):
        results = [{"name": "n", "description": "d", "label": "L", "distance": 1}]
        entries = engine._format_graph_entries(results, "test")
        assert entries[0]["store"] == "graph"

    def test_filters_near_zero_deep_unrelated_result(self, engine):
        results = [
            {
                "name": "x",
                "description": "Distant unrelated entity",
                "label": "Concept",
                "distance": 10,
            }
        ]

        assert engine._format_graph_entries(results, "obscure") == []

    def test_metadata_includes_label(self, engine):
        results = [{"name": "n", "description": "d", "label": "CustomLabel", "distance": 1}]
        entries = engine._format_graph_entries(results, "test")
        assert entries[0]["metadata"]["label"] == "CustomLabel"

    def test_handles_empty_results(self, engine):
        assert engine._format_graph_entries([], "test") == []


# ---------------------------------------------------------------------------
# _merge_and_boost
# ---------------------------------------------------------------------------


class TestMergeAndBoost:
    def test_cross_reference_detection(self, engine):
        """Nearly identical content in both stores should merge with boost."""
        vector_entries = [
            {"content": "Fix authentication timeout", "score": 0.8, "store": "vector", "metadata": {}},
        ]
        graph_entries = [
            {
                "content": "Fix authentication timeout bug",
                "score": 0.7,
                "store": "graph",
                "metadata": {"name": "auth", "label": "Concept"},
            },
        ]
        merged = engine._merge_and_boost(vector_entries, graph_entries)

        # Should produce exactly one cross-referenced entry
        cross_refs = [e for e in merged if e["store"] == "both"]
        assert len(cross_refs) == 1
        # Score should be boosted: max(0.8, 0.7) * 1.5 = 1.2 -> capped at 1.0
        assert cross_refs[0]["score"] == 1.0

    def test_boost_factor_applied(self, engine):
        """Verify boost factor is correctly applied when not capped."""
        vector_entries = [
            {"content": "pool size config", "score": 0.4, "store": "vector", "metadata": {}},
        ]
        graph_entries = [
            {
                "content": "pool size configuration",
                "score": 0.3,
                "store": "graph",
                "metadata": {"name": "pool", "label": "Concept"},
            },
        ]
        merged = engine._merge_and_boost(vector_entries, graph_entries)
        cross_refs = [e for e in merged if e["store"] == "both"]
        assert len(cross_refs) == 1
        # max(0.4, 0.3) * 1.5 = 0.6
        assert abs(cross_refs[0]["score"] - 0.6) < 0.001

    def test_no_cross_reference_for_dissimilar_content(self, engine):
        vector_entries = [
            {"content": "Fix authentication bug", "score": 0.8, "store": "vector", "metadata": {}},
        ]
        graph_entries = [
            {
                "content": "Database connection pooling",
                "score": 0.7,
                "store": "graph",
                "metadata": {"name": "db", "label": "Concept"},
            },
        ]
        merged = engine._merge_and_boost(vector_entries, graph_entries)
        assert len(merged) == 2
        stores = {e["store"] for e in merged}
        assert "both" not in stores

    def test_deduplication(self, engine):
        """A graph entry matched by a vector entry should not appear again."""
        vector_entries = [
            {"content": "Fix login timeout", "score": 0.9, "store": "vector", "metadata": {}},
        ]
        graph_entries = [
            {
                "content": "Fix login timeout",
                "score": 0.5,
                "store": "graph",
                "metadata": {"name": "login", "label": "Action"},
            },
        ]
        merged = engine._merge_and_boost(vector_entries, graph_entries)
        # Exact match -> cross-ref, graph entry consumed -> only 1 result
        assert len(merged) == 1
        assert merged[0]["store"] == "both"

    def test_all_graph_entries_preserved_when_no_match(self, engine):
        vector_entries = [
            {"content": "AAA", "score": 0.9, "store": "vector", "metadata": {}},
        ]
        graph_entries = [
            {"content": "BBB", "score": 0.5, "store": "graph", "metadata": {"name": "b", "label": "X"}},
            {"content": "CCC", "score": 0.4, "store": "graph", "metadata": {"name": "c", "label": "Y"}},
        ]
        merged = engine._merge_and_boost(vector_entries, graph_entries)
        assert len(merged) == 3

    def test_empty_vector_entries(self, engine):
        graph_entries = [
            {"content": "x", "score": 0.5, "store": "graph", "metadata": {"name": "n", "label": "L"}},
        ]
        merged = engine._merge_and_boost([], graph_entries)
        assert len(merged) == 1
        assert merged[0]["store"] == "graph"

    def test_empty_graph_entries(self, engine):
        vector_entries = [
            {"content": "x", "score": 0.5, "store": "vector", "metadata": {}},
        ]
        merged = engine._merge_and_boost(vector_entries, [])
        assert len(merged) == 1
        assert merged[0]["store"] == "vector"

    def test_both_empty(self, engine):
        assert engine._merge_and_boost([], []) == []


# ---------------------------------------------------------------------------
# _is_fuzzy_match
# ---------------------------------------------------------------------------


class TestIsFuzzyMatch:
    def test_identical_strings_match(self, engine):
        assert engine._is_fuzzy_match("hello world", "hello world") is True

    def test_case_insensitive(self, engine):
        assert engine._is_fuzzy_match("Hello World", "hello world") is True

    def test_very_different_strings_no_match(self, engine):
        assert engine._is_fuzzy_match("abc", "xyz") is False

    def test_empty_strings_no_match(self, engine):
        assert engine._is_fuzzy_match("", "hello") is False
        assert engine._is_fuzzy_match("hello", "") is False
        assert engine._is_fuzzy_match("", "") is False

    def test_similar_strings_above_threshold(self, engine):
        # "Fix authentication timeout" vs "Fix authentication timeout bug"
        # SequenceMatcher ratio should be well above 0.7
        assert engine._is_fuzzy_match(
            "Fix authentication timeout",
            "Fix authentication timeout bug",
        ) is True

    def test_moderately_different_strings_below_threshold(self, engine):
        assert engine._is_fuzzy_match(
            "Fix authentication",
            "Database pooling configuration",
        ) is False


# ---------------------------------------------------------------------------
# _format_markdown
# ---------------------------------------------------------------------------


class TestFormatMarkdown:
    """Tests for _format_markdown.

    New format uses numbered list entries:
        ## Memory Recall ({n} results, confidence: {level})
        1. [{score}] ({source}) {content}
        > Query: "{task}" | Top {top_k}
    """

    def test_empty_entries(self, engine):
        result = engine._format_markdown([])
        assert "No relevant memories found" in result

    def test_graph_section(self, engine):
        entries = [
            {
                "content": "Auth module handles tokens",
                "score": 0.9,
                "store": "graph",
                "metadata": {"label": "Concept", "name": "auth"},
            },
        ]
        result = engine._format_markdown(entries)
        assert "## Memory Recall" in result
        assert "(graph)" in result
        assert "Auth module handles tokens" in result

    def test_vector_section(self, engine):
        entries = [
            {
                "content": "Fix login timeout",
                "score": 0.85,
                "store": "vector",
                "metadata": {"source": "action_log", "tags": ["auth", "bug"]},
            },
        ]
        result = engine._format_markdown(entries)
        assert "(vector)" in result
        assert "Fix login timeout" in result

    def test_cross_ref_section(self, engine):
        entries = [
            {
                "content": "Pool sizing fix",
                "score": 0.95,
                "store": "both",
                "metadata": {"graph_name": "pool_config"},
            },
        ]
        result = engine._format_markdown(entries)
        assert "(graph+vector)" in result
        assert "Pool sizing fix" in result

    def test_all_sections_present(self, engine):
        entries = [
            {"content": "g", "score": 0.9, "store": "graph", "metadata": {"label": "Concept", "name": "g"}},
            {"content": "v", "score": 0.8, "store": "vector", "metadata": {"source": "s", "tags": []}},
            {"content": "b", "score": 0.95, "store": "both", "metadata": {"graph_name": "b"}},
        ]
        result = engine._format_markdown(entries)
        assert "(graph)" in result
        assert "(vector)" in result
        assert "(graph+vector)" in result

    def test_graph_entry_format(self, engine):
        """Graph entries appear as numbered items with score and source."""
        entries = [
            {
                "content": "auth",
                "score": 0.9,
                "store": "graph",
                "metadata": {"label": "Concept", "name": "auth"},
            },
        ]
        result = engine._format_markdown(entries)
        assert "1." in result
        assert "(graph)" in result
        assert "auth" in result

    def test_graph_entry_content_differs_from_name(self, engine):
        entries = [
            {
                "content": "Detailed description",
                "score": 0.9,
                "store": "graph",
                "metadata": {"label": "Action", "name": "short_name"},
            },
        ]
        result = engine._format_markdown(entries)
        assert "Detailed description" in result

    def test_vector_entry_format(self, engine):
        entries = [
            {
                "content": "test",
                "score": 0.5,
                "store": "vector",
                "metadata": {"source": "src", "tags": []},
            },
        ]
        result = engine._format_markdown(entries)
        assert "(vector)" in result
        assert "test" in result


# ---------------------------------------------------------------------------
# recall (full pipeline)
# ---------------------------------------------------------------------------


class TestRecall:
    @pytest.mark.asyncio
    async def test_full_pipeline(self, mock_graph, mock_vector, engine):
        mock_vector.search.return_value = [
            {
                "id": "v1",
                "score": 0.9,
                "text": "Fixed auth timeout by increasing pool",
                "metadata": {"source": "action_log", "tags": ["auth"], "domain": "security"},
            },
        ]
        mock_graph.query_related_multihop.return_value = [
            {
                "name": "auth",
                "description": "Authentication service",
                "label": "Concept",
                "distance": 1,
            },
        ]

        query = ContextQuery(task="Fix auth issue", tags=["auth"], top_k=5)
        response = await engine.recall(query)

        assert response.context_block
        assert len(response.sources) > 0
        assert response.score > 0.0

    @pytest.mark.asyncio
    async def test_both_stores_empty(self, mock_graph, mock_vector, engine):
        mock_vector.search.return_value = []
        mock_graph.query_related_multihop.return_value = []

        query = ContextQuery(task="unknown", top_k=5)
        response = await engine.recall(query)

        assert response.sources == []
        assert response.score == 0.0
        assert "No relevant memories found" in response.context_block

    @pytest.mark.asyncio
    async def test_only_vector_results(self, mock_graph, mock_vector, engine):
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.7, "text": "Vector only result", "metadata": {}},
        ]
        mock_graph.query_related_multihop.return_value = []

        query = ContextQuery(task="search", top_k=5)
        response = await engine.recall(query)

        assert len(response.sources) == 1
        assert response.sources[0].store == "vector"

    @pytest.mark.asyncio
    async def test_unlinked_graph_owned_result_remains_recallable(
        self, mock_graph, mock_vector, engine
    ):
        """Sleep-cycle and legacy graph-only knowledge has no vector lifecycle."""
        mock_vector.search.return_value = []
        mock_graph.query_related_multihop.return_value = [
            {"name": "concept", "description": "A graph concept", "label": "Concept", "distance": 1},
        ]

        query = ContextQuery(task="search", top_k=5)
        response = await engine.recall(query)

        assert len(response.sources) == 1
        assert response.sources[0].store == "graph"
        assert response.sources[0].metadata["lifecycle_verified"] is False

    @pytest.mark.asyncio
    async def test_top_k_limits_results(self, mock_graph, mock_vector, engine):
        mock_vector.search.return_value = [
            {"id": f"v{i}", "score": 0.9 - i * 0.1, "text": f"Result {i}", "metadata": {}}
            for i in range(5)
        ]
        mock_graph.query_related_multihop.return_value = [
            {"name": f"g{i}", "description": f"Graph {i}", "label": "C", "distance": i + 1}
            for i in range(5)
        ]

        query = ContextQuery(task="search", top_k=3)
        response = await engine.recall(query)

        assert len(response.sources) <= 3

    @pytest.mark.asyncio
    async def test_all_low_scores(self, mock_graph, mock_vector, engine):
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.1, "text": "Marginal embedding match on obscure topic", "metadata": {}},
        ]
        mock_graph.query_related_multihop.return_value = [
            {"name": "x", "description": "Distant unrelated entity from deep traversal", "label": "C", "distance": 10},
        ]

        query = ContextQuery(task="obscure", top_k=5)
        response = await engine.recall(query)

        assert len(response.sources) == 1
        # All scores should be low
        for src in response.sources:
            assert src.score <= 0.2

    @pytest.mark.asyncio
    async def test_concurrent_execution(self, mock_graph, mock_vector, engine):
        """Verify both stores are queried concurrently (via asyncio.gather)."""
        import asyncio

        call_order = []

        mock_vector.search.side_effect

        async def slow_vector(*args, **kwargs):
            call_order.append("vector_start")
            await asyncio.sleep(0.01)
            call_order.append("vector_end")
            return [{"id": "v1", "score": 0.5, "text": "v", "metadata": {}}]

        async def slow_graph(*args, **kwargs):
            call_order.append("graph_start")
            await asyncio.sleep(0.01)
            call_order.append("graph_end")
            return [{"name": "g", "description": "g", "label": "C", "distance": 1,
                     "memory_ids": ["v1"]}]

        mock_vector.search.side_effect = slow_vector
        mock_vector.get_lifecycle_states.return_value = {
            "v1": {"id": "v1", "status": "active"}
        }
        mock_graph.query_related_multihop.side_effect = slow_graph

        query = ContextQuery(task="test", top_k=5)
        response = await engine.recall(query)

        # Both should have been called
        assert mock_vector.search.called
        assert mock_graph.query_related_multihop.called
        assert len(response.sources) == 2

    @pytest.mark.asyncio
    async def test_one_store_fails_gracefully(self, mock_graph, mock_vector, engine):
        """If one store raises, the other should still return results."""
        mock_vector.search.side_effect = RuntimeError("vector down")
        mock_graph.query_related_multihop.return_value = [
            {"name": "n", "description": "d", "label": "C", "distance": 1,
             "memory_ids": ["g1"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "g1": {"id": "g1", "status": "active"}
        }

        query = ContextQuery(task="test", top_k=5)
        response = await engine.recall(query)

        # Should have graph result despite vector failure
        assert len(response.sources) == 1
        assert response.sources[0].store == "graph"

    @pytest.mark.asyncio
    async def test_archived_linked_graph_result_is_filtered(
        self, mock_graph, mock_vector, engine
    ):
        mock_vector.search.return_value = []
        mock_graph.query_related_multihop.return_value = [
            {"name": "old", "description": "Old workaround", "label": "Resolution",
             "distance": 1, "memory_ids": ["archived-1"]},
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "archived-1": {"id": "archived-1", "status": "archived"}
        }

        response = await engine.recall(ContextQuery(task="workaround", top_k=5))

        assert response.sources == []

    @pytest.mark.asyncio
    async def test_archived_linked_graph_result_can_be_requested_explicitly(
        self, mock_graph, mock_vector, engine
    ):
        mock_vector.search.return_value = []
        mock_graph.query_related_multihop.return_value = [
            {
                "name": "old",
                "description": "Old workaround",
                "label": "Resolution",
                "distance": 1,
                "memory_ids": ["archived-1"],
            },
        ]
        mock_vector.get_lifecycle_states.return_value = {
            "archived-1": {"id": "archived-1", "status": "archived"}
        }

        response = await engine.recall(
            ContextQuery(task="workaround", top_k=5, include_archived=True)
        )

        assert len(response.sources) == 1
        assert response.sources[0].store == "graph"
        assert response.sources[0].metadata["status"] == "archived"
        assert 0 < response.sources[0].score <= 0.1

    @pytest.mark.asyncio
    async def test_aggregate_score_calculation(self, mock_graph, mock_vector, engine):
        """Aggregate score is the max of individual source scores after min-max normalization."""
        mock_vector.search.return_value = [
            {"id": "v1", "score": 0.8, "text": "Result 1", "metadata": {}},
            {"id": "v2", "score": 0.6, "text": "Result 2", "metadata": {}},
        ]
        mock_graph.query_related_multihop.return_value = []

        query = ContextQuery(task="test", top_k=5)
        response = await engine.recall(query)

        # After min-max normalization: 0.8 -> 1.0, 0.6 -> 0.0
        # aggregate_score = max of source scores = 1.0
        assert response.score == 1.0


# ---------------------------------------------------------------------------
# _parse_rerank_score
# ---------------------------------------------------------------------------


class TestParseRerankScore:
    def test_simple_decimal(self):
        assert RAGEngine._parse_rerank_score("0.85") == 0.85

    def test_zero(self):
        assert RAGEngine._parse_rerank_score("0") == 0.0

    def test_one(self):
        assert RAGEngine._parse_rerank_score("1") == 1.0

    def test_one_point_zero(self):
        assert RAGEngine._parse_rerank_score("1.0") == 1.0

    def test_zero_point_zero(self):
        assert RAGEngine._parse_rerank_score("0.0") == 0.0

    def test_with_surrounding_text(self):
        assert RAGEngine._parse_rerank_score("The relevance score is 0.7") == 0.7

    def test_does_not_match_numbers_above_one(self):
        """Should not extract '1' from '10' or similar."""
        result = RAGEngine._parse_rerank_score("10")
        assert result is None

    def test_garbage_text(self):
        assert RAGEngine._parse_rerank_score("not a number at all") is None

    def test_empty_string(self):
        assert RAGEngine._parse_rerank_score("") is None

    def test_fallback_to_float_parse(self):
        """A plain float string in range should work via fallback."""
        assert RAGEngine._parse_rerank_score("0.42") == 0.42

    def test_out_of_range_rejected(self):
        """No valid [0, 1] score can be extracted from '5.3'."""
        assert RAGEngine._parse_rerank_score("5.3") is None


# ---------------------------------------------------------------------------
# _apply_lifecycle_scoring
# ---------------------------------------------------------------------------


class TestLifecycleScoring:
    def test_active_memories_get_full_score(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "active"}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 1.0

    def test_superseded_memories_penalized(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "superseded"}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 0.5

    def test_deprecated_memories_heavily_penalized(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "deprecated"}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == pytest.approx(0.1)

    def test_archived_memories_excluded(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "archived"}}]
        result = engine._apply_lifecycle_scoring(items)
        assert len(result) == 0

    def test_confidence_boosts_confirmed_memories(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "active", "confirmed_count": 4, "contradicted_count": 0}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 5.0  # (1+4)/(1+0) = 5

    def test_confidence_penalizes_contradicted_memories(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "active", "confirmed_count": 0, "contradicted_count": 4}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == pytest.approx(0.2)  # (1+0)/(1+4) = 0.2

    def test_no_metadata_treated_as_active(self, engine):
        items = [{"score": 0.8, "content": "test", "metadata": {}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 0.8

    def test_superseded_by_annotation(self, engine):
        items = [{"score": 1.0, "content": "test", "metadata": {"status": "superseded", "superseded_by": "new-id"}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["_superseded_by"] == "new-id"


# ---------------------------------------------------------------------------
# _apply_decay
# ---------------------------------------------------------------------------


class TestApplyDecay:
    def test_no_decay_when_half_life_zero(self, engine):
        """No decay should be applied when memory_type has half-life 0 (reference)."""
        items = [{"score": 1.0, "metadata": {"timestamp": "2020-01-01T00:00:00+00:00", "memory_type": "reference"}}]
        engine._apply_decay(items)
        assert items[0]["score"] == 1.0

    def test_decay_reduces_old_scores(self, engine):
        """Very old memories should have near-zero scores."""
        engine._settings.MEMORY_DECAY_HALF_LIFE_DAYS = 90
        items = [{"score": 1.0, "metadata": {"timestamp": "2020-01-01T00:00:00+00:00"}}]
        engine._apply_decay(items)
        assert items[0]["score"] < 0.01

    def test_no_decay_without_timestamp(self, engine):
        """Items without timestamp should keep original score."""
        items = [{"score": 0.8, "metadata": {}}]
        engine._apply_decay(items)
        assert items[0]["score"] == 0.8

    def test_recent_memory_minimal_decay(self, engine):
        """Recent memories should have minimal decay."""
        from datetime import datetime, timezone, timedelta
        engine._settings.MEMORY_DECAY_HALF_LIFE_DAYS = 90
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        items = [{"score": 1.0, "metadata": {"timestamp": recent}}]
        engine._apply_decay(items)
        assert items[0]["score"] > 0.99


# ---------------------------------------------------------------------------
# _apply_lifecycle_scoring (extended)
# ---------------------------------------------------------------------------


class TestApplyLifecycleScoring:
    def test_active_memory_full_score(self, engine):
        """Active memories should keep full score."""
        items = [{"score": 1.0, "metadata": {"status": "active", "confirmed_count": 0, "contradicted_count": 0}}]
        engine._apply_lifecycle_scoring(items)
        assert items[0]["score"] == 1.0

    def test_superseded_memory_half_score(self, engine):
        """Superseded memories should get 0.5 multiplier."""
        items = [{"score": 1.0, "metadata": {"status": "superseded", "confirmed_count": 0, "contradicted_count": 0}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 0.5

    def test_deprecated_memory_low_score(self, engine):
        """Deprecated memories should get 0.1 multiplier."""
        items = [{"score": 1.0, "metadata": {"status": "deprecated", "confirmed_count": 0, "contradicted_count": 0}}]
        result = engine._apply_lifecycle_scoring(items)
        assert abs(result[0]["score"] - 0.1) < 0.01

    def test_archived_memory_zero_score(self, engine):
        """Archived memories should be excluded entirely."""
        items = [{"score": 1.0, "metadata": {"status": "archived", "confirmed_count": 0, "contradicted_count": 0}}]
        result = engine._apply_lifecycle_scoring(items)
        assert len(result) == 0

    def test_confidence_factor_boosts_confirmed(self, engine):
        """Confirmed memories should get boosted by confidence factor."""
        items = [{"score": 1.0, "metadata": {"status": "active", "confirmed_count": 4, "contradicted_count": 0}}]
        result = engine._apply_lifecycle_scoring(items)
        # confidence = (1+4)/(1+0) = 5.0, no cap in implementation
        assert result[0]["score"] == 5.0

    def test_confidence_factor_reduces_contradicted(self, engine):
        """Contradicted memories should get reduced by confidence factor."""
        items = [{"score": 1.0, "metadata": {"status": "active", "confirmed_count": 0, "contradicted_count": 4}}]
        result = engine._apply_lifecycle_scoring(items)
        # confidence = (1+0)/(1+4) = 0.2
        assert abs(result[0]["score"] - 0.2) < 0.01

    def test_no_status_defaults_to_active(self, engine):
        """Items without status should be treated as active."""
        items = [{"score": 1.0, "metadata": {}}]
        result = engine._apply_lifecycle_scoring(items)
        assert result[0]["score"] == 1.0
