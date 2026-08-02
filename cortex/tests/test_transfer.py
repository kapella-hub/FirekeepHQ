"""Tests for the /memory/export and /memory/import endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.graph import Neo4jClient
from app.db.vector import VectorClient
from app.transfer import create_transfer_router


@pytest.fixture()
def transfer_app():
    """Create a minimal FastAPI app with the transfer router for testing."""
    mock_graph = AsyncMock(spec=Neo4jClient)
    mock_vector = AsyncMock(spec=VectorClient)

    # Default: export_graph returns some data
    mock_graph.export_graph = AsyncMock(return_value={
        "nodes": [
            {"id": "n1", "label": "Domain", "properties": {"name": "auth"}},
            {"id": "n2", "label": "Action", "properties": {"id": "a1", "description": "fixed login"}},
        ],
        "edges": [
            {"source": "n2", "target": "n1", "type": "RELATES_TO"},
        ],
    })
    mock_graph.merge_knowledge_nodes = AsyncMock(return_value=3)

    # scroll_all as an async generator
    async def _mock_scroll_all(namespace=None, batch_size=100):
        points = [
            {
                "id": "p1",
                "text": "Fixed auth timeout",
                "metadata": {"resolution": "increased pool"},
                "namespace": "default",
                "tags": ["auth"],
                "source": "action_log",
                "created_at": "2025-06-01T00:00:00+00:00",
            },
            {
                "id": "p2",
                "text": "Database migration completed",
                "metadata": {},
                "namespace": "default",
                "tags": ["database"],
                "source": "action_log",
                "created_at": "2025-07-01T00:00:00+00:00",
            },
        ]
        if namespace:
            points = [p for p in points if p["namespace"] == namespace]
        for p in points:
            yield p

    mock_vector.scroll_all = _mock_scroll_all
    mock_vector.upsert = AsyncMock(return_value="vec-uuid-imported")

    app = FastAPI()
    # Both routes are Depends(require_scope("admin")), which bites even on the
    # AUTH_ENABLED=false path. These tests assert the routes' OWN logic (streaming
    # shape, parse errors, round-trip), so they build the router with the gate
    # stubbed out — patching `app.transfer.require_scope`, not the source module,
    # because transfer.py holds its own reference from an import-time `from ...`,
    # and patching while the router is CONSTRUCTED because the dependency is
    # captured at decoration time. Bypassing a check in a unit test is only safe
    # when another test proves the check is there: see
    # test_transfer_routes_refuse_anonymous_when_auth_disabled below.
    _admin = {"agent_id": "test-admin", "scopes": ["*"], "authenticated": True}
    with patch("app.transfer.require_scope", lambda scope: (lambda: _admin)):
        router = create_transfer_router(graph=mock_graph, vector=mock_vector)
    app.include_router(router)

    return app, mock_graph, mock_vector


class TestExportEndpoint:
    def test_export_returns_jsonl_stream(self, transfer_app):
        """Export returns JSONL with memory, node, and edge entries."""
        app, mock_graph, mock_vector = transfer_app

        with TestClient(app) as client:
            resp = client.get("/memory/export")

        assert resp.status_code == 200
        assert "application/x-ndjson" in resp.headers["content-type"]

        lines = [json.loads(line) for line in resp.text.strip().split("\n") if line.strip()]

        # 2 memories + 2 nodes + 1 edge = 5 lines
        assert len(lines) == 5

        memory_lines = [ln for ln in lines if ln["type"] == "memory"]
        node_lines = [ln for ln in lines if ln["type"] == "node"]
        edge_lines = [ln for ln in lines if ln["type"] == "edge"]

        assert len(memory_lines) == 2
        assert len(node_lines) == 2
        assert len(edge_lines) == 1

        assert memory_lines[0]["text"] == "Fixed auth timeout"
        assert node_lines[0]["label"] == "Domain"
        assert edge_lines[0]["rel_type"] == "RELATES_TO"

    def test_export_with_namespace_filter(self, transfer_app):
        """Export with namespace filter only returns matching memories."""
        app, mock_graph, mock_vector = transfer_app

        with TestClient(app) as client:
            resp = client.get("/memory/export?namespace=default")

        assert resp.status_code == 200
        lines = [json.loads(line) for line in resp.text.strip().split("\n") if line.strip()]
        memory_lines = [ln for ln in lines if ln["type"] == "memory"]
        assert len(memory_lines) == 2


class TestImportEndpoint:
    def test_import_with_valid_data(self, transfer_app):
        """Import valid JSONL data processes all items."""
        app, mock_graph, mock_vector = transfer_app

        jsonl_body = "\n".join([
            json.dumps({"type": "memory", "text": "Fixed auth", "namespace": "default", "tags": ["auth"]}),
            json.dumps({"type": "node", "id": "n1", "label": "Domain", "properties": {"name": "auth"}}),
            json.dumps({"type": "edge", "source": "n1", "target": "n2", "rel_type": "RELATES_TO"}),
        ])

        with TestClient(app) as client:
            resp = client.post(
                "/memory/import",
                content=jsonl_body,
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["imported_memories"] == 1
        assert body["imported_nodes"] == 1
        assert body["imported_edges"] == 1
        assert body["errors"] == []

    def test_import_with_json_array(self, transfer_app):
        """Import a JSON array body."""
        app, mock_graph, mock_vector = transfer_app

        data = [
            {"type": "memory", "text": "Test memory 1"},
            {"type": "memory", "text": "Test memory 2"},
        ]

        with TestClient(app) as client:
            resp = client.post("/memory/import", json=data)

        assert resp.status_code == 200
        body = resp.json()
        assert body["imported_memories"] == 2
        assert body["errors"] == []

    def test_import_with_mixed_valid_invalid(self, transfer_app):
        """Import with some invalid items reports errors but imports valid ones."""
        app, mock_graph, mock_vector = transfer_app

        jsonl_body = "\n".join([
            json.dumps({"type": "memory", "text": "Valid memory"}),
            json.dumps({"type": "memory", "text": ""}),  # empty text
            json.dumps({"type": "node", "id": "n1", "label": "Domain", "properties": {}}),
            json.dumps({"type": "edge", "source": "", "target": "n2", "rel_type": "X"}),  # missing source
            json.dumps({"type": "unknown_type"}),
        ])

        with TestClient(app) as client:
            resp = client.post(
                "/memory/import",
                content=jsonl_body,
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["imported_memories"] == 1
        assert body["imported_nodes"] == 1
        assert len(body["errors"]) == 3  # empty text, missing source, unknown type

    def test_import_empty_body(self, transfer_app):
        """Import with empty body returns 422."""
        app, mock_graph, mock_vector = transfer_app

        with TestClient(app) as client:
            resp = client.post(
                "/memory/import",
                content=b"",
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert resp.status_code == 422

    def test_export_import_round_trip(self, transfer_app):
        """Export then import produces consistent data."""
        app, mock_graph, mock_vector = transfer_app

        with TestClient(app) as client:
            # Export
            export_resp = client.get("/memory/export")
            assert export_resp.status_code == 200
            exported_lines = export_resp.text.strip()

            # Import the exported data back
            import_resp = client.post(
                "/memory/import",
                content=exported_lines.encode("utf-8"),
                headers={"Content-Type": "application/x-ndjson"},
            )

        assert import_resp.status_code == 200
        body = import_resp.json()
        assert body["status"] == "completed"
        # Should have imported 2 memories, 2 nodes, 1 edge
        assert body["imported_memories"] == 2
        assert body["imported_nodes"] == 2
        assert body["imported_edges"] == 1
        assert body["errors"] == []


def test_transfer_routes_refuse_anonymous_when_auth_disabled():
    """The transfer router shipped with NO scope gate at all — `create_transfer_router`
    declared no dependency and `main.py` registered it without `dependencies=`. That is
    the same exposure as audit blocker 7, on a wider surface: GET /memory/export streams
    every memory in the deployment, and POST /memory/import bulk-writes memories AND
    arbitrary Neo4j labels and relationship types (`item.get("label", "Entity")` and
    `rel_type` are passed through verbatim).

    Both are operator backup/migration functions with no production caller — only tests
    and docs reference them — so admin is the right gate and nothing in the running
    system depends on them being open.
    """
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from auth import keys as _keys

    assert _keys._AUTH_ENABLED is False, "this test is about the auth-DISABLED path"
    assert "admin" not in _keys.ANONYMOUS_SCOPES

    test_app = FastAPI()
    test_app.include_router(create_transfer_router(  # the REAL gate, not the stub
        AsyncMock(spec=Neo4jClient), AsyncMock(spec=VectorClient)))
    client = TestClient(test_app)

    for method, path in (("get", "/memory/export"), ("post", "/memory/import")):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403, f"{method.upper()} {path} -> {resp.status_code}"
        assert "admin" in resp.json()["detail"]
