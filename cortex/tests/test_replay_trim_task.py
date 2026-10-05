"""The replay retention trim is scheduled (THREAT-MODEL §5.18, 2026-10-05).

replay.emitter.trim_old_events was defined but nothing ran it, so rp:events
was bounded only by RP_STREAM_MAXLEN while its rp:eid lookup keys expired at
RP_RETENTION_DAYS. Beat/include pin mirrors test_beat_schedule_ladder.py.
"""

from __future__ import annotations

import time
from datetime import timedelta

import fakeredis.aioredis
import pytest

from replay.config import ReplaySettings, get_replay_settings
from replay.emitter import _STREAM_KEY


def test_replay_trim_is_registered_on_beat_daily():
    from app.workers.sleep_cycle import celery_app

    name = "app.workers.replay_trim.trim_replay_events"
    assert "app.workers.replay_trim" in celery_app.conf.include
    entry = celery_app.conf.beat_schedule["replay-trim"]
    assert entry["task"] == name
    interval = get_replay_settings().TRIM_INTERVAL_SECONDS
    assert entry["schedule"] == timedelta(seconds=interval)
    assert ReplaySettings.model_fields["TRIM_INTERVAL_SECONDS"].default == 86400

    import app.workers.replay_trim as mod
    assert mod.trim_replay_events.name == name


@pytest.mark.asyncio
async def test_the_task_trims_with_the_retention_setting():
    from app.workers.replay_trim import trim_replay_retention

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    now_ms = int(time.time() * 1000)
    old_ms = now_ms - 31 * 86400 * 1000
    await r.xadd(_STREAM_KEY, {"id": "old", "session_id": "s"}, id=f"{old_ms}-0")
    await r.xadd(_STREAM_KEY, {"id": "new", "session_id": "s"}, id=f"{now_ms}-0")

    assert await trim_replay_retention(r, ReplaySettings(RETENTION_DAYS=30)) == 1
    assert [f["id"] for _, f in await r.xrange(_STREAM_KEY)] == ["new"]
    await r.aclose()


@pytest.mark.asyncio
async def test_the_task_opens_and_closes_its_own_client(monkeypatch):
    import redis.asyncio as aioredis

    from app.workers.replay_trim import trim_replay_retention

    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    closed = []
    real_close = r.aclose

    async def _close():
        closed.append(True)
        await real_close()

    r.aclose = _close
    urls = []

    def _from_url(url, **_kw):
        urls.append(url)
        return r

    monkeypatch.setattr(aioredis, "from_url", _from_url)
    settings = ReplaySettings(REDIS_URL="redis://redis:6379/6")
    assert await trim_replay_retention(settings=settings) == 0
    assert urls == ["redis://redis:6379/6"]
    assert closed == [True]
