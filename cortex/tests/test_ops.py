from __future__ import annotations

import pytest

from app.ops import _get_queue_depths, _inspect_workers


class _FakeInspect:
    def stats(self):
        return {
            "worker@node": {
                "pool": {"implementation": "solo", "processes": [1234]},
                "total": {"task-a": 2, "task-b": 1},
            }
        }

    def active(self):
        return {"worker@node": [{"id": "a"}]}

    def reserved(self):
        return {"worker@node": [{"id": "b"}, {"id": "c"}]}

    def scheduled(self):
        return {"worker@node": [{"id": "d"}]}

    def registered(self):
        return {"worker@node": ["task-a", "task-b", "task-c"]}


class _FakeRedis:
    async def llen(self, key):
        return 3 if key == "celery" else 1 if key == "training" else 0


def test_inspect_workers_normalizes_state():
    workers = _inspect_workers(_FakeInspect())

    assert len(workers) == 1
    assert workers[0]["name"] == "worker@node"
    assert workers[0]["active"] == 1
    assert workers[0]["reserved"] == 2
    assert workers[0]["scheduled"] == 1
    assert workers[0]["registered_tasks"] == 3
    assert workers[0]["total_tasks"] == 3


@pytest.mark.asyncio
async def test_get_queue_depths_reads_default_and_training():
    queues = await _get_queue_depths(_FakeRedis(), ("celery", "training"))

    assert queues == [
        {"name": "celery", "depth": 3},
        {"name": "training", "depth": 1},
    ]


def test_inspect_workers_exposes_dashboard_aliases():
    """The dashboard reads worker.active_tasks — assert the alias is present."""
    workers = _inspect_workers(_FakeInspect())
    assert workers[0]["active_tasks"] == workers[0]["active"]
    # _FakeInspect.active() items carry "id" not "name", so name falls back to "?"
    assert workers[0]["active_task_names"] == ["?"]


def test_ops_router_registers_expected_paths():
    """Smoke-check that the router mounts /ops/workers and /ops/queues."""
    from app.ops import create_ops_router

    router = create_ops_router()
    paths = {r.path for r in router.routes}
    assert "/ops/workers" in paths
    assert "/ops/queues" in paths
    assert "/ops/dlq/requeue" in paths


def test_post_dlq_requeue_forwards_limit_and_names_queue():
    """POST /ops/dlq/requeue drives app.workers.backfill.requeue_dlq and tags
    the response with the queue it operates on (memory_backfill_dlq only —
    event_dlq: POST /ops/dlq/retry-events; distill_dlq: Bridge POST
    /ops/distill-dlq/requeue)."""
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.ops import create_ops_router

    test_app = FastAPI()
    test_app.include_router(create_ops_router())

    requeue_result = {
        "status": "completed",
        "requeued": 4,
        "failed": 0,
        "malformed_kept": 0,
        "remaining": 0,
    }
    with patch("app.ops.requeue_dlq", AsyncMock(return_value=requeue_result)) as spy:
        client = TestClient(test_app)
        resp = client.post("/ops/dlq/requeue?limit=5")

    assert resp.status_code == 200
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["limit"] == 5
    body = resp.json()
    assert body["queue"] == "memory_backfill_dlq"
    assert body["requeued"] == 4


def test_post_dlq_requeue_rejects_out_of_range_limit():
    """The limit query param is bounded (1..10000, DLQ_MAX_SIZE ceiling)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.ops import create_ops_router

    test_app = FastAPI()
    test_app.include_router(create_ops_router())
    client = TestClient(test_app)

    assert client.post("/ops/dlq/requeue?limit=0").status_code == 422
    assert client.post("/ops/dlq/requeue?limit=10001").status_code == 422


def test_get_queues_includes_backfill_depths():
    """SP0: /ops/queues must surface the memory:backfill stream + DLQ depths.

    The backfill stream lives in the data DB (settings.REDIS_URL), not the
    Celery broker DB — both connections are patched to the same fake here.
    """
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.ops import create_ops_router

    event_key = get_settings().REDIS_STREAM_KEY

    class _FakeOpsRedis:
        async def llen(self, key):
            return {
                "celery": 3,
                event_key: 2,
                f"{event_key}:dlq": 1,
                "memory:backfill:dlq": 4,
                "nb:distill:dlq": 2,
            }.get(key, 0)

        async def xlen(self, key):
            assert key == "memory:backfill"
            return 7

        async def aclose(self):
            pass

    test_app = FastAPI()
    test_app.include_router(create_ops_router())

    with patch("app.ops.aioredis.from_url", return_value=_FakeOpsRedis()):
        client = TestClient(test_app)
        resp = client.get("/ops/queues")

    assert resp.status_code == 200
    queues = resp.json()["queues"]
    # Existing keys preserved (dashboard contract)
    assert queues["celery"] == 3
    assert queues["event_stream"] == 2
    assert queues["event_dlq"] == 1
    # New SP0 keys
    assert queues["memory_backfill"] == 7
    assert queues["memory_backfill_dlq"] == 4
    # Bridge distill DLQ (Redis DB 3, same instance) — spec D1 requires it surfaced
    assert queues["distill_dlq"] == 2


@pytest.mark.asyncio
async def test_retry_event_dlq_moves_items_oldest_first_with_limit():
    from fakeredis import aioredis as fakeaioredis

    from app.config import get_settings
    from app.ops import retry_event_dlq

    event_key = get_settings().REDIS_STREAM_KEY
    fake = fakeaioredis.FakeRedis(decode_responses=True)
    for i in range(3):  # lpush → item-0 is the oldest (tail)
        await fake.lpush(f"{event_key}:dlq", f"item-{i}")

    result = await retry_event_dlq(redis_client=fake, limit=2)

    assert result["requeued"] == 2
    assert result["remaining"] == 1
    # the two oldest items are back on the main (rpop-consumed FIFO) queue
    main = await fake.lrange(event_key, 0, -1)
    assert sorted(main) == ["item-0", "item-1"]


@pytest.mark.asyncio
async def test_retry_event_dlq_restores_item_when_queue_write_fails():
    from fakeredis import aioredis as fakeaioredis

    from app.config import get_settings
    from app.ops import retry_event_dlq

    event_key = get_settings().REDIS_STREAM_KEY
    fake = fakeaioredis.FakeRedis(decode_responses=True)
    await fake.lpush(f"{event_key}:dlq", "item-x")
    real_lpush = fake.lpush

    async def flaky_lpush(key, *values):
        if key == event_key:
            raise RuntimeError("redis blip")
        return await real_lpush(key, *values)

    fake.lpush = flaky_lpush
    result = await retry_event_dlq(redis_client=fake)

    assert result["requeued"] == 0
    assert result["failed"] == 1
    assert await fake.llen(f"{event_key}:dlq") == 1  # restored, not lost


def test_post_event_dlq_retry_route_forwards_limit_and_names_queue():
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.ops import create_ops_router

    test_app = FastAPI()
    test_app.include_router(create_ops_router())
    retry_result = {"status": "completed", "requeued": 2, "failed": 0, "remaining": 0}

    with patch("app.ops.retry_event_dlq", AsyncMock(return_value=retry_result)) as spy:
        client = TestClient(test_app)
        resp = client.post("/ops/dlq/retry-events?limit=7")

    assert resp.status_code == 200
    assert spy.await_args.kwargs["limit"] == 7
    assert resp.json()["queue"] == "event_dlq"


def test_event_queue_depths_read_from_data_db_not_broker():
    """The event stream + DLQ live on REDIS_URL (DB 0) — producer main.py:1219
    and consumer sleep_cycle.py:339 both use it. Reading their depths from the
    Celery broker DB always returned 0 and hid the event_dlq row."""
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.ops import create_ops_router

    settings = get_settings()
    event_key = settings.REDIS_STREAM_KEY

    class _BrokerRedis:
        async def llen(self, key):
            # Only "celery" legitimately lives here. Event keys queried on the
            # broker DB return 0 — exactly the bug this test pins.
            return {"celery": 3}.get(key, 0)

        async def xlen(self, key):
            return 0

        async def aclose(self):
            pass

    class _DataRedis:
        async def llen(self, key):
            return {event_key: 5, f"{event_key}:dlq": 7, "memory:backfill:dlq": 0,
                    "nb:distill:dlq": 0}.get(key, 0)

        async def xlen(self, key):
            return 0

        async def aclose(self):
            pass

    def _from_url(url, **kwargs):
        return _BrokerRedis() if url == settings.CELERY_BROKER_URL else _DataRedis()

    test_app = FastAPI()
    test_app.include_router(create_ops_router())

    with patch("app.ops.aioredis.from_url", side_effect=_from_url):
        client = TestClient(test_app)
        resp = client.get("/ops/queues")

    queues = resp.json()["queues"]
    assert queues["celery"] == 3
    assert queues["event_stream"] == 5   # was 0 — read from the wrong DB
    assert queues["event_dlq"] == 7      # was 0 — read from the wrong DB


def test_get_workers_endpoint_returns_normalized_shape():
    """GET /ops/workers must preserve the existing dashboard-facing response
    shape ({"workers": [...], "count": N}) once the blocking inspect calls
    are offloaded to a threadpool.
    """
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.ops import create_ops_router

    test_app = FastAPI()
    test_app.include_router(create_ops_router())

    with patch("app.ops.celery_app") as mock_celery_app:
        mock_celery_app.control.inspect.return_value = _FakeInspect()
        client = TestClient(test_app)
        resp = client.get("/ops/workers")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["workers"][0]["name"] == "worker@node"
    assert body["workers"][0]["active_tasks"] == 1
    assert body["workers"][0]["reserved"] == 2
    assert body["workers"][0]["scheduled"] == 1
    assert body["workers"][0]["registered_tasks"] == 3


def test_get_workers_offloads_blocking_inspect_to_threadpool():
    """The Celery inspect broadcasts (~2s each, ~10s total across 5 calls) are
    blocking and must NOT run inline on the event loop — otherwise they stall
    every other concurrent /ops and /admin request behind them. Assert the
    handler routes the blocking work through starlette's run_in_threadpool
    rather than calling _inspect_workers directly.
    """
    from unittest.mock import AsyncMock, patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.ops import create_ops_router

    test_app = FastAPI()
    test_app.include_router(create_ops_router())

    async def _fake_run_in_threadpool(func, *args, **kwargs):
        return func(*args, **kwargs)

    spy = AsyncMock(side_effect=_fake_run_in_threadpool)

    with patch("app.ops.celery_app") as mock_celery_app, \
            patch("app.ops.run_in_threadpool", spy):
        mock_celery_app.control.inspect.return_value = _FakeInspect()
        client = TestClient(test_app)
        resp = client.get("/ops/workers")

    assert resp.status_code == 200
    spy.assert_awaited_once()
    # The blocking inspect work must be handed to run_in_threadpool as a callable,
    # not executed directly on the event loop.
    offloaded_callable = spy.await_args.args[0]
    assert callable(offloaded_callable)

    body = resp.json()
    assert body["count"] == 1
    assert body["workers"][0]["name"] == "worker@node"


@pytest.mark.asyncio
async def test_retry_event_dlq_logs_full_record_when_both_writes_fail(caplog):
    """Guarded-restore invariant: when the requeue lpush AND the restore lpush
    BOTH fail, the popped record must be logged IN FULL at CRITICAL — the only
    forensic trail after genuine data loss. A mutant dropping that log (or
    logging the exception instead of the record) must fail this test."""
    import logging

    from fakeredis import aioredis as fakeaioredis

    from app.config import get_settings
    from app.ops import retry_event_dlq

    event_key = get_settings().REDIS_STREAM_KEY
    fake = fakeaioredis.FakeRedis(decode_responses=True)
    await fake.lpush(f"{event_key}:dlq", "lost-forever-item")

    async def always_fail_lpush(key, *values):
        raise RuntimeError("redis down")

    fake.lpush = always_fail_lpush
    with caplog.at_level(logging.CRITICAL, logger="app.ops"):
        result = await retry_event_dlq(redis_client=fake)

    assert result["requeued"] == 0
    assert any("lost-forever-item" in r.getMessage() for r in caplog.records
               if r.levelno == logging.CRITICAL)


def test_distill_dlq_read_from_bridge_db_3():
    """collect_queue_depths must read nb:distill:dlq from Redis DB 3 (Bridge),
    not the cortex data DB — a dropped '/3' rewrite would silently return 0 and
    hide the distill DLQ row, the same bug class 563e72b fixed for events."""
    from unittest.mock import patch

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.ops import create_ops_router

    settings = get_settings()
    event_key = settings.REDIS_STREAM_KEY
    bridge_url = settings.REDIS_URL.rsplit("/", 1)[0] + "/3"

    class _Redis:
        def __init__(self, is_bridge):
            self.is_bridge = is_bridge

        async def llen(self, key):
            if key == "nb:distill:dlq":
                # ONLY the /3 (bridge) connection should ever be asked for this.
                return 11 if self.is_bridge else -999
            return {"celery": 0, event_key: 0, f"{event_key}:dlq": 0,
                    "memory:backfill:dlq": 0}.get(key, 0)

        async def xlen(self, key):
            return 0

        async def aclose(self):
            pass

    def _from_url(url, **kwargs):
        return _Redis(is_bridge=(url == bridge_url))

    test_app = FastAPI()
    test_app.include_router(create_ops_router())
    with patch("app.ops.aioredis.from_url", side_effect=_from_url):
        resp = TestClient(test_app).get("/ops/queues")

    assert resp.json()["queues"]["distill_dlq"] == 11  # read from /3, not -999
