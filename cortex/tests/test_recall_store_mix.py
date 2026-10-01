"""Which recall leg produced each returned row — the receipt field, the daily
store-mix counter and its admin readout.

Why it exists: on the 2026-09-30 LongMemEval legs not one graph row reached a
top-k slot in 2,000 recalls. That store's graph came from /learn chains only,
so the production question — does the graph leg ever change what an agent
sees? — needs counting on real traffic before RECALL_GRAPH_ENABLED moves.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.main import (
    _bump_recall_store_mix,
    _store_counts,
    get_recall_store_mix,
)


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def test_store_counts_counts_each_leg_and_ignores_unknown():
    assert _store_counts(["vector", "vector", "graph", "both", "corpus", ""]) == {
        "vector": 2, "graph": 1, "both": 1,
    }
    assert _store_counts([]) == {"vector": 0, "graph": 0, "both": 0}


@pytest.mark.asyncio
async def test_bump_accumulates_rows_and_graph_recalls(fake_redis):
    await _bump_recall_store_mix(fake_redis, {"vector": 3, "graph": 0, "both": 0})
    await _bump_recall_store_mix(fake_redis, {"vector": 2, "graph": 0, "both": 1})
    await _bump_recall_store_mix(fake_redis, {"vector": 1, "graph": 1, "both": 0})

    (key,) = await fake_redis.keys("cortex:recall_store_mix:*")
    assert await fake_redis.hgetall(key) == {
        "recalls": "3",
        # a graph-boosted vector row counts: the graph leg changed the ranking
        "recalls_with_graph": "2",
        "rows_vector": "6",
        "rows_graph": "1",
        "rows_both": "1",
    }
    assert 0 < await fake_redis.ttl(key) <= 86400 * 35


@pytest.mark.asyncio
async def test_bump_never_raises():
    broken = MagicMock()  # redis' pipeline() is synchronous
    broken.pipeline.side_effect = RuntimeError("redis down")
    await _bump_recall_store_mix(broken, {"vector": 1, "graph": 0, "both": 0})


@pytest.mark.asyncio
async def test_readout_totals_and_graph_share(fake_redis):
    await _bump_recall_store_mix(fake_redis, {"vector": 3, "graph": 0, "both": 0})
    await _bump_recall_store_mix(fake_redis, {"vector": 2, "graph": 1, "both": 0})
    await _bump_recall_store_mix(fake_redis, {"vector": 3, "graph": 0, "both": 0})
    await _bump_recall_store_mix(fake_redis, {"vector": 3, "graph": 0, "both": 0})

    out = await get_recall_store_mix(redis_client=fake_redis, identity={}, days=3)

    assert out["totals"]["recalls"] == 4
    assert out["totals"]["recalls_with_graph"] == 1
    assert out["totals"]["rows_graph"] == 1
    assert out["graph_share"] == 0.25
    assert len(out["by_day"]) == 3


@pytest.mark.asyncio
async def test_readout_with_no_traffic_says_unknown_not_zero(fake_redis):
    out = await get_recall_store_mix(redis_client=fake_redis, identity={}, days=1)
    assert out["totals"]["recalls"] == 0
    # No recalls is not "the graph never helps".
    assert out["graph_share"] is None


def test_readout_is_admin_only(test_client):
    # Counts span every workspace; with auth off no caller holds "admin".
    resp = test_client.get("/admin/recall-store-mix")
    assert resp.status_code in (401, 403)


def test_recall_receipt_carries_store_counts(test_client, mock_graph, mock_vector):
    mock_graph.query_related.return_value = []
    mock_graph.query_related_multihop.return_value = []
    mock_vector.search.return_value = [
        {"id": "m1", "score": 0.9, "text": "deploy runs on port 8100",
         "metadata": {"id": "m1", "status": "active"}},
    ]

    with patch("app.main._replay_emit", new_callable=AsyncMock) as mock_emit:
        resp = test_client.post("/memory/recall", json={"task": "which port", "format": "raw"})

    assert resp.status_code == 200
    payload = mock_emit.call_args.kwargs["payload"]
    assert payload["store_counts"] == {"vector": 1, "graph": 0, "both": 0}
    assert payload["token_budget"] == 600
    assert payload["tokens_used"] < 600


@pytest.mark.asyncio
async def test_budget_bound_recalls_are_counted_and_shared(fake_redis):
    await _bump_recall_store_mix(fake_redis, {"vector": 2, "graph": 0, "both": 0}, budget_bound=True)
    await _bump_recall_store_mix(fake_redis, {"vector": 3, "graph": 0, "both": 0})

    out = await get_recall_store_mix(redis_client=fake_redis, identity={}, days=1)

    assert out["totals"]["recalls_budget_bound"] == 1
    assert out["budget_bound_share"] == 0.5
