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


def _principal(scopes, member="m-bob", ws="ws1"):
    return {"workspace_id": ws, "member_id": member, "scopes": list(scopes)}


# --- The knowledge router is a SECOND corpus front door (review, claims 1 & 3):
# it must enforce the same reserved-prefix and visibility rules as /corpus/*. ---

def test_generic_key_cannot_claim_docdex_source_via_knowledge(client):
    """A plain memory:write key must not claim a reserved docdex: name here —
    the corpus router blocks it, and this door was the bypass."""
    with patch("auth.principal.request_principal",
               return_value=_principal(["memory:write"])):
        resp = client.post("/knowledge/ingest",
                           json={"content": "x", "source_name": "docdex:s1:abc",
                                 "source_type": "wiki"})
    assert resp.status_code == 403


def test_dex_scoped_key_may_ingest_docdex_via_knowledge(client):
    with (
        patch("auth.principal.request_principal",
              return_value=_principal(["memory:write", "dex:docdex"])),
        patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as core,
        patch("corpus.api.get_corpus_sources", new=AsyncMock(return_value=[])),
    ):
        resp = client.post("/knowledge/ingest",
                           json={"content": "x", "source_name": "docdex:s1:abc",
                                 "source_type": "wiki"})
    assert resp.status_code == 202
    core.assert_awaited_once()


def test_cannot_overwrite_another_members_private_source_via_knowledge(client):
    """Bob (dex-scoped, so the prefix check passes) still cannot overwrite —
    and thereby generation-sweep — Alice's private source through this door."""
    alice_private = {"name": "docdex:s1:abc", "workspace_id": "ws1",
                     "member_id": "m-alice", "visibility": "member"}
    with (
        patch("auth.principal.request_principal",
              return_value=_principal(["memory:write", "dex:docdex"], member="m-bob")),
        patch("corpus.api.get_corpus_sources",
              new=AsyncMock(return_value=[alice_private])),
        patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as core,
    ):
        resp = client.post("/knowledge/ingest",
                           json={"content": "x", "source_name": "docdex:s1:abc",
                                 "source_type": "wiki"})
    assert resp.status_code == 403
    core.assert_not_awaited()


def test_sources_hides_another_members_private_source(client, mock_vector):
    """A private source's NAME is private data — /knowledge/sources filters it
    exactly like /corpus/sources. The review found it returned every record."""
    sources = [
        {"name": "docdex:s1:abc", "source_type": "wiki", "chunks": 3,
         "workspace_id": "ws1", "member_id": "m-alice", "visibility": "member"},
        {"name": "Team Runbook", "source_type": "wiki", "chunks": 5,
         "workspace_id": "ws1", "visibility": "workspace"},
    ]

    async def _scroll(collection_name, scroll_filter, limit, with_payload, with_vectors):
        return ([], None)
    mock_vector._client.scroll = AsyncMock(side_effect=_scroll)

    with (
        patch("auth.principal.request_principal",
              return_value=_principal(["memory:read"], member="m-bob")),
        patch("corpus.api.get_corpus_sources", new=AsyncMock(return_value=sources)),
        patch("app.knowledge.api.get_ingest_status", new=AsyncMock(return_value=None)),
    ):
        resp = client.get("/knowledge/sources")

    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()["sources"]}
    assert "docdex:s1:abc" not in names, "Alice's private source name leaked to Bob"
    assert "Team Runbook" in names


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


# ---------------------------------------------------------------------------
# MIGRATION_FREEZE gate (identity-v2 D6) — POST /knowledge/ingest writes into
# the corpus + queues skill drafting, so it must refuse during the freeze
# window. GET /knowledge/sources is a read and stays unaffected.
# ---------------------------------------------------------------------------


def test_ingest_503_when_frozen(mock_vector, mock_redis):
    from app.config import Settings, get_settings

    app = _make_app(mock_vector, mock_redis)
    app.dependency_overrides[get_settings] = lambda: Settings(MIGRATION_FREEZE=True)
    frozen_client = TestClient(app)

    with patch("app.knowledge.api.ingest_knowledge_document", new=AsyncMock()) as mock_core:
        resp = frozen_client.post(
            "/knowledge/ingest",
            json={"content": "x", "source_name": "Runbook", "source_type": "wiki"},
        )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "memory store migration in progress; retry shortly"
    mock_core.assert_not_awaited()


def test_sources_stays_200_when_frozen(mock_vector, mock_redis):
    from app.config import Settings, get_settings

    app = _make_app(mock_vector, mock_redis)
    app.dependency_overrides[get_settings] = lambda: Settings(MIGRATION_FREEZE=True)
    frozen_client = TestClient(app)

    with (
        patch("corpus.api.get_corpus_sources", new=AsyncMock(return_value=[])),
        patch("app.knowledge.api.get_ingest_status", new=AsyncMock(return_value=None)),
    ):
        resp = frozen_client.get("/knowledge/sources")

    assert resp.status_code == 200
