"""Regression tests for defect #8/#12 — search() must carry lifecycle fields
into result metadata so _apply_lifecycle_scoring actually applies the
documented 0.5x/0.1x multipliers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db.vector import VectorClient
from app.engine.rag import RAGEngine
from app.models import ContextQuery


def _point(pid: str, score: float, payload: dict) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.score = score
    p.payload = payload
    return p


@pytest.fixture()
def vector_client() -> VectorClient:
    settings = Settings(
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_collection",
        EMBEDDING_DIM=768,
        LLM_BASE_URL="http://localhost:11434/v1",
        LLM_API_KEY="test-api-key",
        EMBEDDING_MODEL="test-embed",
    )
    client = VectorClient(settings)
    client._client = AsyncMock()
    client._http_client = AsyncMock()
    return client


class TestSearchCarriesLifecycleFields:
    @pytest.mark.asyncio
    async def test_metadata_includes_lifecycle_fields(self, vector_client):
        payload = {
            "text": "old fact",
            "source": "agent",
            "tags": [],
            "domain": "general",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "status": "superseded",
            "confirmed_count": 2,
            "contradicted_count": 1,
            "superseded_by": "point-2",
            "metadata": {},
        }
        result_obj = MagicMock()
        result_obj.points = [_point("point-1", 0.9, payload)]
        vector_client._client.query_points = AsyncMock(return_value=result_obj)

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("query")

        md = results[0]["metadata"]
        assert md["status"] == "superseded"
        assert md["confirmed_count"] == 2
        assert md["contradicted_count"] == 1
        assert md["superseded_by"] == "point-2"

    @pytest.mark.asyncio
    async def test_lifecycle_defaults_for_legacy_points(self, vector_client):
        payload = {
            "text": "legacy point without lifecycle fields",
            "source": "",
            "tags": [],
            "domain": "",
            "timestamp": "",
            "metadata": {},
        }
        result_obj = MagicMock()
        result_obj.points = [_point("p", 0.5, payload)]
        vector_client._client.query_points = AsyncMock(return_value=result_obj)

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            results = await vector_client.search("query")

        md = results[0]["metadata"]
        assert md["status"] == "active"
        assert md["confirmed_count"] == 0
        assert md["contradicted_count"] == 0
        assert md["superseded_by"] is None


class TestSupersededRanksBelowReplacement:
    @pytest.mark.asyncio
    async def test_superseded_ranks_below_active_replacement(self):
        """End-to-end through RAGEngine.recall(): a superseded memory with the
        SAME raw cosine score as its active replacement must rank below it.
        (Equal raw scores are used deliberately: min-max normalization maps
        both to 1.0, so only the lifecycle multipliers differentiate — the
        exact scenario defect #8 broke.)"""
        mock_vector = AsyncMock()
        mock_vector.search = AsyncMock(
            return_value=[
                {
                    "id": "old",
                    "score": 0.9,
                    "text": "cortex API runs on port 8000",
                    "metadata": {
                        "status": "superseded",
                        "confirmed_count": 0,
                        "contradicted_count": 1,
                        "superseded_by": "new",
                        "timestamp": "",
                    },
                },
                {
                    "id": "new",
                    "score": 0.9,
                    "text": "cortex API runs on port 8100",
                    "metadata": {
                        "status": "active",
                        "confirmed_count": 0,
                        "contradicted_count": 0,
                        "superseded_by": None,
                        "timestamp": "",
                    },
                },
            ]
        )
        mock_graph = AsyncMock()
        mock_graph.query_related = AsyncMock(return_value=[])
        mock_graph.query_related_multihop = AsyncMock(return_value=[])

        engine = RAGEngine(graph=mock_graph, vector=mock_vector)
        resp = await engine.recall(
            ContextQuery(task="which port does the cortex API use", top_k=2, format="raw")
        )

        contents = [s.content for s in resp.sources]
        assert contents[0] == "cortex API runs on port 8100"
        assert "[SUPERSEDED]" in resp.context_block


# --------------------------------------------------------------------------- #
# OWM: efficacy payload -> search metadata -> recall multiplier               #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_search_metadata_carries_owm_fields(vector_client):
    result_obj = MagicMock()
    result_obj.points = [_point("m1", 0.9, {"text": "t", "owm_efficacy": 0.8, "owm_n": 12})]
    vector_client._client.query_points = AsyncMock(return_value=result_obj)
    with patch.object(
        vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
    ):
        out = await vector_client.search("q")
    assert out[0]["metadata"]["owm_efficacy"] == 0.8
    assert out[0]["metadata"]["owm_n"] == 12


def _owm_engine(enabled=True, weight=0.15):
    from unittest.mock import AsyncMock as _AM
    from app.engine.rag import RAGEngine
    eng = RAGEngine(graph=_AM(), vector=_AM())
    eng._settings.OWM_ENABLED = enabled
    eng._settings.OWM_WEIGHT = weight
    return eng


def test_owm_multiplier_boosts_and_penalizes():
    eng = _owm_engine()
    items = [
        {"score": 1.0, "metadata": {"owm_efficacy": 1.0}},
        {"score": 1.0, "metadata": {"owm_efficacy": 0.0}},
        {"score": 1.0, "metadata": {"owm_efficacy": 0.5}},
        {"score": 1.0, "metadata": {}},  # never scored -> neutral
    ]
    out = eng._apply_lifecycle_scoring(items)
    scores = [i["score"] for i in out]
    assert abs(scores[0] - 1.15) < 1e-9   # proven helpful: +W
    assert abs(scores[1] - 0.85) < 1e-9   # proven misleading: -W
    assert scores[2] == 1.0               # neutral: bit-identical
    assert scores[3] == 1.0               # unscored: bit-identical


def test_owm_disabled_is_bit_identical(monkeypatch):
    eng = _owm_engine(enabled=False)
    out = eng._apply_lifecycle_scoring(
        [{"score": 1.0, "metadata": {"owm_efficacy": 1.0}}])
    assert out[0]["score"] == 1.0
