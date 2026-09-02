"""The fleet ledger: the store forgets (rejection is deletion, no approval
timestamp existed), so approval rates come from monotonic counters."""
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr
import pytest
import pytest_asyncio

from app.fleet import ledger

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def redis():
    r = fr.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_record_increments_total_and_daily_with_ttl(redis):
    assert await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=NOW) is True
    assert await redis.hget(ledger.total_key(ledger.JOB_REAUTHOR), "produced") == "1"
    day = ledger.day_key(ledger.JOB_REAUTHOR, "2026-09-02")
    assert await redis.hget(day, "produced") == "1"
    assert 0 < await redis.ttl(day) <= 400 * 86400


@pytest.mark.asyncio
async def test_unknown_job_or_counter_is_ignored(redis):
    assert await ledger.record(redis, "not_a_job", "produced") is False
    assert await ledger.record(redis, ledger.JOB_REAUTHOR, "bogus") is False
    assert await redis.keys("fleet:*") == []


@pytest.mark.asyncio
async def test_record_never_raises_without_redis():
    assert await ledger.record(None, ledger.JOB_REAUTHOR, "produced") is False


def test_rate_is_null_on_zero_denominator():
    assert ledger.rate(0, 0) is None
    assert ledger.rate(3, 4) == 0.75


@pytest.mark.asyncio
async def test_summarize_windows_by_day_and_reports_all_time(redis):
    old = NOW - timedelta(days=10)
    for _ in range(4):
        await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=old)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "approved", now=old)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=NOW)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "rejected", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "proposed", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "resolved", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "matched", now=NOW)

    out = await ledger.summarize(redis, days=7, now=NOW)
    re = out[ledger.JOB_REAUTHOR]
    assert re["window"] == {"produced": 1, "approved": 0, "rejected": 1, "approval_rate": 0.0}
    assert re["all_time"] == {"produced": 5, "approved": 1, "rejected": 1,
                              "approval_rate": 0.5, "pending": 3}
    v = out[ledger.JOB_VERDICT]
    assert v["window"] == {"proposed": 1, "resolved": 1, "matched": 1, "match_rate": 1.0}
    assert v["all_time"]["match_rate"] == 1.0
    # A job with no activity still appears, with null rates — the dashboard must
    # show "not enough evidence", never a missing row or 0%.
    d = out[ledger.JOB_DISTILL]
    assert d["window"]["approval_rate"] is None and d["all_time"]["pending"] == 0


@pytest.mark.asyncio
async def test_rejected_reauthor_marker(redis):
    await ledger.mark_rejected_reauthor(redis, "sk-1")
    key = ledger.rejected_reauthor_key("sk-1")
    assert await redis.exists(key) == 1
    assert 0 < await redis.ttl(key) <= 90 * 86400


@pytest_asyncio.fixture
async def redis_bytes():
    # The app's real client is `redis.asyncio.from_url(settings.REDIS_URL)`
    # with NO `decode_responses=True` (see app/main.py) — HGETALL comes back
    # with bytes KEYS as well as bytes values. This fixture reproduces that,
    # unlike every other test here which uses decode_responses=True and would
    # not catch a key-decoding bug.
    r = fr.FakeRedis()
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_summarize_against_bytes_mode_redis(redis_bytes):
    assert await ledger.record(redis_bytes, ledger.JOB_REAUTHOR, "produced", now=NOW) is True
    assert await ledger.record(redis_bytes, ledger.JOB_REAUTHOR, "produced", now=NOW) is True
    assert await ledger.record(redis_bytes, ledger.JOB_REAUTHOR, "approved", now=NOW) is True

    out = await ledger.summarize(redis_bytes, days=7, now=NOW)
    re = out[ledger.JOB_REAUTHOR]
    # approval_rate = approved / (approved + rejected) = 1/1, pending = 2-1-0
    assert re["all_time"] == {"produced": 2, "approved": 1, "rejected": 0,
                              "approval_rate": 1.0, "pending": 1}
    assert re["window"] == {"produced": 2, "approved": 1, "rejected": 0,
                            "approval_rate": 1.0}
