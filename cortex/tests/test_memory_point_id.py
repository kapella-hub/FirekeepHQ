"""Identity-v2 D1/D3: memory_point_id — scoped identity, fail-closed minting.

Today's minting (app/db/vector.py) is uuid5(FIREKEEP_UUID_NAMESPACE, text) —
text-only, so identical text across two workspaces (or two namespaces in the
same workspace) collapses onto the same point id. memory_point_id seeds on
["mem2", workspace_id, namespace, text] instead, so scope always separates
identity. These tests pin the exact seed encoding (every later task and every
migration script depends on reproducing it byte-for-byte) plus upsert()'s
fail-closed minting branch: no verified workspace_id, no mint.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings
from app.db.vector import FIREKEEP_UUID_NAMESPACE, VectorClient, memory_point_id
from app.exceptions import VectorStoreError


def _expected(ws, ns, text):
    seed = json.dumps(["mem2", ws, ns, text], separators=(",", ":"), ensure_ascii=False)
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed))


def test_seed_is_the_registered_encoding():
    assert memory_point_id("ws1", "default", "hello") == _expected("ws1", "default", "hello")


@pytest.mark.parametrize("text", [
    'a|b|c', 'a","b', 'line\nline', '["mem2","ws1","default","x"]',
    'unicode — em dash → arrow', '{"json": true}',
])
def test_hostile_texts_cannot_forge_identity(text):
    a = memory_point_id("ws1", "default", text)
    b = memory_point_id("ws2", "default", text)
    c = memory_point_id("ws1", "other", text)
    assert len({a, b, c}) == 3            # scope always separates
    assert a == memory_point_id("ws1", "default", text)  # deterministic


def test_namespace_is_normalized_before_seeding():
    from app.models import normalize_namespace
    raw = "My Namespace"
    assert memory_point_id("ws1", raw, "t") == memory_point_id("ws1", normalize_namespace(raw), "t")


def test_falsy_workspace_refuses():
    for bad in (None, ""):
        with pytest.raises(ValueError):
            memory_point_id(bad, "default", "t")


# ---------------------------------------------------------------------------
# upsert() minting-branch behavior (fixture conventions from
# tests/test_lifecycle_upsert.py)
# ---------------------------------------------------------------------------


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


class TestUpsertMintingBranchFailsClosed:
    @pytest.mark.asyncio
    async def test_no_point_id_no_workspace_id_refuses(self, vector_client, mock_qdrant_client):
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            with pytest.raises(VectorStoreError):
                await vector_client.upsert(
                    text="unverified write",
                    metadata={"source": "action_log"},
                )
        mock_qdrant_client.upsert.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_point_id_with_workspace_id_mints_scoped_id(
        self, vector_client, mock_qdrant_client
    ):
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text="verified write",
                metadata={"source": "action_log", "workspace_id": "ws1"},
                namespace="default",
            )

        assert point_id == memory_point_id("ws1", "default", "verified write")

    @pytest.mark.asyncio
    async def test_explicit_point_id_exempt_no_workspace_id_required(
        self, vector_client, mock_qdrant_client
    ):
        """Corpus shape (corpus/store.py): point_id= given, metadata carries no
        workspace_id. Must keep succeeding byte-identically — corpus scopes
        identity itself via corpus_point_id, not via workspace_id metadata."""
        mock_qdrant_client.retrieve = AsyncMock(return_value=[])
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text="corpus chunk",
                metadata={"source": "corpus"},
                point_id="explicit-corpus-id",
            )

        assert point_id == "explicit-corpus-id"
        mock_qdrant_client.upsert.assert_called_once()

    @pytest.mark.asyncio
    async def test_payload_namespace_equals_seeded_namespace_for_raw_input(
        self, vector_client, mock_qdrant_client
    ):
        """namespace is normalized ONCE at the top and that normalized value
        is used for both the stored payload and the mint seed — a raw,
        un-normalized namespace must not leak into the payload."""
        from app.models import normalize_namespace

        raw_namespace = "My-Namespace"
        normalized = normalize_namespace(raw_namespace)
        assert normalized != raw_namespace  # sanity: this input actually needs normalizing

        mock_qdrant_client.retrieve = AsyncMock(return_value=[])
        with patch.object(
            vector_client, "_embed", new_callable=AsyncMock, return_value=[0.1] * 768
        ):
            point_id = await vector_client.upsert(
                text="raw namespace write",
                metadata={"source": "action_log", "workspace_id": "ws1"},
                namespace=raw_namespace,
            )

        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["namespace"] == normalized
        assert point_id == memory_point_id("ws1", normalized, "raw namespace write")
