"""REST endpoint tests for corpus module."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from corpus.api import create_corpus_router


@pytest.fixture()
def app():
    """FastAPI app with corpus router mounted."""
    app = FastAPI()
    router = create_corpus_router()
    app.include_router(router)
    return app


@pytest.fixture()
def client(app):
    return TestClient(app)


class TestIngestEndpoint:
    def test_rejects_empty_content(self, client):
        resp = client.post(
            "/corpus/ingest",
            json={"content": "", "source_name": "test"},
        )
        assert resp.status_code == 422 or resp.status_code == 400

    def test_accepts_valid_request(self, client):
        with patch("corpus.api.ingest_document", new_callable=AsyncMock) as mock_ingest:
            mock_ingest.return_value = {
                "source_name": "Test Doc",
                "chunks_stored": 3,
                "entities_extracted": 5,
                "relationships_extracted": 2,
                "entity_types_discovered": ["System", "Process"],
            }
            resp = client.post(
                "/corpus/ingest",
                json={
                    "content": "The billing system integrates with CRM.",
                    "source_name": "Test Doc",
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["chunks_stored"] == 3
            assert data["entities_extracted"] == 5
            assert "namespace" not in data


class TestSourcesEndpoint:
    def test_lists_sources(self, client):
        with patch("corpus.api.get_corpus_sources", new_callable=AsyncMock) as mock_sources:
            mock_sources.return_value = [
                {
                    "name": "Billing Wiki",
                    "source_type": "wiki",
                    "chunks": 5,
                    "entities": 12,
                    "last_ingested": "2026-03-21T10:00:00Z",
                }
            ]
            resp = client.get("/corpus/sources")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["sources"]) == 1
            assert data["sources"][0]["name"] == "Billing Wiki"


class TestDeleteEndpoint:
    def test_returns_503_when_not_initialized(self, client):
        resp = client.delete("/corpus/sources/TestDoc")
        assert resp.status_code == 503

    def test_deletes_source(self, client):
        with patch("corpus.api.delete_corpus_source", new_callable=AsyncMock) as mock_delete:
            mock_delete.return_value = {
                "source_name": "TestDoc",
                "chunks_deleted": "all",
                "entities_deleted": "all",
            }
            resp = client.delete("/corpus/sources/TestDoc")
            assert resp.status_code == 200
            data = resp.json()
            assert data["source_name"] == "TestDoc"
            assert data["chunks_deleted"] == "all"
            mock_delete.assert_called_once_with(source_name="TestDoc")


class TestEntitiesEndpointGone:
    def test_entities_endpoint_removed(self, client):
        """The /corpus/entities endpoint was removed — ontology graph was write-only."""
        resp = client.get("/corpus/entities")
        assert resp.status_code == 404
