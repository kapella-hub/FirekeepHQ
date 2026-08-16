# Trust Ledger Round 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A per-agent trust ledger — declared/reconciled counts, prediction-match calibration, reversals, sessions — aggregated on demand from the `rp:events` replay stream and surfaced at `GET /autopilot/trust` and a dashboard card, visibility-only.

**Architecture:** One new module `cortex/app/autopilot/trust.py` in the `compliance.py` mold: a bounded stream read (`scan_gateway_events`) feeds pure aggregation (`build_rows`) behind an orchestrator (`build_trust`). A route on the existing autopilot router and a render function in the dashboard's `autopilotPanel` sentinel block. No writes, no new event types, no LLM, no new dependencies.

**Tech Stack:** Python 3.11 / FastAPI / redis-py async (`xrevrange`), the existing `app.evals.scorers.brier_score`, pytest with `fakeredis.aioredis`, and node-executed dashboard render tests.

## Global Constraints

- **Visibility only — reports, never gates.** No gating, promotion, or thresholds that block anything. (spec §1)
- **Frozen formulas.** The definitions in spec §3 are frozen at birth; implement them exactly, do not "improve" them.
- **Deployment-global, no workspace parameter.** Matches `build_compliance(replay_redis)`; replay events carry no `workspace_id`. (spec §5)
- **Truncation nulls the biased metrics.** Under `truncated`, `reconciliation_rate`, `calibration`, `calibration_trend`, `first_seen_in_window` are `null`; `declared`/`reconciled`/`reversals`/`scored_predictions`/`sessions` stay lower bounds; `last_seen_in_window` survives. (spec §2)
- **Invalid input is counted, never silently dropped** — a visible top-level `invalid` breakdown. (spec §3)
- **Frozen constants** (module constants in `trust.py`, mirroring `compliance.SCAN_CAP`): `TRUST_WINDOW_DAYS = 30`, `TRUST_SCAN_CAP = 50000`, `TRUST_MIN_CALIBRATION_POINTS = 5`.
- `replay_redis` is `decode_responses=True` (the reader treats stream fields as `str`); write for `str` fields.
- Run: `cd cortex && python -m pytest tests/<file> -q` (set `AUTH_ENABLED=false` if a test drives the router through a bare app). Full suite before final commit: `cd cortex && python -m pytest tests/ -q` and `python -m pytest tests/ -q` (repo root).

---

### Task 1: `scan_gateway_events` — the bounded stream read

**Files:**
- Create: `cortex/app/autopilot/trust.py`
- Test: `cortex/tests/test_trust_scan.py`

**Interfaces:**
- Produces: `async def scan_gateway_events(replay_redis, window_days=TRUST_WINDOW_DAYS, cap=TRUST_SCAN_CAP) -> tuple[list[dict], int, bool, dict[str,int]]` — returns `(events, scanned, truncated, invalid)`. `events` are parsed dicts `{event_type, agent_id, session_id, action_id, ts, payload}` for `agent.action.predict`/`reconcile` only; `invalid` is `{blank_agent, blank_session, missing_action_id, malformed, bad_timestamp}`.
- Produces module constants: `TRUST_WINDOW_DAYS = 30`, `TRUST_SCAN_CAP = 50000`, `TRUST_MIN_CALIBRATION_POINTS = 5`, `_STREAM_KEY = "rp:events"`, `_GATEWAY_TYPES = {"agent.action.predict", "agent.action.reconcile"}`.

- [ ] **Step 1: Write the failing test**

```python
# cortex/tests/test_trust_scan.py
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
    # blank agent, and malformed payload
    await r.xadd("rp:events", {"event_type": "agent.action.predict", "agent_id": "",
                               "session_id": "s", "payload": json.dumps({"action_id": "p"}),
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms}-0")
    await r.xadd("rp:events", {"event_type": "agent.action.reconcile", "agent_id": "a",
                               "session_id": "s", "payload": "{not json",
                               "timestamp": "2026-08-16T00:00:00+00:00"}, id=f"{now_ms+1}-0")
    events, _, _, invalid = await trust.scan_gateway_events(r, window_days=3650, cap=100)
    assert events == []
    assert invalid["blank_agent"] == 1
    assert invalid["malformed"] == 1
```

- [ ] **Step 2: Run — expect FAIL** (`cd cortex && python -m pytest tests/test_trust_scan.py -q`; ModuleNotFoundError)

- [ ] **Step 3: Implement `scan_gateway_events`**

```python
# cortex/app/autopilot/trust.py
"""Trust Ledger round 1 (spec docs/superpowers/specs/2026-08-16-trust-ledger-round1-design.md).

Per-agent employment record from gateway declarations already in replay.
Visibility only — reports, never gates. Same discipline as compliance.py:
a bounded scan, invalids counted not dropped, honest truncation. The frozen
formulas live in build_rows and MUST NOT drift (they are pre-registered)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.evals.scorers import brier_score

logger = logging.getLogger(__name__)

TRUST_WINDOW_DAYS = 30
TRUST_SCAN_CAP = 50000
TRUST_MIN_CALIBRATION_POINTS = 5

_STREAM_KEY = "rp:events"
_GATEWAY_TYPES = {"agent.action.predict", "agent.action.reconcile"}


def _empty_invalid() -> dict[str, int]:
    return {"blank_agent": 0, "blank_session": 0, "missing_action_id": 0,
            "malformed": 0, "bad_timestamp": 0}


async def scan_gateway_events(replay_redis, window_days: int = TRUST_WINDOW_DAYS,
                              cap: int = TRUST_SCAN_CAP):
    """Latest cap+1 gateway events within the window, newest-first.

    Returns (events, scanned, truncated, invalid). cap+1 returned => the
    window holds more than the cap => truncated (spec §2). One bad entry is
    COUNTED in `invalid`, never allowed to blank the table."""
    now = datetime.now(timezone.utc)
    window_start_ms = int((now - timedelta(days=window_days)).timestamp() * 1000)
    invalid = _empty_invalid()
    events: list[dict] = []
    scanned = 0
    try:
        rows = await replay_redis.xrevrange(
            _STREAM_KEY, max="+", min=f"{window_start_ms}-0", count=cap + 1)
    except Exception as exc:  # noqa: BLE001 — a read failure must not 500 the card
        logger.warning("trust scan failed: %s", exc)
        return [], 0, False, invalid
    truncated = len(rows) > cap
    for stream_id, fields in rows[:cap]:
        scanned += 1
        etype = fields.get("event_type", "")
        if etype not in _GATEWAY_TYPES:
            continue
        agent = fields.get("agent_id", "")
        session = fields.get("session_id", "")
        if not agent:
            invalid["blank_agent"] += 1
            continue
        if not session:
            invalid["blank_session"] += 1
            continue
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            invalid["malformed"] += 1
            continue
        action_id = payload.get("action_id")
        if not action_id:
            invalid["missing_action_id"] += 1
            continue
        ts = _parse_ts(fields.get("timestamp"))
        if ts is None:
            invalid["bad_timestamp"] += 1
            continue
        events.append({"event_type": etype, "agent_id": agent,
                       "session_id": session, "action_id": action_id,
                       "ts": ts, "payload": payload})
    return events, scanned, truncated, invalid


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** (`git add cortex/app/autopilot/trust.py cortex/tests/test_trust_scan.py && git commit -m "feat(autopilot): trust ledger bounded stream scan (round 1, spec §2)"`)

---

### Task 2: `build_rows` + `build_trust` — the frozen aggregation

**Files:**
- Modify: `cortex/app/autopilot/trust.py`
- Test: `cortex/tests/test_trust_rows.py`

**Interfaces:**
- Consumes: `scan_gateway_events`, `brier_score`, `TRUST_MIN_CALIBRATION_POINTS`.
- Produces: `def build_rows(events: list[dict], truncated: bool) -> list[dict]` — per-agent rows per spec §3.
- Produces: `async def build_trust(replay_redis) -> dict` — `{agents, window_days, scanned, truncated, invalid, generated_at}`.

Row shape (every key present; biased ones `None` under truncation):
`{agent_id, declared, reconciled, reconciliation_rate, scored_predictions, calibration, calibration_trend, reversals, sessions, first_seen_in_window, last_seen_in_window}`.

- [ ] **Step 1: Write the failing test**

```python
# cortex/tests/test_trust_rows.py
"""The frozen aggregation (spec §3). The cohort is in-window PREDICTS;
reconciles pair by action_id to the declaring agent; rate never > 100%;
calibration is prediction-match Brier; truncation nulls biased metrics."""
from datetime import datetime, timezone

from app.autopilot import trust
from app.evals.scorers import brier_score


def _predict(agent, action_id, ts, confidence=None):
    payload = {"action_id": action_id}
    if confidence is not None:
        payload["prediction"] = {"confidence": confidence}
    else:
        payload["prediction"] = None
    return {"event_type": "agent.action.predict", "agent_id": agent,
            "session_id": "s", "action_id": action_id,
            "ts": ts, "payload": payload}


def _reconcile(agent, action_id, ts, *, success=True, match=None):
    return {"event_type": "agent.action.reconcile", "agent_id": agent,
            "session_id": "s", "action_id": action_id, "ts": ts,
            "payload": {"action_id": action_id,
                        "outcome": {"success": success},
                        "prediction_match_score": match}}


T0 = datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_rate_never_exceeds_100_percent():
    # A reconcile whose declaration is OUTSIDE the cohort must not count.
    events = [
        _predict("a", "p1", T0, confidence=0.9),
        _reconcile("a", "p1", T0, match=0.9),
        _reconcile("a", "ORPHAN", T0, match=1.0),  # no in-window predict
    ]
    rows = trust.build_rows(events, truncated=False)
    row = next(r for r in rows if r["agent_id"] == "a")
    assert row["declared"] == 1
    assert row["reconciled"] == 1
    assert row["reconciliation_rate"] == 1.0  # not 2.0


def test_declared_includes_null_prediction_but_scored_does_not():
    events = [
        _predict("a", "p1", T0, confidence=None),   # declaration, no prediction
        _reconcile("a", "p1", T0, match=None),
        _predict("a", "p2", T0, confidence=0.8),
        _reconcile("a", "p2", T0, match=0.7),
    ]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["declared"] == 2
    assert row["scored_predictions"] == 1  # only p2 has confidence+score


def test_calibration_matches_brier_score():
    events = [_predict("a", f"p{i}", T0, confidence=0.8) for i in range(6)] + \
             [_reconcile("a", f"p{i}", T0, match=0.6) for i in range(6)]
    row = trust.build_rows(events, truncated=False)[0]
    expected = brier_score([{"prediction_confidence": 0.8,
                             "prediction_match_score": 0.6}] * 6)
    assert row["calibration"] == expected


def test_calibration_null_below_min_points():
    events = [_predict("a", "p1", T0, confidence=0.8),
              _reconcile("a", "p1", T0, match=0.6)]  # 1 < MIN(5)
    row = trust.build_rows(events, truncated=False)[0]
    assert row["scored_predictions"] == 1
    assert row["calibration"] is None


def test_reversals_count_success_false_only():
    events = [_predict("a", "p1", T0, confidence=0.5),
              _reconcile("a", "p1", T0, success=False, match=0.5),
              _predict("a", "p2", T0, confidence=0.5),
              _reconcile("a", "p2", T0, success=True, match=0.5)]
    row = trust.build_rows(events, truncated=False)[0]
    assert row["reversals"] == 1


def test_truncation_nulls_biased_metrics_keeps_counts():
    events = [_predict("a", f"p{i}", T0, confidence=0.8) for i in range(6)] + \
             [_reconcile("a", f"p{i}", T0, match=0.6) for i in range(6)]
    row = trust.build_rows(events, truncated=True)[0]
    assert row["declared"] == 6 and row["reconciled"] == 6  # lower bounds kept
    assert row["reconciliation_rate"] is None
    assert row["calibration"] is None
    assert row["calibration_trend"] is None
    assert row["first_seen_in_window"] is None
    assert row["last_seen_in_window"] is not None  # newest survives


def test_no_declaration_no_row():
    events = [_reconcile("ghost", "x", T0, match=1.0)]  # reconcile only
    rows = trust.build_rows(events, truncated=False)
    assert all(r["agent_id"] != "ghost" for r in rows)
```

- [ ] **Step 2: Run — expect FAIL**

- [ ] **Step 3: Implement `build_rows` + `build_trust`**

```python
# append to cortex/app/autopilot/trust.py
def build_rows(events: list[dict], truncated: bool) -> list[dict]:
    """Frozen per-agent aggregation (spec §3). The cohort is in-window
    PREDICTS; reconciles pair by action_id to the DECLARING agent. Under
    truncation the biased metrics are null (rate/calibration/trend/first_seen)."""
    # action_id -> (declaring agent, predict confidence or None)
    declared: dict[str, tuple[str, float | None]] = {}
    per_agent: dict[str, dict] = {}

    def _row(agent: str) -> dict:
        return per_agent.setdefault(agent, {
            "agent_id": agent, "declared": 0, "reconciled": 0,
            "reversals": 0, "sessions": set(), "ts": [],
            "scored": [],  # {prediction_confidence, prediction_match_score, ts}
        })

    for e in events:
        if e["event_type"] == "agent.action.predict":
            agent = e["agent_id"]
            pred = e["payload"].get("prediction") or {}
            conf = pred.get("confidence") if isinstance(pred, dict) else None
            declared[e["action_id"]] = (agent, conf)
            row = _row(agent)
            row["declared"] += 1
            row["sessions"].add(e["session_id"])
            row["ts"].append(e["ts"])

    for e in events:
        if e["event_type"] != "agent.action.reconcile":
            continue
        owner = declared.get(e["action_id"])
        if owner is None:
            continue  # orphan reconcile — outside the cohort
        agent, conf = owner
        row = _row(agent)
        row["reconciled"] += 1
        row["sessions"].add(e["session_id"])
        row["ts"].append(e["ts"])
        outcome = e["payload"].get("outcome") or {}
        if outcome.get("success") is False:
            row["reversals"] += 1
        match = e["payload"].get("prediction_match_score")
        if conf is not None and match is not None:
            row["scored"].append({"prediction_confidence": conf,
                                  "prediction_match_score": match, "ts": e["ts"]})

    out: list[dict] = []
    for agent, row in per_agent.items():
        declared_n = row["declared"]
        scored = row["scored"]
        ts_all = sorted(row["ts"])
        rate = None if (truncated or declared_n == 0) else round(row["reconciled"] / declared_n, 4)
        calibration = _calibration(scored, truncated)
        trend = _trend(scored, truncated)
        out.append({
            "agent_id": agent,
            "declared": declared_n,
            "reconciled": row["reconciled"],
            "reconciliation_rate": rate,
            "scored_predictions": len(scored),
            "calibration": calibration,
            "calibration_trend": trend,
            "reversals": row["reversals"],
            "sessions": len(row["sessions"]),
            "first_seen_in_window": None if truncated else (ts_all[0].isoformat() if ts_all else None),
            "last_seen_in_window": ts_all[-1].isoformat() if ts_all else None,
        })
    out.sort(key=lambda r: (-r["declared"], r["agent_id"]))
    return out


def _calibration(scored: list[dict], truncated: bool) -> float | None:
    if truncated or len(scored) < TRUST_MIN_CALIBRATION_POINTS:
        return None
    return brier_score(scored)


def _trend(scored: list[dict], truncated: bool) -> float | None:
    if truncated or len(scored) < TRUST_MIN_CALIBRATION_POINTS:
        return None
    ordered = sorted(scored, key=lambda s: s["ts"])
    mid = ordered[len(ordered) // 2]["ts"]
    older = [s for s in ordered if s["ts"] < mid]
    newer = [s for s in ordered if s["ts"] >= mid]
    if len(older) < TRUST_MIN_CALIBRATION_POINTS or len(newer) < TRUST_MIN_CALIBRATION_POINTS:
        return None
    b_old, b_new = brier_score(older), brier_score(newer)
    if b_old is None or b_new is None:
        return None
    return round(b_new - b_old, 4)  # negative = improving (lower Brier better)


async def build_trust(replay_redis) -> dict:
    """The GET /autopilot/trust body (spec §5). Deployment-global, no principal."""
    events, scanned, truncated, invalid = await scan_gateway_events(replay_redis)
    return {
        "agents": build_rows(events, truncated),
        "window_days": TRUST_WINDOW_DAYS,
        "scanned": scanned,
        "truncated": truncated,
        "invalid": invalid,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
```

- [ ] **Step 4: Run — expect PASS** (both trust test files)
- [ ] **Step 5: Commit** (`git add cortex/app/autopilot/trust.py cortex/tests/test_trust_rows.py && git commit -m "feat(autopilot): trust ledger frozen aggregation — cohort, calibration, reversals (spec §3)"`)

---

### Task 3: `GET /autopilot/trust` route

**Files:**
- Modify: `cortex/app/autopilot/api.py` (import `trust as trust_mod`; add the route next to `/compliance`)
- Test: `cortex/tests/test_trust_api.py`

**Interfaces:**
- Consumes: `trust_mod.build_trust`, the router's existing `admin_dep` + `get_replay_redis`.
- Produces: `GET /autopilot/trust` → `build_trust` body; admin-scoped; no workspace param.

- [ ] **Step 1: Write the failing test**

```python
# cortex/tests/test_trust_api.py
"""GET /autopilot/trust — admin-gated, deployment-global, additive."""
import pytest
import fakeredis.aioredis
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.autopilot.api import create_autopilot_router


@pytest.fixture
def client():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app = FastAPI()
    app.include_router(create_autopilot_router(
        get_redis=lambda: r, get_replay_redis=lambda: r,
        get_vector=lambda: None, settings_fn=lambda: None))
    return TestClient(app)


def test_trust_endpoint_shape(client):
    resp = client.get("/autopilot/trust")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"agents", "window_days", "scanned",
                         "truncated", "invalid", "generated_at"}
    assert body["window_days"] == 30
    assert isinstance(body["agents"], list)
```

(Run with `AUTH_ENABLED=false` so the bare app's `admin_dep` is empty — the compliance tests do the same.)

- [ ] **Step 2: Run — expect FAIL** (404 — route absent)
- [ ] **Step 3: Implement** — in `cortex/app/autopilot/api.py`, add `from app.autopilot import trust as trust_mod` beside the `compliance` import, and after the `/compliance` route:

```python
    @router.get("/trust", dependencies=admin_dep)
    async def trust():
        """Trust Ledger round 1: per-agent declared/reconciled/calibration/
        reversals from replay gateway events. Visibility only — reports,
        never gates. Deployment-global like /compliance (no workspace param);
        replay events carry no workspace_id (see the design spec §5)."""
        return await trust_mod.build_trust(get_replay_redis())
```

- [ ] **Step 4: Run — expect PASS**
- [ ] **Step 5: Commit** (`git add cortex/app/autopilot/api.py cortex/tests/test_trust_api.py && git commit -m "feat(autopilot): GET /autopilot/trust route (spec §5)"`)

---

### Task 4: dashboard trust card + docs

**Files:**
- Modify: `dashboard/index.html` — a `renderAutopilotTrust(d)` inside the `autopilotPanel` sentinel block (`>>> autopilotPanel` … `<<< autopilotPanel`); an `autopilotTrust` container div beside `autopilotCompliance` (~line 1503); a `loadTrust()` fetch wired where `loadCompliance` is called.
- Modify: `docs/guides/knowledge-autopilot.md` — a Trust Ledger row/paragraph in the Autopilot surface section (the change-consistency checklist's guide row), naming the three frozen constants and the honesty notes.
- Test: `tests/test_dashboard_autopilot.py` (extend — it already executes the `autopilotPanel` block under node)

**Interfaces:**
- Consumes: the `GET /autopilot/trust` body shape from Task 2/3.
- Produces: `renderAutopilotTrust(data)` — a pure function of the response, returning HTML; truncation banner; null biased-metric → `—` with reason; empty → "no agent has declared an action yet".

- [ ] **Step 1: Write the failing test** — add to `tests/test_dashboard_autopilot.py`, following its existing `_render`/node-extraction harness for the `autopilotPanel` block:

```python
class TestTrustCard:
    def test_rows_render_with_components(self):
        data = {"agents": [{"agent_id": "agent-x", "declared": 214, "reconciled": 205,
                            "reconciliation_rate": 0.96, "scored_predictions": 180,
                            "calibration": 0.11, "calibration_trend": -0.03, "reversals": 3,
                            "sessions": 28, "first_seen_in_window": "2026-08-01T00:00:00+00:00",
                            "last_seen_in_window": "2026-08-16T00:00:00+00:00"}],
                "window_days": 30, "scanned": 900, "truncated": False,
                "invalid": {"blank_agent": 0, "blank_session": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "2026-08-16T00:00:00+00:00"}
        html = render_trust(data)  # helper in the test that extracts renderAutopilotTrust
        assert "agent-x" in html and "214" in html and "no agent" not in html

    def test_null_calibration_shows_dash_not_zero(self):
        data = {"agents": [{"agent_id": "a", "declared": 3, "reconciled": 2,
                            "reconciliation_rate": None, "scored_predictions": 1,
                            "calibration": None, "calibration_trend": None, "reversals": 0,
                            "sessions": 1, "first_seen_in_window": None,
                            "last_seen_in_window": "2026-08-16T00:00:00+00:00"}],
                "window_days": 30, "scanned": 3, "truncated": False,
                "invalid": {"blank_agent": 0, "blank_session": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        html = render_trust(data)
        assert "—" in html and ">0<" not in html.split("agent")[1][:200]

    def test_truncation_banner(self):
        data = {"agents": [], "window_days": 30, "scanned": 50000, "truncated": True,
                "invalid": {"blank_agent": 0, "blank_session": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        assert "truncat" in render_trust(data).lower()

    def test_empty_says_no_declarations(self):
        data = {"agents": [], "window_days": 30, "scanned": 0, "truncated": False,
                "invalid": {"blank_agent": 0, "blank_session": 0, "missing_action_id": 0,
                            "malformed": 0, "bad_timestamp": 0}, "generated_at": "x"}
        assert "no agent" in render_trust(data).lower()
```

Add a `render_trust(data)` helper in the test that extracts `renderAutopilotTrust` from the `autopilotPanel` sentinel block and executes it under node — copy the existing `_render`/`_extract` helper in this file and point it at `renderAutopilotTrust`.

- [ ] **Step 2: Run — expect FAIL** (function undefined under node)
- [ ] **Step 3: Implement** the `renderAutopilotTrust(data)` function inside the `autopilotPanel` sentinel block, escaping `agent_id` with the panel's existing escaper (`escapeHtml`/`apEsc`), rendering the columns from the mockup (Agent, Declared, Reconciled, Calibration, Reversals, Sessions), a `—` for any null with a `title` giving the reason (truncated / not enough signal), a truncation banner when `data.truncated`, an `invalid`-count footnote when any invalid > 0, the two honesty notes (behavior-not-competence; per-declared-identity), and the empty-state string. Add the `autopilotTrust` div beside `autopilotCompliance` and call `loadTrust()` where `loadCompliance()` is invoked (both fetch, tolerate a 404 from an older server by leaving the card absent).
- [ ] **Step 4: Run — expect PASS**; then the FULL cortex + repo-root suites green with zero edits to pre-existing tests.
- [ ] **Step 5: Commit** (`git add dashboard/index.html tests/test_dashboard_autopilot.py docs/guides/knowledge-autopilot.md && git commit -m "feat(dashboard): trust ledger card + guide (spec §5)"`)

---

### Task 5: full-suite verification, push, deploy

- [ ] **Step 1:** `cd cortex && python -m pytest tests/ -q > /tmp/trust-cortex.txt 2>&1; echo "exit: $?"; tail -1 /tmp/trust-cortex.txt` — green, honest exit capture (never `| tail` alone). Then `python -m pytest tests/ -q` (repo root) and `python -m ruff check cortex/app/autopilot/trust.py cortex/tests/test_trust_*.py`.
- [ ] **Step 2:** Push to main. Wait for CI green (`gh run list --workflow CI` — remember auth/tests + ruff run in CI even when local cortex/corpus pass; run ruff locally first, the Docdex lesson).
- [ ] **Step 3:** Deploy to the VPS **through the deploy runbook skill** (do not inline the hostname — `test_forbidden_tokens` bans it): CI-green check, `update.sh` over SSH per the runbook, verify `/version` + `/health`, then `GET /autopilot/trust` returns 200 with the round-1 shape. Dogfood observation for Enforced Runbooks.

## Self-review

- Spec coverage: §2 → Task 1 (scan, truncation, invalid); §3 → Task 2 (cohort, calibration, trend, reversals, unknown-stays-unknown, invalid surfaced); §5 → Task 3 (route, deployment-global) + Task 4 (card); §4 honesty notes → Task 4 render + guide; §6 constants → Task 1 module constants; §7 module/tests → Tasks 1–4. The tenancy invariant (§5) is a doc statement, not code — carried in the guide (Task 4).
- Type consistency: `scan_gateway_events` returns `(events, scanned, truncated, invalid)` consumed by `build_trust`; `build_rows(events, truncated)` signature matches its tests and `build_trust` call; row keys match the dashboard test fixtures exactly; `brier_score` fed `{prediction_confidence, prediction_match_score}` dicts as its contract requires.
- Frozen-formula note: `_trend` splits at the median timestamp of the SCORED points (not the whole window's wall-clock midpoint) — this is the deterministic, sample-based reading of "window midpoint" and is what the test pins; it is frozen here so round 2 inherits it.
