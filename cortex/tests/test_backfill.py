"""A2 — embedding retry + backfill queue (SP0, defect #2).

Write path is durability-bounded: _embed retries EMBED_RETRY_ATTEMPTS times
with exponential backoff; on final failure /memory/learn enqueues the memory
on the "memory:backfill" Redis stream (DB 0). A Celery beat task drains the
stream every 60s with per-entry backoff capped at 1h; entries exceeding
BACKFILL_MAX_ATTEMPTS move to the "memory:backfill:dlq" list.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fakeredis import aioredis as fakeaioredis

from app.config import Settings
from app.db.vector import VectorClient
from app.exceptions import VectorStoreError
from app.workers.backfill import (
    BACKFILL_DLQ_KEY,
    BACKFILL_STREAM_KEY,
    _drain,
    enqueue_backfill,
    requeue_dlq,
)


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
def vector_client(settings: Settings) -> VectorClient:
    client = VectorClient(settings)
    client._client = AsyncMock()
    client._http_client = AsyncMock()
    return client


@pytest.fixture()
def fake_redis():
    return fakeaioredis.FakeRedis(decode_responses=True)


def _ok_embed_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
    return resp


# ---------------------------------------------------------------------------
# _embed retry
# ---------------------------------------------------------------------------


class TestEmbedRetry:
    @pytest.mark.asyncio
    async def test_retries_on_transport_error_then_succeeds(self, vector_client):
        request_error = httpx.ConnectError(
            "refused", request=httpx.Request("POST", "http://test")
        )
        vector_client._http_client.post = AsyncMock(
            side_effect=[request_error, _ok_embed_response()]
        )
        with patch("app.db.vector.asyncio.sleep", new_callable=AsyncMock):
            result = await vector_client._embed("retry me")
        assert result == [0.1, 0.2]
        assert vector_client._http_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_exhausts_attempts_and_raises(self, vector_client):
        request_error = httpx.ConnectError(
            "refused", request=httpx.Request("POST", "http://test")
        )
        vector_client._http_client.post = AsyncMock(side_effect=request_error)
        with patch("app.db.vector.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(VectorStoreError, match="after 3 attempts"):
                await vector_client._embed("always fails")
        assert vector_client._http_client.post.call_count == 3
        assert mock_sleep.await_count == 2  # no sleep after the final attempt

    @pytest.mark.asyncio
    async def test_4xx_does_not_retry(self, vector_client):
        resp = MagicMock()
        resp.status_code = 422
        resp.text = "bad input"
        exc = httpx.HTTPStatusError("client error", request=MagicMock(), response=resp)
        resp.raise_for_status.side_effect = exc
        vector_client._http_client.post = AsyncMock(return_value=resp)
        with pytest.raises(VectorStoreError, match="Embedding endpoint returned"):
            await vector_client._embed("bad")
        assert vector_client._http_client.post.call_count == 1


# ---------------------------------------------------------------------------
# enqueue_backfill
# ---------------------------------------------------------------------------


class TestEnqueueBackfill:
    @pytest.mark.asyncio
    async def test_enqueue_adds_stream_entry(self, fake_redis):
        await enqueue_backfill(
            "mem-1",
            "some text",
            {"domain": "general", "namespace": "default"},
            redis_client=fake_redis,
        )
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert len(entries) == 1
        _entry_id, fields = entries[0]
        assert fields["memory_id"] == "mem-1"
        assert fields["text"] == "some text"
        assert json.loads(fields["payload"])["domain"] == "general"
        assert fields["attempts"] == "0"


# ---------------------------------------------------------------------------
# _drain
# ---------------------------------------------------------------------------


class TestDrain:
    @pytest.mark.asyncio
    async def test_successful_drain_upserts_and_removes(self, fake_redis):
        await enqueue_backfill(
            "mem-1",
            "text one",
            {"domain": "general", "tags": [], "source": "action_log", "namespace": "infra"},
            redis_client=fake_redis,
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(return_value="mem-1")
        result = await _drain(redis_client=fake_redis, vector_client=vector)
        assert result["drained"] == 1
        vector.upsert.assert_awaited_once()
        kwargs = vector.upsert.await_args.kwargs
        assert kwargs["text"] == "text one"
        assert kwargs["namespace"] == "infra"
        assert "namespace" not in kwargs["metadata"]
        assert await fake_redis.xlen(BACKFILL_STREAM_KEY) == 0

    @pytest.mark.asyncio
    async def test_failed_drain_requeues_with_backoff(self, fake_redis):
        await enqueue_backfill(
            "mem-1", "text one", {"namespace": "default"}, redis_client=fake_redis
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(side_effect=VectorStoreError("ollama down"))
        result = await _drain(redis_client=fake_redis, vector_client=vector)
        assert result["retried"] == 1
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert len(entries) == 1
        _eid, fields = entries[0]
        assert fields["attempts"] == "1"
        assert float(fields["next_attempt_at"]) > time.time()

    @pytest.mark.asyncio
    async def test_not_yet_due_entries_are_skipped(self, fake_redis):
        await fake_redis.xadd(
            BACKFILL_STREAM_KEY,
            {
                "memory_id": "mem-1",
                "text": "t",
                "payload": "{}",
                "attempts": "1",
                "next_attempt_at": str(time.time() + 999),
                "enqueued_at": "x",
            },
        )
        vector = AsyncMock()
        await _drain(redis_client=fake_redis, vector_client=vector)
        vector.upsert.assert_not_awaited()
        assert await fake_redis.xlen(BACKFILL_STREAM_KEY) == 1

    @pytest.mark.asyncio
    async def test_requeue_write_failure_preserves_original_entry(self, fake_redis):
        """Persist-then-delete: if the recovery xadd fails, the original stream
        entry must survive (a lost entry would be silently vector-less forever).
        """
        await enqueue_backfill(
            "mem-1", "text one", {"namespace": "default"}, redis_client=fake_redis
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(side_effect=VectorStoreError("ollama down"))
        real_xadd = fake_redis.xadd
        fake_redis.xadd = AsyncMock(side_effect=RuntimeError("redis blip"))
        try:
            with pytest.raises(RuntimeError, match="redis blip"):
                await _drain(redis_client=fake_redis, vector_client=vector)
        finally:
            fake_redis.xadd = real_xadd
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert len(entries) == 1
        _eid, fields = entries[0]
        assert fields["memory_id"] == "mem-1"
        assert fields["attempts"] == "0"  # original untouched, retried next beat

    @pytest.mark.asyncio
    async def test_dlq_write_failure_preserves_original_entry(self, fake_redis):
        """Persist-then-delete on the DLQ path: if the lpush fails, the stream
        entry must survive so the next drain re-attempts dead-lettering.
        """
        await fake_redis.xadd(
            BACKFILL_STREAM_KEY,
            {
                "memory_id": "mem-1",
                "text": "t",
                "payload": "{}",
                "attempts": "9",
                "next_attempt_at": "0",
                "enqueued_at": "x",
            },
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(side_effect=VectorStoreError("still down"))
        fake_redis.lpush = AsyncMock(side_effect=RuntimeError("redis blip"))
        with pytest.raises(RuntimeError, match="redis blip"):
            await _drain(redis_client=fake_redis, vector_client=vector)
        assert await fake_redis.xlen(BACKFILL_STREAM_KEY) == 1
        assert await fake_redis.llen(BACKFILL_DLQ_KEY) == 0

    @pytest.mark.asyncio
    async def test_legacy_entry_without_workspace_id_is_stamped(self, fake_redis, caplog):
        """identity-v2 D2: a payload lacking workspace_id (enqueued before it
        was always stamped) must be stamped with the deployment owner
        principal before upsert — not left to grind VectorClient.upsert's
        fail-closed mint into permanent retries/DLQ."""
        import logging

        from auth.principal import anonymous_principal

        await enqueue_backfill(
            "mem-legacy",
            "legacy text",
            {"domain": "general", "tags": [], "source": "action_log", "namespace": "infra"},
            redis_client=fake_redis,
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(return_value="mem-legacy")
        with caplog.at_level(logging.WARNING, logger="app.workers.backfill"):
            result = await _drain(redis_client=fake_redis, vector_client=vector)

        assert result["drained"] == 1
        kwargs = vector.upsert.await_args.kwargs
        owner = anonymous_principal()
        assert kwargs["metadata"]["workspace_id"] == owner["workspace_id"]
        assert kwargs["metadata"]["member_id"] == owner["member_id"]
        assert any("no workspace_id" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_modern_entry_with_workspace_id_is_untouched(self, fake_redis, caplog):
        """A payload that already carries a real workspace_id must pass
        through unstamped — the legacy fallback must never override a
        verified scope."""
        import logging

        await enqueue_backfill(
            "mem-modern",
            "modern text",
            {
                "domain": "general", "tags": [], "source": "action_log",
                "namespace": "infra", "workspace_id": "ws-real", "member_id": "member-real",
            },
            redis_client=fake_redis,
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(return_value="mem-modern")
        with caplog.at_level(logging.WARNING, logger="app.workers.backfill"):
            result = await _drain(redis_client=fake_redis, vector_client=vector)

        assert result["drained"] == 1
        kwargs = vector.upsert.await_args.kwargs
        assert kwargs["metadata"]["workspace_id"] == "ws-real"
        assert kwargs["metadata"]["member_id"] == "member-real"
        assert not any("no workspace_id" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_moves_to_dlq_after_max_attempts(self, fake_redis):
        await fake_redis.xadd(
            BACKFILL_STREAM_KEY,
            {
                "memory_id": "mem-1",
                "text": "t",
                "payload": "{}",
                "attempts": "9",
                "next_attempt_at": "0",
                "enqueued_at": "x",
            },
        )
        vector = AsyncMock()
        vector.upsert = AsyncMock(side_effect=VectorStoreError("still down"))
        result = await _drain(redis_client=fake_redis, vector_client=vector)
        assert result["dead_lettered"] == 1
        assert await fake_redis.xlen(BACKFILL_STREAM_KEY) == 0
        assert await fake_redis.llen(BACKFILL_DLQ_KEY) == 1
        record = json.loads(await fake_redis.lindex(BACKFILL_DLQ_KEY, 0))
        assert record["memory_id"] == "mem-1"
        assert record["attempts"] == 10
        assert "still down" in record["last_error"]


# ---------------------------------------------------------------------------
# requeue_dlq — manual DLQ recovery (entries dead-lettered while the embedding
# backend was down have no automatic path back; POST /ops/dlq/requeue calls this)
# ---------------------------------------------------------------------------


def _dlq_record(memory_id: str = "mem-1", text: str = "t") -> str:
    return json.dumps(
        {
            "memory_id": memory_id,
            "text": text,
            "payload": json.dumps({"domain": "general", "namespace": "default"}),
            "attempts": 10,
            "last_error": "embed down",
            "failed_at": "2026-07-01T00:00:00+00:00",
        }
    )


class TestRequeueDlq:
    @pytest.mark.asyncio
    async def test_requeues_item_with_attempts_reset(self, fake_redis):
        await fake_redis.lpush(BACKFILL_DLQ_KEY, _dlq_record())
        result = await requeue_dlq(redis_client=fake_redis)
        assert result["requeued"] == 1
        assert result["remaining"] == 0
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert len(entries) == 1
        _eid, fields = entries[0]
        assert fields["memory_id"] == "mem-1"
        assert fields["text"] == "t"
        assert fields["attempts"] == "0"
        assert fields["next_attempt_at"] == "0"
        assert json.loads(fields["payload"])["domain"] == "general"
        assert await fake_redis.llen(BACKFILL_DLQ_KEY) == 0

    @pytest.mark.asyncio
    async def test_oldest_first_and_limit_respected(self, fake_redis):
        # lpush appends at the head, so mem-0 is the oldest (tail) record.
        for i in range(3):
            await fake_redis.lpush(BACKFILL_DLQ_KEY, _dlq_record(f"mem-{i}"))
        result = await requeue_dlq(redis_client=fake_redis, limit=2)
        assert result["requeued"] == 2
        assert result["remaining"] == 1
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert [f["memory_id"] for _eid, f in entries] == ["mem-0", "mem-1"]

    @pytest.mark.asyncio
    async def test_empty_dlq_is_noop(self, fake_redis):
        result = await requeue_dlq(redis_client=fake_redis)
        assert result["requeued"] == 0
        assert result["remaining"] == 0
        assert await fake_redis.xlen(BACKFILL_STREAM_KEY) == 0

    @pytest.mark.asyncio
    async def test_xadd_failure_restores_record_and_stops(self, fake_redis):
        """Pop-then-write: if the stream write fails, the popped record must be
        restored to the DLQ (never silently lost) and the loop must stop."""
        await fake_redis.lpush(BACKFILL_DLQ_KEY, _dlq_record())
        fake_redis.xadd = AsyncMock(side_effect=RuntimeError("redis blip"))
        result = await requeue_dlq(redis_client=fake_redis)
        assert result["requeued"] == 0
        assert result["failed"] == 1
        assert await fake_redis.llen(BACKFILL_DLQ_KEY) == 1

    @pytest.mark.asyncio
    async def test_malformed_restore_failure_logs_content_and_stops(self, fake_redis, caplog):
        """If the malformed-branch restore lpush itself fails (Redis blip after
        the pop), the record's full content must be logged CRITICAL so an
        operator can restore it by hand — never propagated away unlogged."""
        import logging

        await fake_redis.lpush(BACKFILL_DLQ_KEY, "not json {{{")
        fake_redis.lpush = AsyncMock(side_effect=RuntimeError("redis blip"))
        with caplog.at_level(logging.CRITICAL, logger="app.workers.backfill"):
            result = await requeue_dlq(redis_client=fake_redis)
        assert result["failed"] == 1
        assert result["malformed_kept"] == 0  # it was NOT kept — don't claim it was
        assert any("not json {{{" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_malformed_record_is_kept_and_others_processed(self, fake_redis):
        """A non-JSON DLQ record is pushed back (never dropped) and counted;
        well-formed records still requeue in the same call."""
        await fake_redis.lpush(BACKFILL_DLQ_KEY, "not json {{{")
        await fake_redis.lpush(BACKFILL_DLQ_KEY, _dlq_record("mem-ok"))
        result = await requeue_dlq(redis_client=fake_redis)
        assert result["requeued"] == 1
        assert result["malformed_kept"] == 1
        assert result["remaining"] == 1
        assert await fake_redis.lindex(BACKFILL_DLQ_KEY, 0) == "not json {{{"
        entries = await fake_redis.xrange(BACKFILL_STREAM_KEY, min="-", max="+")
        assert [f["memory_id"] for _eid, f in entries] == ["mem-ok"]


# ---------------------------------------------------------------------------
# Beat wiring
# ---------------------------------------------------------------------------


class TestBeatWiring:
    def test_backfill_registered_in_beat_schedule(self):
        from app.workers.sleep_cycle import celery_app

        schedule = celery_app.conf.beat_schedule
        assert "memory-backfill-drain" in schedule
        assert (
            schedule["memory-backfill-drain"]["task"]
            == "app.workers.backfill.drain_backfill_queue"
        )
        assert schedule["memory-backfill-drain"]["schedule"] == 60.0
        assert "app.workers.backfill" in celery_app.conf.include

    def test_config_defaults(self):
        s = Settings(NEO4J_PASSWORD="x", LLM_API_KEY="x")
        assert s.EMBED_RETRY_ATTEMPTS == 3
        assert s.BACKFILL_MAX_ATTEMPTS == 10


# ---------------------------------------------------------------------------
# /memory/learn enqueues on vector failure (uses conftest test_client fixture)
# ---------------------------------------------------------------------------


class TestLearnEnqueuesOnVectorFailure:
    def test_partial_learn_enqueues_backfill(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        mock_vector.upsert = AsyncMock(side_effect=VectorStoreError("embed down"))
        mock_redis.xadd = AsyncMock(return_value="1-0")
        resp = test_client.post(
            "/memory/learn",
            json={"action": "did a thing", "outcome": "it worked", "domain": "general"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["vector_id"] is None
        assert body["backfill_queued"] is True
        mock_redis.xadd.assert_awaited_once()
        assert mock_redis.xadd.await_args.args[0] == "memory:backfill"
        fields = mock_redis.xadd.await_args.args[1]
        payload = json.loads(fields["payload"])
        assert payload["namespace"] == "default"
        assert payload["domain"] == "general"

    def test_partial_learn_reports_enqueue_failure(
        self, test_client, mock_graph, mock_vector, mock_redis
    ):
        """Enqueue failure must not fake success: backfill_queued stays False."""
        mock_vector.upsert = AsyncMock(side_effect=VectorStoreError("embed down"))
        mock_redis.xadd = AsyncMock(side_effect=RuntimeError("redis write refused"))
        resp = test_client.post(
            "/memory/learn",
            json={"action": "did a thing", "outcome": "it worked", "domain": "general"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"
        assert body["backfill_queued"] is False

    @pytest.mark.asyncio
    async def test_wellformed_double_failure_logs_full_record_at_critical(self, fake_redis, caplog):
        """The well-formed requeue path: when the stream xadd AND the restore
        lpush BOTH fail, the full record must be logged CRITICAL — the only
        forensic trail after real loss. A mutant dropping that log slips past
        the single-failure tests, so pin it here."""
        import logging

        await fake_redis.lpush(BACKFILL_DLQ_KEY, _dlq_record("mem-lost"))
        fake_redis.xadd = AsyncMock(side_effect=RuntimeError("stream down"))
        fake_redis.lpush = AsyncMock(side_effect=RuntimeError("restore down"))
        with caplog.at_level(logging.CRITICAL, logger="app.workers.backfill"):
            result = await requeue_dlq(redis_client=fake_redis)

        assert result["failed"] == 1
        assert any("mem-lost" in r.getMessage() for r in caplog.records
                   if r.levelno == logging.CRITICAL)
