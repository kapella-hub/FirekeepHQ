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

from app.evals.models import recognized_grade_pair

logger = logging.getLogger(__name__)

# 30-day eval TTL bounds the population; the cap is a backstop, not a budget.
SCAN_CAP = 2000

# Below this many parsed evals the halves-split trend is noise dressed as an
# arrow, so it is withheld entirely rather than shown small.
TREND_MIN_SESSIONS = 10

# Below this many self-reported-success sessions, an optimism-skew rate is
# noise dressed as a measurement — withheld (null + insufficient_n) rather
# than shown, exactly like TREND_MIN_SESSIONS above.
MIN_SELF_SUCCESS_N = 30

_Metrics = dict[str, Any]

def _num(value: Any) -> float | None:
    """A metric value, or None when it is not a number.

    Predicates compare; a string that reached a comparison raised TypeError
    inside build_rows and 500'd the whole endpoint — one poisoned metric
    blanking the table, the exact failure the scan's per-record guard claims
    to prevent (found by external review, 2026-08-11). Non-numeric values now
    read as absent: for a count predicate that is non-compliance, and for the
    brier presence predicate a non-numeric score is rightly not a prediction.
    bool is excluded on purpose — True satisfying `> 0` would let a stray
    flag masquerade as a count.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _predicate_input(record: dict) -> _Metrics:
    """The dict handed to a predicate: the eval's metrics, enriched with the
    top-level grade/arm fields under keys no metric collides with (PR4 D2).
    `task_result`, `task_result_source` (evals/models.py) and
    `experiment_group` (PR4 D1) live on the eval RECORD, not inside
    `metrics` — passing the whole record to predicates would make every
    frozen predicate read its metric key off the top level, get None, and
    silently flip to always-False. Every frozen predicate keeps reading its
    metric key out of `metrics` unchanged; only grade_self_reported reads
    the two grade keys, via recognized_grade_pair.
    """
    return {
        **(record.get("metrics") or {}),
        "task_result": record.get("task_result"),
        "task_result_source": record.get("task_result_source"),
        "experiment_group": record.get("experiment_group"),
    }


# The founding-measurement predicates, in the spec's row order. KEYS and
# PREDICATES are frozen to the baseline; the LABEL and DESCRIPTION texts may
# sharpen (they describe the predicate to a human and were found to overclaim
# — external review 2026-08-11) because relabeling changes no measured number.
# key -> (instruction as rendered, predicate description, predicate)
INSTRUCTIONS: list[tuple[str, str, str, Callable[[_Metrics], bool]]] = [
    (
        "recall_before_work",
        "Recall before you answer",
        "memory_read_count > 0",
        lambda m: (_num(m.get("memory_read_count")) or 0) > 0,
    ),
    (
        "write_as_you_go",
        "Write as you go (memory_learn)",
        "memory_write_count > 0",
        lambda m: (_num(m.get("memory_write_count")) or 0) > 0,
    ),
    (
        "recall_visibly_used",
        "Recalled knowledge used (temporal proxy)",
        "recall_used_rate > 0 — a later write/predict follows a read; "
        "proximity, not attribution",
        lambda m: (_num(m.get("recall_used_rate")) or 0) > 0,
    ),
    (
        "ctx_working_state",
        "Working state captured (agent plan/decision)",
        "context_snapshot_count > 0 — counts context_ref events, which only "
        "an agent ctx_update(category=plan|decision) produces; the stop-hook's "
        "scratch snapshots never carry a context_ref, so this measures agent "
        "discipline (Correction 1, 2026-08-12 — the cb36570 disclosure "
        "asserted the opposite of what the code does)",
        lambda m: (_num(m.get("context_snapshot_count")) or 0) > 0,
    ),
    (
        "declared_predictions",
        "Declare consequential actions",
        "brier_score is not None",
        lambda m: _num(m.get("brier_score")) is not None,
    ),
    (
        "outcome_bearing",
        "Outcome-bearing events ≥ 2",
        "outcome_event_count >= 2",
        lambda m: (_num(m.get("outcome_event_count")) or 0) >= 2,
    ),
    # Round 3 — grading ADOPTION (outcome truth PR4 D2, 2026-08-25, appended
    # per the "change a predicate only by adding a NEW row" rule above). Did
    # the agent self-report ANY recognized grade at all — success, partial,
    # or failure via recognized_grade_pair, the one grade-validity check in
    # cortex (evals/models.py) — not whether the grade was good. Reads
    # task_result/task_result_source through the enriched dict built by
    # _predicate_input, never through `e["metrics"]` directly: those two
    # fields live at the top level of the eval record.
    (
        "grade_self_reported",
        "Grade your task on completion (task_result)",
        "recognized (task_result, task_result_source) present",
        lambda m: recognized_grade_pair(
            m.get("task_result"), m.get("task_result_source"))[0] is not None,
    ),
]


# ---------------------------------------------------------------------------
# Round 2 — attribution slicing and exposure (additive; the headline
# hits/total/rate above these helpers never changes).
# ---------------------------------------------------------------------------

# The two derived rows have no instruction text of their own — "was the text
# exposed to this session" is not a question that can be asked of them, so
# their `exposure` is null rather than a zero that would read as measured.
DERIVED_KEYS = frozenset({"recall_visibly_used", "outcome_bearing"})

# Per-key introduction gates: the earliest client version whose artifact can
# carry the key's text, per channel. declared_predictions (the action_before
# paragraph) entered the rendered block at 0.1.40 and the gateway handshake at
# 0.1.41 (Correction 2 — the handshake channel did not exist before that).
# Keys not listed predate attribution entirely, so artifact verification
# alone suffices.
INTRODUCTION_VERSIONS: dict[str, dict[str, tuple[int, ...]]] = {
    "declared_predictions": {"rendered": (0, 1, 40), "gateway": (0, 1, 41)},
}


def _str_or_none(value: Any) -> str | None:
    """A non-empty string, or None — the _num() discipline applied to the
    round-2 attribution fields: a malformed value must read as unattributed,
    never crash the endpoint."""
    if isinstance(value, str) and value:
        return value
    return None


def _parse_version(value: Any) -> tuple[int, ...] | None:
    """A client version as a numeric tuple ("0.1.41" -> (0, 1, 41)), or None
    when unparseable — and unparseable classifies as unknown, never as a
    guess in either direction."""
    if not isinstance(value, str):
        return None
    try:
        return tuple(int(part) for part in value.strip().split("."))
    except ValueError:
        return None


def _runtime_of(record: dict) -> str:
    """The record's runtime slice; anything absent or malformed lands in the
    disclosed "unattributed" bucket."""
    return _str_or_none(record.get("runtime")) or "unattributed"


def _classify_exposure(key: str, record: dict) -> str:
    """One session's exposure to one instruction key: "exposed",
    "not_exposed" or "unknown".

    Exposed = a verified artifact carrying the key's text reached the
    session: rendered block verified current (rendered == expected) or the
    gateway handshake hash present, each gated by the key's introduction
    version where one exists. not_exposed = attributed session where NO
    qualifying artifact reached it (rendered stale/absent with no gateway
    hash, or below the introduction version on every channel). Everything
    else — every unattributed session, every malformed record, every
    unparseable version under a gate — is unknown, never not_exposed.
    """
    instructions = record.get("instructions")
    if not isinstance(instructions, dict):
        return "unknown"
    rendered = _str_or_none(instructions.get("rendered"))
    expected = _str_or_none(instructions.get("expected"))
    gateway = _str_or_none(instructions.get("gateway"))
    if rendered is None and gateway is None:
        # No EVIDENCE-BEARING field arrived. `expected` alone is the wheel's
        # self-declared hash — a claim about the client, not about any
        # artifact reaching the session — so a receipt carrying only it
        # stays unknown, never not_exposed (external review 2026-08-12;
        # "everything else is unknown" read strictly). `rendered` is
        # evidence either way: the literal "absent" is an affirmative
        # report that no block was on disk.
        return "unknown"

    # The client sends the literal "absent" when no block is on disk; it can
    # never equal a wheel hash, so plain equality already classifies it.
    rendered_current = rendered is not None and rendered == expected
    gateway_present = gateway is not None

    gates = INTRODUCTION_VERSIONS.get(key)
    if gates:
        version = _parse_version(record.get("client_version"))
        if version is None:
            return "unknown"  # the introduction gate cannot be evaluated
        rendered_qualifies = rendered_current and version >= gates["rendered"]
        gateway_qualifies = gateway_present and version >= gates["gateway"]
    else:
        rendered_qualifies = rendered_current
        gateway_qualifies = gateway_present

    return "exposed" if (rendered_qualifies or gateway_qualifies) else "not_exposed"


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
    # The floor is on DATED evals, not all evals: undated records cannot be
    # placed in a half, and flooring on the total let ten evals with two dates
    # render a 1-vs-1 comparison as a trend (external review, 2026-08-11).
    with_trend = len(dated) >= TREND_MIN_SESSIONS

    rows: list[dict[str, Any]] = []
    for key, instruction, predicate_desc, predicate in INSTRUCTIONS:
        scored = [(e, predicate(_predicate_input(e))) for e in evals]
        hits = sum(1 for _, hit in scored if hit)
        row: dict[str, Any] = {
            "key": key,
            "instruction": instruction,
            "predicate": predicate_desc,
            "hits": hits,
            "total": len(evals),
            "rate": round(hits / len(evals), 4) if evals else None,
        }
        if with_trend:
            e_hits = sum(1 for e in earlier if predicate(_predicate_input(e)))
            r_hits = sum(1 for e in recent if predicate(_predicate_input(e)))
            row["earlier_rate"] = round(e_hits / len(earlier), 4)
            row["recent_rate"] = round(r_hits / len(recent), 4)

        # Round 2, additive: the SAME frozen predicate over runtime slices.
        # Sessions with no (or malformed) runtime attribution are disclosed
        # as "unattributed" rather than dropped — the headline denominator
        # above is untouched.
        by_runtime: dict[str, dict[str, int]] = {}
        for e, hit in scored:
            bucket = by_runtime.setdefault(_runtime_of(e), {"hits": 0, "total": 0})
            bucket["total"] += 1
            if hit:
                bucket["hits"] += 1
        row["by_runtime"] = by_runtime

        # Round 3, additive to grade_self_reported only: the same predicate
        # sliced by pre-registered arm (PR4 D1). "A"/"B" only — a session
        # with no (or unrecognized) experiment_group is not a measured arm
        # and is EXCLUDED from this split entirely (no bucket of its own),
        # while still counting toward the row's overall hits/total above.
        if key == "grade_self_reported":
            by_experiment_group: dict[str, dict[str, int]] = {}
            for e, hit in scored:
                group = e.get("experiment_group")
                if group not in ("A", "B"):
                    continue
                bucket = by_experiment_group.setdefault(
                    group, {"hits": 0, "total": 0})
                bucket["total"] += 1
                if hit:
                    bucket["hits"] += 1
            row["by_experiment_group"] = by_experiment_group

        # Exposure: null for the derived rows (no instruction text to be
        # exposed to); counted per session for the four text-carrying rows.
        if key in DERIVED_KEYS:
            row["exposure"] = None
        else:
            counts = {"exposed": 0, "not_exposed": 0, "unknown": 0}
            exposed_hits = 0
            for e, hit in scored:
                cls = _classify_exposure(key, e)
                counts[cls] += 1
                if cls == "exposed" and hit:
                    exposed_hits += 1
            row["exposure"] = {
                **counts,
                "exposed_hits": exposed_hits,
                "exposed_rate": (
                    round(exposed_hits / counts["exposed"], 4)
                    if counts["exposed"] else None
                ),
            }
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Round 4 — optimism-skew honesty detector (outcome truth PR4 D3; additive,
# visibility-only). Grading is only useful if the grades are HONEST: this
# measures how often an agent self-reports "success" on a session that also
# carries an INDEPENDENT failure contradiction — a signal ABOUT the grading
# channel, not a compliance row, so it is reported as its own top-level
# `optimism_skew` block rather than a new INSTRUCTIONS row. No LLM, no
# gating, no mutation, and it reads the SAME parsed evals build_rows does
# (no second scan of rp:eval:*).
# ---------------------------------------------------------------------------


def _is_self_success(record: dict) -> bool:
    """Self-reported success = recognized_grade_pair(...)[0] == "success".

    The [0] matters — the fn returns a tuple, never a bare None (it can
    return (None, None)), so indexing rather than truthiness-testing the
    pair is deliberate.
    """
    grade, _source = recognized_grade_pair(
        record.get("task_result"), record.get("task_result_source"))
    return grade == "success"


def _is_skew_hit(record: dict) -> bool:
    """A self-success record is a skew hit iff it also carries at least one
    INDEPENDENT failure contradiction:

      - has_failures: `failure_event_ids` non-empty (EvalResult, per-tool-call
        outcome=="failure" events — independent of the self-reported grade).
      - a GUARDED tool_success_rate < 1.0, counted ONLY when
        outcome_event_count >= 2. NON-NEGOTIABLE: below 2, the outcome
        population is ~= the self-report itself (scorers.py), so the rate is
        not independent evidence and must be ignored — never bare
        tool_success_rate, never failure_rate (same non-independence trap).
    """
    if record.get("failure_event_ids"):
        return True
    metrics = record.get("metrics") or {}
    outcome_event_count = _num(metrics.get("outcome_event_count"))
    tool_success_rate = _num(metrics.get("tool_success_rate"))
    if (
        outcome_event_count is not None and outcome_event_count >= 2
        and tool_success_rate is not None and tool_success_rate < 1.0
    ):
        return True
    return False


def _skew_stats(hits: int, total: int) -> dict[str, Any]:
    """One skew bucket: honest denominators, min-N gated.

    Below MIN_SELF_SUCCESS_N self-success sessions, `rate` is null and
    `insufficient_n` is true — NEVER a bare 0.0 that would read as a clean
    measurement on almost no data (the outcome_event_count lesson, applied
    to skew).
    """
    insufficient = total < MIN_SELF_SUCCESS_N
    return {
        "hits": hits,
        "self_success_total": total,
        "rate": None if insufficient else round(hits / total, 4),
        "insufficient_n": insufficient,
    }


def build_optimism_skew(evals: list[dict]) -> dict[str, Any]:
    """Optimism-skew rate over one snapshot of parsed evals, overall and per
    pre-registered arm (PR4 D1).

    `by_experiment_group` carries only observed "A"/"B" buckets — a session
    with no (or unrecognized) experiment_group is excluded from the split
    entirely (same convention as grade_self_reported's by_experiment_group in
    build_rows) while still counting toward `overall`.

    Bridge `abandoned` (owm.py's strongest hard-negative) is deliberately
    NOT included here: it needs owm._fetch_bridge_statuses, a REST call to
    Bridge, and this function backs a synchronous read-only reporting
    endpoint that today has zero network dependencies beyond the eval scan.
    The two contradictions above are independent and sufficient to ship;
    Bridge-abandoned is a candidate follow-up, not a gap in this detector's
    correctness.
    """
    overall_hits = 0
    overall_total = 0
    group_counts: dict[str, dict[str, int]] = {}
    for record in evals:
        if not _is_self_success(record):
            continue
        overall_total += 1
        hit = _is_skew_hit(record)
        if hit:
            overall_hits += 1
        group = record.get("experiment_group")
        if group in ("A", "B"):
            bucket = group_counts.setdefault(group, {"hits": 0, "total": 0})
            bucket["total"] += 1
            if hit:
                bucket["hits"] += 1

    return {
        "overall": _skew_stats(overall_hits, overall_total),
        "by_experiment_group": {
            group: _skew_stats(counts["hits"], counts["total"])
            for group, counts in group_counts.items()
        },
    }


async def build_compliance(replay_redis) -> dict[str, Any]:
    # Imported here rather than at module scope: arm_comparison reads THIS
    # module's frozen helpers (INSTRUCTIONS, _predicate_input, the two skew
    # predicates), so a top-level import in either direction is a cycle.
    from app.autopilot.arm_comparison import build_arm_comparison

    evals, unparsed, capped = await scan_evals(replay_redis)
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sessions_evaluated": len(evals),
        "dated_sessions": sum(1 for e in evals if _parse_created_at(e) is not None),
        "unparsed": unparsed,
        "approximate": capped,
        "instructions": build_rows(evals),
        "optimism_skew": build_optimism_skew(evals),
        "arm_comparison": await build_arm_comparison(replay_redis, evals),
        "notes": [
            "Compliance measures BEHAVIOR — whether sessions did the instructed "
            "thing. It does not measure whether doing it helped: the outcome "
            "signal is still degenerate (replay-evals-patterns.md), so no "
            "quality claim follows from these rates.",
            "Rates are over ALL evaluated sessions, including sessions that "
            "predate an instruction's rollout — the table measures the fleet, "
            "not obedience among instructed sessions. Attribution (runtime, "
            "client version, instruction-artifact hashes) starts with client "
            "0.1.41 sessions and nothing backfills: the 30-day eval TTL plus "
            "non-overwriting eval writes mean every earlier session stays "
            "unattributed until it ages out, so the per-runtime and exposure "
            "slices tolerate a mostly-unknown window after rollout.",
            "A session counts as exposed to an instruction only when a "
            "verified artifact carrying its text reached it — rendered block "
            "hash matching the wheel's, or the gateway handshake hash "
            "present, with per-key introduction version gates — and every "
            "unattributed session is unknown, never not_exposed.",
            "Predicates are frozen to the 2026-08-11 founding measurement "
            "(docs/superpowers/specs/2026-08-11-living-instructions-design.md); "
            "a changed predicate would orphan the baseline, so changes arrive "
            "as new rows.",
            "Trend halves the DATED evals by eval time; it is withheld below "
            f"{TREND_MIN_SESSIONS} dated sessions rather than shown small.",
            "optimism_skew measures grading HONESTY, not compliance: of "
            "self-reported-success sessions, how many also carry an "
            "independent failure contradiction (has_failures, or a "
            "tool_success_rate < 1.0 guarded to outcome_event_count >= 2). "
            "Visibility only — no gating, no mutation. It reads the SAME "
            "scan as the table above, so the top-level `approximate` and "
            "`unparsed` disclosures apply to it too. Below "
            f"{MIN_SELF_SUCCESS_N} self-success sessions (overall or per "
            "arm) its rate is null with insufficient_n true, never a bare "
            "0.0.",
        ],
    }
    return payload
