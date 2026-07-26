"""Tests for distill DLQ requeue: requeue_dlq + POST /ops/distill-dlq/requeue.

Records dead-lettered by the distill worker (nb:distill:dlq) previously had
no path back onto the queue. requeue_dlq re-enqueues them oldest-first via
enqueue_distillation (the ONE place that owns the queue field shape) with
attempts reset; sessions whose keys hit the post-DLQ 7-day TTL are dropped
with their full content logged (restoring them would make the DLQ row
un-drainable). Guarded-restore invariant throughout: a popped record is
never silently lost.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

import app.mcp_server as mcp_mod
from app.config import Settings
from app.distill_worker import DLQ_KEY, QUEUE_KEY, requeue_dlq


def _dlq_record(session_id="sess-1"):
    return json.dumps({"session_id": session_id, "attempts": 10,
                       "error": "llm down", "failed_at": "2026-07-01T00:00:00+00:00"})


def _redis_with_dlq(records):
    """AsyncMock redis scripted for one requeue pass over `records`."""
    r = AsyncMock()
    r.llen = AsyncMock(side_effect=[len(records), 0])  # initial_len, remaining
    r.rpop = AsyncMock(side_effect=list(records) + [None])
    r.lpush = AsyncMock()
    r.xadd = AsyncMock(return_value="1-0")
    return r


def _mock_manager(session_exists=True):
    mgr = AsyncMock()
    mgr.get_session_data = AsyncMock(return_value={"goal": "g"} if session_exists else None)
    mgr.set_distillation_status = AsyncMock()
    return mgr


class TestRequeueDlq:
    @pytest.mark.asyncio
    async def test_requeues_via_enqueue_distillation_with_attempts_reset(self):
        redis = _redis_with_dlq([_dlq_record()])
        mgr = _mock_manager()
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            result = await requeue_dlq(redis, Settings())
        assert result["requeued"] == 1
        redis.xadd.assert_awaited_once()
        key, fields = redis.xadd.await_args.args
        assert key == QUEUE_KEY
        assert fields["session_id"] == "sess-1"
        assert fields["attempts"] == "0"
        mgr.set_distillation_status.assert_awaited_once_with("sess-1", "queued")

    @pytest.mark.asyncio
    async def test_expired_session_dropped_with_full_log_not_restored(self, caplog):
        import logging
        redis = _redis_with_dlq([_dlq_record("gone-1")])
        mgr = _mock_manager(session_exists=False)
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            with caplog.at_level(logging.WARNING, logger="app.distill_worker"):
                result = await requeue_dlq(redis, Settings())
        assert result["expired_dropped"] == 1
        assert result["requeued"] == 0
        redis.xadd.assert_not_awaited()
        # dropped, NOT restored — restoring would spin the DLQ row forever
        redis.lpush.assert_not_awaited()
        assert any("gone-1" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_malformed_record_restored_and_counted(self):
        redis = _redis_with_dlq(["not json {{{", _dlq_record()])
        redis.llen = AsyncMock(side_effect=[2, 1])
        mgr = _mock_manager()
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            result = await requeue_dlq(redis, Settings())
        assert result["malformed_kept"] == 1
        assert result["requeued"] == 1
        redis.lpush.assert_awaited_once_with(DLQ_KEY, "not json {{{")

    @pytest.mark.asyncio
    async def test_enqueue_failure_restores_record_and_stops(self):
        redis = _redis_with_dlq([_dlq_record()])
        redis.llen = AsyncMock(side_effect=[1, 1])
        redis.xadd = AsyncMock(side_effect=RuntimeError("redis blip"))
        mgr = _mock_manager()
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            result = await requeue_dlq(redis, Settings())
        assert result["failed"] == 1
        assert result["requeued"] == 0
        redis.lpush.assert_awaited_once()  # restored to the DLQ

    @pytest.mark.asyncio
    async def test_double_failure_logs_full_record_at_critical(self, caplog):
        import logging
        raw = _dlq_record("sess-crit")
        redis = _redis_with_dlq([raw])
        redis.llen = AsyncMock(side_effect=[1, 0])
        redis.xadd = AsyncMock(side_effect=RuntimeError("down"))
        redis.lpush = AsyncMock(side_effect=RuntimeError("still down"))
        mgr = _mock_manager()
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            with caplog.at_level(logging.CRITICAL, logger="app.distill_worker"):
                result = await requeue_dlq(redis, Settings())
        assert result["failed"] == 1
        assert any("sess-crit" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_session_lookup_failure_restores_record_and_stops(self):
        redis = _redis_with_dlq([_dlq_record()])
        redis.llen = AsyncMock(side_effect=[1, 1])
        mgr = _mock_manager()
        mgr.get_session_data = AsyncMock(side_effect=RuntimeError("redis blip"))
        with patch("app.distill_worker.SessionManager", MagicMock(return_value=mgr)):
            result = await requeue_dlq(redis, Settings())
        assert result["failed"] == 1
        redis.xadd.assert_not_awaited()
        redis.lpush.assert_awaited_once()  # restored


# ---------------------------------------------------------------------------
# Route: POST /ops/distill-dlq/requeue — admin-scoped via require_scope_asgi
# (same wrapper-test technique as test_session_context_route.py)
# ---------------------------------------------------------------------------


def _make_request(query_string=b"", identity=None):
    scope = {"type": "http", "method": "POST", "path": "/ops/distill-dlq/requeue",
             "headers": [], "query_string": query_string, "state": {}}
    if identity is not None:
        scope["state"]["identity"] = identity
    return Request(scope)


@pytest.fixture
def auth_enabled(monkeypatch):
    # Patch the env-derived settings require_scope_asgi actually reads —
    # keys._AUTH_ENABLED is init_auth() state that never exists in the
    # bridge process (the 2026-07-16 scope-gate regression).
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=True))


@pytest.fixture
def auth_disabled(monkeypatch):
    import auth.asgi as asgi_module
    from auth.config import AuthSettings
    monkeypatch.setattr(asgi_module, "get_auth_settings", lambda: AuthSettings(ENABLED=False))


_OK_RESULT = {"status": "completed", "requeued": 1, "failed": 0,
              "malformed_kept": 0, "expired_dropped": 0, "remaining": 0}


class TestRequeueRoute:
    @pytest.mark.asyncio
    async def test_admin_scope_allowed_and_limit_forwarded(self, auth_enabled, monkeypatch):
        spy = AsyncMock(return_value=_OK_RESULT)
        monkeypatch.setattr(mcp_mod, "requeue_distill_dlq_records", spy)
        monkeypatch.setattr(mcp_mod, "get_redis", AsyncMock(return_value=AsyncMock()))
        resp = await mcp_mod._requeue_distill_dlq(_make_request(
            query_string=b"limit=5",
            identity={"agent_id": "ops", "scopes": ["admin"], "key_id": "k1"}))
        assert resp.status_code == 200
        assert spy.await_args.kwargs["limit"] == 5
        assert json.loads(resp.body)["queue"] == "distill_dlq"

    @pytest.mark.asyncio
    async def test_non_admin_scope_denied(self, auth_enabled, monkeypatch):
        spy = AsyncMock()
        monkeypatch.setattr(mcp_mod, "requeue_distill_dlq_records", spy)
        resp = await mcp_mod._requeue_distill_dlq(_make_request(
            identity={"agent_id": "x", "scopes": ["session:write"], "key_id": "k1"}))
        assert resp.status_code == 403
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_auth_disabled_passes_through(self, auth_disabled, monkeypatch):
        spy = AsyncMock(return_value=dict(_OK_RESULT, requeued=0))
        monkeypatch.setattr(mcp_mod, "requeue_distill_dlq_records", spy)
        monkeypatch.setattr(mcp_mod, "get_redis", AsyncMock(return_value=AsyncMock()))
        resp = await mcp_mod._requeue_distill_dlq(_make_request())
        assert resp.status_code == 200
