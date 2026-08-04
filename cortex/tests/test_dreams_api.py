from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dreams.api import create_dreams_router


def _app(mock_redis):
    app = FastAPI()
    app.include_router(create_dreams_router())
    from app.main import get_redis
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


def test_dreams_status_shaped_when_enabled():
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value={
        b"last_run": b"2026-08-04T12:00:00+00:00",
        b"clusters_done": b"3",
        b"profiles_done": b"1",
        b"errors": b"0",
        b"health": b"ok",
    })
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
        "errors": 0,
        "health": "ok",
    }


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
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value={})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    data = resp.json()
    assert data["health"] == "unknown"
    assert data["last_run"] is None
    assert data["clusters_done"] == 0
    assert data["profiles_done"] == 0
    assert data["errors"] == 0


def test_dreams_counters_coerce_safely_from_garbage():
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value={
        b"clusters_done": b"not-a-number",
        b"profiles_done": b"",
        b"errors": None,
        b"health": b"degraded",
    })
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
    mock_redis = AsyncMock()
    mock_redis.hgetall = AsyncMock(return_value={b"health": b"unavailable"})
    with patch("app.dreams.api.get_settings") as gs:
        gs.return_value.DREAM_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/dreams")
    assert resp.json()["health"] == "unavailable"
