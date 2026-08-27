"""A3 — uuid5(text) collision merges lifecycle fields instead of wholesale replace (defect #6).

Re-learning identical text previously reset confirmed_count (the only GC
immunity), reattributed the memory to the newest agent, and could resurrect
a superseded memory. _merge_lifecycle pins the merge rules; the upsert
integration tests pin the retrieve-then-merge flow.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db.vector import VectorClient, _merge_lifecycle


def _fresh(**overrides) -> dict:
    payload = {
        "text": "same text",
        "source": "action_log",
        "tags": [],
        "domain": "general",
        "namespace": "default",
        "timestamp": "2026-07-08T12:00:00+00:00",
        "created_at": "2026-07-08T12:00:00+00:00",
        "agent_id": "bob",
        "session_id": "sess-new",
        "project": None,
        "metadata": {},
        "status": "active",
        "confirmed_count": 0,
        "contradicted_count": 0,
        "last_confirmed_at": None,
        "superseded_by": None,
    }
    payload.update(overrides)
    return payload


def _existing(**overrides) -> dict:
    payload = _fresh(
        timestamp="2026-01-01T00:00:00+00:00",
        created_at="2026-01-01T00:00:00+00:00",
        agent_id="alice",
        session_id="sess-old",
        project="firekeep",
        status="active",
        confirmed_count=5,
        contradicted_count=1,
        last_confirmed_at="2026-06-01T00:00:00+00:00",
    )
    payload.update(overrides)
    return payload


class TestMergeLifecyclePure:
    def test_none_existing_returns_fresh_unchanged(self):
        fresh = _fresh()
        assert _merge_lifecycle(None, fresh) == fresh

    def test_preserves_created_at_agent_id_and_project(self):
        merged = _merge_lifecycle(_existing(), _fresh())
        assert merged["created_at"] == "2026-01-01T00:00:00+00:00"
        assert merged["agent_id"] == "alice"
        assert merged["project"] == "firekeep"

    def test_created_at_falls_back_to_existing_timestamp(self):
        existing = _existing()
        del existing["created_at"]  # pre-A3 point without the field
        merged = _merge_lifecycle(existing, _fresh())
        assert merged["created_at"] == "2026-01-01T00:00:00+00:00"

    def test_confirmed_and_contradicted_counts_take_max(self):
        merged = _merge_lifecycle(_existing(), _fresh())
        assert merged["confirmed_count"] == 5
        assert merged["contradicted_count"] == 1

    def test_counts_take_max_when_fresh_is_higher(self):
        """Regression lock: max() must not degrade to 'prefer existing'."""
        merged = _merge_lifecycle(
            _existing(confirmed_count=2, contradicted_count=0),
            _fresh(confirmed_count=9, contradicted_count=4),
        )
        assert merged["confirmed_count"] == 9
        assert merged["contradicted_count"] == 4

    def test_superseded_status_is_not_resurrected(self):
        existing = _existing(status="superseded", superseded_by="winner-id")
        merged = _merge_lifecycle(existing, _fresh())
        assert merged["status"] == "superseded"
        assert merged["superseded_by"] == "winner-id"

    def test_timestamp_refreshes_as_last_seen(self):
        merged = _merge_lifecycle(_existing(), _fresh())
        assert merged["timestamp"] == "2026-07-08T12:00:00+00:00"

    def test_last_confirmed_at_preserved(self):
        merged = _merge_lifecycle(_existing(), _fresh())
        assert merged["last_confirmed_at"] == "2026-06-01T00:00:00+00:00"

    def test_archive_provenance_survives_identical_text_relearn(self):
        existing = _existing(
            status="archived",
            archived_at="2026-04-01T00:00:00+00:00",
            archive_source="gc",
            archive_reason="low_value",
            archived_from_status="active",
            purge_eligible_at="2026-06-30T00:00:00+00:00",
        )

        merged = _merge_lifecycle(existing, _fresh())

        assert merged["status"] == "archived"
        assert merged["archived_at"] == existing["archived_at"]
        assert merged["archive_source"] == "gc"
        assert merged["purge_eligible_at"] == existing["purge_eligible_at"]

    def test_unknown_existing_agent_id_yields_to_fresh(self):
        """A previously-unattributed point may gain real attribution."""
        merged = _merge_lifecycle(_existing(agent_id="unknown"), _fresh())
        assert merged["agent_id"] == "bob"

    def test_none_existing_project_yields_to_fresh(self):
        """Regression lock: None is a sentinel too — project's actual default
        per _PROMOTED_PAYLOAD_KEYS. A point stored without a project may gain
        one on re-learn."""
        merged = _merge_lifecycle(
            _existing(project=None), _fresh(project="firekeep-v2")
        )
        assert merged["project"] == "firekeep-v2"

    def test_fresh_text_and_nested_metadata_win(self):
        """Content fields refresh — only lifecycle/attribution is preserved."""
        merged = _merge_lifecycle(
            _existing(metadata={"ingest_id": "old"}),
            _fresh(metadata={"ingest_id": "new"}),
        )
        assert merged["metadata"] == {"ingest_id": "new"}


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_collection",
        EMBEDDING_DIM=768,
        LLM_BASE_URL="http://localhost:11434/v1",
        LLM_API_KEY="test-api-key",
        EMBEDDING_MODEL="test-embed",
    )


@pytest.fixture()
def mock_qdrant_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def vector_client(settings, mock_qdrant_client) -> VectorClient:
    client = VectorClient(settings)
    client._client = mock_qdrant_client
    client._http_client = AsyncMock()
    return client


class TestUpsertMergesLifecycle:
    @pytest.mark.asyncio
    async def test_upsert_preserves_existing_lifecycle(
        self, vector_client, mock_qdrant_client
    ):
        existing_point = MagicMock()
        existing_point.payload = _existing()
        mock_qdrant_client.retrieve = AsyncMock(return_value=[existing_point])

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="same text",
                metadata={
                    "source": "action_log",
                    "agent_id": "bob",
                    "domain": "general",
                    "workspace_id": "ws-test",
                },
            )

        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["agent_id"] == "alice"
        assert payload["project"] == "firekeep"
        assert payload["created_at"] == "2026-01-01T00:00:00+00:00"
        assert payload["confirmed_count"] == 5
        assert payload["timestamp"] != "2026-01-01T00:00:00+00:00"  # refreshed

    @pytest.mark.asyncio
    async def test_upsert_retrieve_failure_treated_as_new(
        self, vector_client, mock_qdrant_client
    ):
        """Pre-fetch failure must not block the write (fail loudly elsewhere, not here)."""
        mock_qdrant_client.retrieve = AsyncMock(side_effect=RuntimeError("qdrant hiccup"))

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text="brand new",
                metadata={"source": "action_log", "workspace_id": "ws-test"},
            )

        assert isinstance(point_id, str)
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["confirmed_count"] == 0
        assert payload["status"] == "active"

    @pytest.mark.asyncio
    async def test_upsert_writes_created_at_on_new_points(
        self, vector_client, mock_qdrant_client
    ):
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await vector_client.upsert(
                text="new",
                metadata={"source": "action_log", "workspace_id": "ws-test"},
            )

        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["created_at"] == payload["timestamp"]
