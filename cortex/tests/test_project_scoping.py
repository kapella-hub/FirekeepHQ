"""Regression tests for defects #5/#14 — project scoping wired end to end."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery


def _bare_vector_client() -> VectorClient:
    client = VectorClient.__new__(VectorClient)
    client._client = AsyncMock()
    client._collection = "firekeep_memory"
    client._embed = AsyncMock(return_value=[0.1] * 768)
    client._client.query_points = AsyncMock(return_value=MagicMock(points=[]))
    return client


class TestVectorSearchProjectFilter:
    @pytest.mark.asyncio
    async def test_project_adds_must_condition(self):
        client = _bare_vector_client()
        await client.search("query", top_k=3, project="firekeep")

        call_kwargs = client._client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        project_conditions = [
            c for c in query_filter.must if getattr(c, "key", None) == "project"
        ]
        assert len(project_conditions) == 1
        assert project_conditions[0].match.value == "firekeep"

    @pytest.mark.asyncio
    async def test_no_project_adds_no_condition(self):
        client = _bare_vector_client()
        await client.search("query", top_k=3)

        call_kwargs = client._client.query_points.call_args.kwargs
        query_filter = call_kwargs["query_filter"]
        must = query_filter.must if (query_filter and query_filter.must) else []
        assert [c for c in must if getattr(c, "key", None) == "project"] == []


class TestScopedRecallReturnsOnlyMatching:
    @pytest.mark.asyncio
    async def test_two_projects_scoped_search_returns_only_matching(self):
        """Fake Qdrant honors the project must-filter: points from two
        projects exist; scoped search returns only the matching one."""
        points = {
            "a": {"project": "alpha", "text": "alpha memory"},
            "b": {"project": "beta", "text": "beta memory"},
        }

        def fake_query_points(collection_name, query, query_filter, limit, with_payload, **kw):
            wanted = None
            if query_filter is not None and query_filter.must:
                for cond in query_filter.must:
                    if getattr(cond, "key", None) == "project":
                        wanted = cond.match.value
            result = MagicMock()
            result.points = []
            for pid, data in points.items():
                if wanted and data["project"] != wanted:
                    continue
                p = MagicMock()
                p.id = pid
                p.score = 0.9
                p.payload = {
                    "text": data["text"], "source": "agent", "tags": [],
                    "domain": "general", "timestamp": "", "metadata": {},
                    "project": data["project"],
                }
                result.points.append(p)
            return result

        client = _bare_vector_client()
        client._client.query_points = AsyncMock(side_effect=fake_query_points)

        results = await client.search("memory", top_k=5, project="alpha")
        assert [r["text"] for r in results] == ["alpha memory"]


class TestRecallForwardsProject:
    @pytest.mark.asyncio
    async def test_recall_passes_project_to_vector_search(self):
        mock_graph = AsyncMock()
        mock_graph.query_related = AsyncMock(return_value=[])
        mock_graph.query_related_multihop = AsyncMock(return_value=[])
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(return_value=[])
        engine = RAGEngine(graph=mock_graph, vector=mock_vector)

        await engine.recall(
            ContextQuery(task="recent work", project="Firekeep", top_k=3, format="raw")
        )

        call_kwargs = mock_vector.search.call_args.kwargs
        assert call_kwargs.get("project") == "firekeep"  # ContextQuery lowercases


def _mock_response(json_data: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_data,
        request=httpx.Request("POST", "http://test"),
    )


@pytest.fixture(autouse=True)
def _reset_client():
    import app.mcp_server as mod

    mod._client = None
    yield
    mod._client = None


class TestMemoryLearnProjectParam:
    @pytest.mark.asyncio
    async def test_memory_learn_forwards_project(self):
        mock_resp = _mock_response({"status": "stored", "vector_id": "v1"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_learn
            await memory_learn(action="did x", outcome="worked", project="Firekeep")

        call_json = mock_post.call_args[1]["json"]
        assert call_json.get("project") == "Firekeep"

    @pytest.mark.asyncio
    async def test_memory_learn_omits_project_when_none(self):
        mock_resp = _mock_response({"status": "stored", "vector_id": "v1"})
        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=mock_resp
        ) as mock_post:
            from app.mcp_server import memory_learn
            await memory_learn(action="did x", outcome="worked")

        call_json = mock_post.call_args[1]["json"]
        assert "project" not in call_json
