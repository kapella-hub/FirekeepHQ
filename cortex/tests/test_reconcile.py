"""Tests for safe draft-skill reconciliation (SP3 Task 2)."""
from __future__ import annotations
from unittest.mock import AsyncMock, MagicMock
import pytest
from app.skills.reconcile import reconcile_source_skills


def _pt(pid, title, status):
    p = MagicMock()
    p.id = pid
    p.payload = {"procedure_title": title, "skill_status": status, "source_doc": "S"}
    return p


@pytest.mark.asyncio
async def test_sweeps_only_vanished_drafts_never_active():
    vector = MagicMock()
    vector._client = AsyncMock()
    # A:draft(kept), B:active(vanished -> MUST NOT delete), C:draft(vanished -> delete)
    vector._client.scroll = AsyncMock(return_value=(
        [_pt("a", "A", "draft"), _pt("b", "B", "active"), _pt("c", "C", "draft")], None))
    vector._client.delete = AsyncMock()
    out = await reconcile_source_skills("S", {"A"}, vector)
    assert out == {"deleted": 1}
    # Exactly one delete, and it is C (the vanished draft), never B (active)
    vector._client.delete.assert_awaited_once()
    assert "c" in str(vector._client.delete.await_args)


@pytest.mark.asyncio
async def test_empty_new_titles_sweeps_all_drafts_but_not_active():
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.scroll = AsyncMock(return_value=(
        [_pt("a", "A", "draft"), _pt("b", "B", "active")], None))
    vector._client.delete = AsyncMock()
    out = await reconcile_source_skills("S", set(), vector)
    assert out == {"deleted": 1}   # only the draft A; active B untouched


@pytest.mark.asyncio
async def test_scroll_filter_scoped_to_skill_type_and_source_doc():
    """Guards against a future regression dropping the source_doc condition from
    the scroll filter, which would turn this into a cross-source mass-deletion."""
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.scroll = AsyncMock(return_value=([], None))
    vector._client.delete = AsyncMock()
    await reconcile_source_skills("S", {"A"}, vector)

    vector._client.scroll.assert_awaited_once()
    scroll_filter = vector._client.scroll.await_args.kwargs["scroll_filter"]
    conditions = scroll_filter.must
    assert len(conditions) == 2

    by_key = {c.key: c.match.value for c in conditions}
    assert by_key.get("memory_type") == "skill"
    assert by_key.get("source_doc") == "S"
