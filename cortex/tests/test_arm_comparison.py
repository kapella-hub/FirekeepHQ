"""Outcome truth PR5 D6/D8/D12/D14 — the `arm_comparison` readout.

This is the ANALYSIS half of a pre-registered experiment, so these tests are
written the way the spec's constants are: as a contract, not as a description
of whatever the code happens to do. The numbers below (5 members, 5 sessions
per member, 99 sessions per arm, +0.10 mean difference, 10pp balance bounds,
the +10pp non-inferiority margin at z=1.645) are REGISTERED VALUES. A test
here failing because someone retuned a constant is the test working.

Two invariants get their own tests because they are the ones a well-meaning
refactor breaks:

  - Absence is never a measured value. A missing receipt is `unknown`, never
    `not_exposed`; a record with no member_token is excluded from the
    member-level analysis rather than being folded in as a pseudo-member.
  - The block never raises. `build_compliance` was a working surface before
    PR5 and its availability is not hostage to this block — a garbage record
    or an exploding dependency degrades to a status, never to a 500.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import fakeredis.aioredis as fr
import pytest

from app.autopilot import arm_comparison as AC
from app.autopilot.arm_comparison import (
    BALANCE_MAX_PP_FRACTION_GAP,
    BALANCE_MAX_SINGLE_MEMBER_SHARE,
    EXPOSED_RUNTIME,
    MIN_ARM_MEAN_DIFF,
    MIN_MEMBERS_PER_ARM,
    MIN_SELF_SUCCESS_PER_ARM,
    MIN_SESSIONS_PER_ARM,
    MIN_SESSIONS_PER_MEMBER,
    NONINFERIORITY_MARGIN,
    NONINFERIORITY_Z,
    build_arm_comparison,
)
from app.briefing.sections import TREATMENT_ARM

T0 = "2026-09-01T00:00:00+00:00"
CONTROL_ARM = "B" if TREATMENT_ARM == "A" else "A"


# --------------------------------------------------------------- fixtures --

@pytest.fixture
def redis():
    return fr.FakeRedis(decode_responses=True)


@pytest.fixture
def t0_set(monkeypatch):
    monkeypatch.setattr(
        AC, "get_settings", lambda: SimpleNamespace(GRADING_NUDGE_T0=T0))


@pytest.fixture
def t0_unset(monkeypatch):
    monkeypatch.setattr(
        AC, "get_settings", lambda: SimpleNamespace(GRADING_NUDGE_T0=""))


def _rec(arm="A", token="m1", delivered=True, runtime="claude",
         created="2026-09-10T00:00:00+00:00", graded=True, briefing="b1",
         self_success=False, skew=False):
    r = {"experiment_group": arm, "member_token": token,
         "briefing_delivered": delivered, "runtime": runtime,
         "created_at": created, "briefing_id": briefing,
         "metrics": {}, "failure_event_ids": []}
    if graded:
        r["task_result"] = "success" if self_success else "partial"
        r["task_result_source"] = "self_reported"
    if self_success and skew:
        r["failure_event_ids"] = ["ev1"]
    return r


def _arm_records(arm, counts, graded_fraction, prefix=""):
    """One arm's per-protocol sessions: `counts` is per-member session counts,
    `graded_fraction` the share of each member's sessions that carry a grade."""
    out = []
    for i, n in enumerate(counts):
        token = f"{prefix}{arm}m{i}"
        n_graded = round(n * graded_fraction)
        for s in range(n):
            out.append(_rec(arm=arm, token=token, graded=(s < n_graded),
                            briefing=f"{token}-b{s}"))
    return out


def _even_arm(arm, members, per_member, graded_fraction):
    return _arm_records(arm, [per_member] * members, graded_fraction)


async def _build(redis, evals):
    return await build_arm_comparison(redis, evals)


# ------------------------------------------------------- pre-registration --

def test_constants_are_the_registered_values():
    """A guard on the pre-registration itself: these are the numbers the
    experiment was registered with, and changing one silently is the failure
    mode the whole D8 gate exists to prevent."""
    assert EXPOSED_RUNTIME == "claude"
    assert MIN_MEMBERS_PER_ARM == 5
    assert MIN_SESSIONS_PER_MEMBER == 5
    assert MIN_SESSIONS_PER_ARM == 99
    assert MIN_ARM_MEAN_DIFF == 0.10
    assert BALANCE_MAX_PP_FRACTION_GAP == 0.10
    assert BALANCE_MAX_SINGLE_MEMBER_SHARE == 0.50
    assert NONINFERIORITY_MARGIN == 0.10
    assert NONINFERIORITY_Z == 1.645
    assert MIN_SELF_SUCCESS_PER_ARM == 30


# ----------------------------------------------------------------- T0 gate --

async def test_unset_t0_is_not_started(redis, t0_unset):
    body = await _build(redis, [_rec()])
    assert body["status"] == "not_started"
    assert body["confirmatory"] is False
    assert "GRADING_NUDGE_T0" in body["note"]
    # Nothing else runs: no classification, no analysis, no numbers at all.
    assert "classification" not in body
    assert "h1_primary" not in body


async def test_unparseable_t0_is_not_started(redis, monkeypatch):
    monkeypatch.setattr(
        AC, "get_settings", lambda: SimpleNamespace(GRADING_NUDGE_T0="soon"))
    body = await _build(redis, [_rec()])
    assert body["status"] == "not_started"


# ------------------------------------------------- D6 classification --

def test_classify_per_protocol():
    arm, bucket, itt = AC._classify(_rec(), AC._parse_t0(T0))
    assert (arm, bucket, itt) == ("A", "per_protocol", True)


def test_classify_undelivered_is_not_exposed():
    """briefing_delivered False is AFFIRMATIVE evidence the text did not
    reach the session — the one branch that may read as not_exposed."""
    _, bucket, itt = AC._classify(_rec(delivered=False), AC._parse_t0(T0))
    assert bucket == "not_exposed"
    assert itt is True


def test_classify_missing_receipt_is_unknown_never_not_exposed():
    """The load-bearing invariant: a NULL receipt is the absence of evidence,
    and absence is never a measured value."""
    _, bucket, itt = AC._classify(_rec(delivered=None), AC._parse_t0(T0))
    assert bucket == "unknown"
    assert itt is True


def test_classify_other_runtime_is_not_exposed():
    _, bucket, _ = AC._classify(_rec(runtime="codex"), AC._parse_t0(T0))
    assert bucket == "not_exposed"


def test_classify_missing_runtime_is_unknown():
    _, bucket, _ = AC._classify(_rec(runtime=None), AC._parse_t0(T0))
    assert bucket == "unknown"


def test_classify_empty_runtime_is_unknown():
    _, bucket, _ = AC._classify(_rec(runtime=""), AC._parse_t0(T0))
    assert bucket == "unknown"


def test_classify_no_arm_is_excluded():
    arm, bucket, itt = AC._classify(_rec(arm=None), AC._parse_t0(T0))
    assert (arm, bucket, itt) == (None, "no_arm", False)


def test_classify_unrecognized_arm_is_excluded():
    _, bucket, itt = AC._classify(_rec(arm="C"), AC._parse_t0(T0))
    assert (bucket, itt) == ("no_arm", False)


def test_classify_pre_t0_is_outside_itt():
    """D4: the experiment begins at T0. A session that ran before it is not
    per-protocol no matter how well-formed its receipts are."""
    rec = _rec(created="2026-08-30T00:00:00+00:00")
    _, bucket, itt = AC._classify(rec, AC._parse_t0(T0))
    assert (bucket, itt) == ("pre_t0", False)


def test_classify_undated_arm_record_is_unknown_and_outside_itt():
    rec = _rec(created=None)
    _, bucket, itt = AC._classify(rec, AC._parse_t0(T0))
    assert (bucket, itt) == ("unknown", False)


async def test_classification_counts_reconcile(redis, t0_set):
    evals = [
        _rec(token="m1"),                                  # PP
        _rec(token="m2", delivered=False),                 # not_exposed
        _rec(token="m3", delivered=None),                  # unknown (in ITT)
        _rec(token="m4", created=None),                    # unknown (undated)
        _rec(token="m5", created="2026-08-01T00:00:00+00:00"),  # pre-T0
        _rec(arm=None),                                    # no arm
    ]
    body = await _build(redis, evals)
    a = body["classification"]["by_arm"]["A"]
    assert body["classification"]["no_arm"] == 1
    assert body["classification"]["pre_t0"] == 1
    assert a["per_protocol"] == 1
    assert a["not_exposed"] == 1
    assert a["unknown"] == 2
    assert a["unknown_undated"] == 1
    # ITT holds only the dated, on-or-after-T0 arm records.
    assert a["itt"] == 3
    assert a["itt"] == (
        a["per_protocol"] + a["not_exposed"] + a["unknown"] - a["unknown_undated"])


async def test_both_arms_always_present_even_when_unobserved(redis, t0_set):
    """A missing arm is a FINDING (assignment broke, or one arm never ran) —
    it must not vanish from the readout the way an unobserved category can."""
    body = await _build(redis, [_rec(arm="A")])
    assert set(body["classification"]["by_arm"]) == {"A", "B"}
    assert body["classification"]["by_arm"]["B"]["itt"] == 0


# ------------------------------------------------------------- D8 floors --

async def test_insufficient_members_reported_with_counts(redis, t0_set):
    """4 qualifying members per arm, each well over the session floors: the
    MEMBER floor is what bites, and the readout says so with numbers."""
    evals = _even_arm("A", 4, 25, 0.8) + _even_arm("B", 4, 25, 0.2)
    body = await _build(redis, evals)
    h1 = body["h1_primary"]
    assert h1["status"] == "insufficient_n"
    assert h1["holds"] is False
    assert h1["qualifying_members"] == {"A": 4, "B": 4}
    assert h1["pp_sessions"] == {"A": 100, "B": 100}
    assert h1["min_members_per_arm"] == MIN_MEMBERS_PER_ARM
    assert h1["min_sessions_per_arm"] == MIN_SESSIONS_PER_ARM


async def test_insufficient_sessions_reported_with_counts(redis, t0_set):
    """5 qualifying members — the member floor clears — but 98 per-protocol
    sessions per arm, one short of the fixed-z bound."""
    counts = [20, 20, 20, 20, 18]  # 98
    evals = (_arm_records("A", counts, 0.8) + _arm_records("B", counts, 0.2))
    body = await _build(redis, evals)
    h1 = body["h1_primary"]
    assert h1["status"] == "insufficient_n"
    assert h1["qualifying_members"] == {"A": 5, "B": 5}
    assert h1["pp_sessions"] == {"A": 98, "B": 98}


async def test_members_below_session_floor_do_not_qualify(redis, t0_set):
    """A member with 4 per-protocol sessions is not a qualifying member, even
    though the arm has plenty of sessions overall."""
    counts = [4, 4, 4, 4, 4, 40, 40]  # 7 members, only 2 qualify, 120 sessions
    evals = _arm_records("A", counts, 0.8) + _arm_records("B", counts, 0.2)
    body = await _build(redis, evals)
    assert body["h1_primary"]["qualifying_members"] == {"A": 2, "B": 2}
    assert body["h1_primary"]["status"] == "insufficient_n"


# ---------------------------------------------------------- H1' primary --

async def test_h1_holds_on_a_clear_win(redis, t0_set):
    """5 members/arm x 20 per-protocol sessions (100/arm), treatment members
    at 0.80 graded and control at 0.20. Exact permutation over C(10,5)=252
    reassignments; only the observed split and its mirror reach |diff|>=0.60."""
    evals = (_even_arm(TREATMENT_ARM, 5, 20, 0.8)
             + _even_arm(CONTROL_ARM, 5, 20, 0.2))
    body = await _build(redis, evals)
    h1 = body["h1_primary"]
    assert h1["status"] == "ok"
    assert h1["holds"] is True
    assert h1["p_value"] < 0.05
    assert h1["method"] == "exact"
    assert h1["treatment_arm"] == TREATMENT_ARM
    assert h1["control_arm"] == CONTROL_ARM
    assert h1["mean_treatment"] == pytest.approx(0.8)
    assert h1["mean_control"] == pytest.approx(0.2)
    assert h1["diff"] == pytest.approx(0.6)
    assert h1["diff"] >= MIN_ARM_MEAN_DIFF


async def test_h1_direction_is_treatment_minus_control(redis, t0_set):
    """The sign is computed through TREATMENT_ARM, never as A-minus-B: with
    the CONTROL arm grading better, diff must be NEGATIVE and holds False
    however the arms happen to be lettered."""
    evals = (_even_arm(TREATMENT_ARM, 5, 20, 0.2)
             + _even_arm(CONTROL_ARM, 5, 20, 0.8))
    h1 = (await _build(redis, evals))["h1_primary"]
    assert h1["status"] == "ok"
    assert h1["diff"] == pytest.approx(-0.6)
    assert h1["holds"] is False


async def test_h1_significant_but_below_the_practical_floor_does_not_hold(
        redis, t0_set):
    """p can be tiny on a difference nobody would act on. The registered
    practical-significance gate is what stops a 5pp win being called a win."""
    evals = (_even_arm(TREATMENT_ARM, 5, 20, 0.55)
             + _even_arm(CONTROL_ARM, 5, 20, 0.5))
    h1 = (await _build(redis, evals))["h1_primary"]
    assert h1["status"] == "ok"
    assert h1["p_value"] < 0.05
    assert h1["diff"] == pytest.approx(0.05)
    assert h1["diff"] < MIN_ARM_MEAN_DIFF
    assert h1["holds"] is False


async def test_members_without_a_token_are_excluded_not_pooled(redis, t0_set):
    """A record with no member_token cannot be attributed to a randomization
    unit. It is disclosed as an `unknown` member group and kept OUT of the
    member-level analysis — pooling it as one pseudo-member would invent a
    member the randomization never assigned."""
    evals = (_even_arm(TREATMENT_ARM, 5, 20, 0.8)
             + _even_arm(CONTROL_ARM, 5, 20, 0.2)
             + [_rec(arm=TREATMENT_ARM, token=None, briefing=f"nb{i}")
                for i in range(10)])
    body = await _build(redis, evals)
    assert body["h1_primary"]["qualifying_members"][TREATMENT_ARM] == 5
    per_member = body["balance"]["by_arm"][TREATMENT_ARM]["sessions_per_member"]
    assert per_member["unknown"] == 10
    assert body["h1_primary"]["mean_treatment"] == pytest.approx(0.8)


# ------------------------------------------------------------- balance --

async def test_balance_bound_a_pp_fraction_gap(redis, t0_set):
    """D6 absolute bound (a): equal per-protocol counts, but the control arm
    carries twice the ITT — a 50pp per-protocol-fraction gap. Differential
    attrition, not a treatment effect, and the primary analysis says so."""
    evals = (_even_arm(TREATMENT_ARM, 5, 2, 0.8)
             + _even_arm(CONTROL_ARM, 5, 2, 0.2)
             + [_rec(arm=CONTROL_ARM, token=f"x{i}", delivered=False,
                     briefing=f"xb{i}") for i in range(10)])
    body = await _build(redis, evals)
    assert body["balance"]["balance_violated"] is True
    assert body["balance"]["by_arm"][TREATMENT_ARM]["pp_fraction"] == 1.0
    assert body["balance"]["by_arm"][CONTROL_ARM]["pp_fraction"] == 0.5
    assert body["balance"]["pp_fraction_gap"] == pytest.approx(0.5)
    assert body["h1_primary"]["status"] == "balance_violated"
    assert body["h1_primary"]["holds"] is False
    # Descriptive numbers survive the verdict.
    assert body["h1_primary"]["pp_sessions"] == {TREATMENT_ARM: 10,
                                                 CONTROL_ARM: 10}


async def test_balance_bound_b_single_member_dominance(redis, t0_set):
    """D6 absolute bound (b): one member owning more than half an arm's
    per-protocol sessions makes the arm a measurement of that person."""
    evals = _arm_records("A", [6, 4], 0.8) + _arm_records("B", [2] * 5, 0.2)
    body = await _build(redis, evals)
    assert body["balance"]["by_arm"]["A"]["max_single_member_share"] == 0.6
    assert body["balance"]["balance_violated"] is True
    assert body["h1_primary"]["status"] == "balance_violated"


async def test_balance_share_exactly_at_the_bound_does_not_violate(
        redis, t0_set):
    """The bound is strictly-greater-than: exactly 50% is the registered
    boundary, and a boundary that fires ON the value is a different bound."""
    evals = _arm_records("A", [5, 5], 0.8) + _arm_records("B", [2] * 5, 0.2)
    body = await _build(redis, evals)
    assert body["balance"]["by_arm"]["A"]["max_single_member_share"] == 0.5
    assert body["balance"]["balance_violated"] is False


async def test_balanced_experiment_is_not_flagged(redis, t0_set):
    evals = (_even_arm("A", 5, 20, 0.8) + _even_arm("B", 5, 20, 0.2))
    body = await _build(redis, evals)
    assert body["balance"]["balance_violated"] is False
    assert body["balance"]["violations"] == []
    assert body["balance"]["bounds"] == {
        "pp_fraction_gap": BALANCE_MAX_PP_FRACTION_GAP,
        "single_member_share": BALANCE_MAX_SINGLE_MEMBER_SHARE,
    }


async def test_runtime_mix_reported_over_itt(redis, t0_set):
    evals = [
        _rec(token="m1"),
        _rec(token="m2", runtime="codex"),
        _rec(token="m3", runtime=None),
    ]
    mix = (await _build(redis, evals))["balance"]["by_arm"]["A"]["runtime_mix"]
    assert mix == {"claude": 1, "codex": 1, "unknown": 1}


# --------------------------------------------- session-level descriptive --

async def test_session_level_is_labelled_descriptive_only(redis, t0_set):
    """Sessions cluster within members; the session-level 2x2 is reported for
    continuity with the round-3 table but must never read as the primary."""
    evals = _even_arm("A", 5, 20, 0.8) + _even_arm("B", 5, 20, 0.2)
    desc = (await _build(redis, evals))["session_level_descriptive"]
    assert "descriptive only" in desc["note"]
    assert "permutation" in desc["note"]
    assert desc["by_arm"]["A"] == {"per_protocol": 100, "graded": 80,
                                   "rate": 0.8}
    assert desc["by_arm"]["B"] == {"per_protocol": 100, "graded": 20,
                                   "rate": 0.2}
    assert desc["chi2"] > 0
    assert desc["p_value"] < 0.05
    assert desc["cohens_h"] > 0
    assert len(desc["ci_diff"]) == 2


async def test_session_level_null_when_an_arm_is_empty(redis, t0_set):
    desc = (await _build(redis, [_rec()]))["session_level_descriptive"]
    assert desc["chi2"] is None
    assert desc["p_value"] is None
    assert desc["ci_diff"] is None


async def test_session_level_survives_a_degenerate_table(redis, t0_set):
    """Every per-protocol session graded in BOTH arms is a zero column: no
    association to test. `_chi_square_2x2`'s stdlib path returns (0.0, 1.0)
    for it while its scipy path raises on the zero expected frequency, so
    without a guard this block would report different numbers on a box with
    scipy than in CI's stdlib-only job — and a pre-registered analysis cannot
    depend on what is installed."""
    evals = _even_arm("A", 2, 3, 1.0) + _even_arm("B", 2, 3, 1.0)
    body = await _build(redis, evals)
    assert body["status"] == "ok"
    desc = body["session_level_descriptive"]
    assert desc["by_arm"]["A"] == {"per_protocol": 6, "graded": 6, "rate": 1.0}
    assert desc["chi2"] == 0.0
    assert desc["p_value"] == 1.0


# ------------------------------------------------- H2' non-inferiority --

def _skew_arm(arm, n_success, n_skew, prefix="s"):
    return [
        _rec(arm=arm, token=f"{prefix}{arm}m{i}", briefing=f"{prefix}{arm}b{i}",
             self_success=True, skew=(i < n_skew))
        for i in range(n_success)
    ]


async def test_h2_holds_when_treatment_skew_is_not_worse(redis, t0_set):
    evals = (_skew_arm(TREATMENT_ARM, 30, 0) + _skew_arm(CONTROL_ARM, 30, 0))
    h2 = (await _build(redis, evals))["h2_noninferiority"]
    assert h2["status"] == "ok"
    assert h2["holds"] is True
    assert h2["by_arm"][TREATMENT_ARM]["self_success"] == 30
    assert h2["treatment_skew_rate"] == 0.0
    assert h2["upper_bound"] < NONINFERIORITY_MARGIN


async def test_h2_fails_when_treatment_skew_is_far_worse(redis, t0_set):
    """The nudge asking for grades must not buy adoption with dishonesty:
    20/30 treatment sessions self-reporting success while carrying an
    independent failure contradiction blows through both conditions."""
    evals = (_skew_arm(TREATMENT_ARM, 30, 20) + _skew_arm(CONTROL_ARM, 30, 0))
    h2 = (await _build(redis, evals))["h2_noninferiority"]
    assert h2["status"] == "ok"
    assert h2["holds"] is False
    assert h2["treatment_skew_rate"] == pytest.approx(20 / 30, abs=1e-4)
    assert h2["upper_bound"] > NONINFERIORITY_MARGIN


async def test_h2_fails_on_the_absolute_skew_ceiling_alone(redis, t0_set):
    """Both arms equally dishonest is not non-inferiority worth reporting.
    140 sessions per arm is enough for the interval condition to PASS on a
    zero difference, so the only thing failing here is the absolute ceiling —
    non-inferiority to a broken control is not a result."""
    evals = (_skew_arm(TREATMENT_ARM, 140, 70) + _skew_arm(CONTROL_ARM, 140, 70))
    h2 = (await _build(redis, evals))["h2_noninferiority"]
    assert h2["status"] == "ok"
    assert h2["treatment_skew_rate"] == 0.5
    assert h2["diff"] == pytest.approx(0.0)
    assert h2["upper_bound"] < NONINFERIORITY_MARGIN  # interval condition met
    assert h2["holds"] is False                       # ceiling alone fails it


async def test_h2_insufficient_below_thirty_self_success(redis, t0_set):
    evals = (_skew_arm(TREATMENT_ARM, 29, 0) + _skew_arm(CONTROL_ARM, 30, 0))
    h2 = (await _build(redis, evals))["h2_noninferiority"]
    assert h2["status"] == "insufficient_n"
    assert h2["holds"] is False
    assert h2["by_arm"][TREATMENT_ARM]["self_success"] == 29
    assert h2["min_self_success_per_arm"] == MIN_SELF_SUCCESS_PER_ARM


async def test_h2_counts_only_per_protocol_sessions(redis, t0_set):
    """An ITT-but-not-exposed session cannot speak to what the intervention
    did to grading honesty."""
    evals = (_skew_arm(TREATMENT_ARM, 30, 0) + _skew_arm(CONTROL_ARM, 30, 0)
             + [_rec(arm=TREATMENT_ARM, token="nx", delivered=False,
                     briefing=f"nx{i}", self_success=True, skew=True)
                for i in range(20)])
    h2 = (await _build(redis, evals))["h2_noninferiority"]
    assert h2["by_arm"][TREATMENT_ARM]["self_success"] == 30
    assert h2["by_arm"][TREATMENT_ARM]["skew_hits"] == 0


# ------------------------------------------------ nudge_shown coverage --

async def test_coverage_counts_receipts_for_treatment_pp_records(redis, t0_set):
    await redis.set("rp:nudge_shown:b1", TREATMENT_ARM)
    evals = [
        _rec(arm=TREATMENT_ARM, token="m1", briefing="b1"),
        _rec(arm=TREATMENT_ARM, token="m2", briefing="b2"),
    ]
    cov = (await _build(redis, evals))["nudge_shown_coverage"]
    assert cov["ids_checked"] == 2
    assert cov["receipts_found"] == 1
    assert cov["coverage"] == 0.5
    assert cov["flagged"] is True


async def test_full_coverage_is_not_flagged(redis, t0_set):
    for i in range(10):
        await redis.set(f"rp:nudge_shown:b{i}", TREATMENT_ARM)
    evals = [_rec(arm=TREATMENT_ARM, token=f"m{i}", briefing=f"b{i}")
             for i in range(10)]
    cov = (await _build(redis, evals))["nudge_shown_coverage"]
    assert cov["coverage"] == 1.0
    assert cov["flagged"] is False


async def test_coverage_is_null_with_no_briefing_ids(redis, t0_set):
    """No ids is not 0% coverage — it is no measurement, and a bare 0.0 here
    would read as a broken nudge rather than an empty window."""
    evals = [_rec(arm=TREATMENT_ARM, token="m1", briefing=None)]
    cov = (await _build(redis, evals))["nudge_shown_coverage"]
    assert cov["coverage"] is None
    assert cov["flagged"] is False
    assert cov["ids_checked"] == 0


async def test_coverage_ignores_the_control_arm(redis, t0_set):
    """Control is the ABSENCE of the section, so a control briefing has no
    nudge_shown receipt by construction — counting them would manufacture a
    coverage failure out of the experiment working correctly."""
    await redis.set("rp:nudge_shown:t1", TREATMENT_ARM)
    evals = [
        _rec(arm=TREATMENT_ARM, token="m1", briefing="t1"),
        _rec(arm=CONTROL_ARM, token="m2", briefing="c1"),
    ]
    cov = (await _build(redis, evals))["nudge_shown_coverage"]
    assert cov["ids_checked"] == 1
    assert cov["coverage"] == 1.0


async def test_coverage_ignores_non_per_protocol_records(redis, t0_set):
    evals = [_rec(arm=TREATMENT_ARM, token="m1", briefing="b1",
                  delivered=False)]
    cov = (await _build(redis, evals))["nudge_shown_coverage"]
    assert cov["ids_checked"] == 0
    assert cov["coverage"] is None


# ------------------------------------------------------- never raises --

async def test_garbage_record_does_not_crash_the_block(redis, t0_set):
    """A record with nothing but an arm must degrade to `unknown`, not to an
    exception the wrapper has to catch."""
    body = await _build(redis, [{"experiment_group": "A"}, _rec()])
    assert body["status"] == "ok"
    assert body["classification"]["by_arm"]["A"]["unknown"] == 1


async def test_empty_evals_is_a_clean_readout(redis, t0_set):
    body = await _build(redis, [])
    assert body["status"] == "ok"
    assert body["h1_primary"]["status"] == "insufficient_n"
    assert body["nudge_shown_coverage"]["coverage"] is None


async def test_exception_degrades_to_status_error(redis, monkeypatch):
    """The compliance surface's availability is not hostage to this block."""
    def boom():
        raise RuntimeError("settings exploded")
    monkeypatch.setattr(AC, "get_settings", boom)
    body = await _build(redis, [_rec()])
    assert body["status"] == "error"
    assert body["confirmatory"] is False
    assert "settings exploded" in body["error"]


async def test_redis_failure_degrades_to_status_error(redis, t0_set):
    class Exploding:
        async def mget(self, *a, **k):
            raise RuntimeError("redis down")
    body = await _build(Exploding(), [_rec(arm=TREATMENT_ARM)])
    assert body["status"] == "error"
    assert "redis down" in body["error"]


# ------------------------------------------------------ D14 disclosure --

async def test_never_claims_to_be_confirmatory(redis, t0_set):
    """D14: the verdict of record is the dated T0+28d snapshot committed as a
    spec addendum. Every live view — including a live view showing `holds` —
    is operational monitoring, and says so in the payload."""
    evals = _even_arm(TREATMENT_ARM, 5, 20, 0.8) + _even_arm(CONTROL_ARM, 5, 20, 0.2)
    body = await _build(redis, evals)
    assert body["h1_primary"]["holds"] is True
    assert body["confirmatory"] is False
    assert any("T0+28d" in n for n in body["notes"])
    assert any("operational monitoring" in n for n in body["notes"])


async def test_interference_caveat_is_part_of_the_contract(redis, t0_set):
    body = await _build(redis, [_rec()])
    assert any("spillover" in n and "bias toward null" in n
               for n in body["notes"])
    assert any("ambient saturation" in n for n in body["notes"])


# ------------------------------------------------- wired into compliance --

async def test_surfaced_on_the_compliance_payload(t0_set):
    from app.autopilot import compliance as comp
    r = fr.FakeRedis(decode_responses=True)
    await r.set("rp:eval:s1", json.dumps(_rec(token="m1")))
    body = await comp.build_compliance(r)
    assert body["arm_comparison"]["status"] == "ok"
    assert body["arm_comparison"]["confirmatory"] is False


async def test_compliance_still_serves_when_the_block_errors(monkeypatch):
    from app.autopilot import compliance as comp

    def boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(AC, "get_settings", boom)
    r = fr.FakeRedis(decode_responses=True)
    await r.set("rp:eval:s1", json.dumps(_rec(token="m1")))
    body = await comp.build_compliance(r)
    assert body["arm_comparison"]["status"] == "error"
    assert body["sessions_evaluated"] == 1  # the frozen surface is untouched
