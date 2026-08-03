import asyncio
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


@pytest.mark.anyio
async def test_post_retries_5xx_then_succeeds(tmp_path, monkeypatch):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return httpx.Response(500, text="server error")
        return httpx.Response(200, json={"status": "ok", "vector_id": "v1"})

    # Mock asyncio.sleep to avoid actual delays in test (return an async no-op)
    async def mock_sleep(_):
        pass

    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    # Create a single row with one session and one pair to isolate the retry behavior
    row = {
        "question_id": "test_retry_q",
        "haystack_session_ids": ["retry_s"],
        "haystack_dates": ["2023/04/01 (Sat) 09:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "test question"},
                {"role": "assistant", "content": "test answer"},
            ]
        ],
    }

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    transport = httpx.MockTransport(handler)
    stats = await ingest.ingest(
        [row], "http://bench", ledger=led, transport=transport,
        max_retries=2,
    )
    # Single session with one pair should succeed after retries
    assert stats.sessions_done == 1
    assert stats.learn_calls == 1
    assert not stats.errors
    # Handler should have been called 3 times: fail, fail, success
    assert call_count == 3


@pytest.mark.anyio
async def test_post_4xx_fails_immediately_without_retry(tmp_path):
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(400, text="bad request")

    # Create a single row with one session and one pair to isolate fail-fast behavior
    row = {
        "question_id": "test_4xx_q",
        "haystack_session_ids": ["fail_fast_s"],
        "haystack_dates": ["2023/04/01 (Sat) 09:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "test question"},
                {"role": "assistant", "content": "test answer"},
            ]
        ],
    }

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    transport = httpx.MockTransport(handler)
    stats = await ingest.ingest(
        [row], "http://bench", ledger=led, transport=transport,
        max_retries=3,
    )
    # Should record error and not mark session done
    assert stats.errors
    assert not led.done("lm_test_4xx_q/fail_fast_s")
    # Handler should have been called exactly once (no retry on 4xx)
    assert call_count == 1


@pytest.mark.anyio
async def test_semaphore_bounds_concurrency(tmp_path):
    in_flight = 0
    max_in_flight = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        # Brief async yield to allow context switches
        await asyncio.sleep(0.001)
        in_flight -= 1
        return httpx.Response(200, json={"status": "ok", "vector_id": "v1"})

    # Create rows with multiple single-pair sessions to test concurrency
    rows = []
    for i in range(4):
        row = {
            "question_id": f"q_{i}",
            "haystack_session_ids": [f"s_{i}_{j}" for j in range(3)],
            "haystack_dates": ["2023/04/01 (Sat) 09:00"] * 3,
            "haystack_sessions": [
                [
                    {"role": "user", "content": f"question {i}_{j}"},
                    {"role": "assistant", "content": f"answer {i}_{j}"},
                ]
                for j in range(3)
            ],
        }
        rows.append(row)

    led = ingest.Ledger(tmp_path / "ledger.jsonl")
    transport = httpx.MockTransport(handler)
    stats = await ingest.ingest(
        rows, "http://bench", ledger=led, transport=transport, concurrency=3,
    )
    assert not stats.errors
    assert stats.learn_calls == 12  # 4 rows * 3 sessions * 1 pair each
    # With concurrency=3, max_in_flight should never exceed 3
    assert max_in_flight <= 3
