from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dreams.api import create_dreams_router
from app.dreams.state import DreamState


def _app(mock_redis):
    app = FastAPI()
    app.include_router(create_dreams_router())
    from app.main import get_redis
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


def _redis(run=None, *, counters=None):
    """A redis double backed by the two key shapes this endpoint actually
    reads: the `dreams:run` HASH and the `dreams:counter:*` STRING keys.

    Every test in this file used to seed `errors` into the RUN HASH — a shape
    task.py never produces, which is exactly why none of them caught that
    `errors` was structurally always 0 (final-review I4). The error path calls
    `bump_counter("errors")` (-> `dreams:counter:errors`) and then
    `record_run(status="error", ...)` with no `errors` field, so the hash
    NEVER carries that key. Routing counter reads through the real key names
    is what makes these tests able to fail.
    """
    counters = counters or {}
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value=run or {})

    async def _get(key):
        key = key.decode() if isinstance(key, bytes) else key
        return counters.get(key)

    mock_redis.get = AsyncMock(side_effect=_get)
    return mock_redis


def test_dreams_status_shaped_when_enabled():
    mock_redis = _redis({
        b"last_run": b"2026-08-04T12:00:00+00:00",
        b"clusters_done": b"3",
        b"profiles_done": b"1",
        b"insights_written": b"6",
        b"health": b"ok",
    }, counters={"dreams:counter:errors": b"0"})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    assert resp.status_code == 200
    data = resp.json()
    assert data == {
        "enabled": True,
        "last_run": "2026-08-04T12:00:00+00:00",
        "clusters_done": 3,
        "profiles_done": 1,
        "insights_written": 6,
        "errors": 0,
        "health": "ok",
    }


def test_dreams_distinguishes_a_productive_run_from_one_that_wrote_nothing():
    """Before this field, these two bodies were IDENTICAL — a run that wrote 6
    dreams and a run that marked every unit done and wrote 0 both reported
    {"clusters_done":3,"profiles_done":2,"errors":0,"health":"ok"}. That is how
    a live 2-of-3-clusters-produced-nothing run passed for healthy.
    """
    shared = {b"clusters_done": b"3", b"profiles_done": b"2"}
    productive = _redis({**shared, b"insights_written": b"6", b"health": b"ok"})
    barren = _redis({**shared, b"insights_written": b"0", b"health": b"degraded"})

    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        good = TestClient(_app(productive)).get("/dreams").json()
        bad = TestClient(_app(barren)).get("/dreams").json()

    assert good["insights_written"] == 6 and good["health"] == "ok"
    assert bad["insights_written"] == 0 and bad["health"] == "degraded"
    assert good != bad, "the two runs must not be indistinguishable"


def test_dreams_insights_written_reads_the_hash_not_the_per_run_counter():
    """Deliberately unlike `errors` (below), and the difference is load-bearing:
    `dreams:counter:insights_written` is per-run and reset_progress clears it at
    completion, so reading the counter would report 0 for the run that just
    finished — precisely when an operator wants the number. task.py mirrors the
    cumulative counter into the run hash on every working tick, so the hash
    carries the current run's running total and then its final one.

    The counter here holds a deliberately contradictory 0: reading it would
    satisfy a naive assertion, so this fails loudly if the read ever moves.
    """
    mock_redis = _redis(
        {b"insights_written": b"4", b"health": b"ok"},
        counters={"dreams:counter:insights_written": b"0"},
    )
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        resp = TestClient(_app(mock_redis)).get("/dreams")
    assert resp.json()["insights_written"] == 4


def test_dreams_errors_reports_the_counter_the_task_actually_bumps():
    """The bug this endpoint shipped with: `errors` was read off the
    `dreams:run` hash, where nothing ever writes it, so a deployment could
    have failed every tick for a week and still report 0 (final-review I4).

    The run hash here carries a DIFFERENT, deliberately misleading errors
    value — reading the hash would return 0 and pass a naive assertion, so
    this fails loudly if the read is ever moved back.
    """
    mock_redis = _redis(
        {b"health": b"degraded", b"errors": b"0"},
        counters={"dreams:counter:errors": b"7"},
    )
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    assert resp.json()["errors"] == 7


def test_dreams_not_mounted_when_disabled():
    """Mirrors the collectors precedent (main.py only include_routers when
    the flag is set): a disabled deploy 404s outright rather than returning
    a disabled-shaped body. Nothing here is DREAM_ENABLED-specific — a plain
    FastAPI() with the router never included is exactly what main.py builds
    when the flag is off."""
    app = FastAPI()
    client = TestClient(app)
    resp = client.get("/dreams")
    assert resp.status_code == 404


def test_dreams_health_unknown_before_any_run():
    mock_redis = _redis({})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    data = resp.json()
    assert data["health"] == "unknown"
    assert data["last_run"] is None
    assert data["clusters_done"] == 0
    assert data["profiles_done"] == 0
    assert data["insights_written"] == 0
    assert data["errors"] == 0


def test_dreams_counters_coerce_safely_from_garbage():
    mock_redis = _redis({
        b"clusters_done": b"not-a-number",
        b"profiles_done": b"",
        b"health": b"degraded",
    }, counters={"dreams:counter:errors": b"not-a-number-either"})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    data = resp.json()
    assert data["clusters_done"] == 0
    assert data["profiles_done"] == 0
    assert data["errors"] == 0
    assert data["health"] == "degraded"


def test_dreams_health_unavailable_is_passed_through():
    """DREAM's generation-backend gate (task.py's _generation_backend_available)
    writes health="unavailable" — distinct from collectors' ok/degraded/error
    set, and must not be coerced or dropped."""
    mock_redis = _redis({b"health": b"unavailable"})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    assert resp.json()["health"] == "unavailable"


def test_dreamstate_is_sync_only_and_raises_on_an_async_client():
    """Pins the rationale in api.py's module docstring for why this endpoint
    does NOT go through DreamState: handing DreamState (synchronous, built
    for the Celery task's own sync redis.Redis client) an async-style client
    does not silently return bad data -- it raises. `hgetall` on an async
    client returns an unawaited coroutine, which is always truthy, so
    DreamState.get_run()'s `raw = ... or {}` guard never fires and the very
    next `.items()` call blows up with AttributeError."""
    async def _fake_hgetall(_key):
        return {}

    mock_redis = MagicMock()
    mock_redis.hgetall = _fake_hgetall
    state = DreamState(mock_redis)
    with pytest.raises(AttributeError):
        state.get_run()
