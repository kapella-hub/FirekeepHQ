"""Spec §4.1 — typed document sources with bounded metadata.

Ingest gains a `document` source type, a `visibility` flag that reaches the
chunk payload TOP LEVEL (where the visibility filter matches) and the Redis
source record, and a client `metadata` dict that is bounded and can never
carry a server-controlled key: a client metadata `member_id` would re-tenant
the chunk, `committed` would un-gate a half-written generation.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corpus.api import create_corpus_router
from corpus.models import Chunk, ChunkMetadata
from corpus.store import store_chunks, track_source, list_sources

_INGEST_RESULT = {
    "source_name": "Doc",
    "chunks_stored": 1,
    "entities_extracted": 0,
    "relationships_extracted": 0,
    "entity_types_discovered": [],
    "extraction_status": "skipped",
}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(create_corpus_router())

    # Attach a verified identity the way auth middleware does, so these tests
    # hold whether or not the local env enables auth (request_principal fails
    # closed on AUTH_ENABLED=true with no identity attached).
    @app.middleware("http")
    async def _attach_identity(request, call_next):
        request.state.identity = {
            "workspace_id": "ws-test", "member_id": "m-test",
            "credential_id": "cred-test",
            # dex:docdex — these tests ingest docdex:-named sources, whose
            # prefix Task 4's gate reserves for dex-scoped credentials.
            "scopes": ["memory:write", "dex:docdex"],
            "authenticated": True,
        }
        return await call_next(request)

    return TestClient(app)


def _chunk(name="Runbook", source_type="document"):
    return Chunk(content="body text", metadata=ChunkMetadata(
        source_name=name, source_type=source_type,
        chunk_index=0, total_chunks=1))


# ---------------------------------------------------------------------------
# Wire: IngestRequest
# ---------------------------------------------------------------------------


class TestIngestRequestWire:
    def test_document_source_type_accepted(self, client):
        with patch("corpus.api.ingest_document", new_callable=AsyncMock) as mock:
            mock.return_value = _INGEST_RESULT
            resp = client.post("/corpus/ingest", json={
                "content": "a synced file", "source_name": "docdex:m1:f1",
                "source_type": "document",
            })
        assert resp.status_code == 200

    def test_visibility_and_metadata_reach_the_pipeline(self, client):
        with patch("corpus.api.ingest_document", new_callable=AsyncMock) as mock:
            mock.return_value = _INGEST_RESULT
            resp = client.post("/corpus/ingest", json={
                "content": "private notes", "source_name": "docdex:m1:f1",
                "source_type": "document", "visibility": "member",
                "metadata": {"team": "sre"},
            })
        assert resp.status_code == 200
        kwargs = mock.call_args.kwargs
        assert kwargs["visibility"] == "member"
        assert kwargs["metadata"] == {"team": "sre"}
        # visibility rides the same path as the verified principal
        assert kwargs["workspace_id"] == "ws-test"
        assert kwargs["member_id"] == "m-test"

    def test_absent_visibility_defaults_to_workspace(self, client):
        with patch("corpus.api.ingest_document", new_callable=AsyncMock) as mock:
            mock.return_value = _INGEST_RESULT
            resp = client.post("/corpus/ingest", json={
                "content": "shared notes", "source_name": "Doc",
            })
        assert resp.status_code == 200
        assert mock.call_args.kwargs["visibility"] == "workspace"

    def test_invalid_visibility_rejected(self, client):
        resp = client.post("/corpus/ingest", json={
            "content": "x", "source_name": "Doc", "visibility": "public",
        })
        assert resp.status_code == 422


class TestBoundedMetadata:
    def test_reserved_key_rejected_naming_the_key(self, client):
        resp = client.post("/corpus/ingest", json={
            "content": "x", "source_name": "Doc",
            "metadata": {"member_id": "mallory"},
        })
        assert resp.status_code == 422
        assert "member_id" in resp.text

    def test_every_reserved_key_rejected(self, client):
        for key in ("workspace_id", "member_id", "visibility", "ingest_id",
                    "source_name", "chunk_index", "total_chunks", "committed"):
            resp = client.post("/corpus/ingest", json={
                "content": "x", "source_name": "Doc", "metadata": {key: "v"},
            })
            assert resp.status_code == 422, key
            assert key in resp.text

    def test_seventeen_keys_rejected(self, client):
        resp = client.post("/corpus/ingest", json={
            "content": "x", "source_name": "Doc",
            "metadata": {f"k{i}": "v" for i in range(17)},
        })
        assert resp.status_code == 422

    def test_sixteen_keys_accepted(self, client):
        with patch("corpus.api.ingest_document", new_callable=AsyncMock) as mock:
            mock.return_value = _INGEST_RESULT
            resp = client.post("/corpus/ingest", json={
                "content": "x", "source_name": "Doc",
                "metadata": {f"k{i}": "v" for i in range(16)},
            })
        assert resp.status_code == 200

    def test_non_string_value_rejected(self, client):
        resp = client.post("/corpus/ingest", json={
            "content": "x", "source_name": "Doc", "metadata": {"n": 42},
        })
        assert resp.status_code == 422

    def test_oversized_serialized_metadata_rejected(self, client):
        resp = client.post("/corpus/ingest", json={
            "content": "x", "source_name": "Doc",
            "metadata": {"blob": "a" * 2100},
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Store: chunk payloads and the Redis source record
# ---------------------------------------------------------------------------


class TestStoreChunksVisibility:
    @pytest.mark.asyncio
    async def test_member_visibility_lands_on_the_chunk_metadata(self, fake_vector):
        await store_chunks([_chunk()], fake_vector, ingest_id="r1",
                           workspace_id="ws1", member_id="m1",
                           visibility="member")
        (call,) = fake_vector.upserts
        assert call["metadata"]["visibility"] == "member"

    @pytest.mark.asyncio
    async def test_default_stamps_workspace_explicitly(self, fake_vector):
        """New writes stamp `workspace`; ABSENCE stays the legacy meaning the
        visibility filter honors for pre-Phase-V points."""
        await store_chunks([_chunk()], fake_vector, workspace_id="ws1")
        (call,) = fake_vector.upserts
        assert call["metadata"]["visibility"] == "workspace"

    @pytest.mark.asyncio
    async def test_client_metadata_rides_but_server_stamps_win(self, fake_vector):
        # Reserved keys are rejected at the API boundary, but store_chunks is
        # also reached by non-HTTP callers — server keys must win regardless.
        await store_chunks([_chunk()], fake_vector, workspace_id="ws1",
                           client_metadata={"team": "sre", "source": "evil"})
        (call,) = fake_vector.upserts
        assert call["metadata"]["team"] == "sre"
        assert call["metadata"]["source"] == "corpus"


class TestSourceRecordVisibility:
    @pytest.mark.asyncio
    async def test_track_source_records_visibility(self):
        import fakeredis.aioredis

        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await track_source("docdex:m1:f1", "document", chunk_count=2,
                           redis_client=r, visibility="member")
        (record,) = await list_sources(redis_client=r)
        assert record["visibility"] == "member"

    @pytest.mark.asyncio
    async def test_track_source_default_is_workspace(self):
        import fakeredis.aioredis

        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await track_source("Doc", "text", chunk_count=1, redis_client=r)
        (record,) = await list_sources(redis_client=r)
        assert record["visibility"] == "workspace"


# ---------------------------------------------------------------------------
# Pipeline: thread-through
# ---------------------------------------------------------------------------


class TestPipelineThreadsVisibility:
    @pytest.mark.asyncio
    async def test_visibility_and_metadata_reach_chunks_and_source_record(
        self, fake_vector
    ):
        import fakeredis.aioredis

        from corpus.pipeline import ingest_document

        fake_vector.delete_by_filter = AsyncMock()
        r = fakeredis.aioredis.FakeRedis(decode_responses=False)
        await ingest_document(
            content="some body text that will chunk",
            source_name="docdex:m1:f1", source_type="document",
            vector_client=fake_vector, redis_client=r,
            workspace_id="ws1", member_id="m1",
            visibility="member", metadata={"team": "sre"},
        )
        assert fake_vector.upserts
        for call in fake_vector.upserts:
            assert call["metadata"]["visibility"] == "member"
            assert call["metadata"]["team"] == "sre"
        (record,) = await list_sources(redis_client=r)
        assert record["visibility"] == "member"


# ---------------------------------------------------------------------------
# Cortex upsert: `visibility` promotes to the top-level Qdrant payload
# ---------------------------------------------------------------------------


def _load_cortex():
    """Import cortex's app.config/app.db.vector the way
    test_point_identity.py does — corpus is a shared lib and cannot
    depend on cortex, so skip when it is not importable."""
    cortex_dir = Path(__file__).resolve().parents[2] / "cortex"
    sys.path.insert(0, str(cortex_dir))
    try:
        config = importlib.import_module("app.config")
        vector = importlib.import_module("app.db.vector")
    except Exception:
        pytest.skip("cortex not importable from the corpus test env")
    finally:
        sys.path.remove(str(cortex_dir))
    return config, vector


def _cortex_upsert_client(config, vector):
    settings = config.Settings(
        QDRANT_HOST="localhost", QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_collection", EMBEDDING_DIM=8,
        LLM_BASE_URL="http://localhost:11434/v1", LLM_API_KEY="k",
        EMBEDDING_MODEL="test-embed",
    )
    client = vector.VectorClient(settings)
    qdrant = AsyncMock()
    qdrant.retrieve = AsyncMock(return_value=[])
    client._client = qdrant
    client._http_client = AsyncMock()
    return client, qdrant


class TestUpsertPromotion:
    @pytest.mark.asyncio
    async def test_visibility_promotes_to_top_level_payload(self):
        """The visibility filter (app/db/visibility.py) matches the top-level
        `visibility` key — a chunk whose flag stays nested is filtered as
        legacy-workspace, silently publishing a private chunk."""
        config, vector = _load_cortex()
        client, qdrant = _cortex_upsert_client(config, vector)
        with patch.object(client, "_embed", new=AsyncMock(return_value=[0.1] * 8)):
            await client.upsert(
                text="private chunk",
                metadata={"source": "corpus", "visibility": "member",
                          "member_id": "m1", "workspace_id": "ws-test"},
            )
        payload = qdrant.upsert.call_args.kwargs["points"][0].payload
        assert payload["visibility"] == "member"

    @pytest.mark.asyncio
    async def test_memory_writes_stay_keyless(self):
        """No default is stamped: absence is the legacy meaning the filter
        honors, and a stamped default would re-scope every memory write."""
        config, vector = _load_cortex()
        client, qdrant = _cortex_upsert_client(config, vector)
        with patch.object(client, "_embed", new=AsyncMock(return_value=[0.1] * 8)):
            await client.upsert(
                text="a plain memory",
                metadata={"source": "action_log", "domain": "general",
                          "workspace_id": "ws-test"},
            )
        payload = qdrant.upsert.call_args.kwargs["points"][0].payload
        assert "visibility" not in payload

    @pytest.mark.asyncio
    async def test_committed_promotes_to_top_level_payload(self):
        """GENERATION_GUARD matches the TOP-LEVEL `committed` key. A chunk
        written committed=False whose flag stays NESTED is never excluded, so a
        mid-ingest generation stays fully recallable — the review's claim-4
        seam, invisible to any fake that sits on one side of it. This drives
        real `upsert`, so it fails if the promotion is dropped."""
        config, vector = _load_cortex()
        client, qdrant = _cortex_upsert_client(config, vector)
        with patch.object(client, "_embed", new=AsyncMock(return_value=[0.1] * 8)):
            await client.upsert(
                text="a staged chunk",
                metadata={"source": "corpus", "committed": False,
                          "workspace_id": "ws-test"},
            )
        payload = qdrant.upsert.call_args.kwargs["points"][0].payload
        assert payload["committed"] is False, (
            "committed must land TOP-level or GENERATION_GUARD is a no-op")
        assert "committed" not in payload.get("metadata", {}), (
            "and must not linger nested — commit_generation writes top-level only")
