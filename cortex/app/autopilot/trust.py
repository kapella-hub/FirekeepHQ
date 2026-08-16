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
    return {"unattributed_predict": 0, "missing_action_id": 0,
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
        # payload / action_id / timestamp are required of BOTH event types —
        # a reconcile pairs by action_id, a predict is keyed by it.
        try:
            payload = json.loads(fields.get("payload", "{}"))
        except (json.JSONDecodeError, TypeError):
            invalid["malformed"] += 1
            continue
        if not isinstance(payload, dict):  # valid JSON, but "null"/a scalar/a list
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
        agent = fields.get("agent_id", "")
        session = fields.get("session_id", "")
        # A reconcile's OWN agent_id and session are irrelevant: it is
        # attributed to the DECLARING agent by action_id pairing in build_rows,
        # and the gateway legitimately emits agent_id="" / session_id="" on a
        # reconcile whose short-lived predict RECORD (ag:predict:{id}, ~300s
        # TTL) has expired — while its predict EVENT still lives in the 30-day
        # stream. Rejecting those here discarded ~99% of real reconciliations
        # (measured live) and undercounted every agent's rate. Only a PREDICT
        # needs a non-empty agent_id, because the predict IS the declaration
        # being attributed; without one it cannot join any agent's row.
        if etype == "agent.action.predict" and not agent:
            invalid["unattributed_predict"] += 1
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
            if e["session_id"]:
                row["sessions"].add(e["session_id"])
            row["ts"].append(e["ts"])

    reconciled_ids: set[str] = set()
    for e in events:
        if e["event_type"] != "agent.action.reconcile":
            continue
        aid = e["action_id"]
        owner = declared.get(aid)
        if owner is None:
            continue  # orphan reconcile — outside the cohort
        if aid in reconciled_ids:
            continue  # dedup: a reconciliation is per ACTION, not per event.
            # The gateway deletes ag:predict:{action_id} on the first reconcile
            # and drops any later one, so a duplicate cannot reach the stream
            # today — but the ledger's ≤100% guarantee must be LOCAL, not a
            # property borrowed from an upstream emitter that could change.
        reconciled_ids.add(aid)
        agent, conf = owner
        row = _row(agent)
        row["reconciled"] += 1
        if e["session_id"]:  # a reconcile's session is often "" (expired record)
            row["sessions"].add(e["session_id"])
        row["ts"].append(e["ts"])
        # `outcome` is a dict in the current reconcile schema, but real stream
        # events in the window are heterogeneous — older ones carry a STRING
        # outcome, and `... or {}` leaves a non-empty string intact (truthy).
        # Guard by type, the way the predict loop already guards `prediction`.
        outcome = e["payload"].get("outcome")
        if isinstance(outcome, dict) and outcome.get("success") is False:
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
    # Split by INDEX at the midpoint, not by timestamp VALUE: a value-split
    # collapses to empty-vs-all when every scored point shares one timestamp
    # (a burst), which would null the trend exactly when there is plenty of
    # signal. Index-split gives balanced halves regardless of ties.
    half = len(ordered) // 2
    older = ordered[:half]
    newer = ordered[half:]
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
