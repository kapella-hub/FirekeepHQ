"""`narrow()` must distinguish "no cause" from "no such event" from "no data".

WHY THIS EXISTS. An empty `suspects` list had three causes and one shape, so
`replay_narrow` gave a byte-identical answer for a real event id and for
`deadbeefdeadbeefdeadbeefdeadbeef` — echoing the fabricated id back with the
same confident sentence, "The failure may have no trace links to follow."

That was not a cosmetic ambiguity on this deployment. A census of the 3,000
most recent events in `rp:events` (27,305 total) found `parent_span_id`
populated 0 times and `trace_links` populated 0 times, with every event
carrying `trace_id == span_id == id`. The emitters never populate the fields
this algorithm walks, so the tool's single message was reporting "no cause
found" about a feature that had nothing to read — the one thing a debugging
tool must never do.

These are unit tests against `get_event` / `get_session_timeline` doubles
rather than a live Redis, because the defect is in what `narrow` REPORTS, not
in how it reads.
"""

from __future__ import annotations

import pytest

from replay import narrowing


@pytest.fixture
def stub_reader(monkeypatch):
    """Install controllable get_event / get_session_timeline doubles."""

    state = {"events": {}, "timeline": []}

    async def _get_event(r, event_id):
        return state["events"].get(event_id)

    async def _get_timeline(r, session_id, **kwargs):
        return {"events": state["timeline"], "total": len(state["timeline"])}

    monkeypatch.setattr(narrowing, "get_event", _get_event)
    monkeypatch.setattr(narrowing, "get_session_timeline", _get_timeline)
    return state


def _event(eid, *, links=None, ts="2026-08-06T10:00:00+00:00"):
    return {
        "id": eid,
        "event_type": "memory_read",
        "timestamp": ts,
        "payload": {},
        "trace_links": links or [],
    }


@pytest.mark.asyncio
async def test_unknown_event_id_is_reported_as_unknown(stub_reader):
    """A fabricated id must not get the same answer as a real one."""
    result = await narrowing.narrow(None, "sess-1", "deadbeef" * 4)
    assert result["suspects"] == []
    assert result["failure_event_found"] is False


@pytest.mark.asyncio
async def test_linkless_session_is_reported_as_having_no_links(stub_reader):
    """The live case: the event exists, the session records no trace links.

    This is missing instrumentation, and saying "no cause found" about it is a
    false negative dressed as a result.
    """
    stub_reader["events"]["e1"] = _event("e1")
    stub_reader["timeline"] = [_event("e1"), _event("e2")]

    result = await narrowing.narrow(None, "sess-1", "e1")

    assert result["failure_event_found"] is True
    assert result["session_has_trace_links"] is False


@pytest.mark.asyncio
async def test_session_with_links_is_reported_as_having_them(stub_reader):
    """A genuinely linked session must report `True`, so a real "walked and
    found nothing" stays distinguishable from "nothing to walk"."""
    linked = _event("e2", links=[{"target_event_id": "e3", "confidence": 0.9}])
    stub_reader["events"] = {"e1": _event("e1"), "e2": linked}
    stub_reader["timeline"] = [_event("e1"), linked]

    result = await narrowing.narrow(None, "sess-1", "e1")

    assert result["failure_event_found"] is True
    assert result["session_has_trace_links"] is True


@pytest.mark.asyncio
async def test_link_census_failure_degrades_quietly(monkeypatch, stub_reader):
    """An unreadable timeline must not take down narrowing.

    Reporting False only makes the caller's message more cautious ("no links
    recorded"), never less — the safe direction for a diagnostic.
    """
    async def _boom(*args, **kwargs):
        raise RuntimeError("redis down")

    stub_reader["events"]["e1"] = _event("e1")
    monkeypatch.setattr(narrowing, "get_session_timeline", _boom)

    result = await narrowing.narrow(None, "sess-1", "e1")

    assert result["failure_event_found"] is True
    assert result["session_has_trace_links"] is False
