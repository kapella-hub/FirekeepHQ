import json

import httpx
import pytest

from bench import recall
from tests.conftest import FIXTURE_ROWS

RESPONSE = {
    "context_block": "ctx",
    "score": 0.8,
    "sources": [
        {"store": "vector", "content": "m1", "score": 0.9,
         "metadata": {"tags": ["lm_session:s_a", "lm_date:2023/04/01 (Sat) 09:00"]}},
        {"store": "graph", "content": "g1", "score": 0.5, "metadata": {}},
        {"store": "both", "content": "m2", "score": 0.4,
         "metadata": {"tags": ["lm_session:s_b", "lm_date:2023/04/10 (Mon) 09:00"]}},
    ],
}


def test_recall_body_uses_config_and_namespace():
    body = recall.recall_body(FIXTURE_ROWS[0], recall.CONFIGS["bench"])
    assert body["namespace"] == "lm_q_multi_1"
    assert body["top_k"] == 10
    assert body["format"] == "raw"
    assert body["token_budget"] == 10000
    assert body["task"] == FIXTURE_ROWS[0]["question"]


def test_extract_hits_preserves_rank_and_joins_tags():
    hits = recall.extract_hits(RESPONSE)
    assert [h["session_id"] for h in hits] == ["s_a", None, "s_b"]
    assert hits[0]["score"] == 0.9
    # Verify dates are extracted and preserved
    assert [h["date"] for h in hits] == ["2023/04/01 (Sat) 09:00", None, "2023/04/10 (Mon) 09:00"]


@pytest.mark.anyio
async def test_run_recall_writes_jsonl_and_resumes(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=RESPONSE)

    out = tmp_path / "recall_bench.jsonl"
    transport = httpx.MockTransport(handler)
    stats = await recall.run_recall(
        FIXTURE_ROWS, "http://bench", "bench", out, transport=transport)
    assert stats.completed == 2 and len(calls) == 2
    # Second run: everything already recorded, no new HTTP calls.
    stats2 = await recall.run_recall(
        FIXTURE_ROWS, "http://bench", "bench", out, transport=transport)
    assert stats2.skipped == 2 and len(calls) == 2


@pytest.mark.anyio
async def test_run_recall_records_error_row(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    out = tmp_path / "recall_bench.jsonl"
    stats = await recall.run_recall(
        FIXTURE_ROWS[:1], "http://bench", "bench", out,
        transport=httpx.MockTransport(handler), max_retries=0)
    assert stats.errored == 1
    row = json.loads(out.read_text().splitlines()[0])
    assert row["error"]
