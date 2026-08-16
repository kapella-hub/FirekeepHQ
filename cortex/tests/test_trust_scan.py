"""The bounded stream read behind the trust ledger (spec §2).

Reads the LATEST cap+1 gateway events in the window, newest-first;
cap+1 returned => truncated. Invalid entries are COUNTED, not dropped."""
import json
import pytest
import fakeredis.aioredis

from app.autopilot import trust


async def _xadd(r, *, etype, agent, session, action_id, ts_ms, payload=None):
    fields = {
        "id": f"e{ts_ms}", "session_id": session, "agent_id": agent,
        "event_type": etype, "timestamp": "2026-08-16T00:00:00+00:00",
        "payload": json.dumps({"action_id": action_id, **(payload or {})}),
        "outcome": "",
    }
    await r.xadd("rp:events", fields, id=f"{ts_ms}-0")


@pytest.fixture
def r():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_keeps_only_gateway_types(r):
    now_ms = 1_800_000_000_000
    await _xadd(r, etype="memory.read", agent="a", session="s", action_id="x", ts_ms=now_ms)
    await _xadd(r, etype="agent.action.predict", agent="a", session="s", action_id="p1", ts_ms=now_ms)
    events, scanned, truncated, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    types = {e["event_type"] for e in events}
    assert types == {"agent.action.predict"}
    assert scanned >= 2 and truncated is False


@pytest.mark.asyncio
async def test_cap_vs_cap_plus_one(r):
    now_ms = 1_800_000_000_000
    for i in range(6):
        await _xadd(r, etype="agent.action.predict", agent="a", session="s",
                    action_id=f"p{i}", ts_ms=now_ms + i)
    # cap 6 -> exactly 6, not truncated
    _, _, trunc6, _ = await trust.scan_gateway_events(r, window_days=3650, cap=6)
    assert trunc6 is False
    # cap 5 -> read 6 (cap+1) -> truncated
    _, _, trunc5, _ = await trust.scan_gateway_events(r, window_days=3650, cap=5)
    assert trunc5 is True


@pytest.mark.asyncio
async def test_invalid_counted_not_dropped(r):
    now_ms = 1_800_000_000_000
    # An unattributable PREDICT (blank agent) and a malformed reconcile payload.
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "",
                               "session_id": "s", "payload": json.dumps({"action_id": "p"}),
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms}-0")
    await r.xadd("rp:events", {"event_type": "agent.action.reconcile", "agent_id": "a",
                               "session_id": "s", "payload": "{not json",
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms+1}-0")
    events, _, _, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    assert events == []
    assert invalid["unattributed_predict"] == 1
    assert invalid["malformed"] == 1


@pytest.mark.asyncio
async def test_blank_agent_reconcile_is_kept_for_pairing(r):
    """A reconcile's OWN agent_id is irrelevant — it is attributed to the
    declaring agent by action_id in build_rows. The gateway emits agent_id=""
    on a reconcile whose predict RECORD expired; keeping it recovered ~99% of
    real reconciliations that the ledger had been discarding (measured live)."""
    now_ms = 1_800_000_000_000
    await r.xadd("rp:events", {"event_type": "agent.action.reconcile", "agent_id": "",
                               "session_id": "", "payload": json.dumps({"action_id": "p1"}),
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms}-0")
    events, _, _, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    assert len(events) == 1  # kept, not rejected
    assert events[0]["event_type"] == "agent.action.reconcile"
    assert invalid["unattributed_predict"] == 0


@pytest.mark.asyncio
async def test_out_of_window_events_excluded(r):
    """The xrevrange min bound drops events older than the window at the
    STREAM level — the cohort filter never sees them. Anchored to REAL now,
    because scan_gateway_events computes the window from datetime.now()."""
    from datetime import datetime, timezone
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    old_ms = now_ms - 40 * 86400 * 1000  # 40 days ago, outside a 30d window
    await _xadd(r, etype="agent.action.predict", agent="a", session="s",
                action_id="old", ts_ms=old_ms)
    await _xadd(r, etype="agent.action.predict", agent="a", session="s",
                action_id="fresh", ts_ms=now_ms)
    events, _, _, _ = await trust.scan_gateway_events(r, window_days=30, cap=100)
    ids = {e["action_id"] for e in events}
    assert "fresh" in ids and "old" not in ids


@pytest.mark.asyncio
async def test_missing_action_id_and_bad_timestamp_counted(r):
    """missing_action_id and bad_timestamp each counted; a PREDICT with a blank
    session is NOT invalid (a real agent declared it) — it is kept, and its
    blank session simply does not contribute to the session count."""
    now_ms = 1_800_000_000_000
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "a",
                               "session_id": "", "payload": json.dumps({"action_id": "keep"}),
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms}-0")
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "a",
                               "session_id": "s", "payload": json.dumps({}),  # no action_id
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms+1}-0")
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "a",
                               "session_id": "s", "payload": json.dumps({"action_id": "p"}),
                               "timestamp": "not-a-timestamp"}, id=f"{now_ms+2}-0")
    events, _, _, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    assert {e["action_id"] for e in events} == {"keep"}  # blank-session predict kept
    assert invalid["missing_action_id"] == 1
    assert invalid["bad_timestamp"] == 1


@pytest.mark.asyncio
async def test_non_dict_payload_counted_malformed(r):
    """Valid JSON that is not an object (a bare null/scalar/list) is malformed,
    not a crash on payload.get."""
    now_ms = 1_800_000_000_000
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "a",
                               "session_id": "s", "payload": "null",  # valid JSON, not a dict
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms}-0")
    events, _, _, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    assert events == []
    assert invalid["malformed"] == 1
