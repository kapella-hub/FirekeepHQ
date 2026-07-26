"""Neo4jClient.connect() must survive a dependency that is still coming up.

Why this exists, from a live production failure on 2026-07-26: during a rolling
`docker compose up -d`, cortex-api started while Neo4j was still initialising.
`connect()` raised on the first DNS failure, the FastAPI lifespan aborted, and
the container exited. It did not retry, back off, or degrade — one transient
blip took the API down permanently until a human noticed.

The fix is bounded retry with backoff. Bounded matters in both directions: it
must ride out a slow dependency, and it must still fail loudly against a
genuinely misconfigured one rather than hanging a deploy forever.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.db.graph import Neo4jClient
from app.exceptions import GraphConnectionError


def _settings(**over):
    base = dict(
        NEO4J_URI="bolt://neo4j:7687",
        NEO4J_USER="neo4j",
        NEO4J_PASSWORD="pw",
        NEO4J_POOL_SIZE=10,
        NEO4J_CONNECT_ATTEMPTS=5,
        NEO4J_CONNECT_BACKOFF_SECONDS=0.01,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Counter:
    """Failures belong to the DEPENDENCY, not to a driver instance.

    connect() builds a fresh driver per attempt (deliberately — a driver whose
    connectivity check failed must not be reused). So "Neo4j is down for the
    first two attempts" has to be counted globally; a per-driver counter would
    reset every attempt and never let the dependency come up.
    """

    def __init__(self, fail_times: int, exc: Exception | None = None):
        self.fail_times = fail_times
        self.calls = 0
        self.exc = exc or ValueError("Cannot resolve address neo4j:7687")


class _Driver:
    def __init__(self, counter: _Counter):
        self._counter = counter
        self.calls = 0
        self.closed = 0

    async def verify_connectivity(self):
        self.calls += 1
        self._counter.calls += 1
        if self._counter.calls <= self._counter.fail_times:
            raise self._counter.exc

    async def close(self):
        self.closed += 1


@pytest.fixture
def patched(monkeypatch):
    """Return a factory that installs a fake AsyncGraphDatabase.driver."""
    made: list[_Driver] = []

    def install(fail_times: int, exc: Exception | None = None):
        counter = _Counter(fail_times, exc)

        def factory(*_a, **_kw):
            d = _Driver(counter)
            made.append(d)
            return d

        monkeypatch.setattr("app.db.graph.AsyncGraphDatabase.driver", factory)
        return made

    return install


@pytest.mark.asyncio
async def test_connects_first_try_when_neo4j_is_up(patched):
    made = patched(fail_times=0)
    await Neo4jClient(_settings()).connect()
    assert made[0].calls == 1, "must not retry when the first attempt succeeds"


@pytest.mark.asyncio
async def test_survives_a_dependency_that_is_still_starting(patched):
    """The production case: two transient DNS failures, then success."""
    made = patched(fail_times=2)
    await Neo4jClient(_settings()).connect()
    assert len(made) == 3, "should have retried past the transient failures"


@pytest.mark.asyncio
async def test_still_fails_loudly_when_neo4j_never_comes_up(patched):
    """Bounded. A misconfigured host must not hang a deploy forever."""
    patched(fail_times=99)
    with pytest.raises(GraphConnectionError) as ei:
        await Neo4jClient(_settings(NEO4J_CONNECT_ATTEMPTS=3)).connect()
    assert "bolt://neo4j:7687" in str(ei.value), "error must name the target"


@pytest.mark.asyncio
async def test_honours_the_configured_attempt_count(patched):
    made = patched(fail_times=99)
    with pytest.raises(GraphConnectionError):
        await Neo4jClient(_settings(NEO4J_CONNECT_ATTEMPTS=4)).connect()
    assert made[-1].calls == 1, "each attempt builds a fresh driver"
    assert len(made) == 4, f"expected exactly 4 attempts, got {len(made)}"


@pytest.mark.asyncio
async def test_a_single_attempt_is_the_old_behaviour(patched):
    """Setting attempts=1 must reproduce the pre-retry semantics exactly, so the
    change is opt-out and a deployment can pin the old behaviour."""
    made = patched(fail_times=99)
    with pytest.raises(GraphConnectionError):
        await Neo4jClient(_settings(NEO4J_CONNECT_ATTEMPTS=1)).connect()
    assert len(made) == 1


@pytest.mark.asyncio
async def test_discards_the_failed_driver_between_attempts(patched):
    """A driver whose connectivity check failed must not be left on the client —
    leaking it would hand later calls a half-open handle."""
    made = patched(fail_times=2)
    client = Neo4jClient(_settings())
    await client.connect()
    assert sum(d.closed for d in made[:-1]) == 2, "failed drivers must be closed"


@pytest.mark.asyncio
async def test_backoff_actually_waits(patched, monkeypatch):
    """Retrying with no delay just burns the attempt budget in microseconds
    against a dependency that needs seconds."""
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    patched(fail_times=2)
    await Neo4jClient(_settings(NEO4J_CONNECT_BACKOFF_SECONDS=0.5)).connect()
    assert len(slept) == 2, "one sleep per failed attempt"
    assert slept[1] > slept[0], "backoff must grow, not stay flat"


@pytest.mark.asyncio
async def test_does_not_sleep_after_the_final_failure(patched, monkeypatch):
    """Sleeping after the last attempt delays the inevitable error for nothing."""
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    patched(fail_times=99)
    with pytest.raises(GraphConnectionError):
        await Neo4jClient(_settings(NEO4J_CONNECT_ATTEMPTS=3)).connect()
    assert len(slept) == 2, "3 attempts means 2 waits, not 3"
