"""Tests for GET /memory/contributors and POST /memory/handoff endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from app.main import app, get_vector, get_rag_engine
from app.models import MemorySource, RecallResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_vector_with_scroll(scroll_return=None):
    """Return a mock VectorClient whose ._client.scroll returns ([], None) by default."""
    if scroll_return is None:
        scroll_return = ([], None)
    mock_inner = AsyncMock()
    mock_inner.scroll = AsyncMock(return_value=scroll_return)
    vector = AsyncMock()
    vector._client = mock_inner
    return vector


def _make_mock_rag_engine(context_block="No recent memories."):
    """Return a mock RAGEngine whose .recall() returns a RecallResponse."""
    engine = AsyncMock()
    engine.recall = AsyncMock(return_value=RecallResponse(
        context_block=context_block,
        sources=[],
        score=0.0,
    ))
    return engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def contributors_client():
    """TestClient with mocked vector (scroll support) and rag_engine."""
    from fastapi.testclient import TestClient

    mock_vector = _make_mock_vector_with_scroll()
    mock_engine = _make_mock_rag_engine()

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _noop_lifespan(a: FastAPI):
        yield

    app.router.lifespan_context = _noop_lifespan

    async def _override_vector():
        return mock_vector

    async def _override_engine():
        return mock_engine

    app.dependency_overrides[get_vector] = _override_vector
    app.dependency_overrides[get_rag_engine] = _override_engine

    # app.state.vector_client must exist for the handoff endpoint's direct access
    app.state.vector_client = mock_vector

    with TestClient(app, raise_server_exceptions=False) as client:
        client._mock_vector = mock_vector
        client._mock_engine = mock_engine
        yield client

    app.dependency_overrides.clear()
    app.router.lifespan_context = original_lifespan


# ---------------------------------------------------------------------------
# GET /memory/contributors
# ---------------------------------------------------------------------------


class TestContributorsEndpoint:
    def test_returns_200_empty_list_when_no_data(self, contributors_client):
        """GET /memory/contributors returns 200 and a list when Qdrant has no points."""
        resp = contributors_client.get("/memory/contributors")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_accepts_project_filter(self, contributors_client):
        """GET /memory/contributors accepts ?project= query param."""
        resp = contributors_client.get("/memory/contributors", params={"project": "testproj"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_accepts_since_filter(self, contributors_client):
        """GET /memory/contributors accepts ?since= query param."""
        resp = contributors_client.get(
            "/memory/contributors",
            params={"project": "testproj", "since": "2026-01-01T00:00:00Z"},
        )
        assert resp.status_code == 200

    def test_aggregates_points_by_agent_id(self, contributors_client):
        """GET /memory/contributors groups points by agent_id and returns stats."""
        # Simulate two points from the same agent
        point_a = MagicMock()
        point_a.payload = {
            "agent_id": "agent-alpha",
            "project": "myapp",
            "domain": "backend",
            "timestamp": "2026-05-01T10:00:00Z",
        }
        point_b = MagicMock()
        point_b.payload = {
            "agent_id": "agent-alpha",
            "project": "myapp",
            "domain": "frontend",
            "timestamp": "2026-05-10T12:00:00Z",
        }

        contributors_client._mock_vector._client.scroll = AsyncMock(
            return_value=([point_a, point_b], None)
        )

        resp = contributors_client.get("/memory/contributors")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        entry = data[0]
        assert entry["contributor_id"] == "agent-alpha"
        assert entry["memory_count"] == 2
        assert "myapp" in entry["projects"]
        assert entry["last_active"] == "2026-05-10T12:00:00Z"

    def test_sorts_by_memory_count_descending(self, contributors_client):
        """Contributors are sorted by memory_count descending."""
        p1 = MagicMock()
        p1.payload = {"agent_id": "agent-low", "project": "x", "domain": "a", "timestamp": "2026-05-01T00:00:00Z"}
        p2 = MagicMock()
        p2.payload = {"agent_id": "agent-high", "project": "x", "domain": "b", "timestamp": "2026-05-01T00:00:00Z"}
        p3 = MagicMock()
        p3.payload = {"agent_id": "agent-high", "project": "x", "domain": "b", "timestamp": "2026-05-02T00:00:00Z"}

        contributors_client._mock_vector._client.scroll = AsyncMock(
            return_value=([p1, p2, p3], None)
        )

        resp = contributors_client.get("/memory/contributors")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["contributor_id"] == "agent-high"
        assert data[0]["memory_count"] == 2
        assert data[1]["contributor_id"] == "agent-low"
        assert data[1]["memory_count"] == 1


# ---------------------------------------------------------------------------
# POST /memory/handoff
# ---------------------------------------------------------------------------


class TestHandoffEndpoint:
    def test_returns_200_with_summary_key(self, contributors_client):
        """POST /memory/handoff returns 200 with a 'summary' key."""
        with patch("app.main.synthesize_memories", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = "Done work. Open: nothing. Pick up: anywhere."
            resp = contributors_client.post(
                "/memory/handoff",
                json={"project": "testproj", "since_days": 7},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert "summary" in data
        assert data["project"] == "testproj"

    def test_summary_falls_back_to_context_block_when_llm_unavailable(self, contributors_client):
        """POST /memory/handoff falls back to context_block when synthesize_memories returns None.

        The recall must return at least one SOURCE for this path to be reached:
        a handoff with no contributors and no sources now short-circuits to an
        explicit "nothing to hand off" (see the empty-project test below), so
        exercising the LLM fallback requires a project that actually has
        content.
        """
        contributors_client._mock_engine.recall = AsyncMock(return_value=RecallResponse(
            context_block="fallback context here",
            sources=[MemorySource(
                store="vector", content="something real",
                score=0.9, metadata={"id": "m1"},
            )],
            score=0.9,
        ))

        with patch("app.main.synthesize_memories", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = None
            resp = contributors_client.post(
                "/memory/handoff",
                json={"project": "testproj", "since_days": 3},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["summary"] == "fallback context here"

    def test_unknown_project_says_so_instead_of_summarising_someone_elses_work(
        self, contributors_client
    ):
        """A handoff for a project with nothing in it must SAY it is empty.

        Measured on the live deployment: `{"project": "__no_such_project_xyz"}`
        returned HTTP 200 with another project's memories rendered as that
        project's handoff ("All 303 Karma tests pass...", every leaked row
        tagged `(graph)`). Two things caused it — the graph retrieval leg
        ignored the project filter entirely (fixed in
        RAGEngine._scope_verdict), and this endpoint then handed whatever
        survived to an LLM and asked for a summary, which it will always write.
        This guard is the second half: with nothing to hand off, say nothing,
        rather than narrating whatever retrieval happened to return.
        """
        contributors_client._mock_engine.recall = AsyncMock(return_value=RecallResponse(
            context_block="", sources=[], score=0.0,
        ))

        with patch("app.main.synthesize_memories", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = "an invented narrative"
            resp = contributors_client.post(
                "/memory/handoff",
                json={"project": "__no_such_project_xyz", "since_days": 7},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["empty"] is True
        assert "__no_such_project_xyz" in data["summary"]
        assert "Nothing to hand off" in data["summary"]
        # The LLM must never have been consulted — there was nothing to say.
        mock_synth.assert_not_awaited()

    def test_passes_the_callers_workspace_to_recall(self, contributors_client):
        """Handoff was the one recall in the system that crossed tenancy.

        /memory/recall passes principal['workspace_id']; this endpoint called
        engine.recall() without it, so the vector leg's hard workspace filter
        was never applied to a handoff.
        """
        contributors_client._mock_engine.recall = AsyncMock(return_value=RecallResponse(
            context_block="x",
            sources=[MemorySource(store="vector", content="x", score=0.5, metadata={})],
            score=0.5,
        ))
        with patch("app.main.synthesize_memories", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = "summary"
            contributors_client.post("/memory/handoff", json={"project": "p"})

        kwargs = contributors_client._mock_engine.recall.await_args.kwargs
        assert kwargs["workspace_id"]

    def test_requires_project_field(self, contributors_client):
        """POST /memory/handoff returns 422 when project is missing."""
        resp = contributors_client.post(
            "/memory/handoff",
            json={"since_days": 7},
        )
        assert resp.status_code == 422

    def test_accepts_default_since_days(self, contributors_client):
        """POST /memory/handoff works with just the project field (since_days defaults to 7)."""
        with patch("app.main.synthesize_memories", new_callable=AsyncMock) as mock_synth:
            mock_synth.return_value = "summary text"
            resp = contributors_client.post(
                "/memory/handoff",
                json={"project": "myapp"},
            )
        assert resp.status_code == 200
