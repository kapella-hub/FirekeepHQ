"""Regression tests for defect #13 — graph/vector score fairness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.engine.rag import RAGEngine
from app.models import ContextQuery


def _engine(vector_results, graph_results) -> RAGEngine:
    mock_graph = AsyncMock()
    mock_graph.query_related = AsyncMock(return_value=graph_results)
    mock_graph.query_related_multihop = AsyncMock(return_value=graph_results)
    mock_vector = AsyncMock()
    mock_vector.search = AsyncMock(return_value=vector_results)
    return RAGEngine(graph=mock_graph, vector=mock_vector)


@pytest.mark.asyncio
async def test_decay_applies_before_normalization():
    """A heavily decayed old memory must not be re-pinned to the top by
    min-max normalization. old: 0.9 * 2^(-90/90) = 0.45 < fresh 0.5, so the
    fresh entry must rank first. (Under the old order the old entry
    normalized to 1.0 first and won at 0.5 vs 0.0.)"""
    old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    fresh_ts = datetime.now(timezone.utc).isoformat()
    vector_results = [
        {"id": "old", "score": 0.9, "text": "old approach",
         "metadata": {"timestamp": old_ts, "memory_type": "episodic"}},
        {"id": "fresh", "score": 0.5, "text": "fresh approach",
         "metadata": {"timestamp": fresh_ts, "memory_type": "episodic"}},
    ]
    engine = _engine(vector_results, [])

    resp = await engine.recall(ContextQuery(task="approach to use", top_k=2, format="raw"))

    contents = [s.content for s in resp.sources]
    assert contents[0] == "fresh approach"


@pytest.mark.asyncio
async def test_bare_node_names_excluded():
    """Description-less graph node names carry no memory content and must not
    compete with real memories."""
    graph_results = [
        {"name": "auth", "description": None, "label": "Concept", "distance": 1},
        {"name": "token flow", "description": "JWT tokens rotate every 24h",
         "label": "Concept", "distance": 1},
    ]
    engine = _engine([], graph_results)

    resp = await engine.recall(ContextQuery(task="auth token rotation", top_k=3, format="raw"))

    contents = [s.content for s in resp.sources]
    assert "auth" not in contents
    assert "JWT tokens rotate every 24h" in contents


@pytest.mark.asyncio
async def test_graph_entries_capped_in_top_k():
    """Graph entries are capped at max(1, top_k // 2) in the final top_k so
    they cannot crowd out real memories."""
    fresh_ts = datetime.now(timezone.utc).isoformat()
    vector_results = [
        {"id": "v1", "score": 0.6, "text": "vector memory one",
         "metadata": {"timestamp": fresh_ts}},
        {"id": "v2", "score": 0.55, "text": "vector memory two",
         "metadata": {"timestamp": fresh_ts}},
    ]
    graph_results = [
        {"name": f"g{i}", "description": f"graph description {i}",
         "label": "Concept", "distance": 1}
        for i in range(3)
    ]
    engine = _engine(vector_results, graph_results)

    resp = await engine.recall(ContextQuery(task="zzz unrelated", top_k=3, format="raw"))

    graph_sources = [s for s in resp.sources if s.store == "graph"]
    vector_sources = [s for s in resp.sources if s.store == "vector"]
    assert len(graph_sources) <= 1  # cap = max(1, 3 // 2) = 1
    assert len(vector_sources) == 2


@pytest.mark.asyncio
async def test_graph_backfills_when_vector_empty():
    """The cap must not starve results: with no vector entries, skipped graph
    entries backfill up to top_k."""
    graph_results = [
        {"name": f"g{i}", "description": f"graph description {i}",
         "label": "Concept", "distance": 1}
        for i in range(3)
    ]
    engine = _engine([], graph_results)

    resp = await engine.recall(ContextQuery(task="zzz unrelated", top_k=3, format="raw"))

    assert len(resp.sources) == 3
