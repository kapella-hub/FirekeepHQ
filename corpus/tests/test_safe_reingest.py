"""SP0 A4 — staged corpus re-ingest (defect #7).

Old flow deleted all old chunks BEFORE storing new ones: a mid-ingest
embedding failure destroyed old content and left stale source metadata.
New flow: stage new chunks (tagged with a per-run ingest_id) -> delete the
old generation (excluding the new ingest_id) -> track_source last.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import FieldCondition

from corpus.pipeline import ingest_document

CONTENT = "CSG handles billing. Provisioning triggers CSG. " * 20


@pytest.fixture()
def call_order():
    return []


@pytest.fixture()
def vector_client(call_order):
    vc = AsyncMock()

    async def _upsert(*args, **kwargs):
        call_order.append("upsert")
        return "point-id"

    async def _delete(*args, **kwargs):
        call_order.append("delete")

    vc.upsert = AsyncMock(side_effect=_upsert)
    vc.delete_by_filter = AsyncMock(side_effect=_delete)
    return vc


@pytest.fixture()
def redis_client(call_order):
    r = AsyncMock()

    async def _set(*args, **kwargs):
        call_order.append("track")

    r.set = AsyncMock(side_effect=_set)
    r.zadd = AsyncMock()
    return r


class TestStagedReingest:
    @pytest.mark.asyncio
    async def test_all_upserts_happen_before_delete(
        self, vector_client, redis_client, call_order
    ):
        await ingest_document(
            content=CONTENT,
            source_name="Doc",
            source_type="text",
            vector_client=vector_client,
            redis_client=redis_client,
        )
        assert "upsert" in call_order and "delete" in call_order
        last_upsert = max(i for i, c in enumerate(call_order) if c == "upsert")
        first_delete = min(i for i, c in enumerate(call_order) if c == "delete")
        assert last_upsert < first_delete

    @pytest.mark.asyncio
    async def test_track_source_runs_after_delete(
        self, vector_client, redis_client, call_order
    ):
        await ingest_document(
            content=CONTENT,
            source_name="Doc",
            vector_client=vector_client,
            redis_client=redis_client,
        )
        assert call_order.index("track") > call_order.index("delete")

    @pytest.mark.asyncio
    async def test_chunks_carry_ingest_id(self, vector_client, redis_client):
        await ingest_document(
            content=CONTENT,
            source_name="Doc",
            vector_client=vector_client,
            redis_client=redis_client,
        )
        for call in vector_client.upsert.await_args_list:
            assert call.kwargs["metadata"]["ingest_id"]

    @pytest.mark.asyncio
    async def test_delete_excludes_new_ingest_id(self, vector_client, redis_client):
        await ingest_document(
            content=CONTENT,
            source_name="Doc",
            vector_client=vector_client,
            redis_client=redis_client,
        )
        delete_filter = vector_client.delete_by_filter.await_args.args[0]
        assert delete_filter.must_not, "old-generation delete must exclude the new ingest_id"
        cond = delete_filter.must_not[0]
        assert isinstance(cond, FieldCondition)
        assert cond.key == "metadata.ingest_id"
        upsert_meta = vector_client.upsert.await_args.kwargs["metadata"]
        assert cond.match.value == upsert_meta["ingest_id"]

    @pytest.mark.asyncio
    async def test_midingest_failure_preserves_old_generation(self, redis_client):
        vc = AsyncMock()
        vc.upsert = AsyncMock(side_effect=RuntimeError("embed died"))
        vc.delete_by_filter = AsyncMock()
        with pytest.raises(RuntimeError, match="embed died"):
            await ingest_document(
                content=CONTENT,
                source_name="Doc",
                vector_client=vc,
                redis_client=redis_client,
            )
        vc.delete_by_filter.assert_not_awaited()  # old chunks untouched
        redis_client.set.assert_not_awaited()  # source metadata unchanged
        redis_client.zadd.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_content_preserves_old_chunks(self, vector_client, redis_client):
        result = await ingest_document(
            content="",
            source_name="Doc",
            vector_client=vector_client,
            redis_client=redis_client,
        )
        assert result["chunks_stored"] == 0
        vector_client.delete_by_filter.assert_not_awaited()
