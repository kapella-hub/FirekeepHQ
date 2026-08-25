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


class _CallCountingRedis:
    """Proxy that records every Redis command invoked through it.

    Used to assert get_session_event_ids performs exactly ONE round trip (a
    single zrange). That single-call shape is the actual source of
    snapshot-stability: with only one Redis interaction, there is no gap
    between two calls for a concurrent append to land in. A reimplementation
    as a multi-call/paged read (e.g. a ZCARD probe, or ZRANGE issued across
    pages) would reopen that TOCTOU window — this proxy catches it directly
    by recording more than one call, rather than relying on timing."""

    def __init__(self, real):
        self._real = real
        self.calls: list[str] = []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if not callable(attr):
            return attr

        async def _spy(*args, **kwargs):
            self.calls.append(name)
            return await attr(*args, **kwargs)

        return _spy


@pytest.mark.asyncio
async def test_ids_are_newest_last_and_bounded(setup_emitter):
    r = setup_emitter
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
async def test_snapshot_is_a_single_atomic_redis_call(setup_emitter):
    """D5/D7 ship gate: get_session_event_ids must be exactly ONE Redis
    round trip (a single zrange), not a multi-call/paged read.

    That single-call shape is what makes the snapshot stable against
    concurrent appends (round-6 finding 2): with only one Redis interaction
    there is no interleaving point for a writer to land in between. A prior
    version of this test only asserted outcomes (`len(snap) == 10`, a live
    re-read differs from the snapshot) — both true the instant the snapshot
    list was assigned, and both still true under a TOCTOU-broken
    multi-call reimplementation, so neither caught a regression. Asserting
    the call count does: it fails the moment someone reintroduces a second
    round trip (a ZCARD probe, or ZRANGE issued in pages)."""
    r = setup_emitter
    from replay.reader import get_session_event_ids
    for i in range(10):
        await emit("ctx_update", "s", "agent", {"i": str(i)})

    spy = _CallCountingRedis(r)
    ids = await get_session_event_ids(spy, "s", limit=10)

    assert len(ids) == 10
    assert spy.calls == ["zrange"], (
        f"expected exactly one zrange call and nothing else, got {spy.calls}"
    )


@pytest.mark.asyncio
async def test_get_event_batch_preserves_order_with_duplicates_and_missing(setup_emitter):
    """get_event_batch must preserve request order, including repeated IDs,
    and silently skip IDs that don't resolve (missing from the index)."""
    r = setup_emitter
    from replay.reader import get_session_event_ids
    for i in range(2):
        await emit("ctx_update", "sess-dup", "agent", {"i": str(i)})
    ids = await get_session_event_ids(r, "sess-dup", limit=2)
    id1, id2 = ids[0], ids[1]

    events = await get_event_batch(r, [id2, "missing", id1, id2])
    assert [e["id"] for e in events] == [id2, id1, id2]
