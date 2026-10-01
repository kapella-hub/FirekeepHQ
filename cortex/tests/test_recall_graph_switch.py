"""RECALL_GRAPH_ENABLED (engine/rag.py::recall and recall_streaming).

Off must mean no Neo4j read at all — the traversal AND the error-query
resolution lookup — on both recall paths, or the LongMemEval graph-leg
ablation measures a half-off system.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, get_settings
from app.engine.rag import RAGEngine
from app.models import ContextQuery

_REPO = Path(__file__).resolve().parents[2]
_GRAPH_ROW = {"name": "n", "description": "graph knowledge", "label": "Concept", "distance": 1}


def _engine(vector_results=None, graph_results=None, **overrides) -> RAGEngine:
    vector = AsyncMock()
    vector.search = AsyncMock(return_value=vector_results or [])
    graph = AsyncMock()
    graph.query_related = AsyncMock(return_value=graph_results or [])
    graph.query_related_multihop = AsyncMock(return_value=graph_results or [])
    graph.query_resolutions = AsyncMock(return_value=[])
    graph.get_lifecycle_states = AsyncMock(return_value={})
    # A copy, never the lru_cached singleton: mutating that leaks the flag into
    # every later test in the session.
    settings = get_settings().model_copy(update=overrides)
    return RAGEngine(graph=graph, vector=vector, settings=settings)


def _hit(mid: str, score: float, text: str) -> dict:
    return {"id": mid, "score": score, "text": text,
            "metadata": {"status": "active", "timestamp": ""}}


@pytest.mark.asyncio
async def test_disabled_graph_leg_makes_no_neo4j_reads():
    eng = _engine([_hit("m1", 0.8, "deploy failed with an error")], [_GRAPH_ROW],
                  RECALL_GRAPH_ENABLED=False)
    # "error" routes the query through the resolution lookup when the graph
    # leg is on; off must mean off for that Neo4j read too.
    resp = await eng.recall(ContextQuery(task="why did the error happen", top_k=3, format="raw"))

    eng._graph.query_related.assert_not_called()
    eng._graph.query_related_multihop.assert_not_called()
    eng._graph.query_resolutions.assert_not_called()
    assert [s.store for s in resp.sources] == ["vector"]
    # Choosing vector-only is not a degraded recall.
    assert resp.degraded is False


@pytest.mark.asyncio
async def test_disabled_graph_leg_on_the_streaming_path():
    eng = _engine([_hit("m1", 0.8, "a vector memory")], [_GRAPH_ROW],
                  RECALL_GRAPH_ENABLED=False)
    events = [e async for e in eng.recall_streaming(ContextQuery(task="memory", top_k=3))]

    eng._graph.query_related.assert_not_called()
    assert [e["data"]["store"] for e in events if e["type"] == "source"] == ["vector"]


@pytest.mark.asyncio
async def test_graph_leg_runs_by_default():
    eng = _engine([_hit("m1", 0.8, "a vector memory")])
    await eng.recall(ContextQuery(task="memory", top_k=3, format="raw"))
    assert eng._graph.query_related_multihop.await_count == 1


def test_compose_default_does_not_override_the_code_default():
    """Compose's `${VAR:-x}` fallback wins over the code default when .env is
    silent, so drift here would change production recall with no code diff
    (the test_decision_config.py precedent)."""
    text = (_REPO / "docker-compose.yml").read_text(encoding="utf-8")
    found = re.findall(
        r"^\s*RECALL_GRAPH_ENABLED:\s*\$\{RECALL_GRAPH_ENABLED:-([^}]+)\}", text, re.MULTILINE
    )
    assert found, "docker-compose.yml no longer sets RECALL_GRAPH_ENABLED"
    assert {v.lower() for v in found} == {str(Settings().RECALL_GRAPH_ENABLED).lower()}
