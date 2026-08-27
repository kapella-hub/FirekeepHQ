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
from app.db.vector import VectorClient, _merge_lifecycle, _v1_point_id, memory_point_id


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


class TestV1LifecycleBridge:
    """Identity-v2 D5 — the transitional bridge for the compat window.

    A post-deploy relearn of text that exists ONLY as an OLD v1 point (id =
    bare uuid5(text)) mints a NEW v2 id via memory_point_id. The v2-id
    prefetch misses that v1 point, so without the bridge _merge_lifecycle(None,
    fresh) returns fresh and an archived/superseded/deprecated memory comes
    back ACTIVE with its provenance dropped — a real adversarial-review
    finding, not a hypothetical.
    """

    @pytest.mark.asyncio
    async def test_v1_archived_point_resurrects_as_archived_under_v2_id(
        self, vector_client, mock_qdrant_client
    ):
        """Bridge ON: the v2 prefetch misses, so upsert() must additionally
        check the v1 id and feed that payload into _merge_lifecycle — the new
        v2 point keeps the archived status and its recovery provenance."""
        text = "same text"
        workspace_id = "ws-bridge"
        namespace = "default"
        v1_id = _v1_point_id(text)
        v2_id = memory_point_id(workspace_id, namespace, text)

        v1_point = MagicMock()
        v1_point.payload = _existing(
            status="archived",
            archived_at="2026-04-01T00:00:00+00:00",
            archive_source="gc",
            archive_reason="low_value",
            archived_from_status="active",
            purge_eligible_at="2026-06-30T00:00:00+00:00",
        )

        async def retrieve(collection, ids, with_payload=True):
            if ids == [v2_id]:
                return []
            if ids == [v1_id]:
                return [v1_point]
            raise AssertionError(f"unexpected retrieve ids: {ids}")

        mock_qdrant_client.retrieve = AsyncMock(side_effect=retrieve)

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text=text,
                metadata={"source": "action_log", "workspace_id": workspace_id},
                namespace=namespace,
            )

        assert point_id == v2_id
        assert mock_qdrant_client.retrieve.await_count == 2
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["status"] == "archived"
        assert payload["archived_at"] == "2026-04-01T00:00:00+00:00"
        assert payload["archive_source"] == "gc"
        assert payload["purge_eligible_at"] == "2026-06-30T00:00:00+00:00"
        # v2 content still wins over v1 provenance.
        assert payload["namespace"] == namespace
        assert payload["text"] == text

    @pytest.mark.asyncio
    async def test_bridge_flag_off_pins_raw_fresh_active_behavior(
        self, settings, mock_qdrant_client
    ):
        """Bridge OFF: today's raw behavior is pinned — a v2-prefetch miss
        must NOT consult the v1 id at all, so the same archived v1 point is
        never found and the new v2 point comes back fresh/active."""
        settings.MEMORY_ID_V1_BRIDGE = False
        client = VectorClient(settings)
        client._client = mock_qdrant_client
        client._http_client = AsyncMock()

        text = "same text"
        workspace_id = "ws-bridge"
        namespace = "default"
        v2_id = memory_point_id(workspace_id, namespace, text)

        async def retrieve(collection, ids, with_payload=True):
            if ids == [v2_id]:
                return []
            raise AssertionError(
                f"v1 bridge must not run when the flag is off; got retrieve({ids})"
            )

        mock_qdrant_client.retrieve = AsyncMock(side_effect=retrieve)

        with patch.object(
            client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            await client.upsert(
                text=text,
                metadata={"source": "action_log", "workspace_id": workspace_id},
                namespace=namespace,
            )

        assert mock_qdrant_client.retrieve.await_count == 1
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["status"] == "active"
        for key in (
            "archived_at", "archive_source", "archive_reason",
            "archived_from_status", "purge_eligible_at",
        ):
            assert payload.get(key) is None

    @pytest.mark.asyncio
    async def test_v2_prefetch_hit_does_not_consult_v1_id(
        self, vector_client, mock_qdrant_client
    ):
        """When the v2-id prefetch HITS, behavior is unchanged: no second
        retrieve, and the v1 id is never touched."""
        text = "same text"
        workspace_id = "ws-bridge"
        namespace = "default"
        v2_id = memory_point_id(workspace_id, namespace, text)

        v2_point = MagicMock()
        v2_point.payload = _existing(status="active", confirmed_count=3)

        mock_qdrant_client.retrieve = AsyncMock(return_value=[v2_point])

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text=text,
                metadata={"source": "action_log", "workspace_id": workspace_id},
                namespace=namespace,
            )

        assert point_id == v2_id
        mock_qdrant_client.retrieve.assert_awaited_once_with(
            "test_collection", [v2_id], with_payload=True
        )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["confirmed_count"] == 3

    @pytest.mark.asyncio
    async def test_explicit_memory_scheme_point_id_still_bridges(
        self, vector_client, mock_qdrant_client
    ):
        """The /memory/learn shape (identity-v2 D2): main.py precomputes
        memory_point_id itself and passes it in as an explicit point_id, so
        `point_id is not None` at upsert() — `minted` alone would miss this,
        the PRIMARY relearn path and the one D5 exists to protect. The bridge
        must still fire by recognizing the id as memory-scheme (recomputing
        and comparing), not merely by "was it minted here."""
        text = "same text"
        workspace_id = "ws-bridge"
        namespace = "default"
        v1_id = _v1_point_id(text)
        v2_id = memory_point_id(workspace_id, namespace, text)

        v1_point = MagicMock()
        v1_point.payload = _existing(
            status="archived",
            archived_at="2026-04-01T00:00:00+00:00",
            archive_source="gc",
            archive_reason="low_value",
            archived_from_status="active",
            purge_eligible_at="2026-06-30T00:00:00+00:00",
        )

        async def retrieve(collection, ids, with_payload=True):
            if ids == [v2_id]:
                return []
            if ids == [v1_id]:
                return [v1_point]
            raise AssertionError(f"unexpected retrieve ids: {ids}")

        mock_qdrant_client.retrieve = AsyncMock(side_effect=retrieve)

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text=text,
                metadata={"source": "action_log", "workspace_id": workspace_id},
                namespace=namespace,
                point_id=v2_id,  # explicit, as main.py's /memory/learn does
            )

        assert point_id == v2_id
        assert mock_qdrant_client.retrieve.await_count == 2
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["status"] == "archived"
        assert payload["archived_at"] == "2026-04-01T00:00:00+00:00"
        assert payload["purge_eligible_at"] == "2026-06-30T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_explicit_non_scheme_point_id_does_not_bridge(
        self, vector_client, mock_qdrant_client
    ):
        """A caller-owned id from a DIFFERENT scheme (corpus's source-scoped
        ids, dreams, skills) must never trigger the bridge, even when a v1
        point with the same text happens to exist — classification is by
        recomputing memory_point_id and comparing, and a corpus-shaped id
        fails that equality. Only one retrieve (the caller's own id)."""
        text = "same text"
        workspace_id = "ws-bridge"
        namespace = "default"
        corpus_id = "corpus-source-scoped-id"

        mock_qdrant_client.retrieve = AsyncMock(return_value=[])

        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text=text,
                metadata={"source": "corpus", "workspace_id": workspace_id},
                namespace=namespace,
                point_id=corpus_id,
            )

        assert point_id == corpus_id
        mock_qdrant_client.retrieve.assert_awaited_once_with(
            "test_collection", [corpus_id], with_payload=True
        )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["status"] == "active"
