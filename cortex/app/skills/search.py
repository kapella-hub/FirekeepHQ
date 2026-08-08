"""Semantic skill matching — the single primitive behind `GET /skills?q=` and the
briefing's skills section.

WHY THIS MODULE EXISTS
Skill retrieval was broken in two places that were copies of each other. Both did a
Qdrant `scroll` (ID-ordered, NOT relevance-ordered) capped at `limit`, then applied a
LITERAL SUBSTRING filter to the page. Two defects compounded:

  (a) the substring test — `skill_recall` sent the first five words of the task and
      required that exact string inside a trigger, so it essentially never matched; and
  (b) `limit` was applied to the scroll BEFORE the filter, so the relevant skill was
      usually not even among the candidates.

The root cause is legible in `list_skills`' own docstring ("mirrors skills/api
list_skills"): an endpoint written as a dashboard LISTER — where scroll-then-filter is
exactly right — was reused as a MATCHER, which needs relevance ranking. This module is
the matcher, factored out so there is one implementation rather than two divergent
inline copies.

WHY NOT `VectorClient.search()`
It is the obvious candidate and it is wrong here, in four independent ways:
  1. It appends an UNCONDITIONAL `must_not skill_status="draft"` — correct for
     memory recall, fatal here, because the dashboard review queue depends on
     `GET /skills?status=draft` returning drafts.
  2. It cannot express this filter at all: it exposes only tags/project/namespace,
     with no `memory_type` / `skill_status` / `domain` / `stale`.
  3. Its projection (`_projected_metadata`) drops every skill-specific payload field
     (trigger, symptoms, content, skill_status, stale, needs_rereview), so a
     `SkillResponse` cannot be built from what it returns.
  4. Skill points carry no `text` payload key, which is what `search()` reads for the
     body — every skill would come back empty.
Routing through `RAGEngine.recall()` is worse still: skill points have no nested
`metadata` sub-dict, so decay defaults them to `episodic` and exponentially penalises an
old-but-valid skill, while lifecycle and OWM multipliers distort what must be pure
similarity.

So this uses the layer beneath both: `vector._embed()` (already used by the skills
router to CREATE skills) plus a raw `query_points` with the CALLER'S OWN filter passed
through verbatim.

THE TWO-PATH CONTRACT
`search_skill_points` returns `(points, semantic)`:
  * `semantic=True`  — points are cosine-ranked and already floored. The caller MUST
                       NOT apply any substring narrowing; doing so would re-introduce
                       the bug on top of a working matcher.
  * `semantic=False` — an ID-ordered scroll page, byte-identical to the legacy path.
                       The caller applies whatever narrowing it did before.

Degradation is scoped deliberately: the EMBED is fail-soft (an embeddings backend that
is down or slow must not take out skill listing, which needed no embedding at all
before this change), while Qdrant itself stays fail-loud (a storage failure must
surface exactly as the old `scroll` failure did, not silently become an empty list).

The empty-result fallback at the end is what makes this a strict superset of the old
behaviour: when nothing clears the floor, the legacy scroll page is returned and the
caller re-applies its substring filter — so a rare literal hit (an error code, an ID)
that embeds poorly is still found, and no floor value can make matching worse than it
was before this change.
"""
from __future__ import annotations

import asyncio
import logging

from qdrant_client.models import Filter

logger = logging.getLogger(__name__)

# Fallbacks for callers whose settings object predates these keys (and for the many
# tests built on a bare MagicMock settings — see _float_setting).
DEFAULT_SCORE_FLOOR = 0.30
# 10.0, not the 1.2 this shipped with. 1.2 was sized to fit the briefing's 2.0s
# per-section budget, which is the wrong constraint for the OTHER caller:
# `GET /skills?q=` (behind `skill_recall`) is a plain request under no such cap.
# Measured cold `_embed` latency inside the live container: 0.62s / 4.63s /
# 8.16s — so `skill_recall` returned n=0 for ordinary tasks on a deployment
# holding 28 skills, and `GET /briefing` reported `skills: {status: 'empty'}`
# for a goal whose skill matched at 0.7316 when the embed was allowed to
# finish. The briefing passes its own tighter budget explicitly (see
# `embed_timeout`), so the two callers no longer share one number that suits
# neither.
DEFAULT_EMBED_TIMEOUT = 10.0

# Strong references to embeds that outlived their caller's timeout (see
# `_embed_with_cache_warm`). Without this the task is only referenced by the
# event loop and CPython is free to collect it mid-flight, which would defeat
# the whole point of letting it run on.
_WARMING: set = set()


def _float_setting(settings, name: str, default: float) -> float:
    """Read a float setting, falling back on anything that isn't a real number.

    Not defensive padding: most Cortex tests build `settings` as a bare `MagicMock`,
    and `MagicMock().SKILL_MATCH_SCORE_FLOOR` auto-creates a *Mock attribute* that is
    neither None nor a float. Passing that straight into `score_threshold` or
    `asyncio.wait_for` raises TypeError deep inside the call, turning a config-shape
    problem into ~20 unrelated test failures. `bool` is excluded explicitly because
    `isinstance(True, int)` is True and a stray boolean must not become 1.0.
    """
    value = getattr(settings, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


async def _scroll_page(vector, settings, must: list, limit: int) -> list:
    """The legacy candidate page — deliberately byte-identical to the call this
    replaced in `list_skills`, so the no-`q` path is provably unchanged."""
    points, _ = await vector._client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=must),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )
    return list(points)


async def _embed_with_cache_warm(vector, q: str, timeout: float) -> list:
    """Embed `q` under `timeout`, letting a slow embed FINISH in the background.

    THE SELF-HEALING PROPERTY. `vector._embed` caches by content hash, and the
    cache write is the LAST thing it does. A plain `asyncio.wait_for` CANCELS
    the coroutine on timeout, so the cache is never populated and the identical
    query times out again on every subsequent call — measured on the live
    deployment: the same `?status=draft&q=<task>` returned 0,0,0,0 across four
    attempts, while a query that happened to land under the budget was cached
    and returned 28,28,28,28 forever after. Whether an agent got skill matching
    was decided by the latency of its first call and never revisited.

    `shield` is what changes that: the caller gives up on schedule, the embed
    runs to completion, and the NEXT call is a cache hit. One slow start
    degrades one call instead of poisoning the query.

    The shielded task is deliberately not awaited anywhere — its exception (if
    any) is consumed in the done-callback so it cannot surface as an
    "exception was never retrieved" warning, and the strong reference in
    `_WARMING` keeps it alive until it finishes.
    """
    task = asyncio.ensure_future(vector._embed(q))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        _WARMING.add(task)

        def _done(t: asyncio.Future) -> None:
            _WARMING.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.debug("background skill embed failed: %s", t.exception())
            else:
                logger.debug("background skill embed completed — cache warmed")

        task.add_done_callback(_done)
        raise
    except BaseException:
        # Any other failure (including our caller being cancelled) leaves no
        # useful cache entry to wait for; don't leak the task.
        task.cancel()
        raise


async def search_skill_points(
    vector,
    settings,
    *,
    must: list,
    query: str | None,
    limit: int,
    embed_timeout: float | None = None,
) -> tuple[list, bool]:
    """Return `(points, semantic)` for a skill query.

    `must` is passed through VERBATIM as the Qdrant `query_filter.must` — it carries the
    caller's `memory_type=skill`, `skill_status=<requested>`, lowercased `project`,
    `domain`, and the append-only `stale` condition. It is never rebuilt or extended
    here; that is what keeps the draft review queue and the three-state `stale` filter
    working, and what keeps this module ignorant of skill semantics.

    See the module docstring for the two-path contract and why the embed is fail-soft
    while Qdrant is fail-loud.
    """
    q = (query or "").strip()
    if not q:
        # No query to embed. This is the production-NORMAL briefing case (a standard
        # Claude Code SessionStart supplies no goal), so it must never pay for — or
        # depend on — an embeddings round trip.
        return await _scroll_page(vector, settings, must, limit), False

    floor = _float_setting(settings, "SKILL_MATCH_SCORE_FLOOR", DEFAULT_SCORE_FLOOR)
    timeout = (
        embed_timeout
        if isinstance(embed_timeout, (int, float)) and not isinstance(embed_timeout, bool)
        else _float_setting(
            settings, "SKILL_MATCH_EMBED_TIMEOUT_SECONDS", DEFAULT_EMBED_TIMEOUT
        )
    )

    try:
        vec = await _embed_with_cache_warm(vector, q, timeout)
    except Exception as exc:  # noqa: BLE001
        # BROAD on purpose. The embed path raises VectorStoreError for its own
        # recognised failures, but a misconfigured client raises TypeError and a slow
        # backend raises TimeoutError; narrowing this to VectorStoreError would let
        # those escape and flip the whole briefing envelope to degraded on every
        # session start. `Exception` does not catch `asyncio.CancelledError`
        # (BaseException), so an outer per-section timeout can still cancel us.
        logger.warning("skill semantic match degraded to scroll (embed failed: %s)", exc)
        return await _scroll_page(vector, settings, must, limit), False

    # NOT wrapped: a Qdrant failure must propagate exactly as the legacy scroll's would.
    # Fail-soft is scoped to the embed; the storage layer stays fail-loud.
    results = await vector._client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=vec,
        query_filter=Filter(must=must),
        limit=limit,
        with_payload=True,
        score_threshold=floor,
    )
    points = list(results.points)
    if points:
        return points, True

    # Nothing cleared the floor. Hand back the legacy page and let the caller re-apply
    # its substring narrowing — the guarantee that no floor value can regress below the
    # pre-fix behaviour.
    return await _scroll_page(vector, settings, must, limit), False
