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
DEFAULT_EMBED_TIMEOUT = 1.2


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


async def search_skill_points(
    vector,
    settings,
    *,
    must: list,
    query: str | None,
    limit: int,
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
    timeout = _float_setting(
        settings, "SKILL_MATCH_EMBED_TIMEOUT_SECONDS", DEFAULT_EMBED_TIMEOUT
    )

    try:
        vec = await asyncio.wait_for(vector._embed(q), timeout)
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
