"""Living Instructions round 1 — the per-instruction compliance table.

Scores each rendered-block instruction against what sessions actually did,
deterministically, from the stored session evals (``rp:eval:*``). The
predicates are BYTE-FOR-BYTE the ones that produced the founding measurement
in docs/superpowers/specs/2026-08-11-living-instructions-design.md — that
document is the pre-registration, and a predicate that drifts from it would
make every later comparison against the baseline meaningless. Change a
predicate only by adding a NEW row with a new key.

WHAT THIS TABLE CAN AND CANNOT CLAIM (the spec's honesty section, enforced
here as response `notes` so the dashboard cannot show the numbers without the
caveat): a compliance rate is a measure of BEHAVIOR — whether sessions did
the instructed thing. It is not a measure of whether doing it helped; the
outcome signal is still degenerate (see replay-evals-patterns.md), so
compliance→quality is exactly the claim round 1 must not make.

The scan deliberately mirrors digest.py's shape: one bounded walk, classify
in Python, `approximate: true` when capped rather than presenting a sample
as a census. `rp:eval:*` cannot match the DLQ (`rp:eval_dlq:{sid}`) or the
index (`rp:eval_index`) — both diverge from the prefix before the colon.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 30-day eval TTL bounds the population; the cap is a backstop, not a budget.
SCAN_CAP = 2000

# Below this many parsed evals the halves-split trend is noise dressed as an
# arrow, so it is withheld entirely rather than shown small.
TREND_MIN_SESSIONS = 10

_Metrics = dict[str, Any]

# The founding-measurement predicates, in the spec's row order.
# key -> (instruction as rendered, predicate description, predicate)
INSTRUCTIONS: list[tuple[str, str, str, Callable[[_Metrics], bool]]] = [
    (
        "recall_before_work",
        "Recall before you answer",
        "memory_read_count > 0",
        lambda m: (m.get("memory_read_count") or 0) > 0,
    ),
    (
        "write_as_you_go",
        "Write as you go (memory_learn)",
        "memory_write_count > 0",
        lambda m: (m.get("memory_write_count") or 0) > 0,
    ),
    (
        "recall_visibly_used",
        "Recalled knowledge visibly used",
        "recall_used_rate > 0",
        lambda m: (m.get("recall_used_rate") or 0) > 0,
    ),
    (
        "ctx_working_state",
        "ctx_update as you go",
        "context_snapshot_count > 0",
        lambda m: (m.get("context_snapshot_count") or 0) > 0,
    ),
    (
        "declared_predictions",
        "Declare consequential actions",
        "brier_score is not None",
        lambda m: m.get("brier_score") is not None,
    ),
    (
        "outcome_bearing",
        "Outcome-bearing events ≥ 2",
        "outcome_event_count >= 2",
        lambda m: (m.get("outcome_event_count") or 0) >= 2,
    ),
]


async def scan_evals(replay_redis) -> tuple[list[dict], int, bool]:
    """One bounded walk over stored evals.

    Returns (parsed evals, unparsed count, capped). A record that fails to
    parse is COUNTED, not silently dropped — `unparsed` in the response is
    what keeps "32 sessions" from quietly meaning "32 of an unknown many".
    """
    parsed: list[dict] = []
    unparsed = 0
    capped = False
    seen = 0
    async for key in replay_redis.scan_iter(match="rp:eval:*", count=200):
        seen += 1
        if seen > SCAN_CAP:
            capped = True
            break
        try:
            raw = await replay_redis.get(key)
            record = json.loads(raw)
            if not isinstance(record.get("metrics"), dict):
                raise ValueError("no metrics dict")
            parsed.append(record)
        except Exception:  # noqa: BLE001 — one bad record must not blank the table
            unparsed += 1
    return parsed, unparsed, capped


def _parse_created_at(record: dict) -> datetime | None:
    raw = record.get("created_at")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_rows(evals: list[dict]) -> list[dict[str, Any]]:
    """Score every instruction over one snapshot of parsed evals.

    Trend is a halves-by-evaluation-time split (older half vs newer half by
    `created_at`, undated records excluded from the split only): coarse on
    purpose — with a 30-day TTL there is no long history to window over, and
    an honest coarse split beats a precise-looking one over the same handful
    of sessions. Withheld entirely below TREND_MIN_SESSIONS.
    """
    dated = sorted(
        (e for e in evals if _parse_created_at(e) is not None),
        key=lambda e: _parse_created_at(e),  # type: ignore[arg-type, return-value]
    )
    half = len(dated) // 2
    earlier, recent = dated[:half], dated[half:]
    with_trend = len(evals) >= TREND_MIN_SESSIONS and half >= 1

    rows: list[dict[str, Any]] = []
    for key, instruction, predicate_desc, predicate in INSTRUCTIONS:
        hits = sum(1 for e in evals if predicate(e.get("metrics", {})))
        row: dict[str, Any] = {
            "key": key,
            "instruction": instruction,
            "predicate": predicate_desc,
            "hits": hits,
            "total": len(evals),
            "rate": round(hits / len(evals), 4) if evals else None,
        }
        if with_trend:
            e_hits = sum(1 for e in earlier if predicate(e.get("metrics", {})))
            r_hits = sum(1 for e in recent if predicate(e.get("metrics", {})))
            row["earlier_rate"] = round(e_hits / len(earlier), 4)
            row["recent_rate"] = round(r_hits / len(recent), 4)
        rows.append(row)
    return rows


async def build_compliance(replay_redis) -> dict[str, Any]:
    evals, unparsed, capped = await scan_evals(replay_redis)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions_evaluated": len(evals),
        "unparsed": unparsed,
        "approximate": capped,
        "instructions": build_rows(evals),
        "notes": [
            "Compliance measures BEHAVIOR — whether sessions did the instructed "
            "thing. It does not measure whether doing it helped: the outcome "
            "signal is still degenerate (replay-evals-patterns.md), so no "
            "quality claim follows from these rates.",
            "Predicates are frozen to the 2026-08-11 founding measurement "
            "(docs/superpowers/specs/2026-08-11-living-instructions-design.md); "
            "a changed predicate would orphan the baseline, so changes arrive "
            "as new rows.",
            "Trend halves the evaluated window by eval time; it is withheld "
            f"below {TREND_MIN_SESSIONS} sessions rather than shown small.",
        ],
    }
    return payload
