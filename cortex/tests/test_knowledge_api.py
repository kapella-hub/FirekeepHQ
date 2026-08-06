"""Tests for POST /knowledge/ingest and GET /knowledge/sources orchestration."""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
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
    v = MagicMock()
    v._client = AsyncMock()
    return v


@pytest.fixture
def mock_redis():
    return AsyncMock()


@pytest.fixture
def client(mock_vector, mock_redis):
    return TestClient(_make_app(mock_vector, mock_redis))


def test_ingest_delegates_and_returns_202(client):
    with patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as mock_core:
        resp = client.post("/knowledge/ingest",
                           json={"content": "x", "source_name": "Runbook", "source_type": "wiki"})
    assert resp.status_code == 202
    data = resp.json()
    assert data["corpus_source"] == "Runbook" and data["status"] == "queued"
    mock_core.assert_awaited_once()
    assert mock_core.await_args.args[:3] == ("x", "Runbook", "wiki")


def test_ingest_core_failure_returns_500(client):
    with patch("app.knowledge.api.ingest_knowledge_document",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        resp = client.post("/knowledge/ingest",
                           json={"content": "x", "source_name": "Runbook", "source_type": "wiki"})
    assert resp.status_code == 500


def test_empty_content_returns_400(client):
    with patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as mock_core:
        resp = client.post(
            "/knowledge/ingest",
            json={"content": "", "source_name": "Doc", "source_type": "text"},
        )

    assert resp.status_code == 400
    mock_core.assert_not_called()


def test_whitespace_only_content_returns_400(client):
    with patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as mock_core:
        resp = client.post(
            "/knowledge/ingest",
            json={"content": "   \n\t  ", "source_name": "Doc", "source_type": "text"},
        )

    assert resp.status_code == 400
    mock_core.assert_not_called()


def test_get_sources_includes_ingest_status(client, mock_vector):
    corpus_sources = [
        {"name": "Runbook", "source_type": "wiki", "chunks": 5, "last_ingested": "2026-07-01T00:00:00+00:00"},
        {"name": "Overview", "source_type": "text", "chunks": 2, "last_ingested": "2026-07-02T00:00:00+00:00"},
    ]

    async def _scroll(collection_name, scroll_filter, limit, with_payload, with_vectors):
        return ([], None)  # draft counts not under test here
    mock_vector._client.scroll = AsyncMock(side_effect=_scroll)

    # RELATIVE, not a fixed date. This test is about STATUS PLUMBING, and a
    # hardcoded stamp silently became a time bomb: once it aged past the
    # drafts-missing grace window the endpoint correctly stopped saying
    # "classified" and the test failed for a reason it was never about.
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    async def _status(name, redis_client):
        if name == "Runbook":
            return {"status": "classified", "disposition": "procedural",
                    "skills_queued": 2, "note": "", "updated_at": recent}
        return None

    with (
        patch("app.knowledge.api.corpus_api") as mock_corpus_api,
        patch("app.knowledge.api.get_ingest_status", new=AsyncMock(side_effect=_status)),
    ):
        mock_corpus_api.get_corpus_sources = AsyncMock(return_value=corpus_sources)
        resp = client.get("/knowledge/sources")

    assert resp.status_code == 200
    by_name = {s["name"]: s for s in resp.json()["sources"]}
    assert by_name["Runbook"]["status"] == "classified"
    assert by_name["Runbook"]["disposition"] == "procedural"
    assert by_name["Runbook"]["updated_at"] == recent
    assert by_name["Overview"]["status"] == "unknown"
    assert by_name["Overview"]["updated_at"] == ""


def test_get_sources_reports_a_long_stale_queued_source_as_drafts_missing(client, mock_vector):
    """End-to-end wiring for the derived verdict, including the grace window.

    The pure function is covered in test_docs_to_skills_works.py; this asserts
    the ENDPOINT actually threads `_draft_grace_seconds(settings)` through, which
    a unit test of `_effective_status` cannot see.
    """
    corpus_sources = [
        {"name": "Runbook", "source_type": "wiki", "chunks": 5,
         "last_ingested": "2026-07-01T00:00:00+00:00"},
    ]

    async def _scroll(collection_name, scroll_filter, limit, with_payload, with_vectors):
        return ([], None)  # no draft points landed — that is the whole point
    mock_vector._client.scroll = AsyncMock(side_effect=_scroll)

    stale = (datetime.now(timezone.utc) - timedelta(days=25)).isoformat()

    async def _status(name, redis_client):
        # No skills_failed key at all — a record written before per-draft
        # outcomes were counted. This is the live 2026-07-12 shape.
        return {"status": "classified", "disposition": "procedural",
                "skills_queued": 1, "note": "", "updated_at": stale}

    with (
        patch("app.knowledge.api.corpus_api") as mock_corpus_api,
        patch("app.knowledge.api.get_ingest_status", new=AsyncMock(side_effect=_status)),
    ):
        mock_corpus_api.get_corpus_sources = AsyncMock(return_value=corpus_sources)
        resp = client.get("/knowledge/sources")

    assert resp.status_code == 200
    row = resp.json()["sources"][0]
    assert row["status"] == "drafts_missing"
    # The stored classify status is never destroyed — classification DID succeed.
    assert row["classify_status"] == "classified"


def test_get_sources_503_when_corpus_not_initialized(client):
    with patch("app.knowledge.api.corpus_api") as mock_corpus_api:
        mock_corpus_api.get_corpus_sources = None
        resp = client.get("/knowledge/sources")

    assert resp.status_code == 503
