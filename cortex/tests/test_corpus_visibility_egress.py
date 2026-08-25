"""Docdex Phase V Task 6 — the visibility builder wired at recall egress (spec §4.4).

The acceptance pair for member privacy: Alice's private chunk is a top hit
for Alice and ABSENT for Bob on both recall paths; legacy points (no
`visibility` field) return for everyone; `committed=False` generations
return for no one; a caller with no member identity gets no private chunks
(fail closed).

The Qdrant fakes here HONOR the filter — must, top-level should, must_not,
and nested Filter branches. test_vector.py's `_filtering_query_points`
ignores a top-level `should`, so it would pass even if the builder were
never wired; a fake that ignores filters proves nothing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import Filter

from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery

ALICE = "mem-alice"
BOB = "mem-bob"

_MISSING = object()


# ---------------------------------------------------------------------------
# Filter-honoring fakes
# ---------------------------------------------------------------------------


def _condition_matches(cond, payload: dict) -> bool:
    """One Qdrant condition against one payload, nested Filters included."""
    if isinstance(cond, Filter):
        return _filter_matches(cond, payload)
    is_empty = getattr(cond, "is_empty", None)
    if is_empty is not None:
        value = payload.get(is_empty.key, _MISSING)
        return value is _MISSING or value is None or value == []
    value = payload.get(cond.key, _MISSING)
    match = cond.match
    if hasattr(match, "value"):
        return value == match.value
    if hasattr(match, "any"):
        if value is _MISSING:
            return False
        if isinstance(value, list):
            return any(v in match.any for v in value)
        return value in match.any
    raise AssertionError(f"unhandled condition shape: {cond!r}")


def _filter_matches(query_filter, payload: dict) -> bool:
    """Real Qdrant semantics: ALL(must) AND >=1(should) AND NONE(must_not)."""
    if query_filter is None:
        return True
    must = query_filter.must or []
    should = query_filter.should or []
    must_not = query_filter.must_not or []
    if not all(_condition_matches(c, payload) for c in must):
        return False
    if should and not any(_condition_matches(c, payload) for c in should):
        return False
    return not any(_condition_matches(c, payload) for c in must_not)


def _fake_qdrant(points) -> AsyncMock:
    """query_points + scroll that actually evaluate the filter they are given."""
    client = AsyncMock()

    async def _query_points(*, query_filter=None, limit=10, **_kw):
        matched = [p for p in points if _filter_matches(query_filter, p.payload)]
        matched.sort(key=lambda p: p.score, reverse=True)
        return SimpleNamespace(points=matched[:limit])

    async def _scroll(*, scroll_filter=None, limit=10, **_kw):
        matched = [p for p in points if _filter_matches(scroll_filter, p.payload)]
        return matched[:limit], None

    client.query_points = AsyncMock(side_effect=_query_points)
    client.scroll = AsyncMock(side_effect=_scroll)
    return client


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _point(pid: str, score: float, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(id=pid, score=score, payload=payload)


def _payload(text: str, **extra) -> dict:
    base = {"text": text, "source": "corpus", "tags": [], "domain": "docs"}
    base.update(extra)
    return base


@pytest.fixture()
def points() -> list[SimpleNamespace]:
    return [
        _point(
            "alice-private",
            0.99,
            _payload(
                "alice private notes",
                visibility="member",
                member_id=ALICE,
                committed=True,
            ),
        ),
        _point(
            "uncommitted",
            0.95,
            _payload(
                "half-ingested generation",
                visibility="workspace",
                committed=False,
            ),
        ),
        _point(
            "workspace-doc",
            0.90,
            _payload("team runbook", visibility="workspace", committed=True),
        ),
        # Pre-Phase-V point: no visibility, no committed field.
        _point("legacy-memory", 0.80, _payload("pre-phase-v memory")),
    ]


@pytest.fixture()
def vector(test_settings, points) -> VectorClient:
    client = VectorClient(test_settings)
    client._client = _fake_qdrant(points)
    client._http_client = AsyncMock()
    client._embed = AsyncMock(return_value=[0.1] * test_settings.EMBEDDING_DIM)
    return client


# ---------------------------------------------------------------------------
# VectorClient.search — the RAG egress
# ---------------------------------------------------------------------------


class TestSearchEgress:
    @pytest.mark.asyncio
    async def test_private_chunk_is_top_hit_for_its_owner(self, vector):
        results = await vector.search(
            "notes", top_k=10, namespace=None, member_id=ALICE
        )
        ids = [r["id"] for r in results]
        assert ids[0] == "alice-private"
        assert "workspace-doc" in ids
        assert "legacy-memory" in ids

    @pytest.mark.asyncio
    async def test_private_chunk_absent_for_teammate(self, vector):
        results = await vector.search(
            "notes", top_k=10, namespace=None, member_id=BOB
        )
        ids = {r["id"] for r in results}
        assert "alice-private" not in ids
        # Workspace and legacy points still return — the filter is surgical.
        assert {"workspace-doc", "legacy-memory"} <= ids

    @pytest.mark.asyncio
    async def test_no_member_identity_fails_closed(self, vector):
        results = await vector.search(
            "notes", top_k=10, namespace=None, member_id=None
        )
        ids = {r["id"] for r in results}
        assert "alice-private" not in ids
        assert {"workspace-doc", "legacy-memory"} <= ids

    @pytest.mark.asyncio
    async def test_uncommitted_generation_returns_for_no_one(self, vector):
        for member_id in (ALICE, BOB, None):
            results = await vector.search(
                "notes", top_k=10, namespace=None, member_id=member_id
            )
            assert "uncommitted" not in {r["id"] for r in results}, (
                f"committed=False chunk leaked for member_id={member_id!r}"
            )


# ---------------------------------------------------------------------------
# VectorClient.list_memories — the listing egress
# ---------------------------------------------------------------------------


class TestListMemoriesEgress:
    @pytest.mark.asyncio
    async def test_owner_sees_private_chunk_in_listing(self, vector):
        rows = await vector.list_memories(limit=10, member_id=ALICE)
        ids = {r["id"] for r in rows}
        assert "alice-private" in ids
        assert {"workspace-doc", "legacy-memory"} <= ids

    @pytest.mark.asyncio
    async def test_teammate_and_anonymous_never_see_private_chunk(self, vector):
        for member_id in (BOB, None):
            rows = await vector.list_memories(limit=10, member_id=member_id)
            ids = {r["id"] for r in rows}
            assert "alice-private" not in ids, f"leaked for {member_id!r}"
            assert {"workspace-doc", "legacy-memory"} <= ids

    @pytest.mark.asyncio
    async def test_query_leg_applies_the_same_filter(self, vector):
        rows = await vector.list_memories(limit=10, query="notes", member_id=BOB)
        ids = {r["id"] for r in rows}
        assert "alice-private" not in ids
        assert "uncommitted" not in ids

    @pytest.mark.asyncio
    async def test_uncommitted_generation_hidden_from_listing(self, vector):
        rows = await vector.list_memories(limit=10, member_id=ALICE)
        assert "uncommitted" not in {r["id"] for r in rows}


# ---------------------------------------------------------------------------
# Both recall paths thread member_id into the one vector search
# ---------------------------------------------------------------------------


class TestRecallPathsThreadMemberId:
    @pytest.mark.asyncio
    async def test_regular_and_streaming_recall_pass_member_id(
        self, mock_graph, mock_vector
    ):
        engine = RAGEngine(graph=mock_graph, vector=mock_vector)
        query = ContextQuery(task="member privacy")

        await engine.recall(query, workspace_id="ws-1", member_id=ALICE)
        _ = [
            event
            async for event in engine.recall_streaming(
                query, workspace_id="ws-1", member_id=ALICE
            )
        ]

        assert mock_vector.search.await_count == 2
        assert all(
            call.kwargs["member_id"] == ALICE
            for call in mock_vector.search.await_args_list
        )


# ---------------------------------------------------------------------------
# The REST call sites hand the VERIFIED principal's member to the engine
# ---------------------------------------------------------------------------


class TestEndpointCallSites:
    def test_memory_recall_passes_principal_member_id(
        self, test_client, mock_vector
    ):
        from auth.principal import deployment_owner_member_id

        resp = test_client.post("/memory/recall", json={"task": "docs"})
        assert resp.status_code == 200
        kwargs = mock_vector.search.call_args.kwargs
        assert kwargs["member_id"] == deployment_owner_member_id()

    @pytest.mark.asyncio
    async def test_sse_recall_passes_principal_member_id(
        self, mock_graph, mock_vector, mock_redis
    ):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.streaming import create_streaming_router
        from auth.principal import deployment_owner_member_id

        rag = RAGEngine(graph=mock_graph, vector=mock_vector)
        router = create_streaming_router(rag, mock_graph, mock_vector)
        sse_app = FastAPI()
        sse_app.include_router(router)
        # D1 (outcome-truth PR2): the endpoint now reads request.app.state.redis_client
        # for the memory_read receipt + access/staleness bumps.
        sse_app.state.redis_client = mock_redis

        with TestClient(sse_app) as client:
            resp = client.post(
                "/memory/recall/stream", json={"task": "docs", "top_k": 5}
            )
            assert resp.status_code == 200

        kwargs = mock_vector.search.call_args.kwargs
        assert kwargs["member_id"] == deployment_owner_member_id()
