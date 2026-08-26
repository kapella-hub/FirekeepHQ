"""Outcome truth PR5 — the `arm_comparison` readout (spec D6, D8, D12, D14).

The analysis half of a PRE-REGISTERED experiment. Every constant below is a
registered value, not a tunable: the whole point of registering them before
the data arrived is that nobody gets to choose them after seeing it. Change
one and you have a new experiment needing a new dated registration, exactly
as `compliance.py`'s predicates are frozen to their founding measurement.

Three design facts worth holding while reading:

1. **The randomization unit is the MEMBER, not the session.** Sessions cluster
   hard within members — one prolific agent can contribute a hundred of them —
   so the primary analysis (D8) permutes members across arms and compares
   per-member graded fractions. The session-level 2x2 is kept for continuity
   with the round-3 compliance table and is labelled descriptive-only in the
   payload itself, where a dashboard cannot show the number without it.

2. **Absence is never a measured value.** A missing exposure receipt is
   `unknown`, never `not_exposed`; a record with no `member_token` is
   disclosed as an unknown group and excluded from member-level analysis
   rather than folded in as a pseudo-member. This mirrors
   `compliance._classify_exposure`, and for the same reason: classifying
   silence as a negative manufactures evidence.

3. **This block never raises.** `build_compliance` was a working operator
   surface before PR5 and its availability is not hostage to an experiment
   readout. Any failure degrades to `{"status": "error", ...}`.

D14: nothing here is confirmatory. The verdict of record is the dated T0+28d
snapshot committed as a spec addendum; every live view is monitoring, and the
payload says so on every response.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Callable

from app.autopilot.compliance import (
    INSTRUCTIONS,
    _is_self_success,
    _is_skew_hit,
    _parse_created_at,
    _predicate_input,
)
from app.autopilot.permutation import permutation_test_member_means
from app.briefing.sections import TREATMENT_ARM
from app.config import get_settings
from app.patterns.statistics import (
    _chi_square_2x2,
    _cohens_h,
    _confidence_interval_diff,
)

# --- Pre-registered constants (spec D6/D8) --------------------------------
EXPOSED_RUNTIME = "claude"          # D6: only verified model-facing channel
MIN_MEMBERS_PER_ARM = 5             # D8 floor (permutation resolution)
MIN_SESSIONS_PER_MEMBER = 5         # D8: qualifying-member floor
MIN_SESSIONS_PER_ARM = 99           # D8 fixed-z bound (descriptive test)
MIN_ARM_MEAN_DIFF = 0.10            # D8: practical-significance gate
BALANCE_MAX_PP_FRACTION_GAP = 0.10  # D6 absolute bound (a)
BALANCE_MAX_SINGLE_MEMBER_SHARE = 0.50  # D6 absolute bound (b)
NONINFERIORITY_MARGIN = 0.10        # H2' margin, +10pp
NONINFERIORITY_Z = 1.645            # one-sided 95%
MIN_SELF_SUCCESS_PER_ARM = 30       # H2' gate (H2's bound, per arm)

# H2's second condition, stated inline in the D8 contract rather than in its
# constants block — named here so the readout can report the bound it applied.
MAX_TREATMENT_SKEW_RATE = 0.15
# H1' significance level. Two-sided, matching permutation_test_member_means.
ALPHA = 0.05
# D12: below this share of treatment per-protocol sessions carrying a
# nudge_shown receipt, the exposure record is too thin to trust and is flagged.
MIN_NUDGE_SHOWN_COVERAGE = 0.90

ARMS = ("A", "B")
# Direction is derived from the registered TREATMENT_ARM, never hardcoded as
# A-minus-B: if the D9 coin had landed the other way, every sign below flips
# with it and nothing else changes.
CONTROL_ARM = "B" if TREATMENT_ARM == "A" else "A"

UNKNOWN_MEMBER = "unknown"
UNKNOWN_RUNTIME = "unknown"

NOT_STARTED_NOTE = (
    "GRADING_NUDGE_T0 unset — the experiment has not begun (spec D4); no "
    "session is per-protocol."
)
SESSION_LEVEL_NOTE = (
    "descriptive only — sessions cluster within members; the member-level "
    "permutation above is the primary analysis (spec D8)"
)
BALANCE_NOTE = (
    "one or both D6 absolute balance bounds are violated, so a difference "
    "between the arms is not attributable to the intervention; the numbers "
    "below are descriptive"
)
H2_NOTE = (
    "H2' is a NON-INFERIORITY test: it asks whether the nudge bought grading "
    "adoption with dishonesty. It holds only when the treatment arm's "
    f"optimism-skew rate is at or below {MAX_TREATMENT_SKEW_RATE} AND the "
    f"one-sided {NONINFERIORITY_Z}-z upper bound on (treatment - control) "
    f"stays under +{NONINFERIORITY_MARGIN}."
)
COVERAGE_NOTE = (
    "D12: the share of treatment per-protocol sessions whose briefing carries "
    "a server-side rp:nudge_shown receipt. Control briefings have no receipt "
    "by construction (control is the ABSENCE of the section) and are not "
    "counted. Low coverage means the exposure record is incomplete, not that "
    "the intervention failed."
)
NOTES = [
    "the verdict of record is the dated T0+28d snapshot committed as a spec "
    "addendum (D14); every live view is operational monitoring",
    "both disclosed spillover channels (in-repo text, shared memory) bias "
    "toward null — a positive H1' survives them; a null H1' is ambiguous "
    "between no-effect and ambient saturation",
]


# --- pure helpers ---------------------------------------------------------

def _parse_t0(raw: Any) -> datetime | None:
    """T0 as an aware datetime, or None when unset or unparseable.

    Unparseable reads as UNSET, not as "start of time": a typo in the env var
    must stop the analysis, never silently enrol every historical session.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        ts = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _grade_predicate() -> Callable[[dict], bool]:
    """The FROZEN grade_self_reported predicate, looked up from the compliance
    table rather than reimplemented. A second copy of this predicate would
    drift from the one scoring the compliance row and quietly make the two
    surfaces disagree about the same sessions."""
    return next(p for key, _, _, p in INSTRUCTIONS if key == "grade_self_reported")


def _member_key(record: dict) -> str:
    token = record.get("member_token")
    return token if isinstance(token, str) and token else UNKNOWN_MEMBER


def _runtime_bucket(record: dict) -> str:
    runtime = record.get("runtime")
    return runtime if isinstance(runtime, str) and runtime else UNKNOWN_RUNTIME


def _classify(record: dict, t0: datetime) -> tuple[str | None, str, bool]:
    """D6 record-level classification → (arm, bucket, in_itt).

    Buckets: `no_arm` (not a randomized session at all), `pre_t0` (ran before
    the experiment began — D4), `per_protocol`, `not_exposed`, `unknown`.

    Only `per_protocol`, `not_exposed` and IN-WINDOW `unknown` records are
    ITT; an undated arm record is `unknown` AND outside ITT, because a record
    that cannot be placed in time cannot be placed relative to T0 either.

    `not_exposed` requires AFFIRMATIVE evidence of non-exposure: an explicit
    `briefing_delivered: false`, or a runtime that is present and is not the
    one verified channel. A null receipt or a missing runtime is `unknown`.
    """
    arm = record.get("experiment_group")
    if arm not in ARMS:
        return None, "no_arm", False
    created = _parse_created_at(record)
    if created is None:
        return arm, "unknown", False
    if created < t0:
        return arm, "pre_t0", False

    delivered = record.get("briefing_delivered")
    runtime = record.get("runtime")
    if delivered is True and runtime == EXPOSED_RUNTIME:
        return arm, "per_protocol", True
    if delivered is False or (
        runtime not in (None, "") and runtime != EXPOSED_RUNTIME
    ):
        return arm, "not_exposed", True
    return arm, "unknown", True


def _member_sessions(records: list[dict]) -> dict[str, int]:
    """member_token → per-protocol session count. Tokenless records are
    grouped under `unknown` so they are disclosed, not dropped."""
    out: dict[str, int] = {}
    for record in records:
        key = _member_key(record)
        out[key] = out.get(key, 0) + 1
    return out


def _member_fractions(
    records: list[dict], graded: Callable[[dict], bool]
) -> dict[str, float]:
    """Per-member graded fraction over per-protocol sessions, for QUALIFYING
    members only (a real token, and at least MIN_SESSIONS_PER_MEMBER
    sessions). A member with three sessions carries a fraction that can only
    be 0, 1/3, 2/3 or 1 — resolution the primary analysis must not pretend to
    have."""
    totals: dict[str, int] = {}
    hits: dict[str, int] = {}
    for record in records:
        key = _member_key(record)
        if key == UNKNOWN_MEMBER:
            continue
        totals[key] = totals.get(key, 0) + 1
        if graded(_predicate_input(record)):
            hits[key] = hits.get(key, 0) + 1
    return {
        key: hits.get(key, 0) / total
        for key, total in totals.items()
        if total >= MIN_SESSIONS_PER_MEMBER
    }


def _balance(
    itt: dict[str, list[dict]], pp: dict[str, list[dict]]
) -> dict[str, Any]:
    """D6 balance: the two ABSOLUTE bounds, plus the tables they are read off.

    These are bounds, not tests. Differential attrition between the arms, or
    one member owning half an arm, breaks the comparison outright — no p-value
    rescues it, so the verdict is stated rather than inferred.
    """
    by_arm: dict[str, Any] = {}
    for arm in ARMS:
        itt_n = len(itt[arm])
        pp_n = len(pp[arm])
        per_member = _member_sessions(pp[arm])
        known = {k: v for k, v in per_member.items() if k != UNKNOWN_MEMBER}
        share = max(known.values()) / pp_n if known and pp_n else None
        runtime_mix: dict[str, int] = {}
        for record in itt[arm]:
            bucket = _runtime_bucket(record)
            runtime_mix[bucket] = runtime_mix.get(bucket, 0) + 1
        by_arm[arm] = {
            "itt": itt_n,
            "per_protocol": pp_n,
            "pp_fraction": round(pp_n / itt_n, 4) if itt_n else None,
            "sessions_per_member": per_member,
            "runtime_mix": runtime_mix,
            "max_single_member_share": (
                round(share, 4) if share is not None else None),
        }

    violations: list[str] = []
    frac_a = by_arm["A"]["pp_fraction"]
    frac_b = by_arm["B"]["pp_fraction"]
    gap = (round(abs(frac_a - frac_b), 4)
           if frac_a is not None and frac_b is not None else None)
    if gap is not None and gap > BALANCE_MAX_PP_FRACTION_GAP:
        violations.append(
            f"per-protocol fraction gap {gap} exceeds the D6 bound of "
            f"{BALANCE_MAX_PP_FRACTION_GAP} (differential attrition)")
    for arm in ARMS:
        share = by_arm[arm]["max_single_member_share"]
        if share is not None and share > BALANCE_MAX_SINGLE_MEMBER_SHARE:
            violations.append(
                f"arm {arm}: one member holds {share} of its per-protocol "
                f"sessions, over the D6 bound of "
                f"{BALANCE_MAX_SINGLE_MEMBER_SHARE}")

    return {
        "by_arm": by_arm,
        "pp_fraction_gap": gap,
        "balance_violated": bool(violations),
        "violations": violations,
        # Nested so the BOUNDS never share a key name with the MEASURED
        # per-arm values above (`max_single_member_share` is both).
        "bounds": {
            "pp_fraction_gap": BALANCE_MAX_PP_FRACTION_GAP,
            "single_member_share": BALANCE_MAX_SINGLE_MEMBER_SHARE,
        },
    }


def _h1_primary(
    fractions: dict[str, dict[str, float]],
    pp: dict[str, list[dict]],
    balance_violated: bool,
) -> dict[str, Any]:
    """H1' (D8): member-level permutation on per-member graded fractions.

    Order of verdicts is deliberate — balance FIRST. A balance-violated
    experiment's primary analysis is uninterpretable however much data it
    has, so reporting `insufficient_n` there would name the wrong problem and
    imply that waiting for more sessions fixes it.
    """
    qualifying = {arm: len(fractions[arm]) for arm in ARMS}
    pp_sessions = {arm: len(pp[arm]) for arm in ARMS}
    base: dict[str, Any] = {
        "holds": False,
        "treatment_arm": TREATMENT_ARM,
        "control_arm": CONTROL_ARM,
        "qualifying_members": qualifying,
        "pp_sessions": pp_sessions,
        "member_means": {
            arm: (round(sum(f.values()) / len(f), 6) if f else None)
            for arm, f in fractions.items()
        },
        "min_members_per_arm": MIN_MEMBERS_PER_ARM,
        "min_sessions_per_member": MIN_SESSIONS_PER_MEMBER,
        "min_sessions_per_arm": MIN_SESSIONS_PER_ARM,
        "min_arm_mean_diff": MIN_ARM_MEAN_DIFF,
        "alpha": ALPHA,
    }
    if balance_violated:
        return {**base, "status": "balance_violated", "note": BALANCE_NOTE}
    if any(
        qualifying[arm] < MIN_MEMBERS_PER_ARM
        or pp_sessions[arm] < MIN_SESSIONS_PER_ARM
        for arm in ARMS
    ):
        return {**base, "status": "insufficient_n"}

    # Treatment first, so the returned `diff` IS mean_treatment - mean_control
    # by construction. Sorted because Redis SCAN order is not stable and a
    # pre-registered analysis must return the same p on every run over the
    # same snapshot — the Monte-Carlo path samples positions in this list.
    result = permutation_test_member_means(
        sorted(fractions[TREATMENT_ARM].values()),
        sorted(fractions[CONTROL_ARM].values()),
    )
    diff = result["diff"]
    # `- 1e-12` is float tolerance on a `>=`, not a loosened threshold: a
    # per-member difference of exactly the registered +0.10 (12/20 vs 10/20)
    # subtracts to 0.09999999999999998 in IEEE754, so a bare `>=` reports the
    # registered floor as below the registered floor. permutation.py:28
    # carries the same tolerance for the same reason. MIN_ARM_MEAN_DIFF is
    # unchanged — this is what makes the comparison mean what it says.
    return {
        **base,
        "status": "ok",
        "holds": (result["p_value"] < ALPHA
                  and diff >= MIN_ARM_MEAN_DIFF - 1e-12),
        "p_value": round(result["p_value"], 6),
        "diff": round(diff, 6),
        "mean_treatment": round(result["mean_a"], 6),
        "mean_control": round(result["mean_b"], 6),
        "method": result["method"],
        "reassignments": result["reassignments"],
    }


def _chi_square_guarded(a: int, b: int, c: int, d: int) -> tuple[float, float]:
    """`_chi_square_2x2` with the DEGENERATE table handled before the call.

    A zero row or column marginal (nobody graded in either arm, or everybody
    did) has no association to test. The stdlib fallback path inside
    `_chi_square_2x2` returns (0.0, 1.0) for exactly this case, but its scipy
    path raises ValueError on the resulting zero expected frequency — and it
    only catches ImportError. Guarding here keeps this readout identical
    whether or not scipy is installed on the box: a pre-registered analysis
    must not return different numbers in CI's stdlib-only job than on a
    developer's machine.
    """
    if (a + b) == 0 or (c + d) == 0 or (a + c) == 0 or (b + d) == 0:
        return 0.0, 1.0
    return _chi_square_2x2(a, b, c, d)


def _session_level(
    pp: dict[str, list[dict]], graded: Callable[[dict], bool]
) -> dict[str, Any]:
    """The pooled session-level 2x2 — reported, and labelled descriptive."""
    by_arm = {}
    for arm in ARMS:
        total = len(pp[arm])
        hits = sum(1 for r in pp[arm] if graded(_predicate_input(r)))
        by_arm[arm] = {
            "per_protocol": total,
            "graded": hits,
            "rate": round(hits / total, 4) if total else None,
        }
    block: dict[str, Any] = {
        "by_arm": by_arm,
        "treatment_arm": TREATMENT_ARM,
        "control_arm": CONTROL_ARM,
        "note": SESSION_LEVEL_NOTE,
    }
    n_t = by_arm[TREATMENT_ARM]["per_protocol"]
    n_c = by_arm[CONTROL_ARM]["per_protocol"]
    if not n_t or not n_c:
        return {**block, "chi2": None, "p_value": None,
                "cohens_h": None, "ci_diff": None}

    g_t = by_arm[TREATMENT_ARM]["graded"]
    g_c = by_arm[CONTROL_ARM]["graded"]
    chi2, p_value = _chi_square_guarded(g_t, n_t - g_t, g_c, n_c - g_c)
    rate_t, rate_c = g_t / n_t, g_c / n_c
    return {
        **block,
        "chi2": round(chi2, 6),
        "p_value": round(p_value, 6),
        "cohens_h": round(_cohens_h(rate_t, rate_c), 6),
        "ci_diff": list(_confidence_interval_diff(rate_t, n_t, rate_c, n_c)),
    }


def _h2_noninferiority(pp: dict[str, list[dict]]) -> dict[str, Any]:
    """H2' (D8): the nudge must not buy grading adoption with dishonesty.

    Denominator is self-reported-SUCCESS per-protocol sessions per arm; the
    numerator is `compliance._is_skew_hit` — the same independent failure
    contradiction the optimism-skew block uses, reused rather than re-derived.
    """
    by_arm = {}
    for arm in ARMS:
        successes = [r for r in pp[arm] if _is_self_success(r)]
        hits = sum(1 for r in successes if _is_skew_hit(r))
        total = len(successes)
        by_arm[arm] = {
            "self_success": total,
            "skew_hits": hits,
            "skew_rate": round(hits / total, 4) if total else None,
        }
    block: dict[str, Any] = {
        "holds": False,
        "by_arm": by_arm,
        "treatment_arm": TREATMENT_ARM,
        "control_arm": CONTROL_ARM,
        "margin": NONINFERIORITY_MARGIN,
        "z": NONINFERIORITY_Z,
        "max_treatment_skew_rate": MAX_TREATMENT_SKEW_RATE,
        "min_self_success_per_arm": MIN_SELF_SUCCESS_PER_ARM,
        "note": H2_NOTE,
    }
    n_t = by_arm[TREATMENT_ARM]["self_success"]
    n_c = by_arm[CONTROL_ARM]["self_success"]
    if n_t < MIN_SELF_SUCCESS_PER_ARM or n_c < MIN_SELF_SUCCESS_PER_ARM:
        return {**block, "status": "insufficient_n",
                "treatment_skew_rate": None, "control_skew_rate": None,
                "diff": None, "se": None, "upper_bound": None}

    skew_t = by_arm[TREATMENT_ARM]["skew_hits"] / n_t
    skew_c = by_arm[CONTROL_ARM]["skew_hits"] / n_c
    diff = skew_t - skew_c
    se = math.sqrt(
        skew_t * (1 - skew_t) / n_t + skew_c * (1 - skew_c) / n_c)
    upper = diff + NONINFERIORITY_Z * se
    return {
        **block,
        "status": "ok",
        "holds": skew_t <= MAX_TREATMENT_SKEW_RATE
        and upper < NONINFERIORITY_MARGIN,
        "treatment_skew_rate": round(skew_t, 4),
        "control_skew_rate": round(skew_c, 4),
        "diff": round(diff, 6),
        "se": round(se, 6),
        "upper_bound": round(upper, 6),
    }


async def _coverage(replay_redis, pp: dict[str, list[dict]]) -> dict[str, Any]:
    """D12 nudge_shown coverage over TREATMENT per-protocol sessions."""
    records = pp[TREATMENT_ARM]
    ids = [r.get("briefing_id") for r in records]
    ids = [i for i in ids if isinstance(i, str) and i]
    block: dict[str, Any] = {
        "treatment_arm": TREATMENT_ARM,
        "treatment_pp_records": len(records),
        "ids_checked": len(ids),
        "distinct_ids": len(set(ids)),
        "min_coverage": MIN_NUDGE_SHOWN_COVERAGE,
        "note": COVERAGE_NOTE,
    }
    if not ids:
        # No ids is NO MEASUREMENT, not 0% coverage — a bare 0.0 here would
        # read as a broken nudge rather than an empty window.
        return {**block, "receipts_found": 0, "coverage": None,
                "flagged": False}
    values = await replay_redis.mget([f"rp:nudge_shown:{i}" for i in ids])
    found = sum(1 for value in values if value is not None)
    coverage = found / len(ids)
    return {
        **block,
        "receipts_found": found,
        "coverage": round(coverage, 4),
        "flagged": coverage < MIN_NUDGE_SHOWN_COVERAGE,
    }


# --- entry point ----------------------------------------------------------

async def _build(replay_redis, evals: list[dict], approximate: bool) -> dict[str, Any]:
    t0 = _parse_t0(get_settings().GRADING_NUDGE_T0)
    if t0 is None:
        return {"status": "not_started", "confirmatory": False,
                "approximate": approximate, "note": NOT_STARTED_NOTE}

    graded = _grade_predicate()
    counts = {
        arm: {"itt": 0, "per_protocol": 0, "not_exposed": 0,
              "unknown": 0, "unknown_undated": 0}
        for arm in ARMS
    }
    itt: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    pp: dict[str, list[dict]] = {arm: [] for arm in ARMS}
    no_arm = 0
    pre_t0 = 0

    for record in evals:
        arm, bucket, in_itt = _classify(record, t0)
        if bucket == "no_arm":
            no_arm += 1
            continue
        if bucket == "pre_t0":
            pre_t0 += 1
            continue
        counts[arm][bucket] += 1
        if bucket == "unknown" and not in_itt:
            counts[arm]["unknown_undated"] += 1
        if in_itt:
            counts[arm]["itt"] += 1
            itt[arm].append(record)
        if bucket == "per_protocol":
            pp[arm].append(record)

    balance = _balance(itt, pp)
    fractions = {arm: _member_fractions(pp[arm], graded) for arm in ARMS}

    return {
        "status": "ok",
        "confirmatory": False,
        # Same field name as the enclosing compliance payload's disclosure:
        # when the eval scan hit SCAN_CAP these are SOME sessions, not all of
        # them. It has to travel WITH the block — the D14 snapshot is read
        # standalone, where the top-level disclosure is not in the frame.
        "approximate": approximate,
        "t0": t0.isoformat(),
        "treatment_arm": TREATMENT_ARM,
        "control_arm": CONTROL_ARM,
        "exposed_runtime": EXPOSED_RUNTIME,
        "classification": {
            "no_arm": no_arm,
            "pre_t0": pre_t0,
            "by_arm": counts,
        },
        "balance": balance,
        "h1_primary": _h1_primary(fractions, pp, balance["balance_violated"]),
        "session_level_descriptive": _session_level(pp, graded),
        "h2_noninferiority": _h2_noninferiority(pp),
        "nudge_shown_coverage": await _coverage(replay_redis, pp),
        "notes": list(NOTES),
    }


async def build_arm_comparison(
    replay_redis, evals: list[dict], *, approximate: bool = False,
) -> dict[str, Any]:
    """The `arm_comparison` block on /autopilot/compliance.

    `approximate` is the caller's `scan_evals` cap flag: True means `evals` is
    a truncated population, and every non-error payload repeats it so the
    block stays honest when read on its own.

    Wrapped exception-tight ON PURPOSE: the compliance surface predates this
    experiment and must keep serving its frozen rows even if the analysis
    below cannot run. Failure is a status, never a 500.
    """
    try:
        return await _build(replay_redis, evals, approximate)
    except Exception as exc:  # noqa: BLE001 — see docstring
        return {"status": "error", "confirmatory": False, "error": str(exc)}
