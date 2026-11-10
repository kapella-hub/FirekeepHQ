"""A superseded memory never outranks a live one in recall (rag._rank_key).
It is demoted, never dropped, and its score is not rewritten."""

from unittest.mock import AsyncMock

import pytest

from app.engine.rag import RAGEngine, _order_results
from app.models import ContextQuery


def _entry(mid, score, status=None, store="vector"):
    e = {"content": f"memory {mid}", "score": score, "store": store,
         "metadata": {"id": mid}}
    if status:
        e["_lifecycle_status"] = status
    return e


def _ids(entries):
    return [e["metadata"]["id"] for e in entries]


def test_superseded_sorts_after_every_live_entry():
    entries = [
        _entry("old", 0.95, status="superseded"),
        _entry("live-strong", 0.80),
        _entry("live-weak", 0.20),
    ]
    assert _ids(_order_results(entries)) == ["live-strong", "live-weak", "old"]


def test_superseded_sorts_after_a_live_entry_scored_zero():
    # min-max normalization pins the weakest live candidate at exactly 0.0 —
    # the case no score multiplier can sort a superseded row below.
    entries = [_entry("old", 2.0, status="superseded"), _entry("live", 0.0)]
    assert _ids(_order_results(entries)) == ["live", "old"]


def test_nothing_is_dropped_and_scores_are_untouched():
    entries = [_entry("old", 0.9, status="superseded"), _entry("new", 0.7)]
    ordered = _order_results(entries)
    assert [(e["metadata"]["id"], e["score"]) for e in ordered] == [
        ("new", 0.7), ("old", 0.9)]


def test_superseded_entries_keep_score_order_among_themselves():
    entries = [
        _entry("a", 0.4, status="superseded"),
        _entry("b", 0.9, status="superseded"),
    ]
    assert _ids(_order_results(entries)) == ["b", "a"]


def test_only_superseded_is_demoted():
    # Deprecated rows already carry a 0.1 multiplier and contested rows are
    # deliberately not down-ranked; neither is touched by this rule.
    entries = [
        _entry("dep", 0.3, status="deprecated"),
        _entry("old", 0.9, status="superseded"),
        _entry("live", 0.2),
    ]
    assert _ids(_order_results(entries)) == ["dep", "live", "old"]


def _hit(mid, score, text, status, confirmed=0):
    return {"id": mid, "score": score, "text": text, "metadata": {
        "status": status, "confirmed_count": confirmed, "contradicted_count": 0,
        "superseded_by": "x" if status == "superseded" else None, "timestamp": "",
    }}


def _engine(hits):
    vector = AsyncMock()
    vector.search = AsyncMock(return_value=hits)
    graph = AsyncMock()
    graph.query_related = AsyncMock(return_value=[])
    graph.query_related_multihop = AsyncMock(return_value=[])
    return RAGEngine(graph=graph, vector=vector)


@pytest.mark.asyncio
async def test_recall_does_not_let_a_superseded_memory_take_a_live_ones_slot():
    """End to end: the superseded row is the closest match AND confirmed, which
    is how it used to beat the 0.5 multiplier. With top_k=2 and two live
    candidates, both live ones are returned and the superseded one is not."""
    engine = _engine([
        _hit("old", 0.95, "deploy with the v1 script", "superseded", confirmed=3),
        _hit("live-a", 0.80, "deploy with update.sh", "active"),
        _hit("live-b", 0.60, "update.sh needs the pull flag", "active"),
    ])
    resp = await engine.recall(
        ContextQuery(task="how do we deploy", top_k=2, format="raw"))

    assert [s.content for s in resp.sources] == [
        "deploy with update.sh", "update.sh needs the pull flag"]
    assert "[SUPERSEDED]" not in resp.context_block


@pytest.mark.asyncio
async def test_recall_still_returns_a_superseded_memory_when_there_is_room():
    engine = _engine([
        _hit("old", 0.95, "deploy with the v1 script", "superseded", confirmed=3),
        _hit("live-a", 0.80, "deploy with update.sh", "active"),
    ])
    resp = await engine.recall(
        ContextQuery(task="how do we deploy", top_k=5, format="raw"))

    assert [s.content for s in resp.sources] == [
        "deploy with update.sh", "deploy with the v1 script"]
    assert "[SUPERSEDED]" in resp.context_block
