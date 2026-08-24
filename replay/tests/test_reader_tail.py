"""get_session_event_ids: a snapshot-stable tail-ID list (outcome truth D7).

The grade lift snapshots the newest event IDs once, then hydrates locally —
immune to concurrent appends (fixed ID list) and to missing bodies (walks
IDs, not hydrated events)."""
import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter
from replay.reader import get_event_batch


@pytest_asyncio.fixture
async def redis_client():
    r = aioredis.from_url("redis://localhost:6379/6", decode_responses=True)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not available on localhost:6379")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def setup_emitter(redis_client):
    settings = ReplaySettings(
        ENABLED=True,
        REDIS_URL="redis://localhost:6379/6",
        STREAM_MAXLEN=10000,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield redis_client
    await close_emitter()


@pytest.mark.asyncio
async def test_ids_are_newest_last_and_bounded(setup_emitter):
    r = setup_emitter
    from replay.emitter import emit
    for i in range(5):
        await emit("ctx_update", "sess-n", "agent", {"i": str(i)})
    from replay.reader import get_session_event_ids
    ids = await get_session_event_ids(r, "sess-n", limit=3)
    assert len(ids) == 3                              # newest 3 only
    events = await get_event_batch(r, ids)
    # emit() serializes payload values through json.dumps — "i" round-trips
    # as a string, not the original int index.
    assert [e["payload"]["i"] for e in events] == ["2", "3", "4"]
    assert await get_session_event_ids(r, "nope") == []
    assert await get_session_event_ids(r, "sess-n", limit=0) == []


@pytest.mark.asyncio
async def test_snapshot_is_stable_under_appends(setup_emitter):
    """Round-6 finding 2: a live rank-relative window would shift under
    appends and skip the grade; the ID snapshot does not."""
    r = setup_emitter
    from replay.emitter import emit
    from replay.reader import get_session_event_ids
    for i in range(10):
        await emit("ctx_update", "s", "agent", {"i": str(i)})
    snap = await get_session_event_ids(r, "s", limit=10)
    for i in range(200):                              # heavy concurrent appends
        await emit("memory_read", "s", "agent", {"j": str(i)})
    # the snapshot still names the original 10 events, unshifted
    assert await get_session_event_ids(r, "s", limit=10) != snap  # live read moved
    assert len(snap) == 10                            # our captured list did not


@pytest.mark.asyncio
async def test_get_event_batch_preserves_order_with_duplicates_and_missing(setup_emitter):
    """get_event_batch must preserve request order, including repeated IDs,
    and silently skip IDs that don't resolve (missing from the index)."""
    r = setup_emitter
    from replay.emitter import emit
    from replay.reader import get_session_event_ids
    for i in range(2):
        await emit("ctx_update", "sess-dup", "agent", {"i": str(i)})
    ids = await get_session_event_ids(r, "sess-dup", limit=2)
    id1, id2 = ids[0], ids[1]

    events = await get_event_batch(r, [id2, "missing", id1, id2])
    assert [e["id"] for e in events] == [id2, id1, id2]
