"""Tests for knowledge lifecycle management."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.lifecycle import create_lifecycle_router


@pytest.fixture()
def mock_graph() -> AsyncMock:
    graph = AsyncMock()
    graph.create_supersession = AsyncMock()
    return graph


@pytest.fixture()
def mock_vector() -> AsyncMock:
    vector = AsyncMock()
    vector.update_status = AsyncMock()
    vector.confirm_memory = AsyncMock(return_value=True)
    vector.restore_memory = AsyncMock(return_value=True)
    vector.get_memory = AsyncMock(return_value=None)
    return vector


@pytest.fixture()
def lifecycle_client(mock_graph: AsyncMock, mock_vector: AsyncMock) -> TestClient:
    """TestClient with just the lifecycle router mounted."""
    test_app = FastAPI()
    router = create_lifecycle_router(graph=mock_graph, vector=mock_vector)
    test_app.include_router(router)
    return TestClient(test_app, raise_server_exceptions=False)


class TestDeprecateEndpoint:
    def test_deprecate_sets_status(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        resp = lifecycle_client.post(
            "/memory/deprecate",
            json={
                "memory_ids": ["mem-1"],
                "status": "deprecated",
                "reason": "Outdated information",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["updated"] == 1
        mock_vector.update_status.assert_called_once_with(
            memory_id="mem-1", status="deprecated", superseded_by=None,
            reason="Outdated information",
        )

    def test_deprecate_superseded_creates_graph_edge(
        self,
        lifecycle_client: TestClient,
        mock_vector: AsyncMock,
        mock_graph: AsyncMock,
    ):
        resp = lifecycle_client.post(
            "/memory/deprecate",
            json={
                "memory_ids": ["old-mem"],
                "status": "superseded",
                "reason": "Replaced by newer finding",
                "superseded_by": "new-mem",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1
        mock_graph.create_supersession.assert_called_once_with(
            newer_id="new-mem",
            older_id="old-mem",
            reason="Replaced by newer finding",
            detected="manual",
        )

    def test_deprecate_partial_failure_returns_count(
        self,
        lifecycle_client: TestClient,
        mock_vector: AsyncMock,
    ):
        # First call succeeds, second raises
        mock_vector.update_status = AsyncMock(
            side_effect=[None, RuntimeError("db error")]
        )
        resp = lifecycle_client.post(
            "/memory/deprecate",
            json={
                "memory_ids": ["ok-mem", "fail-mem"],
                "status": "archived",
                "reason": "Cleanup",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

    def test_deprecate_empty_memory_ids_rejected(self, lifecycle_client: TestClient):
        resp = lifecycle_client.post(
            "/memory/deprecate",
            json={
                "memory_ids": [],
                "status": "deprecated",
                "reason": "test",
            },
        )
        assert resp.status_code == 422

    def test_deprecate_superseded_without_superseded_by_skips_edge(
        self,
        lifecycle_client: TestClient,
        mock_graph: AsyncMock,
    ):
        """Superseded status without superseded_by should not create graph edge."""
        resp = lifecycle_client.post(
            "/memory/deprecate",
            json={
                "memory_ids": ["mem-1"],
                "status": "superseded",
                "reason": "No replacement specified",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1
        mock_graph.create_supersession.assert_not_called()


class TestConfirmEndpoint:
    def test_confirm_bumps_count(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.confirm_memory = AsyncMock(return_value=True)
        resp = lifecycle_client.post(
            "/memory/confirm", json={"memory_ids": ["mem-1", "mem-2"]}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "confirmed"
        assert data["confirmed"] == 2

    def test_confirm_nonexistent_returns_zero(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.confirm_memory = AsyncMock(return_value=False)
        resp = lifecycle_client.post(
            "/memory/confirm", json={"memory_ids": ["nonexistent"]}
        )
        assert resp.status_code == 200
        assert resp.json()["confirmed"] == 0

    def test_confirm_partial_failure(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.confirm_memory = AsyncMock(
            side_effect=[True, RuntimeError("fail"), True]
        )
        resp = lifecycle_client.post(
            "/memory/confirm", json={"memory_ids": ["a", "b", "c"]}
        )
        assert resp.status_code == 200
        assert resp.json()["confirmed"] == 2

    def test_confirm_empty_memory_ids_rejected(self, lifecycle_client: TestClient):
        resp = lifecycle_client.post("/memory/confirm", json={"memory_ids": []})
        assert resp.status_code == 422


class TestRestoreEndpoint:
    def test_restore_archived_memories(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        resp = lifecycle_client.post(
            "/memory/restore", json={"memory_ids": ["mem-1", "mem-2"]}
        )

        assert resp.status_code == 200
        assert resp.json() == {"status": "restored", "restored": 2}
        assert mock_vector.restore_memory.await_count == 2

    def test_restore_counts_only_changed_records(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.restore_memory = AsyncMock(
            side_effect=[True, False, RuntimeError("store unavailable")]
        )

        resp = lifecycle_client.post(
            "/memory/restore", json={"memory_ids": ["a", "b", "c"]}
        )

        assert resp.status_code == 200
        assert resp.json()["restored"] == 1

    def test_restore_empty_memory_ids_rejected(self, lifecycle_client: TestClient):
        resp = lifecycle_client.post("/memory/restore", json={"memory_ids": []})
        assert resp.status_code == 422

    def test_successful_restore_appends_lifecycle_audit(
        self, mock_graph: AsyncMock, mock_vector: AsyncMock, mock_redis: AsyncMock
    ):
        app = FastAPI()
        app.include_router(
            create_lifecycle_router(mock_graph, mock_vector, redis_client=mock_redis)
        )

        resp = TestClient(app).post(
            "/memory/restore", json={"memory_ids": ["archived-1"]}
        )

        assert resp.status_code == 200
        key, raw = mock_redis._pipeline.lpush.call_args.args
        assert key == "gc:eviction:log"
        assert json.loads(raw)["action"] == "restored"
        mock_redis._pipeline.ltrim.assert_called_once_with(
            "gc:eviction:log", 0, 999
        )
        mock_redis._pipeline.execute.assert_awaited_once()


class TestMemoryHistory:
    def test_returns_correct_status_and_metadata(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.get_memory = AsyncMock(
            return_value={
                "status": "active",
                "superseded_by": None,
                "confirmed_count": 3,
                "contradicted_count": 1,
                "last_confirmed_at": "2026-01-15T10:00:00Z",
            }
        )
        resp = lifecycle_client.get("/memory/test-id-123/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["memory_id"] == "test-id-123"
        assert data["status"] == "active"
        assert data["superseded_by"] is None
        assert data["confirmed_count"] == 3
        assert data["contradicted_count"] == 1
        assert data["last_confirmed_at"] == "2026-01-15T10:00:00Z"


# ---------------------------------------------------------------------------
# MIGRATION_FREEZE gate (identity-v2 D6) — lifecycle mutators must 503 while
# frozen; the read routes (history/backlinks) stay unaffected.
# ---------------------------------------------------------------------------


class TestLifecycleMigrationFreezeGate:
    @pytest.fixture()
    def frozen_client(self, mock_graph: AsyncMock, mock_vector: AsyncMock) -> TestClient:
        from app.config import Settings, get_settings

        test_app = FastAPI()
        test_app.include_router(create_lifecycle_router(graph=mock_graph, vector=mock_vector))
        test_app.dependency_overrides[get_settings] = lambda: Settings(MIGRATION_FREEZE=True)
        return TestClient(test_app, raise_server_exceptions=False)

    def test_deprecate_503_when_frozen(self, frozen_client: TestClient):
        resp = frozen_client.post(
            "/memory/deprecate",
            json={"memory_ids": ["mem-1"], "status": "deprecated", "reason": "r"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "memory store migration in progress; retry shortly"

    def test_confirm_503_when_frozen(self, frozen_client: TestClient):
        resp = frozen_client.post("/memory/confirm", json={"memory_ids": ["mem-1"]})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "memory store migration in progress; retry shortly"

    def test_restore_503_when_frozen(self, frozen_client: TestClient):
        resp = frozen_client.post("/memory/restore", json={"memory_ids": ["mem-1"]})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "memory store migration in progress; retry shortly"

    def test_history_read_stays_200_when_frozen(
        self, frozen_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.get_memory = AsyncMock(
            return_value={"status": "active", "confirmed_count": 0, "contradicted_count": 0}
        )
        resp = frozen_client.get("/memory/some-id/history")
        assert resp.status_code == 200

    def test_404_for_nonexistent_memory(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.get_memory = AsyncMock(return_value=None)
        resp = lifecycle_client.get("/memory/does-not-exist/history")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_superseded_by_populated(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        mock_vector.get_memory = AsyncMock(
            return_value={
                "status": "superseded",
                "superseded_by": "newer-mem-id",
                "confirmed_count": 0,
                "contradicted_count": 0,
                "last_confirmed_at": None,
            }
        )
        resp = lifecycle_client.get("/memory/old-mem/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "superseded"
        assert data["superseded_by"] == {"id": "newer-mem-id"}

    def test_history_defaults_for_missing_fields(
        self, lifecycle_client: TestClient, mock_vector: AsyncMock
    ):
        """Memory dict with minimal fields should still return valid response."""
        mock_vector.get_memory = AsyncMock(return_value={"status": "active"})
        resp = lifecycle_client.get("/memory/minimal/history")
        assert resp.status_code == 200
        data = resp.json()
        assert data["confirmed_count"] == 0
        assert data["contradicted_count"] == 0
        assert data["last_confirmed_at"] is None
        assert data["superseded_by"] is None
