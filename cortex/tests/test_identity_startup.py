"""Startup hardening + the migration freeze gate (identity-v2 D6).

Covers the pieces that don't already have a natural home in an existing test
file:
  - `VectorClient.initialize()` double-start idempotency (the single no-op
    case already lives in tests/test_vector.py::TestInitialize alongside the
    creation/error cases it was updated for in the same change).
  - `migrate_single_workspace`'s own MEMORY_MIGRATION_KEY gate, and
    `backfill_memories`'s quarantine/legacy_unscoped skip.
  - The `require_not_frozen` dependency in isolation.

Route-level MIGRATION_FREEZE 503 tests live beside each gated route's
existing tests (tests/test_api.py, tests/test_lifecycle.py,
tests/test_transfer.py, tests/test_knowledge_api.py,
corpus/tests/test_api.py) per the repo's per-router test convention.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.db.vector import VectorClient
from app.migration_gate import require_not_frozen
from app.workspace_migration import (
    QUARANTINE_WORKSPACE,
    backfill_memories,
    migrate_single_workspace,
)
from auth.workspace import MEMORY_MIGRATION_KEY


# ---------------------------------------------------------------------------
# initialize() — double-start
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        QDRANT_HOST="localhost",
        QDRANT_PORT=6333,
        QDRANT_COLLECTION="test_collection",
        EMBEDDING_DIM=768,
        LLM_BASE_URL="http://localhost:11434/v1",
        LLM_API_KEY="test-api-key",
        EMBEDDING_MODEL="test-embed",
    )


@pytest.fixture()
def mock_qdrant_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def vector_client(settings: Settings, mock_qdrant_client: AsyncMock) -> VectorClient:
    client = VectorClient(settings)
    client._client = mock_qdrant_client
    client._http_client = AsyncMock()
    return client


class TestInitializeDoubleStart:
    @pytest.mark.asyncio
    async def test_double_start_is_idempotent_no_op(
        self, vector_client: VectorClient, mock_qdrant_client: AsyncMock
    ):
        """Two initialize() calls against a name that already resolves
        (directly or via alias) never attempt create_collection."""
        mock_qdrant_client.get_collection.return_value = MagicMock()

        await vector_client.initialize()
        await vector_client.initialize()

        assert mock_qdrant_client.get_collection.await_count == 2
        mock_qdrant_client.create_collection.assert_not_called()


# ---------------------------------------------------------------------------
# migrate_single_workspace — marker gate + quarantine/legacy_unscoped skip
# ---------------------------------------------------------------------------


class _Qdrant:
    def __init__(self, points):
        self.points = points
        self.indexes = []
        self.scroll_calls = 0

    async def scroll(self, **_kwargs):
        self.scroll_calls += 1
        return self.points, None

    async def set_payload(self, *, payload, points, **_kwargs):
        wanted = set(points)
        for point in self.points:
            if point.id in wanted:
                point.payload.update(payload)

    async def create_payload_index(self, **kwargs):
        self.indexes.append(kwargs["field_name"])


class _Vector:
    def __init__(self, points):
        self._client = _Qdrant(points)
        self._collection = "test"


@pytest.mark.asyncio
async def test_boots_twice_quarantine_point_workspace_unchanged(monkeypatch):
    """A sentinel/legacy_unscoped point must never be adopted into the
    deployment workspace, on the first run OR any later one."""
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-q")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-q")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    quarantine_point = SimpleNamespace(
        id="quarantined-1",
        payload={
            "text": "orphaned memory",
            "workspace_id": QUARANTINE_WORKSPACE,
            "legacy_unscoped": True,
        },
    )
    vector = _Vector([quarantine_point])
    try:
        first = await migrate_single_workspace(redis, vector)
        second = await migrate_single_workspace(redis, vector)

        assert first == second
        assert quarantine_point.payload["workspace_id"] == QUARANTINE_WORKSPACE
        assert quarantine_point.payload["legacy_unscoped"] is True
        assert "member_id" not in quarantine_point.payload
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_marker_present_skips_backfill_entirely(monkeypatch):
    """`migrate_single_workspace` reads its own completion marker FIRST and
    returns early -- the scan/backfill must never run again once it is set,
    even against a store that would otherwise raise (a quarantine point that
    reused a real workspace_id would trip backfill_memories's cross-workspace
    guard if it were ever re-scanned)."""
    monkeypatch.setenv("FIREKEEP_WORKSPACE_ID", "workspace-m")
    monkeypatch.setenv("FIREKEEP_OWNER_MEMBER_ID", "member-owner-m")
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.set(MEMORY_MIGRATION_KEY, "complete:workspace-m:0")

    class _ExplodingQdrant(_Qdrant):
        async def scroll(self, **_kwargs):
            raise AssertionError("backfill must not scan Qdrant when the marker is set")

    vector = _Vector([])
    vector._client = _ExplodingQdrant([])
    try:
        workspace = await migrate_single_workspace(redis, vector)
        assert workspace.workspace_id == "workspace-m"
        assert workspace.owner_member_id == "member-owner-m"
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_backfill_memories_skips_quarantine_and_legacy_unscoped_points():
    """Direct unit test of the skip predicate: neither point is touched by
    set_payload, neither is required to carry workspace_id/member_id in the
    post-backfill verification, and neither trips the cross-workspace guard
    despite carrying a workspace_id that differs from the target."""
    quarantine_point = SimpleNamespace(
        id="q1", payload={"text": "a", "workspace_id": QUARANTINE_WORKSPACE},
    )
    legacy_point = SimpleNamespace(
        id="q2",
        payload={"text": "b", "workspace_id": "some-other-workspace", "legacy_unscoped": True},
    )
    ordinary_point = SimpleNamespace(id="p1", payload={"text": "c"})
    points = [quarantine_point, legacy_point, ordinary_point]
    vector = _Vector(points)

    total = await backfill_memories(vector, "workspace-a", "member-owner-a")

    assert total == 3
    assert quarantine_point.payload["workspace_id"] == QUARANTINE_WORKSPACE
    assert "member_id" not in quarantine_point.payload
    assert legacy_point.payload["workspace_id"] == "some-other-workspace"
    assert "member_id" not in legacy_point.payload
    assert ordinary_point.payload["workspace_id"] == "workspace-a"
    assert ordinary_point.payload["member_id"] == "member-owner-a"


# ---------------------------------------------------------------------------
# require_not_frozen — the dependency in isolation
# ---------------------------------------------------------------------------


def _freeze_app(frozen: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/gated", dependencies=[Depends(require_not_frozen)])
    async def _gated():
        return {"ok": True}

    app.dependency_overrides[get_settings] = lambda: Settings(MIGRATION_FREEZE=frozen)
    return app


class TestRequireNotFrozen:
    def test_raises_503_when_frozen(self):
        client = TestClient(_freeze_app(True), raise_server_exceptions=False)
        resp = client.get("/gated")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "memory store migration in progress; retry shortly"

    def test_passes_through_when_not_frozen(self):
        client = TestClient(_freeze_app(False), raise_server_exceptions=False)
        resp = client.get("/gated")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
