import httpx
import pytest

from bench import common, ingest
from tests.conftest import FIXTURE_ROWS


def test_turn_pairs_pairs_user_with_assistant():
    session = FIXTURE_ROWS[0]["haystack_sessions"][0]
    pairs = ingest.turn_pairs(session)
    assert pairs[0] == ("I'm planning a move to Lisbon.", "Exciting! When do you move?")
    assert len(pairs) == 2


def test_turn_pairs_handles_dangling_user_turn():
    pairs = ingest.turn_pairs([{"role": "user", "content": "hello?"}])
    assert pairs == [("hello?", "(no reply)")]


def test_turn_pairs_truncates_to_api_limit():
    pairs = ingest.turn_pairs([
        {"role": "user", "content": "x" * 9000},
        {"role": "assistant", "content": "y" * 9000},
    ])
    assert len(pairs[0][0]) == 5000 and len(pairs[0][1]) == 5000


def test_learn_payloads_stamps_namespace_and_tags():
    payloads = ingest.learn_payloads(FIXTURE_ROWS[0])
    assert all(p["namespace"] == "lm_q_multi_1" for p in payloads)
    first = payloads[0]
    assert common.session_tag("s_a") in first["tags"]
    assert common.date_tag("2023/04/01 (Sat) 09:00") in first["tags"]
    assert first["memory_type"] == "episodic"
    assert first["domain"] == "longmemeval"
    # 2 pairs from session s_a + 1 pair from s_b
    assert len(payloads) == 3


def test_ledger_roundtrip(tmp_path):
    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    key = "lm_q_multi_1/s_a"
    assert not led.done(key)
    led.mark(key, n_memories=2)
    assert led.done(key)
    # A fresh instance re-reads the file (resume-after-crash behavior).
    led2 = ingest.Ledger(tmp_path / "ledger.jsonl")
    assert led2.done(key)
    assert led2.memories_per_session("lm_q_multi_1") == {"s_a": 2}


@pytest.mark.anyio
async def test_ingest_skips_ledgered_sessions_and_posts_the_rest(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "vector_id": "v1"})

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    led.mark("lm_q_multi_1/s_a", n_memories=2)
    transport = httpx.MockTransport(handler)
    stats = await ingest.ingest(
        [FIXTURE_ROWS[0]], "http://bench", ledger=led, transport=transport
    )
    assert stats.sessions_skipped == 1
    assert stats.sessions_done == 1
    assert stats.learn_calls == 1  # only s_b's single pair
    assert not stats.errors


@pytest.mark.anyio
async def test_ingest_records_error_and_continues(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    stats = await ingest.ingest(
        [FIXTURE_ROWS[0]], "http://bench", ledger=led,
        transport=httpx.MockTransport(handler), max_retries=1,
    )
    assert stats.errors  # recorded, not raised
    assert not led.done("lm_q_multi_1/s_a")  # failed session NOT marked done
