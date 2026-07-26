"""Tests for queued distillation with retry/backoff and DLQ (SP0 D1)."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.config import Settings
from app.distill_worker import (
    DLQ_KEY,
    MAX_ATTEMPTS,
    QUEUE_KEY,
    enqueue_distillation,
    process_queue_once,
)


def _entry(session_id="sess-1", attempts=0, next_at=0.0):
    return (
        "1-0",
        {
            "session_id": session_id,
            "attempts": str(attempts),
            "next_attempt_at": str(next_at),
        },
    )


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_adds_stream_entry(self, mock_redis):
        mock_redis.xadd = AsyncMock(return_value="1-0")
        entry_id = await enqueue_distillation(mock_redis, "sess-1")
        assert entry_id == "1-0"
        key, fields = mock_redis.xadd.call_args.args
        assert key == QUEUE_KEY
        assert fields["session_id"] == "sess-1"
        assert fields["attempts"] == "0"


class TestProcessQueueOnce:
    @pytest.mark.asyncio
    async def test_success_sets_status_and_ttl_and_deletes_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(return_value=[_entry()])
        mock_redis.xdel = AsyncMock()
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(
                return_value={"goal": "g", "outcome": "done", "agent_id": "alice"}
            )
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "success", "firekeep_memory_id": "v1"}
            )
            mock_get_d.return_value = distiller

            n = await process_queue_once(mock_redis, Settings())

        assert n == 1
        distiller.distill.assert_awaited_once()
        assert distiller.distill.call_args.kwargs["session_id"] == "sess-1"
        mgr.set_distillation_status.assert_awaited_once_with("sess-1", "success")
        mgr.expire_session_keys.assert_awaited_once_with("sess-1")
        mock_redis.xdel.assert_awaited_once_with(QUEUE_KEY, "1-0")
        mock_redis.lpush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_requeues_with_backoff_and_no_ttl(self, mock_redis):
        mock_redis.xrange = AsyncMock(return_value=[_entry(attempts=0)])
        mock_redis.xdel = AsyncMock()
        mock_redis.xadd = AsyncMock()
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"goal": "g"})
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "failed", "error": "cortex down"}
            )
            mock_get_d.return_value = distiller

            n = await process_queue_once(mock_redis, Settings())

        assert n == 0
        fields = mock_redis.xadd.call_args.args[1]
        assert fields["session_id"] == "sess-1"
        assert fields["attempts"] == "1"
        assert float(fields["next_attempt_at"]) > time.time() - 1
        mgr.expire_session_keys.assert_not_awaited()  # TTL only after success/DLQ
        mock_redis.lpush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_max_attempts_moves_to_dlq_and_sets_ttl(self, mock_redis):
        mock_redis.xrange = AsyncMock(
            return_value=[_entry(attempts=MAX_ATTEMPTS - 1)]
        )
        mock_redis.xdel = AsyncMock()
        mock_redis.lpush = AsyncMock()
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"goal": "g"})
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "failed", "error": "cortex down"}
            )
            mock_get_d.return_value = distiller

            await process_queue_once(mock_redis, Settings())

        key, payload = mock_redis.lpush.call_args.args
        assert key == DLQ_KEY
        record = json.loads(payload)
        assert record["session_id"] == "sess-1"
        assert record["attempts"] == MAX_ATTEMPTS
        assert record["error"] == "cortex down"
        mgr.set_distillation_status.assert_awaited_once_with("sess-1", "dlq")
        mgr.expire_session_keys.assert_awaited_once_with("sess-1")

    @pytest.mark.asyncio
    async def test_backoff_not_elapsed_skips_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(
            return_value=[_entry(next_at=time.time() + 600)]
        )
        mock_redis.xdel = AsyncMock()
        with patch("app.distill_worker._get_distiller") as mock_get_d:
            n = await process_queue_once(mock_redis, Settings())
        assert n == 0
        mock_get_d.assert_not_called()
        mock_redis.xdel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_vanished_session_drops_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(return_value=[_entry()])
        mock_redis.xdel = AsyncMock()
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value=None)
            MockMgr.return_value = mgr
            await process_queue_once(mock_redis, Settings())
        mock_redis.xdel.assert_awaited_once_with(QUEUE_KEY, "1-0")
        mock_get_d.assert_not_called()


class TestPersistThenDelete:
    """A crash between the terminal write and xdel must leave the queue entry
    intact. Worst case is a duplicate distill/retry (harmless); a lost entry
    is a permanent silent leak — the session stays distillation="queued"
    forever, never TTL'd, and pinned from cleanup by _enforce_max_sessions.
    Mirrors cortex test_backfill.py::test_requeue_write_failure_preserves_original_entry.
    """

    @pytest.mark.asyncio
    async def test_success_status_write_failure_preserves_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(return_value=[_entry()])
        mock_redis.xdel = AsyncMock()
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"goal": "g"})
            mgr.set_distillation_status = AsyncMock(
                side_effect=RuntimeError("redis blip")
            )
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "success", "firekeep_memory_id": "v1"}
            )
            mock_get_d.return_value = distiller

            with pytest.raises(RuntimeError, match="redis blip"):
                await process_queue_once(mock_redis, Settings())

        mock_redis.xdel.assert_not_awaited()  # entry survives for next pass

    @pytest.mark.asyncio
    async def test_retry_requeue_failure_preserves_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(return_value=[_entry(attempts=0)])
        mock_redis.xdel = AsyncMock()
        mock_redis.xadd = AsyncMock(side_effect=RuntimeError("redis blip"))
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"goal": "g"})
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "failed", "error": "cortex down"}
            )
            mock_get_d.return_value = distiller

            with pytest.raises(RuntimeError, match="redis blip"):
                await process_queue_once(mock_redis, Settings())

        mock_redis.xdel.assert_not_awaited()  # entry survives for next pass

    @pytest.mark.asyncio
    async def test_dlq_write_failure_preserves_entry(self, mock_redis):
        mock_redis.xrange = AsyncMock(
            return_value=[_entry(attempts=MAX_ATTEMPTS - 1)]
        )
        mock_redis.xdel = AsyncMock()
        mock_redis.lpush = AsyncMock(side_effect=RuntimeError("redis blip"))
        with (
            patch("app.distill_worker.SessionManager") as MockMgr,
            patch("app.distill_worker._get_distiller") as mock_get_d,
        ):
            mgr = AsyncMock()
            mgr.get_session_data = AsyncMock(return_value={"goal": "g"})
            MockMgr.return_value = mgr
            distiller = AsyncMock()
            distiller.distill = AsyncMock(
                return_value={"status": "failed", "error": "cortex down"}
            )
            mock_get_d.return_value = distiller

            with pytest.raises(RuntimeError, match="redis blip"):
                await process_queue_once(mock_redis, Settings())

        mock_redis.xdel.assert_not_awaited()  # entry survives for next pass
        mgr.expire_session_keys.assert_not_awaited()  # TTL must not land either


class TestCompleteSessionQueues:
    @pytest.mark.asyncio
    async def test_complete_session_enqueues_and_sets_no_ttl(self, mock_redis):
        from app.session import SessionManager

        mock_redis.get = AsyncMock(return_value="sess-1")
        mock_redis.hgetall = AsyncMock(
            return_value={"goal": "g", "agent_id": "alice", "status": "active"}
        )
        mgr = SessionManager(mock_redis, Settings())

        result = await mgr.complete_session(session_id="sess-1")

        assert result["status"] == "completed"

        # Final-review fix: enqueue XADD now happens on the SAME transaction
        # pipeline as the "queued" state hset, not as a separate post-commit
        # call — proves state-and-job commit atomically.
        pipe = mock_redis._pipeline
        pipe.xadd.assert_called_once()
        assert pipe.xadd.call_args.args[0] == QUEUE_KEY
        assert pipe.xadd.call_args.args[1]["session_id"] == "sess-1"
        mock_redis.xadd.assert_not_called()  # not fired outside the pipeline

        pipe.expire.assert_not_called()  # D1: worker owns the TTL now
        mapping = pipe.hset.call_args.kwargs["mapping"]
        assert mapping["distillation"] == "queued"

    @pytest.mark.asyncio
    async def test_enforce_max_skips_undistilled_completed(self, mock_redis):
        from app.session import SessionManager

        settings = Settings(MAX_SESSIONS=1)
        mock_redis.zcard = AsyncMock(return_value=3)
        mock_redis.zrange = AsyncMock(return_value=["old-1", "old-2"])
        metas = {
            "old-1": {"status": "completed", "distillation": "queued"},
            "old-2": {"status": "completed", "distillation": "success"},
        }
        mock_redis.hgetall = AsyncMock(
            side_effect=lambda key: metas.get(key.split(":")[-1], {})
        )
        mgr = SessionManager(mock_redis, settings)

        await mgr._enforce_max_sessions()

        deleted_keys = [c.args[0] for c in mock_redis.delete.call_args_list]
        assert all("old-1" not in k for k in deleted_keys)
        assert any("old-2" in k for k in deleted_keys)


class TestCtxCompleteNoInlineDistill:
    @pytest.mark.asyncio
    async def test_ctx_complete_reports_queued(self):
        from app.mcp_server import ctx_complete_session

        with (
            patch("app.mcp_server._get_manager") as mock_get,
            patch("app.mcp_server.get_http_headers", return_value={}),
            patch("app.mcp_server._trigger_eval", new=AsyncMock(return_value=True)),
            patch(
                "app.mcp_server._trigger_skill_evaluate",
                new=AsyncMock(return_value=True),
            ),
        ):
            mgr = AsyncMock()
            mgr.complete_session = AsyncMock(
                return_value={"status": "completed", "session_id": "s1"}
            )
            mock_get.return_value = mgr
            result = await ctx_complete_session()

        assert result["distillation"] == "queued"


class TestLifespanWiring:
    def test_mcp_server_registers_worker_lifespan(self):
        import app.mcp_server as mod

        assert mod.mcp._lifespan is mod._lifespan

    @pytest.mark.asyncio
    async def test_lifespan_closes_distiller_on_shutdown(self):
        """The worker's module-level Distiller (httpx client) must be closed
        when the server lifespan exits — restores the explicit-cleanup pattern
        the old mcp_server._shutdown had for its inline distiller."""
        import app.distill_worker as dw
        import app.mcp_server as mod

        fake_distiller = AsyncMock()
        with (
            patch("app.distill_worker.distill_worker_loop", new=AsyncMock()),
            patch.object(dw, "_distiller", fake_distiller),
        ):
            async with mod._lifespan(None):
                pass
            fake_distiller.close.assert_awaited_once()
