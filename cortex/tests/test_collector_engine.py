"""Tests for CollectorEngine (SP3 Task 6)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from app.collectors.engine import CollectorEngine


class FakeAdapter:
    name = "fake"
    def __init__(self, items):
        self._items = items
        self.closed = False
        self.last_total_seen = len(self._items)
    async def discover_changed(self, seen):
        out = []
        for it in self._items:
            if it["version"] > await seen(it["stable_id"]):
                out.append(it)
        return out
    async def fetch_content(self, item):
        return (f"md-{item['stable_id']}", f"Src:{item['label']}", "wiki")
    async def aclose(self):
        self.closed = True


def _settings():
    s = MagicMock()
    s.REDIS_URL = "redis://localhost:6379/0"
    s.QDRANT_COLLECTION = "firekeep_memory"
    s.COLLECTOR_LOCK_TTL_SECONDS = 3600
    return s


@pytest.mark.asyncio
async def test_disabled_builds_no_clients():
    with patch("app.collectors.engine.redis.asyncio.from_url") as mock_fromurl, \
         patch("app.collectors.engine.VectorClient") as mock_vec:
        out = await CollectorEngine().run(
            lambda pat: FakeAdapter([]), name="fake", enabled=False,
            pat_vault_key="k", settings=_settings())
    assert out["status"] == "disabled"
    mock_fromurl.assert_not_called()
    mock_vec.assert_not_called()


@pytest.mark.asyncio
async def test_only_changed_pages_ingested_and_versions_recorded():
    import fakeredis.aioredis
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    # pre-seed p1 at v3 (unchanged), p2 absent (new)
    await fake.hset("collector:versions:fake", "p1", "3")
    items = [{"stable_id": "p1", "version": 3, "label": "P1", "meta": {}},
             {"stable_id": "p2", "version": 5, "label": "P2", "meta": {}}]
    adapter = FakeAdapter(items)
    ingested = []
    with patch("app.collectors.engine.redis.asyncio.from_url", return_value=fake), \
         patch("app.collectors.engine.VectorClient", return_value=MagicMock(close=AsyncMock())), \
         patch("app.collectors.engine._bootstrap_vault_and_pat", new=AsyncMock(return_value="pat")), \
         patch("app.collectors.engine.ingest_knowledge_document",
               new=AsyncMock(side_effect=lambda *a, **k: ingested.append(a[1]))), \
         patch("app.collectors.engine.emit", new=AsyncMock()):
        out = await CollectorEngine().run(
            lambda pat: adapter, name="fake", enabled=True, pat_vault_key="k", settings=_settings())
    assert out["ingested"] == 1 and out["skipped"] == 1
    assert ingested == ["Src:P2"]
    assert await CollectorState_seen(fake, "p2") == 5   # recorded only after success
    assert adapter.closed is True


async def CollectorState_seen(redis, pid):
    from app.collectors.state import CollectorState
    return await CollectorState.seen_version("fake", pid, redis)


@pytest.mark.asyncio
async def test_missing_pat_records_error_no_ingest():
    import fakeredis.aioredis
    from app.collectors.state import CollectorState
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with patch("app.collectors.engine.redis.asyncio.from_url", return_value=fake), \
         patch("app.collectors.engine.VectorClient", return_value=MagicMock(close=AsyncMock())), \
         patch("app.collectors.engine._bootstrap_vault_and_pat", new=AsyncMock(return_value=None)), \
         patch("app.collectors.engine.ingest_knowledge_document", new=AsyncMock()) as mock_ing, \
         patch("app.collectors.engine.emit", new=AsyncMock()):
        out = await CollectorEngine().run(
            lambda pat: FakeAdapter([]), name="fake", enabled=True, pat_vault_key="k", settings=_settings())
    assert out["health"] == "error"
    mock_ing.assert_not_awaited()
    rec = await CollectorState.get_run("fake", fake)
    assert rec["health"] == "error" and rec["errors"] == 1


@pytest.mark.asyncio
async def test_pat_env_value_used_and_vault_skipped():
    """SP3 env-first PAT: when pat_env_value is truthy, the engine must skip
    _bootstrap_vault_and_pat entirely (K8s deployments never touch Vault) and
    the run must proceed normally using the env-supplied token."""
    import fakeredis.aioredis
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    items = [{"stable_id": "p1", "version": 1, "label": "P1", "meta": {}}]
    adapter = FakeAdapter(items)
    captured = {}

    def factory(pat):
        captured["pat"] = pat
        return adapter

    with patch("app.collectors.engine.redis.asyncio.from_url", return_value=fake), \
         patch("app.collectors.engine.VectorClient", return_value=MagicMock(close=AsyncMock())), \
         patch("app.collectors.engine._bootstrap_vault_and_pat", new=AsyncMock()) as mock_vault, \
         patch("app.collectors.engine.ingest_knowledge_document", new=AsyncMock()), \
         patch("app.collectors.engine.emit", new=AsyncMock()):
        out = await CollectorEngine().run(
            factory, name="fake", enabled=True, pat_vault_key="k",
            pat_env_value="envtok", settings=_settings())
    mock_vault.assert_not_awaited()
    assert captured["pat"] == "envtok"
    assert out["health"] == "ok"
    assert out["ingested"] == 1


@pytest.mark.asyncio
async def test_locked_run_returns_locked_and_writes_no_record():
    import fakeredis.aioredis
    from app.collectors.state import CollectorState
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await fake.set("collector:lock:fake", "1")   # pre-contended: SETNX will fail
    with patch("app.collectors.engine.redis.asyncio.from_url", return_value=fake), \
         patch("app.collectors.engine.VectorClient") as mock_vec, \
         patch("app.collectors.engine.emit", new=AsyncMock()):
        out = await CollectorEngine().run(
            lambda pat: FakeAdapter([]), name="fake", enabled=True, pat_vault_key="k", settings=_settings())
    assert out["status"] == "locked"
    mock_vec.assert_not_called()
    assert await CollectorState.get_run("fake", fake) is None


@pytest.mark.asyncio
async def test_lock_acquisition_redis_error_returns_error_not_raise():
    bad_client = MagicMock()
    bad_client.set = AsyncMock(side_effect=ConnectionError("redis unavailable"))
    bad_client.aclose = AsyncMock()
    with patch("app.collectors.engine.redis.asyncio.from_url", return_value=bad_client), \
         patch("app.collectors.engine.VectorClient") as mock_vec:
        out = await CollectorEngine().run(
            lambda pat: FakeAdapter([]), name="fake", enabled=True, pat_vault_key="k", settings=_settings())
    assert out == {"status": "error", "health": "error"}
    mock_vec.assert_not_called()
    bad_client.aclose.assert_awaited_once()
