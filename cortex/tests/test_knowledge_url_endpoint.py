"""Tests for POST /knowledge/ingest-url (URL crawl + ingest queueing).

Hermetic: patches is_safe_url and the Celery task's .delay so no real network,
crawl, or Celery broker is touched.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.knowledge.api import create_knowledge_router


def _make_app(mock_vector, mock_redis):
    app = FastAPI()
    router = create_knowledge_router()
    app.include_router(router)
    from app.main import get_redis, get_vector
    app.dependency_overrides[get_vector] = lambda: mock_vector
    app.dependency_overrides[get_redis] = lambda: mock_redis
    return app


@pytest.fixture
def mock_vector():
    return MagicMock()


@pytest.fixture
def mock_redis():
    return MagicMock()


@pytest.fixture
def client(mock_vector, mock_redis):
    return TestClient(_make_app(mock_vector, mock_redis))


def test_ingest_url_unsafe_returns_400(client):
    with (
        patch("app.knowledge.api.is_safe_url", return_value=(False, "blocked")) as mock_safe,
        patch("app.knowledge.api.run_url_ingest") as mock_task,
    ):
        resp = client.post("/knowledge/ingest-url", json={"url": "http://169.254.169.254/"})

    assert resp.status_code == 400
    # F6: the caller gets a GENERIC message (no leaked reason / resolved private IP);
    # the specific "blocked" reason is logged server-side only.
    assert resp.json()["detail"] == "URL rejected: not permitted"
    assert "blocked" not in resp.json()["detail"]
    mock_safe.assert_called_once()
    mock_task.delay.assert_not_called()


def test_ingest_url_safe_returns_202_and_enqueues(client):
    with (
        patch("app.knowledge.api.is_safe_url", return_value=(True, "")),
        patch("app.knowledge.api.run_url_ingest") as mock_task,
    ):
        resp = client.post(
            "/knowledge/ingest-url",
            json={"url": "https://example.com/docs", "depth": 1, "max_pages": 10},
        )

    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "queued"
    assert data["url"] == "https://example.com/docs"
    assert "note" in data
    mock_task.delay.assert_called_once_with("https://example.com/docs", 1, 10)


def test_ingest_url_defaults_depth_and_max_pages(client):
    with (
        patch("app.knowledge.api.is_safe_url", return_value=(True, "")),
        patch("app.knowledge.api.run_url_ingest") as mock_task,
    ):
        resp = client.post("/knowledge/ingest-url", json={"url": "https://example.com/docs"})

    assert resp.status_code == 202
    mock_task.delay.assert_called_once_with("https://example.com/docs", 0, 25)


def test_ingest_url_clamps_depth_and_max_pages_over_limit(client):
    with (
        patch("app.knowledge.api.is_safe_url", return_value=(True, "")),
        patch("app.knowledge.api.run_url_ingest") as mock_task,
    ):
        resp = client.post(
            "/knowledge/ingest-url",
            json={"url": "https://example.com/docs", "depth": 999, "max_pages": 999999},
        )

    assert resp.status_code == 202
    # clamped to settings.KNOWLEDGE_CRAWL_MAX_DEPTH / KNOWLEDGE_CRAWL_MAX_PAGES
    called_url, called_depth, called_max_pages = mock_task.delay.call_args.args
    assert called_url == "https://example.com/docs"
    assert called_depth <= 2
    assert called_max_pages <= 25


def test_ingest_url_clamps_negative_depth_to_zero(client):
    with (
        patch("app.knowledge.api.is_safe_url", return_value=(True, "")),
        patch("app.knowledge.api.run_url_ingest") as mock_task,
    ):
        resp = client.post(
            "/knowledge/ingest-url",
            json={"url": "https://example.com/docs", "depth": -5, "max_pages": 0},
        )

    assert resp.status_code == 202
    called_depth, called_max_pages = mock_task.delay.call_args.args[1:3]
    assert called_depth == 0
    assert called_max_pages >= 1


def test_ingest_url_missing_url_returns_422(client):
    with patch("app.knowledge.api.run_url_ingest") as mock_task:
        resp = client.post("/knowledge/ingest-url", json={})

    assert resp.status_code == 422
    mock_task.delay.assert_not_called()
