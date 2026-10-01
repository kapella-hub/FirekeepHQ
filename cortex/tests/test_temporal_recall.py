"""Relative-time recall: the parser, the two-lane merge, the recall wiring,
the vector filter and the fields it rests on.

Why it exists: on LongMemEval-S (2026-09-30) 8 of the 11 questions recall
still missed at top-10 named a relative time ("10 days ago", "last Tuesday").
Variant B (lanes taken in turn) is what shipped; variant A (in-window rows
first) measured as a near-hard filter and is pinned against here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import Settings, get_settings
from app.db.vector import VectorClient, time_window_condition
from app.engine.rag import RAGEngine, _merge_windowed, _order_results
from app.engine.temporal import parse_time_window
from app.models import ActionLog, ContextQuery

# 2023-05-30 is a Tuesday.
AS_OF = datetime(2023, 5, 30, 23, 40, tzinfo=timezone.utc)


def _days(window):
    """A window as (days-before-as_of of its start, of its end)."""
    start, end = window
    return round((AS_OF - start) / timedelta(days=1), 2), round((AS_OF - end) / timedelta(days=1), 2)


# ---------------------------------------------------------------------------
# parse_time_window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What kitchen appliance did I buy 10 days ago?", (11, 9)),
        ("the social media activity I participated 5 days ago", (6, 4)),
        ("an investment four weeks ago", (31, 25)),
        ("Which book did I finish a week ago?", (10, 4)),
        ("a couple of weeks ago we fixed it", (17, 11)),
        ("What did I do with Rachel on the Wednesday two months ago?", (70, 50)),
        ("the outage one year ago", (410, 320)),
        ("what did we deploy yesterday", (2, 0)),
        ("Who did I meet with during the lunch last Tuesday?", (8, 6)),  # Tue -> previous Tue
        ("the call last Monday", (2, 0)),
        ("the release last month", (45, 15)),
        ("plans from last week", (10.5, 3.5)),
    ],
)
def test_recognised_expressions_resolve_to_a_window(text, expected):
    window = parse_time_window(text, AS_OF)
    assert window is not None
    assert _days(window) == expected
    assert window[0].tzinfo is not None


@pytest.mark.parametrize(
    "text",
    [
        # Asking FOR the duration names no anchor.
        "How many days ago did I meet Emma?",
        "How many weeks ago did I attend the friends and family sale?",
        "plans for last weekend",
        "deploy the service to staging",
        "the last weekday release",
        "0 days ago",
        "",
    ],
)
def test_unanchored_or_unrelated_text_parses_to_none(text):
    assert parse_time_window(text, AS_OF) is None


def test_first_expression_by_position_wins():
    window = parse_time_window("last month, or was it 3 days ago?", AS_OF)
    assert _days(window) == (45, 15)


def test_parser_never_raises():
    assert parse_time_window(None, AS_OF) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _merge_windowed / _order_results
# ---------------------------------------------------------------------------


def _hit(i: str, score: float) -> dict:
    return {"id": i, "score": score, "text": f"memory {i}", "metadata": {"id": i}}


def test_merge_windowed_unions_dedupes_and_marks_without_mutating():
    unfiltered = [_hit("a", 0.9), _hit("b", 0.8)]
    windowed = [_hit("b", 0.8), _hit("w", 0.5)]

    merged = _merge_windowed(unfiltered, windowed)

    assert [r["id"] for r in merged] == ["a", "b", "w"]
    assert [r["metadata"].get("in_time_window", False) for r in merged] == [False, True, True]
    assert "in_time_window" not in unfiltered[1]["metadata"]


def _entry(name: str, score: float, in_window: bool = False) -> dict:
    md = {"in_time_window": True} if in_window else {}
    return {"content": name, "score": score, "store": "vector", "metadata": md}


def test_order_is_plain_score_order_without_a_window():
    entries = [_entry("lo", 0.1), _entry("hi", 0.9), _entry("mid", 0.5)]
    assert [e["content"] for e in _order_results(entries)] == ["hi", "mid", "lo"]


def test_window_lane_takes_every_other_slot_not_all_of_them():
    entries = [
        _entry("u1", 0.9), _entry("u2", 0.8), _entry("u3", 0.7),
        _entry("w1", 0.4, True), _entry("w2", 0.3, True), _entry("w3", 0.2, True),
    ]
    order = [e["content"] for e in _order_results(entries)]
    assert order == ["w1", "u1", "w2", "u2", "w3", "u3"]
    # Variant A (in-window rows first) filled a top-3 with w1, w2, w3 and lost
    # every out-of-window hit; the lanes keep u1 in the top 3.
    assert "u1" in order[:3]


def test_uneven_lanes_drain_the_longer_one():
    entries = [_entry("u1", 0.9), _entry("w1", 0.4, True), _entry("w2", 0.3, True)]
    assert [e["content"] for e in _order_results(entries)] == ["w1", "u1", "w2"]


# ---------------------------------------------------------------------------
# recall wiring
# ---------------------------------------------------------------------------


def _engine(unfiltered, windowed=None, **overrides) -> RAGEngine:
    vector = AsyncMock()

    async def _search(query, **kwargs):
        if "time_window" in kwargs:
            if isinstance(windowed, Exception):
                raise windowed
            return windowed or []
        return unfiltered

    vector.search = AsyncMock(side_effect=_search)
    graph = AsyncMock()
    graph.query_related = AsyncMock(return_value=[])
    graph.query_related_multihop = AsyncMock(return_value=[])
    graph.query_resolutions = AsyncMock(return_value=[])
    settings = get_settings().model_copy(update=overrides)
    return RAGEngine(graph=graph, vector=vector, settings=settings)


def _vhit(i: str, score: float) -> dict:
    return {"id": i, "score": score, "text": f"memory {i}",
            "metadata": {"id": i, "status": "active", "timestamp": ""}}


def _window_calls(engine) -> list:
    return [c for c in engine._vector.search.call_args_list if "time_window" in c.kwargs]


@pytest.mark.asyncio
async def test_a_named_time_adds_a_windowed_search_relative_to_as_of():
    eng = _engine([_vhit("u1", 0.9), _vhit("u2", 0.8)], [_vhit("w1", 0.5)],
                  TEMPORAL_RECALL_ENABLED=True)

    resp = await eng.recall(ContextQuery(
        task="what did I buy 10 days ago", top_k=3, format="raw", as_of=AS_OF,
    ))

    (call,) = _window_calls(eng)
    assert _days(call.kwargs["time_window"]) == (11, 9)
    order = [s.content for s in resp.sources]
    assert order == ["memory w1", "memory u1", "memory u2"]
    assert resp.sources[0].metadata["in_time_window"] is True


@pytest.mark.asyncio
async def test_no_named_time_means_no_second_search_and_unchanged_order():
    eng = _engine([_vhit("u2", 0.8), _vhit("u1", 0.9)], [_vhit("w1", 0.5)],
                  TEMPORAL_RECALL_ENABLED=True)

    resp = await eng.recall(ContextQuery(task="how do we deploy the api", top_k=3, format="raw"))

    assert _window_calls(eng) == []
    assert [s.content for s in resp.sources] == ["memory u1", "memory u2"]


@pytest.mark.asyncio
async def test_disabled_flag_never_searches_a_window():
    eng = _engine([_vhit("u1", 0.9)], [_vhit("w1", 0.5)], TEMPORAL_RECALL_ENABLED=False)
    await eng.recall(ContextQuery(task="what did I buy 10 days ago", top_k=3, format="raw"))
    assert _window_calls(eng) == []


@pytest.mark.asyncio
async def test_a_failing_window_search_neither_fails_nor_degrades_recall():
    eng = _engine([_vhit("u1", 0.9)], RuntimeError("qdrant hiccup"), TEMPORAL_RECALL_ENABLED=True)

    resp = await eng.recall(ContextQuery(task="what did I buy 10 days ago", top_k=3, format="raw"))

    assert [s.content for s in resp.sources] == ["memory u1"]
    assert resp.degraded is False


@pytest.mark.asyncio
async def test_as_of_defaults_to_now():
    eng = _engine([_vhit("u1", 0.9)], [], TEMPORAL_RECALL_ENABLED=True)
    await eng.recall(ContextQuery(task="the deploy yesterday", top_k=3, format="raw"))
    (call,) = _window_calls(eng)
    start, end = call.kwargs["time_window"]
    now = datetime.now(timezone.utc)
    assert start < now - timedelta(days=1) < end <= now + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# vector filter, payload and fields
# ---------------------------------------------------------------------------


def test_time_window_condition_prefers_occurred_at_and_falls_back_to_timestamp():
    start, end = AS_OF - timedelta(days=2), AS_OF
    cond = time_window_condition(start, end)
    occurred, fallback = cond.should
    assert occurred.key == "occurred_at"
    assert occurred.range.gte == start and occurred.range.lte == end
    is_empty, ts = fallback.must
    assert is_empty.is_empty.key == "occurred_at"
    assert ts.key == "timestamp" and ts.range.gte == start


def _vector_client() -> VectorClient:
    settings = get_settings().model_copy(update={"QDRANT_COLLECTION": "t", "EMBEDDING_DIM": 4})
    client = VectorClient(settings)
    client._client = AsyncMock()
    client._http_client = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_search_applies_the_window_filter_only_when_asked():
    vc = _vector_client()
    vc._client.query_points = AsyncMock(return_value=MagicMock(points=[]))
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4):
        await vc.search("q", workspace_id="ws")
        await vc.search("q", workspace_id="ws", time_window=(AS_OF - timedelta(days=1), AS_OF))

    plain, windowed = (c.kwargs["query_filter"].must for c in vc._client.query_points.call_args_list)
    def _has_window(must):
        return any(getattr(c, "should", None) and getattr(c.should[0], "key", None) == "occurred_at"
                   for c in must)
    assert not _has_window(plain)
    assert _has_window(windowed)


@pytest.mark.asyncio
async def test_upsert_promotes_occurred_at_and_search_projects_it():
    vc = _vector_client()
    vc._client.retrieve = AsyncMock(return_value=[])
    vc._client.upsert = AsyncMock()
    with patch.object(vc, "_embed", new_callable=AsyncMock, return_value=[0.1] * 4):
        await vc.upsert(
            text="bought a blender",
            metadata={"workspace_id": "ws", "occurred_at": "2023-05-20T10:00:00+00:00"},
            namespace="default",
        )
    payload = vc._client.upsert.call_args.kwargs["points"][0].payload
    assert payload["occurred_at"] == "2023-05-20T10:00:00+00:00"
    assert "occurred_at" not in payload["metadata"]

    from app.db.vector import _projected_metadata
    assert _projected_metadata(payload, "p1")["occurred_at"] == "2023-05-20T10:00:00+00:00"
    assert "occurred_at" not in _projected_metadata({"text": "x"}, "p2")


def test_naive_datetimes_are_read_as_utc():
    q = ContextQuery(task="x", as_of="2023-05-30T23:40:00")
    assert q.as_of == AS_OF
    a = ActionLog(action="a", outcome="b", occurred_at="2023-05-20T12:00:00+02:00")
    assert a.occurred_at == datetime(2023, 5, 20, 10, 0, tzinfo=timezone.utc)
    assert ContextQuery(task="x").as_of is None
    assert ActionLog(action="a", outcome="b").occurred_at is None


def test_on_by_default_and_compose_agrees():
    """Compose's `${VAR:-x}` fallback wins over the code default when .env is
    silent — a drift would change production recall with no code diff."""
    assert Settings().TEMPORAL_RECALL_ENABLED is True
    text = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    found = re.findall(r"^\s*TEMPORAL_RECALL_ENABLED:\s*\$\{TEMPORAL_RECALL_ENABLED:-([^}]+)\}", text, re.MULTILINE)
    assert found == ["true"]
