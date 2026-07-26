from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.collectors.api import create_collectors_router


def _app(mock_redis):
    app = FastAPI()
    app.include_router(create_collectors_router())
    from app.main import get_redis
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


def test_collectors_lists_known_with_run_record():
    mock_redis = AsyncMock()
    with patch("app.collectors.api.CollectorState.get_run",
               new=AsyncMock(return_value={"last_run": "t", "pages_seen": 5, "pages_ingested": 2,
                                           "pages_skipped": 3, "errors": 0, "health": "ok"})), \
         patch("app.collectors.api.get_settings") as gs:
        gs.return_value.COLLECTORS_ENABLED = True
        gs.return_value.CONFLUENCE_COLLECTOR_ENABLED = True
        client = TestClient(_app(mock_redis))
        resp = client.get("/collectors")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    c = data["collectors"][0]
    assert c["name"] == "confluence" and c["enabled"] is True and c["health"] == "ok"


def test_collectors_empty_when_never_run():
    mock_redis = AsyncMock()
    with patch("app.collectors.api.CollectorState.get_run", new=AsyncMock(return_value=None)), \
         patch("app.collectors.api.get_settings") as gs:
        gs.return_value.COLLECTORS_ENABLED = True
        gs.return_value.CONFLUENCE_COLLECTOR_ENABLED = False
        client = TestClient(_app(mock_redis))
        resp = client.get("/collectors")
    c = resp.json()["collectors"][0]
    assert c["enabled"] is False and c["last_run"] is None
