"""trim_old_events enforces RP_RETENTION_DAYS on the stream (§5.18, 2026-10-05).

It was defined but never scheduled, so ``rp:events`` was bounded only by
STREAM_MAXLEN while the ``rp:eid:{id}`` index keys expired at the retention
horizon — old events lingered, reachable by the unscoped in-process readers and
by none of the scoped REST reads. Cortex's beat now runs it daily
(``app.workers.replay_trim``), which needs it to:

* take a client and settings explicitly — a worker never calls init_emitter,
  and a module-global client outliving its asyncio.run loop is a hazard;
* drain the whole backlog in one run, not one 1000-entry batch per run;
* be safe to run twice at once: count what IT deleted, and never delete a
  session index another writer is still appending to (Relay's ``relay``
  session index is written on every coordination event).
"""

from __future__ import annotations

import asyncio
import time

import fakeredis.aioredis
import pytest
import pytest_asyncio

from replay.config import ReplaySettings
from replay.emitter import (
    _EVENT_IDX_PREFIX,
    _SESSION_IDX_PREFIX,
    _STREAM_KEY,
    trim_old_events,
)

RETENTION_DAYS = 30
SETTINGS = ReplaySettings(ENABLED=True, RETENTION_DAYS=RETENTION_DAYS)


@pytest_asyncio.fixture
async def r():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def _seed(r, n: int, *, age_days: float, session_id: str, start_seq: int = 0) -> list[str]:
    """n events ``age_days`` old, written the way emit() writes them."""
    base_ms = int((time.time() - age_days * 86400) * 1000)
    eids = []
    for i in range(n):
        eid = f"{session_id}-{age_days}-{start_seq + i}"
        stream_id = f"{base_ms}-{start_seq + i}"
        await r.xadd(_STREAM_KEY, {"id": eid, "session_id": session_id, "event_type": "x"}, id=stream_id)
        await r.zadd(f"{_SESSION_IDX_PREFIX}{session_id}", {eid: base_ms / 1000})
        await r.set(f"{_EVENT_IDX_PREFIX}{eid}", stream_id)
        eids.append(eid)
    return eids


@pytest.mark.asyncio
async def test_trims_the_whole_backlog_in_one_run(r):
    old = await _seed(r, 2500, age_days=RETENTION_DAYS + 5, session_id="old-sess")
    recent = await _seed(r, 3, age_days=1, session_id="new-sess")

    trimmed = await trim_old_events(r, SETTINGS)

    assert trimmed == len(old)
    assert await r.xlen(_STREAM_KEY) == len(recent)
    assert not await r.exists(f"{_SESSION_IDX_PREFIX}old-sess")
    assert await r.zcard(f"{_SESSION_IDX_PREFIX}new-sess") == len(recent)
    assert not await r.exists(f"{_EVENT_IDX_PREFIX}{old[0]}")
    assert await r.exists(f"{_EVENT_IDX_PREFIX}{recent[0]}")


@pytest.mark.asyncio
async def test_a_hot_session_index_keeps_its_recent_entries(r):
    old = await _seed(r, 5, age_days=RETENTION_DAYS + 1, session_id="relay")
    recent = await _seed(r, 2, age_days=0.01, session_id="relay", start_seq=100)

    assert await trim_old_events(r, SETTINGS) == len(old)

    members = set(await r.zrange(f"{_SESSION_IDX_PREFIX}relay", 0, -1))
    assert members == set(recent)


@pytest.mark.asyncio
async def test_two_concurrent_runs_trim_each_event_once(r):
    old = await _seed(r, 1500, age_days=RETENTION_DAYS + 2, session_id="s")

    counts = await asyncio.gather(trim_old_events(r, SETTINGS), trim_old_events(r, SETTINGS))

    assert sum(counts) == len(old)
    assert await r.xlen(_STREAM_KEY) == 0


@pytest.mark.asyncio
async def test_nothing_old_is_a_no_op_and_rerunning_is_idempotent(r):
    await _seed(r, 4, age_days=2, session_id="s")
    assert await trim_old_events(r, SETTINGS) == 0
    assert await trim_old_events(r, SETTINGS) == 0
    assert await r.xlen(_STREAM_KEY) == 4


@pytest.mark.asyncio
async def test_retention_comes_from_the_setting(r):
    await _seed(r, 3, age_days=10, session_id="s")
    assert await trim_old_events(r, ReplaySettings(RETENTION_DAYS=30)) == 0
    assert await trim_old_events(r, ReplaySettings(RETENTION_DAYS=7)) == 3


def test_retention_default_is_unchanged():
    assert ReplaySettings.model_fields["RETENTION_DAYS"].default == 30


@pytest.mark.asyncio
async def test_without_a_client_or_initialised_emitter_it_does_nothing():
    assert await trim_old_events() == 0
