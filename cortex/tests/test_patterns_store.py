"""Task 2 (N=1 learning loop): get_observed_patterns returns the caller's OWN
recent candidate/observed patterns — the descriptive, UNVALIDATED surface that
get_relevant_patterns (trial+ only) filters out.

Observed != validated: these are the caller's own not-yet-promoted patterns,
surfaced descriptively with provenance. Never a promoted strategy card.
"""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.patterns.models import PatternCard
from app.patterns.store import get_observed_patterns, store_patterns

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def fake_store():
    # fakeredis.aioredis.FakeRedis has a SYNC constructor — a sync fixture works
    # under both strict and auto asyncio modes.
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


async def _add(r, *, id: str, agent: str, stage: str, category: str) -> None:
    """Store one PatternCard. `agent` maps onto the model's source_agent field
    (that is what the 'not mine' exclusion below actually tests)."""
    await store_patterns(
        r,
        [PatternCard(id=id, source_agent=agent, stage=stage, category=category)],
    )


async def test_get_observed_returns_own_candidate_patterns(fake_store):
    await _add(fake_store, id="a", agent="me", stage="observed", category="risk")
    await _add(fake_store, id="b", agent="me", stage="validated", category="risk")  # excluded (validated -> the other surface)
    await _add(fake_store, id="c", agent="other", stage="observed", category="risk")  # excluded (not mine)
    out = await get_observed_patterns(fake_store, agent_id="me", limit=5)
    assert [p.id for p in out] == ["a"]
