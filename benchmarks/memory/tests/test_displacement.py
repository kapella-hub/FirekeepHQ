"""Guards for the displacement analysis.

The load-bearing test is `test_reproduces_the_hand_audited_dreaming_ab`, which
pins the module to the two REAL run records on disk and the findings a human
computed from them by hand. It now runs WITHOUT the 265 MB dataset — see the
comment on `EVIDENCE_FIXTURE` — because a guarantee that only holds on a
machine that happened to download a gitignored file is not a guarantee.

Everything else is a defensive-shape case (the module must fail loud, never
compute over a partial join), a gate-rule case (the threshold must fire on a
pattern and not on a single event, and must not be vetoed by gains on unrelated
questions), or a positive-control case (a comparison whose 'after' leg recalled
nothing must be refused, not certified).
"""
import json
import math
from pathlib import Path

import pytest

from bench import displacement

RESULTS = Path(__file__).resolve().parents[1] / "results"
DATASET = Path(__file__).resolve().parents[1] / "data" / "longmemeval_s.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EVIDENCE_FIXTURE = FIXTURES / "ab_answer_session_ids.json"
PRE_DREAM = RESULTS / "20260805-093535-pre-dream.json"
POST_DREAM = RESULTS / "20260806-070427-post-dream.json"

# The ground-truth tests used to be skipped unless `data/longmemeval_s.json`
# was on disk — a 265 MB file that `.gitignore` excludes and that no CI job
# fetches. So the three assertions pinning this module to the hand audit ran
# only as a local courtesy, which is not a guarantee at all.
#
# The evidence join is the only thing they needed the dataset FOR, and it is
# 32 KB of it: `{question_id: answer_session_ids}`, exactly the block
# `bench.score_retrieval.displacement_facts` now stamps into every run record.
# (The two committed A/B records predate that stamp — they carry
# `per_question` rows but no `displacement` block — so reading it back off the
# records themselves is not available for these two files, though it will be
# for every record written from now on.) Committing that extract is what makes
# the pinning tests runnable anywhere the records are.
#
# `known_session_ids` is deliberately NOT extracted: it needs every question's
# ~48 haystack ids, 430 KB rather than 32 KB, to assert a number measured at
# zero. That one assertion stays dataset-gated and says so.
_MISSING = [p.name for p in (PRE_DREAM, POST_DREAM) if not p.exists()]
_MISSING_RECORDS = bool(_MISSING)
# Name the file. A skip that says "needs the committed A/B run records" reads
# like an environment prerequisite; one that names an absent artefact reads
# like the missing commit it actually is.
_MISSING_REASON = (
    f"these A/B run records are not in the tree: {', '.join(_MISSING)} — the "
    "hand-audit pins cannot run until they are committed"
)


def _committed_evidence() -> dict:
    """The hand audit's evidence join, from the committed fixture."""
    return json.loads(EVIDENCE_FIXTURE.read_text(encoding="utf-8"))["answer_session_ids"]


def _ab_records():
    return (json.loads(PRE_DREAM.read_text(encoding="utf-8")),
            json.loads(POST_DREAM.read_text(encoding="utf-8")))


def _row(qid, *session_ids, error=None):
    """A per-question audit row in the published shape."""
    return {
        "question_id": qid,
        "hits": [{"session_id": s, "rank": i + 1} for i, s in enumerate(session_ids)],
        "error": error,
    }


def _compare(before, after, evidence, **kw):
    return displacement.compare_displacement(before, after, evidence, **kw)


def _counts(*, completed, last=None, skipped=0, errored=0):
    """A `recall_counts` block in the shape `bench.run._record_recall_counts`
    writes: cumulative total plus what the final invocation executed."""
    return {"recall_counts": {
        "completed": completed, "skipped": skipped, "errored": errored,
        "invocations": 1,
        "completed_last_invocation": completed if last is None else last,
    }}


def _ran(n=500):
    """An 'after' score block whose final invocation demonstrably recalled."""
    return _counts(completed=n)


# ---------------------------------------------------------------------------
# Ground truth. These two files are committed; the numbers below were computed
# by hand from them before this module existed.
# ---------------------------------------------------------------------------

def test_the_committed_evidence_fixture_matches_the_dataset():
    """The fixture is an extract, so it can drift from its source. This is the
    one ground-truth test that genuinely needs the 265 MB file, and it is the
    right one to gate on it: when the dataset IS present the extract is proved
    faithful, and when it is not, every test below still runs against an
    extract that was proved faithful the last time anyone had the file."""
    if not DATASET.exists():
        pytest.skip("needs the downloaded dataset (data/longmemeval_s.json)")
    evidence, _ = displacement.evidence_from_dataset(DATASET)
    committed = _committed_evidence()
    assert set(committed) == set(evidence)
    assert all(sorted(committed[q]) == sorted(evidence[q]) for q in evidence)


@pytest.mark.skipif(_MISSING_RECORDS, reason=_MISSING_REASON)
def test_reproduces_the_hand_audited_dreaming_ab():
    """Runs WITHOUT the dataset: the evidence join comes from the committed
    32 KB extract."""
    before, after = _ab_records()
    out = displacement.compare_displacement_files(
        before, after, _committed_evidence())

    assert set(out) == {"bench", "defaults"}

    # defaults (k=3): dreams never reached top-3 at all.
    d = out["defaults"]["metrics"]
    assert d["k"] == 3
    assert d["changed_count"] == 0
    assert d["untagged_slots_before"] == 0 and d["untagged_slots_after"] == 0
    assert d["net_evidence_delta"] == 0
    assert out["defaults"]["regressed"] is False

    # bench (k=10): two result sets changed, three slots went to dream points.
    b = out["bench"]["metrics"]
    assert b["k"] == 10
    assert b["changed_count"] == 2
    assert b["changed_questions"] == ["00ca467f", "031748ae_abs"]
    assert b["untagged_slots_before"] == 0
    assert b["untagged_slots_after"] == 3

    # Exactly one question lost evidence, and it lost exactly one hit.
    assert b["lost_evidence_count"] == 1
    assert b["gained_evidence_count"] == 0
    (lost,) = b["lost_evidence_questions"]
    assert lost["question_id"] == "031748ae_abs"
    assert (lost["evidence_before"], lost["evidence_after"]) == (9, 8)
    assert lost["delta"] == -1
    assert b["net_evidence_delta"] == -1

    # 00ca467f changed but stayed neutral — a dream took a slot from a
    # non-evidence session, which is not displacement of evidence.
    assert "00ca467f" not in [q["question_id"] for q in b["lost_evidence_questions"]]
    assert "00ca467f" not in [q["question_id"] for q in b["gained_evidence_questions"]]

    # The loss sits entirely in the abstention half — which `score_run`
    # excludes from every aggregate, so the mean-based gate could not have
    # seen it even in principle.
    assert b["scored"]["lost_evidence_count"] == 0
    assert b["abstention"]["lost_evidence_count"] == 1

    # One event is not a pattern: reported, warned about, not gated.
    assert out["bench"]["regressed"] is False
    assert any("BELOW GATE" in w for w in out["bench"]["warnings"])


@pytest.mark.skipif(
    _MISSING_RECORDS or not DATASET.exists(),
    reason="foreign-session detection needs the dataset's haystack ids",
)
def test_the_measured_run_carries_no_foreign_session_slots():
    """The brief's premise, checked rather than assumed: a dream point surfaces
    as an UNTAGGED hit (`session_id is None`), not as a hit naming some other
    session. If this ever fails, `untagged_slots` has stopped being the right
    instrument and the module needs a second one.

    This is the one assertion that genuinely cannot be made from the records:
    "foreign" means "outside this question's own haystack", and the haystack
    ids exist only in the dataset. Extracting them too would be 430 KB of
    committed fixture to assert a zero, against 32 KB for the evidence join
    every other ground-truth assertion needs. It is split out rather than left
    to gate the whole file — which is what it used to do."""
    before, after = _ab_records()
    evidence, known = displacement.evidence_from_dataset(DATASET)
    out = displacement.compare_displacement_files(
        before, after, evidence, known_sessions=known)
    for analysis in out.values():
        assert analysis["metrics"]["foreign_session_slots_before"] == 0
        assert analysis["metrics"]["foreign_session_slots_after"] == 0


@pytest.mark.skipif(_MISSING_RECORDS, reason=_MISSING_REASON)
def test_the_hand_audited_loss_is_far_below_the_aggregate_tolerance():
    """Why this module exists, expressed as an assertion rather than prose: the
    one lost evidence hit shifts the aggregate by orders of magnitude less than
    the gate's tolerance, so no tightening of `--tolerance` reaches it.

    This one never needed the dataset at all — it reads only the two records'
    own `overall` blocks — and was skipped for want of it anyway."""
    from bench import dream_ab

    before, after = _ab_records()
    comparisons = dream_ab.compare_result_files(before, after)
    for metric, delta in comparisons["bench"]["deltas"].items():
        assert abs(delta) < 0.005, metric
    assert comparisons["bench"]["regressed"] is False


# ---------------------------------------------------------------------------
# Core behaviour.
# ---------------------------------------------------------------------------

def test_an_unchanged_pair_reports_nothing_and_does_not_gate():
    rows = [_row("q1", "e1", "x"), _row("q2", "e2", "y")]
    out = _compare(rows, [dict(r) for r in rows], {"q1": ["e1"], "q2": ["e2"]},
                   after_run=_ran(2))
    m = out["metrics"]
    assert m["changed_count"] == 0
    assert m["net_evidence_delta"] == 0
    assert m["lost_evidence_questions"] == []
    assert out["regressed"] is False
    assert out["warnings"] == []


def test_a_comparison_with_no_provenance_is_unverified_not_silently_clean():
    """The default must fail toward visible doubt. A caller who hands the pure
    primitive two row lists has made no claim about where the 'after' rows came
    from, and a clean table over rows of unknown provenance is exactly the
    reassuring-looking output this module must never emit without a caveat."""
    rows = [_row("q1", "e1", "x")]
    out = _compare(rows, [dict(r) for r in rows], {"q1": ["e1"]})
    assert out["regressed"] is False
    assert out["metrics"]["recall_provenance"]["status"] == "unverified"
    assert out["metrics"]["recall_provenance"]["supplied"] is False
    assert any("UNVERIFIED" in w for w in out["warnings"])


def test_a_reordering_that_keeps_every_evidence_hit_is_change_without_loss():
    before = [_row("q1", "e1", "x", "y")]
    after = [_row("q1", "x", "e1", "y")]
    out = _compare(before, after, {"q1": ["e1"]})
    assert out["metrics"]["changed_count"] == 1
    assert out["metrics"]["net_evidence_delta"] == 0
    assert out["metrics"]["lost_evidence_count"] == 0
    assert out["regressed"] is False


def test_an_untagged_hit_that_displaces_a_distractor_is_neutral():
    """The `00ca467f` shape: a dream took a slot, but the slot held a
    non-evidence session. Untagged count rises, evidence count does not move,
    and nothing gates."""
    before = [_row("q1", "e1", "distractor")]
    after = [_row("q1", "e1", None)]
    out = _compare(before, after, {"q1": ["e1"]})
    m = out["metrics"]
    assert m["untagged_slot_delta"] == 1
    assert m["net_evidence_delta"] == 0
    assert m["lost_evidence_count"] == 0
    assert m["questions_with_untagged_slots_after"] == ["q1"]
    assert out["regressed"] is False


def test_an_untagged_hit_that_displaces_evidence_is_recorded_as_a_loss():
    before = [_row("q1", "e1", "e1")]
    after = [_row("q1", "e1", None)]
    out = _compare(before, after, {"q1": ["e1"]})
    (lost,) = out["metrics"]["lost_evidence_questions"]
    assert lost["evidence_before"] == 2 and lost["evidence_after"] == 1
    assert out["metrics"]["net_evidence_delta"] == -1


def test_top_k_truncation_is_applied_before_anything_is_counted():
    """A hit past k did not occupy a slot the product spent, so it must not
    count as evidence held — nor as evidence lost when it disappears."""
    before = [_row("q1", "x", "y", "e1")]
    after = [_row("q1", "x", "y", "z")]
    out = _compare(before, after, {"q1": ["e1"]}, k=2)
    assert out["metrics"]["changed_count"] == 0
    assert out["metrics"]["evidence_hits_before"] == 0
    assert out["metrics"]["net_evidence_delta"] == 0


def test_gained_evidence_is_reported_separately_from_lost():
    before = [_row("q1", "x", "y"), _row("q2", "e2", "e2")]
    after = [_row("q1", "e1", "y"), _row("q2", "e2", "x")]
    out = _compare(before, after, {"q1": ["e1"], "q2": ["e2"]})
    m = out["metrics"]
    assert [q["question_id"] for q in m["gained_evidence_questions"]] == ["q1"]
    assert [q["question_id"] for q in m["lost_evidence_questions"]] == ["q2"]
    assert m["net_evidence_delta"] == 0


def test_abstention_and_scored_questions_are_split_but_both_counted():
    before = [_row("q1", "e1", "e1"), _row("q2_abs", "a1", "a1")]
    after = [_row("q1", "e1", None), _row("q2_abs", "a1", None)]
    out = _compare(before, after, {"q1": ["e1"], "q2_abs": ["a1"]})
    m = out["metrics"]
    assert m["scored"]["lost_evidence_count"] == 1
    assert m["abstention"]["lost_evidence_count"] == 1
    assert m["lost_evidence_count"] == 2          # the gate counts both
    assert m["net_evidence_delta"] == -2


# ---------------------------------------------------------------------------
# Rank-level detail.
# ---------------------------------------------------------------------------

def test_rank_detail_names_the_ranks_the_lost_evidence_held_before():
    before = [_row("q1", "e1", "e1", "x", "e1")]
    after = [_row("q1", "e1", "e1", "x", None)]
    (lost,) = _compare(before, after, {"q1": ["e1"]})["metrics"]["lost_evidence_questions"]
    (session,) = lost["sessions"]
    assert session["session_id"] == "e1"
    assert session["ranks_before"] == [1, 2, 4]
    assert session["ranks_after"] == [1, 2]
    assert session["lost_ranks_before"] == [4]


def test_a_loss_is_attributed_to_the_deepest_ranks_it_held():
    """The documented convention: occurrences of one session are
    interchangeable, so n lost occurrences are attributed to the n deepest
    ranks — what a top-k truncation actually does."""
    before = [_row("q1", "e1", "e1", "e1", "e1")]
    after = [_row("q1", "e1", "e1", None, None)]
    (lost,) = _compare(before, after, {"q1": ["e1"]})["metrics"]["lost_evidence_questions"]
    (session,) = lost["sessions"]
    assert session["lost_ranks_before"] == [3, 4]


def test_rank_detail_distinguishes_two_evidence_sessions_moving_opposite_ways():
    before = [_row("q1", "e1", "e2", "e2")]
    after = [_row("q1", "e1", "e1", "e2")]
    out = _compare(before, after, {"q1": ["e1", "e2"]})
    # Net zero, so neither lost nor gained lists the question.
    assert out["metrics"]["net_evidence_delta"] == 0
    assert out["metrics"]["lost_evidence_questions"] == []
    detail = displacement.evidence_rank_detail(
        ["e1", "e2", "e2"], ["e1", "e1", "e2"], frozenset({"e1", "e2"}))
    by_session = {d["session_id"]: d for d in detail}
    assert by_session["e1"]["delta"] == 1
    assert by_session["e2"]["delta"] == -1
    assert by_session["e2"]["lost_ranks_before"] == [3]


# ---------------------------------------------------------------------------
# Rank degradation of evidence that stayed in top-k.
# ---------------------------------------------------------------------------

def test_evidence_sliding_down_inside_top_k_is_reported_not_invisible():
    """The defect: evidence moving from rank 1 to rank 9 within k=10 produced
    `changed=1, lost=0, net=0`, verdict OK, and NO row anywhere in the report
    said anything had happened — while that question's MRR went 1.00 -> 0.11,
    which averages to ~1e-3 over 500 questions."""
    before = [_row("q1", "e1", *[f"x{i}" for i in range(9)])]
    after = [_row("q1", *[f"x{i}" for i in range(8)], "e1", "x8")]
    out = _compare(before, after, {"q1": ["e1"]}, k=10, after_run=_ran())
    rank = out["metrics"]["rank_shift"]
    assert out["metrics"]["lost_evidence_count"] == 0     # nothing left top-k
    assert rank["questions"] == 1
    assert rank["worst_shift"] == 8
    assert rank["worst_question"] == "q1"
    assert rank["degraded_count"] == 1
    assert rank["degraded_questions"][0]["rank_before"] == 1
    assert rank["degraded_questions"][0]["rank_after"] == 9
    assert any("RANK DEGRADATION" in w for w in out["warnings"])


def test_rank_degradation_warns_and_never_gates():
    """The deliberate choice: evidence still inside the top-k the product
    spends has not been displaced OUT of it, and reordering is what dreams are
    for. Visible, not fatal."""
    before = [_row(f"q{i}", "e", "x", "x", "x") for i in range(50)]
    after = [_row(f"q{i}", "x", "x", "x", "e") for i in range(50)]
    out = _compare(before, after, {f"q{i}": ["e"] for i in range(50)},
                   k=4, after_run=_ran())
    assert out["metrics"]["rank_shift"]["degraded_count"] == 50
    assert out["regressed"] is False
    assert any("RANK DEGRADATION" in w for w in out["warnings"])


def test_a_small_rank_shift_is_counted_but_not_named():
    """Sub-threshold movement is the noise floor of inserting points into a
    deterministic ranking — reported in the mean so it is never hidden, but not
    called out question by question."""
    before = [_row("q1", "e1", "x", "y")]
    after = [_row("q1", "x", "e1", "y")]
    out = _compare(before, after, {"q1": ["e1"]}, after_run=_ran())
    rank = out["metrics"]["rank_shift"]
    assert rank["mean_shift"] == 1.0
    assert rank["worst_shift"] == 1
    assert rank["degraded_count"] == 0
    assert not any("RANK DEGRADATION" in w for w in out["warnings"])


def test_the_rank_shift_threshold_is_configurable():
    before = [_row("q1", "e1", "x", "y")]
    after = [_row("q1", "x", "e1", "y")]
    out = _compare(before, after, {"q1": ["e1"]}, rank_shift_warn=1,
                   after_run=_ran())
    assert out["metrics"]["rank_shift"]["degraded_count"] == 1


def test_evidence_that_left_top_k_is_a_loss_not_a_rank_shift():
    """A question with no evidence on one side has no comparable rank, so
    counting it as a shift would double-count the loss already recorded."""
    before = [_row("q1", "x", "e1")]
    after = [_row("q1", "x", "y")]
    out = _compare(before, after, {"q1": ["e1"]}, after_run=_ran())
    assert out["metrics"]["lost_evidence_count"] == 1
    assert out["metrics"]["rank_shift"]["questions"] == 0
    assert out["metrics"]["rank_shift"]["degraded_count"] == 0


def test_an_improving_rank_is_not_reported_as_a_worst_case():
    before = [_row("q1", "x", "y", "e1")]
    after = [_row("q1", "e1", "x", "y")]
    out = _compare(before, after, {"q1": ["e1"]}, after_run=_ran())
    rank = out["metrics"]["rank_shift"]
    assert rank["mean_shift"] == -2.0
    assert rank["improved_count"] == 1
    assert rank["worst_question"] is None
    assert rank["degraded_count"] == 0


def test_the_rendered_report_names_a_degraded_question(tmp_path):
    before = _record({"bench": [_row("q1", "e1", "x", "y", "z")]}, {"bench": {"k": 10}})
    after = _record({"bench": [_row("q1", "x", "y", "z", "e1")]}, {"bench": {"k": 10}})
    md = displacement.render_markdown(
        displacement.compare_displacement_files(before, after, {"q1": ["e1"]}))
    assert "best evidence rank shift" in md
    assert "1 -> 4" in md


# ---------------------------------------------------------------------------
# The gate rule.
# ---------------------------------------------------------------------------

def _losing_pair(n_lost: int, n_total: int):
    """`n_total` questions, the first `n_lost` of which each lose one evidence
    hit to an untagged slot."""
    before, after, evidence = [], [], {}
    for i in range(n_total):
        qid = f"q{i:03d}"
        evidence[qid] = ["e"]
        before.append(_row(qid, "e", "e"))
        after.append(_row(qid, "e", None) if i < n_lost else _row(qid, "e", "e"))
    return before, after, evidence


def test_one_lost_question_is_noise_and_does_not_gate():
    out = _compare(*_losing_pair(1, 500))
    assert out["regressed"] is False
    assert any("BELOW GATE" in w for w in out["warnings"])


def test_two_lost_questions_still_do_not_gate_at_the_default_floor():
    out = _compare(*_losing_pair(2, 500))
    assert out["regressed"] is False


def test_three_lost_questions_are_a_pattern_and_gate():
    out = _compare(*_losing_pair(3, 500))
    assert out["regressed"] is True
    assert "EVIDENCE DISPLACEMENT" in out["verdict"]
    assert "3" in out["verdict"]


def test_the_threshold_is_configurable_tighter():
    out = _compare(*_losing_pair(2, 500),
                   min_lost_questions=2, lost_question_rate=0.0)
    assert out["regressed"] is True


def test_lowering_only_the_floor_cannot_tighten_below_the_rate():
    """The two knobs are a conjunction (`max`), so a caller who lowers one and
    expects a stricter gate gets the rate's share instead. That is deliberate
    — documented on `effective_threshold` and in the CLI help — and pinned here
    so it cannot drift into `min` semantics, which would neuter the rate on a
    larger split."""
    out = _compare(*_losing_pair(2, 500), min_lost_questions=1)
    assert out["metrics"]["threshold"]["effective"] == 3
    assert out["regressed"] is False


def test_the_threshold_can_be_loosened_by_raising_the_floor():
    out = _compare(*_losing_pair(3, 500), min_lost_questions=10)
    assert out["metrics"]["threshold"]["effective"] == 10
    assert out["regressed"] is False


def test_the_rate_scales_the_threshold_on_a_bigger_split():
    """A fixed floor of 3 would be hypersensitive at ten times the question
    count; the rate is what keeps the gate proportionate."""
    assert displacement.effective_threshold(
        500, min_lost_questions=3, lost_question_rate=0.005) == 3
    assert displacement.effective_threshold(
        5000, min_lost_questions=3, lost_question_rate=0.005) == 25
    out = _compare(*_losing_pair(10, 5000))
    assert out["regressed"] is False        # 10 < ceil(0.005 * 5000) = 25
    out = _compare(*_losing_pair(25, 5000))
    assert out["regressed"] is True


def _gaining_pair(n_lost, n_gained, n_total):
    """`n_lost` questions each lose one evidence hit; `n_gained` DIFFERENT
    questions each gain one. The global net is `n_gained - n_lost`."""
    before, after, evidence = _losing_pair(n_lost, n_total)
    for i in range(n_lost, n_lost + n_gained):
        before[i] = _row(f"q{i:03d}", "e", "x")
        after[i] = _row(f"q{i:03d}", "e", "e")
    return before, after, evidence


def test_gains_on_other_questions_cannot_veto_a_pattern_of_losses():
    """The defect: the gate required `net_evidence_delta < 0` as well as
    breadth, and the net is a GLOBAL SUM. Five questions each losing an
    evidence occurrence — over the threshold of 3 — while one unrelated
    question gained five netted to 0 and the gate stayed silent, reporting a
    CHURN warning at most.

    That is a mean wearing an event counter's clothes, which is the exact
    instrument failure this module exists to escape one level up. Question B
    gaining evidence does not repair question A's answer."""
    before, after, evidence = _losing_pair(5, 500)
    before[400] = _row("q400", "e", "x", "x", "x", "x", "x")
    after[400] = _row("q400", "e", "e", "e", "e", "e", "e")
    out = _compare(before, after, evidence, after_run=_ran())
    m = out["metrics"]
    assert m["net_evidence_delta"] == 0          # the veto that used to fire
    assert m["lost_evidence_count"] == 5
    assert m["gained_evidence_count"] == 1
    assert out["regressed"] is True
    assert "EVIDENCE DISPLACEMENT" in out["verdict"]
    # And it must say WHY a flat net is not a defence, not merely fire.
    assert "not a defence" in out["verdict"]


def test_a_net_positive_run_still_gates_when_enough_questions_lose():
    """Stronger than the balanced case: the run is net POSITIVE overall and the
    gate still fires, because the losses are per-question facts."""
    before, after, evidence = _gaining_pair(3, 40, 500)
    out = _compare(before, after, evidence, after_run=_ran())
    assert out["metrics"]["net_evidence_delta"] == 37
    assert out["regressed"] is True


def test_a_trade_inside_one_question_is_not_a_loss():
    """Netting WITHIN a question is legitimate and is retained: one evidence
    session giving a slot to another leaves that question no worse off, so it
    is not counted — which is what keeps the per-question rule from firing on
    ordinary reordering."""
    before = [_row("q1", "e1", "e2", "e2")]
    after = [_row("q1", "e1", "e1", "e2")]
    out = _compare(before, after, {"q1": ["e1", "e2"]}, after_run=_ran())
    assert out["metrics"]["lost_evidence_count"] == 0
    assert out["metrics"]["net_evidence_delta"] == 0
    assert out["regressed"] is False


def test_losses_below_the_threshold_still_warn_even_when_the_net_is_positive():
    """The early-warning line follows the gate: it counts questions, not the
    net, so a rising loss count is visible while gains elsewhere keep the sum
    positive."""
    before, after, evidence = _gaining_pair(2, 40, 500)
    out = _compare(before, after, evidence, after_run=_ran())
    assert out["metrics"]["net_evidence_delta"] > 0
    assert out["regressed"] is False
    assert any("BELOW GATE" in w for w in out["warnings"])


def test_a_net_negative_below_the_floor_warns_but_does_not_gate():
    out = _compare(*_losing_pair(2, 500))
    assert out["regressed"] is False
    assert any("BELOW GATE" in w for w in out["warnings"])


def test_untagged_slots_alone_never_gate():
    """A dream taking a slot from a distractor is not a regression, and a gate
    that fired on it would fire on the feature working as designed."""
    before, after, evidence = [], [], {}
    for i in range(100):
        qid = f"q{i:03d}"
        evidence[qid] = ["e"]
        before.append(_row(qid, "e", "distractor"))
        after.append(_row(qid, "e", None))
    out = _compare(before, after, evidence)
    assert out["metrics"]["untagged_slot_delta"] == 100
    assert out["regressed"] is False


# ---------------------------------------------------------------------------
# The positive control. An 'after' leg that recalled nothing did not measure
# the store; comparing its rows slot by slot compares them with themselves.
# ---------------------------------------------------------------------------

def test_an_after_leg_that_completed_zero_recalls_is_refused():
    """The deliverable. Two records with identical rows and
    `completed_last_invocation=0` used to print
    "VERDICT: OK - no question lost evidence to a displaced slot" and exit 0,
    with no warning — the same defect the aggregate gate's control was built
    for, reintroduced one module over."""
    rows = [_row("q1", "e1", "e1"), _row("q2", "e2", "e2")]
    out = _compare(rows, [dict(r) for r in rows], {"q1": ["e1"], "q2": ["e2"]},
                   after_run=_counts(completed=500, last=0, skipped=500))
    assert out["regressed"] is True
    assert out["metrics"]["questions_compared"] == 0
    assert out["metrics"]["recall_provenance"]["status"] == "refused"
    assert "completed=0" in out["verdict"]
    assert "final invocation" in out["verdict"]
    # It must name the SHAPE of the zero, as compare_runs does — skipped and
    # errored have opposite remedies.
    assert "skipped" in out["verdict"]


def test_the_refusal_names_a_backend_failure_differently_from_a_resume():
    out = _compare([_row("q1", "e1")], [_row("q1", "e1")], {"q1": ["e1"]},
                   after_run=_counts(completed=0, errored=500))
    assert out["regressed"] is True
    assert "ERRORED" in out["verdict"]
    assert "backend/connectivity" in out["verdict"]


def test_the_refusal_beats_a_malformed_row_to_the_verdict():
    """Ordering matters for the remedy printed: if the leg never ran, telling
    an operator their rows are malformed points at the wrong repair."""
    before = [{"question_id": "q1", "hits": "not a list", "error": None}]
    out = _compare(before, [_row("q1", "e1")], {"q1": ["e1"]},
                   after_run=_counts(completed=0))
    assert "completed=0" in out["verdict"]


def test_a_cumulative_only_count_is_unverified_not_confirmed():
    """`completed` accumulates across a label's invocations, so a large total
    cannot rule out a final invocation that skipped everything."""
    block = _counts(completed=500)
    del block["recall_counts"]["completed_last_invocation"]
    rows = [_row("q1", "e1")]
    out = _compare(rows, [dict(r) for r in rows], {"q1": ["e1"]}, after_run=block)
    assert out["regressed"] is False
    assert out["metrics"]["recall_provenance"]["status"] == "unverified"
    assert any("CUMULATIVE" in w for w in out["warnings"])


def test_a_cumulative_zero_is_still_refused():
    """`0` is unambiguous whichever figure it is — nothing was recalled."""
    block = _counts(completed=0)
    del block["recall_counts"]["completed_last_invocation"]
    out = _compare([_row("q1", "e1")], [_row("q1", "e1")], {"q1": ["e1"]},
                   after_run=block)
    assert out["regressed"] is True
    assert out["metrics"]["recall_provenance"]["status"] == "refused"


def test_a_record_with_counts_but_no_numbers_is_unverified_not_zero():
    """`None` (the record does not say) and `0` (it says nothing ran) must
    never collapse into each other."""
    rows = [_row("q1", "e1")]
    out = _compare(rows, [dict(r) for r in rows], {"q1": ["e1"]},
                   after_run={"recall_counts": {"invocations": 1}})
    assert out["regressed"] is False
    assert out["metrics"]["recall_provenance"]["completed"] is None
    assert any("records no recall counts" in w for w in out["warnings"])


def test_a_boolean_completed_is_not_read_as_a_count():
    """`True` is an `int` in Python and would otherwise read as completed=1,
    manufacturing a confirmation out of a malformed record."""
    out = _compare([_row("q1", "e1")], [_row("q1", "e1")], {"q1": ["e1"]},
                   after_run={"recall_counts": {"completed_last_invocation": True}})
    assert out["metrics"]["recall_provenance"]["status"] == "unverified"


def test_the_control_reads_the_after_config_block_from_the_record():
    """End to end through the files API: the counts are stamped per config, so
    that is where they must be read from."""
    rows = [_row("q1", "e1", "e1")]
    before = _record({"bench": rows}, {"bench": {"k": 10}})
    after = _record({"bench": [dict(r) for r in rows]},
                    {"bench": dict(_counts(completed=500, last=0, skipped=500), k=10)})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["bench"]["regressed"] is True
    assert "completed=0" in out["bench"]["verdict"]
    assert displacement.refused_configs(out) == ["bench"]


def test_one_config_recalling_nothing_does_not_condemn_the_other():
    """Counts are per config and a run can legitimately complete one config's
    recalls and skip the other's, so the refusal is scoped to the config it
    belongs to."""
    rows = [_row("q1", "e1", "e1")]
    before = _record({"bench": rows, "defaults": rows}, {"bench": {"k": 10}, "defaults": {"k": 3}})
    after = _record(
        {"bench": [dict(r) for r in rows], "defaults": [dict(r) for r in rows]},
        {"bench": dict(_counts(completed=500), k=10),
         "defaults": dict(_counts(completed=0, skipped=500), k=3)},
    )
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["defaults"]["regressed"] is True
    assert out["bench"]["regressed"] is False
    assert out["bench"]["metrics"]["recall_provenance"]["status"] == "confirmed"
    assert displacement.refused_configs(out) == ["defaults"]


def test_a_record_stating_no_counts_is_unverified_through_the_files_api():
    """The two committed A/B records are exactly this shape. They must stay
    analysable — and must never read as silently clean."""
    rows = [_row("q1", "e1")]
    before = _record({"bench": rows}, {"bench": {"k": 10}})
    after = _record({"bench": [dict(r) for r in rows]}, {"bench": {"k": 10}})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["bench"]["regressed"] is False
    assert out["bench"]["metrics"]["recall_provenance"]["supplied"] is True
    assert any("UNVERIFIED" in w for w in out["bench"]["warnings"])


def test_the_cli_refuses_a_zero_recall_after_leg_and_names_the_cause(tmp_path, capsys):
    """The whole finding, end to end: this exact pair printed
    "VERDICT: OK" and exited 0."""
    rows = [_row(f"q{i:03d}", "e", "e") for i in range(20)]
    evidence = {f"q{i:03d}": ["e"] for i in range(20)}
    counts = dict(_counts(completed=500, last=0, skipped=500), k=10)
    before = _write(tmp_path, "before.json", {
        "retrieval": {"bench": dict(counts, displacement={"answer_session_ids": evidence})},
        "per_question": {"bench": rows}})
    after = _write(tmp_path, "after.json", {
        "retrieval": {"bench": counts},
        "per_question": {"bench": [dict(r) for r in rows]}})
    rc = displacement.main(["--before", before, "--after", after])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT CERTIFIED" in out
    assert "completed=0" in out
    assert "REFUSED" in out                       # the provenance row in the table
    assert "no question lost evidence" not in out
    # And no zero-filled measurement table above the refusal: `empty_metrics()`
    # renders "questions that LOST evidence | 0", which is the reassuring
    # output the refusal exists to suppress.
    assert "questions that LOST evidence" not in out
    assert "NOT ANALYSED" in out


def test_the_cli_reports_provenance_even_when_the_pair_is_clean(tmp_path, capsys):
    rows = [_row("q1", "e1")]
    evidence = {"q1": ["e1"]}
    before = _write(tmp_path, "before.json", {
        "retrieval": {"bench": dict(_counts(completed=1), k=10,
                                    displacement={"answer_session_ids": evidence})},
        "per_question": {"bench": rows}})
    after = _write(tmp_path, "after.json", {
        "retrieval": {"bench": dict(_counts(completed=1), k=10)},
        "per_question": {"bench": [dict(r) for r in rows]}})
    rc = displacement.main(["--before", before, "--after", after])
    out = capsys.readouterr().out
    assert rc == 0
    assert "recall provenance" in out
    assert "confirmed" in out


# ---------------------------------------------------------------------------
# Loud failures. Nothing here may return a number.
# ---------------------------------------------------------------------------

def _assert_loud(out, *fragments):
    assert out["regressed"] is True
    assert out["metrics"]["questions_compared"] == 0
    assert out["warnings"] == []
    for fragment in fragments:
        assert fragment in out["verdict"], out["verdict"]


def test_mismatched_question_sets_are_a_loud_error():
    out = _compare([_row("q1", "e1")], [_row("q2", "e1")], {"q1": ["e1"], "q2": ["e1"]})
    _assert_loud(out, "different question sets")


def test_a_duplicate_question_id_is_fatal_not_last_wins():
    before = [_row("q1", "e1"), _row("q1", "x")]
    out = _compare(before, [_row("q1", "e1")], {"q1": ["e1"]})
    _assert_loud(out, "duplicate question_id")


def test_missing_evidence_for_a_compared_question_refuses_the_partial_join():
    """The defect this refusal prevents: with no evidence set, both sides score
    zero evidence hits and the question reads as unchanged."""
    out = _compare([_row("q1", "e1", "e1")], [_row("q1", "e1", None)], {})
    _assert_loud(out, "partial join", "q1")


def test_a_hit_with_no_session_id_key_is_malformed_not_untagged():
    """`.get()` would silently turn a malformed row into an untagged slot,
    inflating the exact number this module reports."""
    before = [{"question_id": "q1", "hits": [{"rank": 1}], "error": None}]
    out = _compare(before, [_row("q1", None)], {"q1": ["e1"]})
    _assert_loud(out, "no 'session_id' key")


def test_a_rank_that_disagrees_with_its_slot_is_a_loud_error():
    before = [{"question_id": "q1",
               "hits": [{"session_id": "e1", "rank": 7}], "error": None}]
    out = _compare(before, [_row("q1", "e1")], {"q1": ["e1"]})
    _assert_loud(out, "does not match its slot order")


def test_rows_that_are_not_a_list_fail_loud():
    _assert_loud(_compare({"q1": []}, [_row("q1", "e1")], {"q1": ["e1"]}), "not a list")


def test_a_row_without_a_question_id_fails_loud():
    _assert_loud(
        _compare([{"hits": []}], [_row("q1", "e1")], {"q1": ["e1"]}),
        "no usable 'question_id'",
    )


def test_a_row_without_a_hits_list_fails_loud():
    before = [{"question_id": "q1", "error": None}]
    _assert_loud(_compare(before, [_row("q1", "e1")], {"q1": ["e1"]}), "no 'hits' list")


def test_a_non_string_session_id_fails_loud():
    before = [{"question_id": "q1", "hits": [{"session_id": 7}], "error": None}]
    _assert_loud(_compare(before, [_row("q1", "e1")], {"q1": ["e1"]}), "non-string")


def test_evidence_given_as_a_bare_string_fails_loud():
    out = _compare([_row("q1", "e1")], [_row("q1", "e1")], {"q1": "e1"})
    _assert_loud(out, "is a string")


def test_a_known_session_map_missing_a_question_fails_loud():
    out = _compare([_row("q1", "e1")], [_row("q1", "e1")], {"q1": ["e1"]},
                   known_sessions={})
    _assert_loud(out, "known-session map")


# ---------------------------------------------------------------------------
# Errored questions: excluded, but never silently.
# ---------------------------------------------------------------------------

def test_an_errored_question_is_excluded_and_announced():
    before = [_row("q1", "e1", "e1"), _row("q2", "e2")]
    after = [_row("q1", "e1", "e1"), _row("q2", error="boom")]
    out = _compare(before, after, {"q1": ["e1"], "q2": ["e2"]})
    assert out["metrics"]["excluded_errored"] == ["q2"]
    assert out["metrics"]["questions_compared"] == 1
    assert out["metrics"]["net_evidence_delta"] == 0   # not a fabricated -1
    assert any("EXCLUDED" in w for w in out["warnings"])


def test_every_question_erroring_is_a_loud_error_not_an_empty_pass():
    before = [_row("q1", "e1")]
    after = [_row("q1", error="boom")]
    out = _compare(before, after, {"q1": ["e1"]})
    assert out["regressed"] is True
    assert "no question survived" in out["verdict"]
    assert out["metrics"]["excluded_errored"] == ["q1"]


# ---------------------------------------------------------------------------
# Run-record plumbing.
# ---------------------------------------------------------------------------

def _record(per_question: dict, retrieval: dict | None = None) -> dict:
    return {"generated_at": "2026-08-06T00:00:00Z", "meta": {},
            "retrieval": retrieval or {}, "per_question": per_question}


def test_compare_files_reads_k_from_the_record():
    before = _record({"bench": [_row("q1", "x", "y", "e1")]}, {"bench": {"k": 2}})
    after = _record({"bench": [_row("q1", "x", "y", "z")]}, {"bench": {"k": 2}})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["bench"]["metrics"]["k"] == 2
    assert out["bench"]["metrics"]["evidence_hits_before"] == 0   # e1 was past k


def test_compare_files_refuses_a_k_mismatch():
    before = _record({"bench": [_row("q1", "e1")]}, {"bench": {"k": 3}})
    after = _record({"bench": [_row("q1", "e1")]}, {"bench": {"k": 10}})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["bench"]["regressed"] is True
    assert "k mismatch" in out["bench"]["verdict"]


def test_compare_files_warns_when_no_record_states_k():
    before = _record({"bench": [_row("q1", "e1")]})
    after = _record({"bench": [_row("q1", "e1")]})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert out["bench"]["metrics"]["k"] is None
    assert any("UNVERIFIED" in w for w in out["bench"]["warnings"])


def test_the_qa_block_is_not_mistaken_for_a_recall_config():
    before = _record({"bench": [_row("q1", "e1")], "qa": []})
    after = _record({"bench": [_row("q1", "e1")], "qa": []})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert set(out) == {"bench"}


def test_a_record_without_per_question_rows_is_a_loud_error():
    out = displacement.compare_displacement_files(
        {"retrieval": {"bench": {"k": 10}}}, {"retrieval": {"bench": {"k": 10}}}, {})
    assert set(out) == {"error"}
    assert out["error"]["regressed"] is True
    assert "per-question rows" in out["error"]["verdict"]


def test_no_common_config_is_a_loud_error():
    before = _record({"bench": [_row("q1", "e1")]})
    after = _record({"defaults": [_row("q1", "e1")]})
    out = displacement.compare_displacement_files(before, after, {"q1": ["e1"]})
    assert set(out) == {"error"}
    assert out["error"]["regressed"] is True


# ---------------------------------------------------------------------------
# Evidence resolution.
# ---------------------------------------------------------------------------

def _stamped(qid_map: dict) -> dict:
    return {"retrieval": {"bench": {"k": 10, "displacement": {
        "k": 10, "answer_session_ids": qid_map}}}}


def test_evidence_is_read_from_the_stamped_block_without_the_dataset():
    got = displacement.evidence_from_record(_stamped({"q1": ["e1"]}))
    assert got == {"q1": ["e1"]}


def test_a_record_predating_the_stamp_yields_no_evidence_rather_than_a_guess():
    assert displacement.evidence_from_record({"retrieval": {"bench": {"k": 10}}}) == {}
    assert displacement.evidence_from_record({}) == {}


def test_a_record_disagreeing_with_itself_about_evidence_raises():
    record = {"retrieval": {
        "bench": {"displacement": {"answer_session_ids": {"q1": ["e1"]}}},
        "defaults": {"displacement": {"answer_session_ids": {"q1": ["e2"]}}},
    }}
    with pytest.raises(ValueError, match="disagrees with itself"):
        displacement.evidence_from_record(record)


def test_two_records_disagreeing_about_the_evidence_are_refused(tmp_path):
    """`evidence_from_record` already RAISED when two configs of ONE record
    disagreed ("guessing would poison every number downstream") while
    `resolve_evidence` silently took the LAST when the two RECORDS disagreed:
    before stamping `q1 -> [A]` and after stamping `q1 -> [Z]` returned
    `{q1: [Z]}` with `error=None`. Same defect, opposite handling, one call
    site apart."""
    resolved = displacement.resolve_evidence(
        _stamped({"q1": ["A"]}), _stamped({"q1": ["Z"]}),
        default_dataset=tmp_path / "nope.json")
    assert resolved["evidence"] is None
    assert "disagree" in resolved["error"]
    assert "q1" in resolved["error"]
    # Both sides must be named — a reader has to know which record to re-score.
    assert "before" in resolved["error"] and "after" in resolved["error"]


def test_two_records_agreeing_about_the_evidence_merge_cleanly(tmp_path):
    resolved = displacement.resolve_evidence(
        _stamped({"q1": ["A"]}), _stamped({"q1": ["A"], "q2": ["B"]}),
        default_dataset=tmp_path / "nope.json")
    assert resolved["error"] is None
    assert resolved["evidence"] == {"q1": ["A"], "q2": ["B"]}


def test_evidence_order_is_not_treated_as_a_disagreement(tmp_path):
    """`answer_session_ids` is a set in meaning; a different order is the same
    claim and must not fail a comparison."""
    resolved = displacement.resolve_evidence(
        _stamped({"q1": ["A", "B"]}), _stamped({"q1": ["B", "A"]}),
        default_dataset=tmp_path / "nope.json")
    assert resolved["error"] is None


def test_a_self_inconsistent_record_is_reported_against_its_own_side(tmp_path):
    """The two scopes stay distinguishable: a broken record and two records
    scored from different data have different remedies."""
    broken = {"retrieval": {
        "bench": {"displacement": {"answer_session_ids": {"q1": ["e1"]}}},
        "defaults": {"displacement": {"answer_session_ids": {"q1": ["e2"]}}},
    }}
    resolved = displacement.resolve_evidence(
        broken, _stamped({"q1": ["e1"]}), default_dataset=tmp_path / "nope.json")
    assert resolved["evidence"] is None
    assert "disagrees with itself" in resolved["error"]
    assert "'before' record is unusable" in resolved["error"]


def test_the_cli_exits_nonzero_when_the_two_records_disagree(tmp_path, capsys):
    before = _write(tmp_path, "before.json", dict(
        _stamped({"q1": ["A"]}), per_question={"bench": [_row("q1", "A")]}))
    after = _write(tmp_path, "after.json", dict(
        _stamped({"q1": ["Z"]}), per_question={"bench": [_row("q1", "A")]}))
    rc = displacement.main(["--before", before, "--after", after])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT RUN" in out
    assert "disagree" in out


def test_resolve_prefers_the_stamped_block_over_the_default_dataset(tmp_path):
    resolved = displacement.resolve_evidence(
        _stamped({"q1": ["e1"]}), _stamped({"q1": ["e1"]}),
        default_dataset=tmp_path / "nope.json")
    assert resolved["error"] is None
    assert resolved["evidence"] == {"q1": ["e1"]}
    assert resolved["known_sessions"] is None      # not derivable from a stamp
    assert "stamped" in resolved["source"]


def test_resolve_reports_a_missing_explicit_dataset_rather_than_falling_back(tmp_path):
    resolved = displacement.resolve_evidence(
        _stamped({"q1": ["e1"]}), _stamped({"q1": ["e1"]}),
        dataset_path=tmp_path / "missing.json")
    assert resolved["evidence"] is None
    assert "does not exist" in resolved["error"]


def test_resolve_says_what_to_do_when_there_is_no_evidence_anywhere(tmp_path):
    resolved = displacement.resolve_evidence(
        {"retrieval": {}}, {"retrieval": {}}, default_dataset=tmp_path / "nope.json")
    assert resolved["evidence"] is None
    assert "--dataset" in resolved["error"]


def test_resolve_falls_back_to_the_default_dataset_when_it_exists(tmp_path):
    dataset = tmp_path / "longmemeval_s.json"
    dataset.write_text(json.dumps([{
        "question_id": "q1", "question_type": "single-session-user",
        "question": "?", "answer": "a", "question_date": "2023/01/01 (Sun) 09:00",
        "haystack_dates": ["2023/01/01 (Sun) 09:00"], "haystack_session_ids": ["s1"],
        "haystack_sessions": [[]], "answer_session_ids": ["s1"],
    }]), encoding="utf-8")
    resolved = displacement.resolve_evidence(
        {"retrieval": {}}, {"retrieval": {}}, default_dataset=dataset)
    assert resolved["evidence"] == {"q1": ["s1"]}
    assert resolved["known_sessions"] == {"q1": {"s1"}}


# ---------------------------------------------------------------------------
# Rendering and CLI.
# ---------------------------------------------------------------------------

def test_render_markdown_shows_the_table_the_detail_and_the_verdict():
    before = _record({"bench": [_row("q1", "e1", "e1")]}, {"bench": {"k": 10}})
    after = _record({"bench": [_row("q1", "e1", None)]}, {"bench": {"k": 10}})
    md = displacement.render_markdown(
        displacement.compare_displacement_files(before, after, {"q1": ["e1"]}))
    assert "| measure | value |" in md
    assert "untagged" in md
    assert "Lost evidence:" in md
    assert "q1" in md
    assert "OK:" in md


def test_the_untagged_row_does_not_claim_a_dream_caused_it():
    """The rendered table read `untagged (dream-shaped) slots | 0 -> 1 (+1)`,
    which asserts provenance the data cannot support: `results/METHODOLOGY.md`
    documents graph-only hits occupying rank slots with `session_id=None` too.
    The module docstring and README were honest about this; the table a reader
    actually sees was not."""
    before = _record({"bench": [_row("q1", "e1", "x")]}, {"bench": {"k": 10}})
    after = _record({"bench": [_row("q1", "e1", None)]}, {"bench": {"k": 10}})
    md = displacement.render_markdown(
        displacement.compare_displacement_files(before, after, {"q1": ["e1"]}))
    assert "dream-shaped" not in md
    assert "untagged slots (session_id=None)" in md
    # The caveat has to travel with the table, not live only in a docstring.
    assert "graph-only hit" in md
    assert "never proof" in md


def test_render_markdown_says_foreign_slots_were_not_computed_when_they_were_not():
    before = _record({"bench": [_row("q1", "e1")]}, {"bench": {"k": 10}})
    md = displacement.render_markdown(
        displacement.compare_displacement_files(before, before, {"q1": ["e1"]}))
    assert "not computed" in md


def test_render_markdown_never_crashes_on_a_loud_failure_shape():
    md = displacement.render_markdown(
        displacement.compare_displacement_files({}, {}, {}))
    assert "ERROR" in md


def _write(tmp_path, name, record):
    path = tmp_path / name
    path.write_text(json.dumps(record), encoding="utf-8")
    return str(path)


def test_cli_exits_nonzero_when_the_evidence_gate_fires(tmp_path, capsys):
    before_rows, after_rows, evidence = _losing_pair(5, 500)
    before = _write(tmp_path, "before.json", {
        "retrieval": {"bench": {"k": 10, "displacement": {
            "answer_session_ids": evidence}}},
        "per_question": {"bench": before_rows}})
    after = _write(tmp_path, "after.json", {
        "retrieval": {"bench": {"k": 10}}, "per_question": {"bench": after_rows}})
    rc = displacement.main(["--before", before, "--after", after])
    out = capsys.readouterr().out
    assert rc == 1
    assert "EVIDENCE DISPLACEMENT" in out


def test_cli_exits_zero_when_the_loss_is_a_single_event(tmp_path, capsys):
    before_rows, after_rows, evidence = _losing_pair(1, 500)
    before = _write(tmp_path, "before.json", {
        "retrieval": {"bench": {"k": 10, "displacement": {
            "answer_session_ids": evidence}}},
        "per_question": {"bench": before_rows}})
    after = _write(tmp_path, "after.json", {
        "retrieval": {"bench": {"k": 10}}, "per_question": {"bench": after_rows}})
    rc = displacement.main(["--before", before, "--after", after])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BELOW GATE" in out


def test_cli_exits_nonzero_when_evidence_cannot_be_resolved(tmp_path, capsys):
    before = _write(tmp_path, "before.json", {"per_question": {"bench": []}})
    after = _write(tmp_path, "after.json", {"per_question": {"bench": []}})
    rc = displacement.main(
        ["--before", before, "--after", after,
         "--dataset", str(tmp_path / "missing.json")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT RUN" in out


def test_cli_threshold_flags_are_honoured(tmp_path, capsys):
    before_rows, after_rows, evidence = _losing_pair(1, 500)
    before = _write(tmp_path, "before.json", {
        "retrieval": {"bench": {"k": 10, "displacement": {
            "answer_session_ids": evidence}}},
        "per_question": {"bench": before_rows}})
    after = _write(tmp_path, "after.json", {
        "retrieval": {"bench": {"k": 10}}, "per_question": {"bench": after_rows}})
    rc = displacement.main([
        "--before", before, "--after", after,
        "--min-lost-questions", "1", "--lost-question-rate", "0",
    ])
    assert rc == 1
    assert "EVIDENCE DISPLACEMENT" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Threshold arithmetic.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("compared,rate,floor,expected", [
    (0, 0.005, 3, 3),
    (500, 0.005, 3, 3),
    (500, 0.01, 3, 5),
    (5000, 0.005, 3, 25),
    (500, 0.0, 3, 3),
    (500, 0.005, 0, math.ceil(0.005 * 500)),
])
def test_effective_threshold(compared, rate, floor, expected):
    assert displacement.effective_threshold(
        compared, min_lost_questions=floor, lost_question_rate=rate) == expected


def test_a_negative_knob_cannot_produce_a_threshold_below_zero():
    assert displacement.effective_threshold(
        500, min_lost_questions=-5, lost_question_rate=-1.0) == 0


# ---------------------------------------------------------------------------
# Magnitude axis. Review finding: the redesigned gate had exactly ONE axis —
# breadth — so a catastrophe confined to a single question passed. One question
# losing ALL NINE of its evidence hits scores lost_evidence_count=1, under any
# sensible floor, verdict OK. That is the aggregate's own blindness on a
# different axis: an effect real enough to ruin one user's answer, averaged into
# invisibility by a threshold that only counts questions.
# ---------------------------------------------------------------------------

def _wipe_pair(before_hits, after_hits, evidence, n_filler=500):
    """One question with a controllable evidence outcome, plus filler questions
    byte-identical on both sides so breadth can never reach the floor. Uses the
    published per-question row shape (`_row`), not a bare dict."""
    before = [_row("q_wiped", *before_hits)]
    after = [_row("q_wiped", *after_hits)]
    ev = {"q_wiped": set(evidence)}
    for i in range(n_filler):
        qid = f"f{i}"
        before.append(_row(qid, f"s{i}_a", f"s{i}_b"))
        after.append(_row(qid, f"s{i}_a", f"s{i}_b"))
        ev[qid] = {f"s{i}_a"}
    return before, after, ev


def test_a_single_question_losing_all_its_evidence_gates_despite_breadth():
    """The reviewer's case: 9 -> 0 on one question among 500 compared."""
    ev = [f"e{i}" for i in range(9)]
    before, after, evidence = _wipe_pair(ev, [f"d{i}" for i in range(9)], ev)
    got = displacement.compare_displacement(before, after, evidence, k=10)
    assert got["regressed"] is True, "a total evidence loss must gate at any sample size"
    assert "WIPED" in got["verdict"]
    assert "q_wiped" in got["verdict"]


def test_a_partial_loss_on_one_question_still_does_not_gate():
    """The magnitude axis is deliberately TOTAL-loss only. A partial drop is what
    the breadth axis is for; a second fuzzy threshold would just be two ways to
    argue about noise."""
    ev = [f"e{i}" for i in range(9)]
    kept = ev[:4] + [f"d{i}" for i in range(5)]
    before, after, evidence = _wipe_pair(ev, kept, ev)
    got = displacement.compare_displacement(before, after, evidence, k=10)
    assert got["regressed"] is False
    assert "WIPED" not in got["verdict"]


def test_a_question_that_never_had_evidence_is_not_a_wipe():
    """evidence_before == 0 must not read as a total loss — there was nothing to
    lose, and counting it would fire the gate on every unanswerable question."""
    before, after, evidence = _wipe_pair(["x1", "x2"], ["y1", "y2"], [])
    got = displacement.compare_displacement(before, after, evidence, k=10)
    assert got["regressed"] is False
