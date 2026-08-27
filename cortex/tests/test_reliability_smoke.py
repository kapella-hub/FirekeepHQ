"""SP0 end-to-end memory-reliability smoke tests (spec §6) — mocked backends.

Scenarios:
1. learn -> recall roundtrip returns the stored memory with correct attribution
2. learn during simulated Ollama outage -> honest partial (never fake success)
3. backfill enqueue -> drain -> memory gets its vector (recallable)
4. superseded memory ranks below its replacement
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.engine.rag import RAGEngine
from app.exceptions import VectorStoreError
from app.main import app, get_graph, get_rag_engine, get_redis, get_vector
from app.models import ContextQuery


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class StatefulFakeVector:
    """In-memory VectorClient stand-in: upsert stores, search returns."""

    def __init__(self):
        self.points: dict[str, dict] = {}

    async def initialize(self):
        pass

    async def close(self):
        pass

    async def _embed(self, text):
        return [0.1] * 768

    async def upsert(self, text, metadata, namespace="default", point_id=None):
        # identity-v2 D2: the real VectorClient.upsert accepts a caller-scoped
        # point_id (the route mints it once via memory_point_id and passes it
        # here); this fake must honour it rather than always re-deriving its
        # own, or every write silently ignores the id the caller chose.
        if point_id is None:
            point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, text))
        self.points[point_id] = {
            "text": text,
            "source": metadata.get("source", "unknown"),
            "tags": metadata.get("tags", []),
            "domain": metadata.get("domain", "general"),
            "namespace": namespace,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_id": metadata.get("agent_id", "unknown"),
            "session_id": metadata.get("session_id", "unknown"),
            "project": metadata.get("project"),
            "status": "active",
            "confirmed_count": 0,
            "contradicted_count": 0,
            "superseded_by": None,
        }
        return point_id

    async def search(self, query, top_k=5, **kwargs):
        # Mirrors app.db.vector.namespace_condition: None = every namespace,
        # any string = exactly that one (a missing field counting as "default").
        # This fake previously reimplemented the old `!= "default"` wildcard,
        # which made it disagree with production the moment that stopped being
        # the contract.
        namespace = kwargs.get("namespace")
        out = []
        for pid, p in self.points.items():
            if namespace is not None and (p.get("namespace") or "default") != namespace:
                continue
            out.append({
                "id": pid,
                "score": 0.9,
                "text": p["text"],
                "metadata": {
                    "source": p["source"],
                    "tags": p["tags"],
                    "domain": p["domain"],
                    "timestamp": p["timestamp"],
                    # C2: lifecycle fields carried into search metadata
                    "status": p["status"],
                    "confirmed_count": p["confirmed_count"],
                    "contradicted_count": p["contradicted_count"],
                    "superseded_by": p["superseded_by"],
                },
            })
        return out[:top_k]


class FakeStreamRedis:
    """Minimal async Redis fake: streams (xadd/xrange/xdel) + lists."""

    def __init__(self):
        self.streams: dict[str, list] = {}
        self.lists: dict[str, list] = {}
        self._seq = 0

    async def xadd(self, key, fields, **kwargs):
        self._seq += 1
        entry_id = f"{self._seq}-0"
        self.streams.setdefault(key, []).append((entry_id, dict(fields)))
        return entry_id

    async def xlen(self, key):
        return len(self.streams.get(key, []))

    async def xrange(self, key, min="-", max="+", count=None):
        entries = list(self.streams.get(key, []))
        return entries[:count] if count else entries

    async def xdel(self, key, *ids):
        before = len(self.streams.get(key, []))
        self.streams[key] = [
            e for e in self.streams.get(key, []) if e[0] not in ids
        ]
        return before - len(self.streams[key])

    async def lpush(self, key, *values):
        self.lists.setdefault(key, [])
        self.lists[key][0:0] = list(values)
        return len(self.lists[key])

    async def rpush(self, key, *values):
        self.lists.setdefault(key, []).extend(values)
        return len(self.lists[key])

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def ltrim(self, key, start, stop):
        self.lists[key] = self.lists.get(key, [])[start:stop + 1 if stop != -1 else None]
        return True

    async def aclose(self):
        pass

    def __getattr__(self, name):
        # Tolerate incidental client calls (e.g. ping, expire) without failing.
        return AsyncMock()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_client(vector, mock_graph, mock_redis):
    """TestClient with dependency overrides and a no-op lifespan."""

    @asynccontextmanager
    async def _noop_lifespan(a: FastAPI):
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = _noop_lifespan

    engine = RAGEngine(graph=mock_graph, vector=vector)
    app.dependency_overrides[get_graph] = lambda: mock_graph
    app.dependency_overrides[get_vector] = lambda: vector
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_rag_engine] = lambda: engine

    client = TestClient(app, raise_server_exceptions=False)
    return client, original_lifespan


def _teardown(original_lifespan):
    app.dependency_overrides.clear()
    app.router.lifespan_context = original_lifespan


# ---------------------------------------------------------------------------
# Scenario 1: learn -> recall roundtrip with attribution
# ---------------------------------------------------------------------------


def test_learn_recall_roundtrip_with_attribution(mock_graph, mock_redis):
    fake_vector = StatefulFakeVector()
    client, original = _make_client(fake_vector, mock_graph, mock_redis)
    try:
        with patch("app.main.detect_and_supersede", new=AsyncMock(return_value=[])):
            with client:
                resp = client.post(
                    "/memory/learn",
                    json={
                        "action": "Configured the Qdrant snapshot cron",
                        "outcome": "Nightly snapshots verified working",
                        "domain": "infrastructure",
                        "tags": ["qdrant", "backup"],
                        "project": "Firekeep",
                    },
                    headers={"X-Agent-Id": "alice", "X-Session-Id": "sess-1"},
                )
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "stored"
                assert body["vector_id"] is not None

                # Attribution: identity headers persisted to the stored payload
                stored = fake_vector.points[body["vector_id"]]
                assert stored["agent_id"] == "alice"
                assert stored["session_id"] == "sess-1"
                assert stored["project"] == "firekeep"  # normalized lowercase

                # Roundtrip: the memory is semantically recallable
                recall = client.post(
                    "/memory/recall",
                    json={"task": "qdrant snapshot cron", "format": "raw"},
                )
                assert recall.status_code == 200
                data = recall.json()
                assert data["sources"], "stored memory must be recallable"
                assert "Qdrant snapshot cron" in data["sources"][0]["content"]
    finally:
        _teardown(original)


# ---------------------------------------------------------------------------
# Scenario 2: simulated Ollama outage -> honest partial
# ---------------------------------------------------------------------------


def test_embed_outage_reports_partial_not_success(mock_graph, mock_redis):
    failing_vector = AsyncMock()
    failing_vector.upsert = AsyncMock(
        side_effect=VectorStoreError("embedding failed after 3 attempts")
    )
    client, original = _make_client(failing_vector, mock_graph, mock_redis)
    try:
        with client:
            resp = client.post(
                "/memory/learn",
                json={
                    "action": "Learned during outage",
                    "outcome": "Ollama was down",
                    "domain": "general",
                    "tags": [],
                },
                headers={"X-Agent-Id": "alice", "X-Session-Id": "sess-1"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "partial"       # never reported as full success
        assert body["vector_id"] is None          # honest: no vector was stored
        assert body["graph_id"] is not None       # graph half succeeded
    finally:
        _teardown(original)


# ---------------------------------------------------------------------------
# Scenario 3: backfill enqueue -> drain -> vector restored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_backfill_writes_to_stream():
    from app.workers import backfill

    fake_redis = FakeStreamRedis()
    # Task 2's injection surface is the redis_client kwarg (no module factories)
    await backfill.enqueue_backfill(
        "mem-1", "orphaned vectorless text",
        {"namespace": "default", "agent_id": "alice"},
        redis_client=fake_redis,
    )

    entries = fake_redis.streams.get("memory:backfill", [])
    assert len(entries) == 1
    assert "mem-1" in str(entries[0][1])


@pytest.mark.asyncio
async def test_drain_backfill_upserts_vector_and_clears_stream():
    from app.workers import backfill

    fake_redis = FakeStreamRedis()
    fake_vector = AsyncMock()
    fake_vector.upsert = AsyncMock(return_value="mem-1")

    # Enqueue through the contract API so the entry shape matches the drain
    await backfill.enqueue_backfill(
        "mem-1", "orphaned vectorless text", {"namespace": "default"},
        redis_client=fake_redis,
    )
    assert fake_redis.streams.get("memory:backfill"), "entry must be enqueued"

    # Drive the async core directly with injected fakes; the Celery wrapper
    # drain_backfill_queue() is a thin sync shim over _drain() (Task 2)
    await backfill._drain(redis_client=fake_redis, vector_client=fake_vector)

    # Drained: stream empty, nothing dead-lettered, text reached the vector store
    assert fake_redis.streams.get("memory:backfill") == []
    assert fake_redis.lists.get("memory:backfill:dlq", []) == []
    assert "orphaned vectorless text" in str(fake_vector.mock_calls)


# ---------------------------------------------------------------------------
# Scenario 4: superseded memory ranks below its replacement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_superseded_memory_ranks_below_replacement(mock_graph):
    now = datetime.now(timezone.utc).isoformat()
    old_id = "11111111-1111-5111-8111-111111111111"
    new_id = "22222222-2222-5222-8222-222222222222"

    vector = AsyncMock()
    vector.search = AsyncMock(return_value=[
        {
            "id": old_id,
            "score": 0.9,
            "text": "Use docker-compose v1 commands",
            "metadata": {
                "source": "action_log", "tags": [], "domain": "ops",
                "timestamp": now,
                "status": "superseded", "confirmed_count": 0,
                "contradicted_count": 1, "superseded_by": new_id,
            },
        },
        {
            "id": new_id,
            "score": 0.9,
            "text": "Use docker compose v2 commands",
            "metadata": {
                "source": "action_log", "tags": [], "domain": "ops",
                "timestamp": now,
                "status": "active", "confirmed_count": 1,
                "contradicted_count": 0, "superseded_by": None,
            },
        },
    ])

    engine = RAGEngine(graph=mock_graph, vector=vector)
    result = await engine.recall(
        ContextQuery(task="docker compose commands", top_k=2, format="raw")
    )

    assert len(result.sources) == 2
    scores = {s.content: s.score for s in result.sources}
    assert scores["Use docker compose v2 commands"] > scores["Use docker-compose v1 commands"]
    assert result.sources[0].content == "Use docker compose v2 commands"
