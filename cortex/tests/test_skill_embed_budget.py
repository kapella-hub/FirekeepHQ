"""A slow first embed must degrade ONE call, not poison the query forever.

WHY THESE EXIST. `search_skill_points` bounded the query embed with
`asyncio.wait_for`, which CANCELS the coroutine on timeout. `VectorClient._embed`
writes its LRU cache entry LAST, so a cancelled embed caches nothing and the
identical query times out again on every subsequent call.

Measured on the live deployment: `GET /skills?status=draft&q=<task>` repeated
four times returned 0,0,0,0, while a query that happened to land under the
budget was cached and returned 28,28,28,28 forever after. Whether an agent got
skill matching at all was decided by the latency of its FIRST call and never
revisited.

The budget itself was also wrong for one of its two callers. 1.2s was sized to
fit the briefing's 2.0s per-section cap and then applied to `GET /skills?q=`,
which runs under no such cap; cold `_embed` inside the live container measured
0.62s / 4.63s / 8.16s, so `skill_recall` returned NOTHING for ordinary tasks
against a store of 28 skills — including "publish the firekeep marketing
website", for which an active skill triggering on exactly that exists and
scores 0.7316 when the embed is allowed to finish. Two deadlines need two
numbers.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.skills import search as S


class _SlowEmbed:
    """Embeds after `delay`, caching the result — like the real `_embed`."""

    def __init__(self, delay: float):
        self.delay = delay
        self.cache: dict[str, list[float]] = {}
        self.starts = 0
        self.completions = 0

    async def __call__(self, text: str):
        if text in self.cache:
            return self.cache[text]
        self.starts += 1
        await asyncio.sleep(self.delay)
        self.cache[text] = [0.1, 0.2]
        self.completions += 1
        return self.cache[text]


@pytest.mark.asyncio
async def test_timed_out_embed_still_populates_the_cache():
    """The self-healing property: give up on schedule, let the embed finish.

    Without the shield the embed is cancelled, nothing is cached, and the same
    query fails identically forever.
    """
    embed = _SlowEmbed(delay=0.15)
    vector = MagicMock()
    vector._embed = embed

    with pytest.raises(asyncio.TimeoutError):
        await S._embed_with_cache_warm(vector, "a task", timeout=0.02)

    # The caller has already given up. The embed has not.
    await asyncio.sleep(0.25)
    assert embed.completions == 1
    assert "a task" in embed.cache


@pytest.mark.asyncio
async def test_the_retry_after_a_timeout_succeeds():
    """The whole point, stated as the user-visible behaviour."""
    embed = _SlowEmbed(delay=0.1)
    vector = MagicMock()
    vector._embed = embed

    with pytest.raises(asyncio.TimeoutError):
        await S._embed_with_cache_warm(vector, "a task", timeout=0.01)
    await asyncio.sleep(0.2)

    assert await S._embed_with_cache_warm(vector, "a task", timeout=0.01) == [0.1, 0.2]
    assert embed.starts == 1, "the retry must be a cache hit, not a second embed"


@pytest.mark.asyncio
async def test_a_failing_embed_does_not_leak_a_pending_task():
    """A backend error is not something to wait for; don't hold the task."""

    async def _boom(text):
        raise RuntimeError("backend down")

    vector = MagicMock()
    vector._embed = _boom

    with pytest.raises(RuntimeError):
        await S._embed_with_cache_warm(vector, "a task", timeout=5.0)
    assert not S._WARMING


@pytest.mark.asyncio
async def test_the_timed_out_task_is_released_when_it_finishes():
    """`_WARMING` is a leak guard, not a leak."""
    embed = _SlowEmbed(delay=0.1)
    vector = MagicMock()
    vector._embed = embed

    with pytest.raises(asyncio.TimeoutError):
        await S._embed_with_cache_warm(vector, "a task", timeout=0.01)
    assert S._WARMING  # held while in flight
    await asyncio.sleep(0.25)
    assert not S._WARMING  # released on completion


class TestBudgets:
    def test_endpoint_budget_is_sized_for_a_cold_cpu_embed(self):
        """1.2s was the briefing's budget applied to a caller that has none.

        Cold `_embed` measured up to 8.16s in the live container; anything at
        or below that does not degrade matching, it disables it.
        """
        assert Settings().SKILL_MATCH_EMBED_TIMEOUT_SECONDS >= 10.0
        assert S.DEFAULT_EMBED_TIMEOUT >= 10.0

    def test_briefing_budget_still_fits_the_per_section_cap(self):
        """`_run_section` is hard-capped at 2.0s and converts an overrun into
        status='unavailable' + degraded on the WHOLE envelope. The briefing
        must give up sooner than the endpoint, not later."""
        s = Settings()
        assert s.SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS < 2.0
        assert (
            s.SKILL_MATCH_BRIEFING_EMBED_TIMEOUT_SECONDS
            < s.SKILL_MATCH_EMBED_TIMEOUT_SECONDS
        )

    @pytest.mark.asyncio
    async def test_an_explicit_embed_timeout_overrides_the_setting(self):
        """The decoupling seam itself — without it there is one number for two
        deadlines, and it suits neither."""
        seen = {}

        async def _fake(vector, q, timeout):
            seen["timeout"] = timeout
            return [0.0]

        settings = MagicMock()
        settings.SKILL_MATCH_EMBED_TIMEOUT_SECONDS = 10.0
        settings.SKILL_MATCH_SCORE_FLOOR = 0.3
        settings.QDRANT_COLLECTION = "c"

        vector = MagicMock()
        results = MagicMock()
        results.points = [MagicMock()]

        async def _qp(**kwargs):
            return results

        vector._client.query_points = _qp

        orig = S._embed_with_cache_warm
        S._embed_with_cache_warm = _fake
        try:
            await S.search_skill_points(
                vector, settings, must=[], query="task", limit=3, embed_timeout=1.2,
            )
        finally:
            S._embed_with_cache_warm = orig

        assert seen["timeout"] == 1.2
