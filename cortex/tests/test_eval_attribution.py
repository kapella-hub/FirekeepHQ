"""Living Instructions round 2 — compute_session_eval attribution fields.

The Bridge rides the five X-Firekeep-* headers (and briefing_id) on the
session_start replay payload; compute_session_eval reads them back out of the
timeline it ALREADY loads — no new I/O — into the new optional EvalResult
fields. `agents` comes from get_session_summary, which always computed it and
previously discarded it. `metrics` stays dict[str, float]: attribution is
never a metric.

Old stored records carry none of these fields and must keep parsing — the
30-day TTL plus non-overwriting eval writes mean the store holds round-1
records for a full window after rollout.
"""

from __future__ import annotations

import json

import pytest

from app.evals.models import EvalResult


def _event(event_type: str, payload: dict | None = None, i: int = 0) -> dict:
    return {
        "id": f"event-{i}",
        "event_type": event_type,
        "outcome": None,
        "timestamp": f"2026-08-12T10:{i:02d}:00+00:00",
        "payload": payload or {},
        "context_ref": None,
        "session_id": "s1",
        "agent_id": "default",
    }


ATTRIBUTED_PAYLOAD = {
    "goal": "g",
    "tags": [],
    "briefing_id": "bf_1",
    "runtime": "claude",
    "client_version": "0.1.41",
    "instr_rendered": "aaa111bbb222",
    "instr_expected": "aaa111bbb222",
    "instr_gateway": "ccc333ddd444",
}


async def _compute(monkeypatch, events, agents=("default",)):
    import fakeredis.aioredis
    import replay.reader as reader_mod
    from unittest.mock import AsyncMock
    from app.evals import compute as compute_mod

    # Task 4: compute_session_eval's metrics scan (and find_terminal_grade's
    # grade lift) read through get_session_event_ids/get_event_batch now —
    # serve `events` from both, keyed by each event's own "id" (set by
    # `_event` above).
    async def fake_summary(*args, **kwargs):
        return {"event_count": max(len(events), 1), "duration_ms": 1000,
                "agents": list(agents)}

    async def fake_ids(*args, **kwargs):
        return [e["id"] for e in events if e.get("id")]

    async def fake_batch(r, ids):
        by_id = {e["id"]: e for e in events if e.get("id")}
        return [by_id[i] for i in ids if i in by_id]

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)
    monkeypatch.setattr(compute_mod.aioredis, "from_url",
                        lambda *a, **k: AsyncMock())
    monkeypatch.setattr("app.webhooks.fire_webhooks", AsyncMock())
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        return await compute_mod.compute_session_eval(r, "s1")
    finally:
        await r.aclose()


@pytest.mark.asyncio
async def test_attributed_session_populates_every_field(monkeypatch):
    events = [
        _event("session_start", dict(ATTRIBUTED_PAYLOAD), 0),
        _event("memory_read", {}, 1),
    ]
    result = await _compute(monkeypatch, events, agents=("alice", "bob"))

    assert result is not None
    assert result.runtime == "claude"
    assert result.client_version == "0.1.41"
    assert result.instructions == {
        "rendered": "aaa111bbb222",
        "expected": "aaa111bbb222",
        "gateway": "ccc333ddd444",
    }
    assert result.briefing_delivered is True
    assert result.agents == ["alice", "bob"]


@pytest.mark.asyncio
async def test_partial_headers_keep_only_the_keys_that_arrived(monkeypatch):
    payload = {"goal": "g", "tags": [], "briefing_id": "",
               "instr_gateway": "ccc333ddd444"}
    result = await _compute(monkeypatch, [_event("session_start", payload)])

    assert result is not None
    assert result.instructions == {"gateway": "ccc333ddd444"}
    assert result.runtime is None
    assert result.client_version is None


@pytest.mark.asyncio
async def test_unattributed_session_start_reads_honestly(monkeypatch):
    """A pre-0.1.41 session HAS a session_start event but no attribution
    keys: fields None, instructions None (never {}), and briefing_delivered
    False — the briefing_id KEY is present and empty, so the bridge measured
    "no briefing" and False is a real measurement, not missing data."""
    payload = {"goal": "g", "tags": [], "briefing_id": ""}
    result = await _compute(monkeypatch, [_event("session_start", payload)])

    assert result is not None
    assert result.runtime is None
    assert result.client_version is None
    assert result.instructions is None
    assert result.briefing_delivered is False


@pytest.mark.asyncio
async def test_missing_briefing_key_is_unknown_not_false(monkeypatch):
    """A session_start payload from a PRE-round-2 bridge carries no
    briefing_id key at all — the old emitter sent only {goal, tags}. Reading
    that absence as a measured False is exactly the absent-vs-measured
    conflation the contract bans (external review 2026-08-12): the bridge may
    well have delivered a briefing; the receipt just never rode the payload.
    Key absent -> None, never False."""
    payload = {"goal": "g", "tags": []}
    result = await _compute(monkeypatch, [_event("session_start", payload)])

    assert result is not None
    assert result.briefing_delivered is None


@pytest.mark.asyncio
async def test_no_session_start_event_means_briefing_unknown(monkeypatch):
    """No session_start at all (round-1 emitters, truncated timelines): the
    briefing receipt is UNKNOWN, not False — None is the only honest value."""
    result = await _compute(
        monkeypatch, [_event("memory_read")], agents=("default",)
    )

    assert result is not None
    assert result.briefing_delivered is None
    assert result.runtime is None
    assert result.instructions is None
    assert result.agents == ["default"]


@pytest.mark.asyncio
async def test_malformed_attribution_values_read_as_absent(monkeypatch):
    """Per-record isolation: junk on the wire must degrade to unattributed,
    never crash the eval."""
    payload = {
        "goal": "g", "tags": [], "briefing_id": None,
        "runtime": 42, "client_version": ["0.1.41"],
        "instr_rendered": {"h": "x"}, "instr_expected": "",
        "instr_gateway": "ok-hash",
    }
    result = await _compute(monkeypatch, [_event("session_start", payload)])

    assert result is not None
    assert result.runtime is None
    assert result.client_version is None
    assert result.instructions == {"gateway": "ok-hash"}
    assert result.briefing_delivered is False


@pytest.mark.asyncio
async def test_malformed_payload_and_agents_do_not_crash(monkeypatch):
    result = await _compute(
        monkeypatch,
        [_event("session_start", None) | {"payload": "not-a-dict"}],
        agents=("ok", 42, None),
    )

    assert result is not None
    assert result.briefing_delivered is None, (
        "a session_start whose payload is junk carries no briefing receipt"
    )
    assert result.agents == ["ok"]


@pytest.mark.asyncio
async def test_attribution_never_leaks_into_metrics(monkeypatch):
    """`metrics` stays dict[str, float] — attribution is never a metric."""
    events = [
        _event("session_start", dict(ATTRIBUTED_PAYLOAD), 0),
        _event("memory_read", {}, 1),
    ]
    result = await _compute(monkeypatch, events)

    assert result is not None
    for forbidden in ("runtime", "client_version", "instructions",
                      "briefing_delivered", "agents", "instr_rendered"):
        assert forbidden not in result.metrics, forbidden
    assert all(isinstance(v, float) for v in result.metrics.values())


def test_old_stored_records_keep_parsing():
    """A round-1 record (no attribution fields) must validate with defaults —
    nothing backfills, and the store holds these for a full TTL window."""
    old = json.dumps({
        "session_id": "s-old",
        "created_at": "2026-08-01T00:00:00+00:00",
        "trigger": "session_complete",
        "metrics": {"memory_read_count": 2.0},
        "event_count": 5,
    })
    result = EvalResult.model_validate_json(old)
    assert result.runtime is None
    assert result.client_version is None
    assert result.instructions is None
    assert result.briefing_delivered is None
    assert result.agents == []


def test_new_fields_round_trip_through_the_store_shape():
    result = EvalResult(
        session_id="s1",
        trigger="session_complete",
        runtime="kiro",
        client_version="0.1.41",
        instructions={"rendered": "a", "expected": "a"},
        briefing_delivered=False,
        agents=["default"],
    )
    parsed = EvalResult.model_validate_json(result.model_dump_json())
    assert parsed.runtime == "kiro"
    assert parsed.instructions == {"rendered": "a", "expected": "a"}
    assert parsed.briefing_delivered is False
    assert parsed.agents == ["default"]
