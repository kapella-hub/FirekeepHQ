"""Displacement analysis — the tail the aggregate regression gate cannot see.

Why this exists, measured rather than hypothesised. The first Dreaming A/B ran
at low dream density (38 insights, 0.040% of a 96,123-point store) and
`bench.dream_ab` reported `recall_at_k +0.0000  coverage_at_k +0.0000
mrr +0.0000  ndcg_at_k -0.0000` — a clean pass. A hand audit of the same two
run records found that under the `bench` config 2 of 500 result sets had
changed, three top-10 slots were now held by untagged (dream-shaped) points,
and one question's evidence occupancy had gone from 9 hits to 8. A dream had
displaced real evidence.

The aggregate gate cannot see that, and no tightening of its tolerance would
fix it, because the two instruments measure different things. `compare_runs`
watches four MEANS over ~470 questions; displacement is a TAIL — a handful of
questions where a synthesized point took a rank slot that had held retrieved
evidence. One lost evidence hit moves `ndcg_at_k` by ~1e-5, four orders of
magnitude below the 0.005 tolerance, so a design that leaks evidence at that
rate passes the gate indefinitely while degrading. Worse, the one question that
lost evidence in the measured run was `031748ae_abs`, an abstention question —
`score_run` excludes those from every aggregate, so that loss was not merely
below tolerance, it was outside the metric's scope entirely.

So this module counts events, not means, and it counts them over EVERY question
the two runs have in common (abstention included), reporting the scored /
abstention split rather than inheriting the aggregate's blind spot.

`compare_displacement` is the pure core and is defensive by construction, in
`bench.dream_ab`'s sense: every doubtful shape returns a loud failure with
`regressed=True` and a verdict naming exactly what made the runs incomparable,
never a number computed over a partial join. The join with the dataset's
`answer_session_ids` is the specific place that matters — a question compared
without its evidence set would silently score zero evidence hits on both sides
and read as "unchanged".

What an untagged slot is, and what it is not. A recall hit's `session_id` comes
from `bench.recall.extract_hits`, which reads the ingest-time `lm_session:` tag;
a point carrying no such tag reports `session_id=None`. A dream insight is
written by `cortex/app/dreams/store.py` with its own payload and no LongMemEval
tags, so it surfaces as exactly that shape — verified against the artefacts:
the pre-dream leg has ZERO untagged slots across both configs (1019 + 5000
slots), the post-dream leg has three, and all three carry generalised
insight prose with no `lm_date` either. But "untagged" is not a synonym for
"dream": `results/METHODOLOGY.md` already documents graph-only hits as taking
rank slots with `session_id=None`. The count is therefore named for what it
observes — the rendered table says `untagged slots (session_id=None)` and
carries a footnote, because a reader who only ever sees the table must not be
able to read it as proof that a dream took the slot. `foreign_session_slots`
(a tagged hit naming a session outside the question's own haystack) is carried
alongside it as a floor against a future dream that somehow inherited a tag —
measured at 0 on both legs.

The positive control. This module runs the SAME zero-recall control the
aggregate gate does, from the same implementation in `bench.common`: an "after"
leg whose final invocation completed 0 recalls did not measure the store, it
re-scored rows an earlier invocation left on disk. That failure is worse here
than in the aggregate, not better — two identical row sets compared slot by
slot produce a table of perfect zeroes and the single most reassuring verdict
this tool can print. `compare_displacement` therefore REFUSES such a pair, and
a pair whose provenance is merely absent is reported UNVERIFIED rather than
passing silently.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from bench.common import (
    DATA_DIR,
    completed_recalls,
    describe_zero_recalls,
    is_abstention,
    load_dataset,
)

# The gate's two knobs. See `_gate_verdict` for the argument behind the values.
MIN_LOST_QUESTIONS_DEFAULT = 3
LOST_QUESTION_RATE_DEFAULT = 0.005

# How far an evidence hit must slide down the page before it is called out by
# name. See `_rank_shift_stats` for the argument behind the value; it warns, it
# does not gate.
RANK_SHIFT_WARN_DEFAULT = 3

# The provenance verdict on the 'after' leg's recall stage, carried in
# `metrics["recall_provenance"]["status"]`.
PROVENANCE_CONFIRMED = "confirmed"     # its final invocation recalled
PROVENANCE_UNVERIFIED = "unverified"   # it did not say, or said only cumulatively
PROVENANCE_REFUSED = "refused"         # it positively recalled nothing

# `report._load_per_question` stores the QA audit rows under this key alongside
# the recall configs. It is not a recall config and has an entirely different
# row shape, so it is excluded by name rather than left to fail validation.
_QA_KEY = "qa"


# ---------------------------------------------------------------------------
# Result shapes. Every failure path returns the SAME keys as a success, so a
# renderer or caller never needs a separate empty-result branch (the lesson
# `bench.dream_ab` encodes in its `warnings`-key-on-every-return contract).
# ---------------------------------------------------------------------------

def _empty_split() -> dict:
    return {
        "questions_compared": 0,
        "lost_evidence_count": 0,
        "gained_evidence_count": 0,
        "net_evidence_delta": 0,
    }


def _empty_rank_shift() -> dict:
    return {
        # Questions holding evidence in top-k on BOTH sides — the only ones for
        # which "the rank moved" is a statement about the same thing twice.
        "questions": 0,
        "mean_shift": 0.0,
        "worst_shift": 0,
        "worst_question": None,
        "improved_count": 0,
        "degraded_count": 0,
        "degraded_questions": [],
        "min_shift": RANK_SHIFT_WARN_DEFAULT,
    }


def _empty_provenance() -> dict:
    return {"status": PROVENANCE_UNVERIFIED, "completed": None,
            "per_invocation": False, "supplied": False}


def empty_metrics() -> dict:
    return {
        "k": None,
        "questions_compared": 0,
        "excluded_errored": [],
        "changed_questions": [],
        "changed_count": 0,
        "changed_pct": 0.0,
        "evidence_hits_before": 0,
        "evidence_hits_after": 0,
        "net_evidence_delta": 0,
        "lost_evidence_questions": [],
        "gained_evidence_questions": [],
        "lost_evidence_count": 0,
        "gained_evidence_count": 0,
        "untagged_slots_before": 0,
        "untagged_slots_after": 0,
        "untagged_slot_delta": 0,
        "questions_with_untagged_slots_after": [],
        # `None`, not 0 — "not computed" (no known-session map was supplied)
        # and "computed and found none" are different claims and must not
        # collapse into each other.
        "foreign_session_slots_before": None,
        "foreign_session_slots_after": None,
        "scored": _empty_split(),
        "abstention": _empty_split(),
        "rank_shift": _empty_rank_shift(),
        "recall_provenance": _empty_provenance(),
        "threshold": {
            "min_lost_questions": MIN_LOST_QUESTIONS_DEFAULT,
            "lost_question_rate": LOST_QUESTION_RATE_DEFAULT,
            "effective": MIN_LOST_QUESTIONS_DEFAULT,
        },
    }


def _failure(verdict: str, metrics: dict | None = None) -> dict:
    return {
        "metrics": metrics if metrics is not None else empty_metrics(),
        "regressed": True,
        "verdict": verdict,
        "warnings": [],
    }


# ---------------------------------------------------------------------------
# Input validation. Nothing below this line trusts a row it did not check.
# ---------------------------------------------------------------------------

def _index_rows(rows, side: str) -> tuple[dict | None, str | None]:
    """`({question_id: row}, None)` or `(None, reason)`.

    A duplicate question id is fatal rather than last-wins: the two rows may
    disagree, and silently keeping one of them is a partial join wearing a
    complete join's clothes.
    """
    if not isinstance(rows, list):
        return None, f"'{side}' per-question rows are not a list (got {type(rows).__name__})"
    indexed: dict[str, dict] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            return None, f"'{side}' row {i} is not an object (got {type(row).__name__})"
        qid = row.get("question_id")
        if not isinstance(qid, str) or not qid:
            return None, f"'{side}' row {i} has no usable 'question_id' ({qid!r})"
        if qid in indexed:
            return None, f"'{side}' contains duplicate question_id {qid!r}"
        indexed[qid] = row
    return indexed, None


def _sessions(row: dict, qid: str, side: str, k: int | None) -> tuple[list | None, str | None]:
    """The ordered `session_id` per rank slot, truncated to `k`.

    A hit may legitimately carry `session_id: null` (an untagged point — the
    whole subject of this module), so a null VALUE is data. A missing KEY is
    malformed and fails loud, because it is indistinguishable from an untagged
    hit once read with `.get()` and would silently inflate the untagged count.

    A published row also carries `rank`; when present it must agree with the
    slot's position, since every downstream number here is positional.
    """
    hits = row.get("hits")
    if not isinstance(hits, list):
        return None, f"'{side}' question {qid!r} has no 'hits' list (got {type(hits).__name__})"
    out = []
    for i, hit in enumerate(hits):
        if not isinstance(hit, dict):
            return None, f"'{side}' question {qid!r} hit {i + 1} is not an object"
        if "session_id" not in hit:
            return None, f"'{side}' question {qid!r} hit {i + 1} has no 'session_id' key"
        session = hit["session_id"]
        if session is not None and not isinstance(session, str):
            return None, (
                f"'{side}' question {qid!r} hit {i + 1} has a non-string "
                f"session_id ({session!r})"
            )
        rank = hit.get("rank")
        if rank is not None and rank != i + 1:
            return None, (
                f"'{side}' question {qid!r} hit {i + 1} declares rank {rank!r} — "
                "the row's rank order does not match its slot order"
            )
        out.append(session)
    return (out if k is None else out[:k]), None


def _evidence_set(evidence, qid: str) -> tuple[frozenset | None, str | None]:
    ids = evidence.get(qid) if isinstance(evidence, dict) else None
    if ids is None:
        return None, (
            f"no answer_session_ids for question {qid!r} — refusing to compute "
            "displacement over a partial join (supply the dataset with "
            "--dataset, or re-score with a harness that stamps the "
            "'displacement' block)"
        )
    if isinstance(ids, (str, bytes)):
        return None, f"answer_session_ids for {qid!r} is a string, expected a list"
    try:
        return frozenset(ids), None
    except TypeError:
        return None, f"answer_session_ids for {qid!r} is not iterable ({ids!r})"


# ---------------------------------------------------------------------------
# Per-question analysis (pure).
# ---------------------------------------------------------------------------

def _ranks_by_session(sequence: list, evidence: frozenset) -> dict[str, list[int]]:
    ranks: dict[str, list[int]] = {}
    for i, session in enumerate(sequence, 1):
        if session is not None and session in evidence:
            ranks.setdefault(session, []).append(i)
    return ranks


def evidence_rank_detail(before: list, after: list, evidence: frozenset) -> list[dict]:
    """Per-evidence-session occupancy change, with the ranks a lost occurrence
    held BEFORE — the rank-level detail the aggregate cannot express.

    Attribution convention, stated because it is a convention and not an
    observation: an evidence session routinely occupies several top-k slots
    (`031748ae_abs` held `answer_8748f791_abs_2` at ranks 2, 3, 4 and 7), and
    those occurrences are interchangeable — nothing in a recall row identifies
    WHICH memory of that session left. A loss of n occurrences is therefore
    attributed to the n DEEPEST ranks the session held, because that is what a
    top-k truncation does. `ranks_before` is reported in full so a reader can
    apply a different reading.
    """
    ranks_before = _ranks_by_session(before, evidence)
    ranks_after = _ranks_by_session(after, evidence)
    detail = []
    for session in sorted(set(ranks_before) | set(ranks_after)):
        held_before = ranks_before.get(session, [])
        held_after = ranks_after.get(session, [])
        if len(held_before) == len(held_after):
            continue
        detail.append({
            "session_id": session,
            "before": len(held_before),
            "after": len(held_after),
            "delta": len(held_after) - len(held_before),
            "ranks_before": held_before,
            "ranks_after": held_after,
            "lost_ranks_before": held_before[len(held_after):],
        })
    return detail


def best_evidence_rank(sequence: list, evidence: frozenset) -> int | None:
    """The 1-based rank of the FIRST evidence hit, or `None` if there is none.

    This is the quantity MRR keys on, which is why it is the one tracked for
    rank movement: a question whose evidence slides from rank 1 to rank 9 keeps
    every hit it had — `evidence_delta` is 0 and nothing else in this module
    notices — while its MRR goes 1.00 -> 0.11. Averaged over 500 questions that
    is a ~1e-3 move in the aggregate, the same tail-versus-mean blindness this
    module exists for, one level down.
    """
    for i, session in enumerate(sequence, 1):
        if session is not None and session in evidence:
            return i
    return None


def analyse_question(before: list, after: list, evidence: frozenset,
                     known_sessions: frozenset | None = None) -> dict:
    """Pure. One question's displacement facts from its two rank-slot lists."""
    ev_before = sum(1 for s in before if s is not None and s in evidence)
    ev_after = sum(1 for s in after if s is not None and s in evidence)
    untagged_before = sum(1 for s in before if s is None)
    untagged_after = sum(1 for s in after if s is None)
    foreign_before = foreign_after = None
    if known_sessions is not None:
        foreign_before = sum(1 for s in before if s is not None and s not in known_sessions)
        foreign_after = sum(1 for s in after if s is not None and s not in known_sessions)
    rank_before = best_evidence_rank(before, evidence)
    rank_after = best_evidence_rank(after, evidence)
    # `None` on either side means the two ranks are not the same measurement:
    # evidence that entered or left top-k is an occupancy change, already
    # counted as a gain or a loss, and calling it a "shift" would double-count
    # it as a rank move as well.
    shift = (rank_after - rank_before
             if rank_before is not None and rank_after is not None else None)
    return {
        "changed": before != after,
        "evidence_before": ev_before,
        "evidence_after": ev_after,
        "evidence_delta": ev_after - ev_before,
        "untagged_before": untagged_before,
        "untagged_after": untagged_after,
        "foreign_before": foreign_before,
        "foreign_after": foreign_after,
        "best_rank_before": rank_before,
        "best_rank_after": rank_after,
        "best_rank_shift": shift,
        "sessions": evidence_rank_detail(before, after, evidence),
    }


# ---------------------------------------------------------------------------
# The gate.
# ---------------------------------------------------------------------------

def effective_threshold(questions_compared: int, *, min_lost_questions: int,
                        lost_question_rate: float) -> int:
    """`max(floor, ceil(rate x compared))` — a floor that binds on today's
    500-question dataset and a rate that keeps the gate meaningful if the
    harness is ever pointed at a larger split.

    `max`, not `min`, because the two knobs are a conjunction: a pattern must
    be both absolutely non-trivial (three questions, not one) and
    proportionately non-trivial (0.5% of what was compared). The practical
    consequence, and it is a real one: TIGHTENING the gate means lowering BOTH
    knobs — dropping `min_lost_questions` alone cannot take the threshold below
    the rate's share. `min` would be the wrong trade: on a 5000-question split
    the floor of 3 would bind and the rate would never do its job.
    """
    scaled = math.ceil(max(lost_question_rate, 0.0) * max(questions_compared, 0))
    return max(max(min_lost_questions, 0), scaled)


def _gate_verdict(metrics: dict) -> tuple[bool, str]:
    """The evidence-displacement gate: fires when enough SEPARATE questions each
    end up worse off.

    The rule is `lost_evidence_questions >= max(min_lost_questions,
    ceil(rate x questions_compared))`, where a question is counted at most once
    and only if ITS OWN evidence occupancy fell.

    **There is deliberately no global `net_evidence_delta < 0` condition, and
    re-adding one would restore the exact defect this module exists to escape.**
    The gate used to require it, and the arithmetic that killed it is trivial:
    five questions each losing one evidence hit while one unrelated question
    gains five sums to a net of 0, and the gate stayed silent. That is a MEAN
    wearing an event counter's clothes. Displacement is a tail — a handful of
    questions where a synthesized point took a slot that held retrieved
    evidence — and summing across questions is precisely the averaging that hid
    the defect in `compare_runs`. Question B gaining an evidence hit does not
    repair question A's answer; the two are different queries with different
    users. Netting is a legitimate operation WITHIN one question (evidence
    session `e1` losing a slot to evidence session `e2` leaves that question's
    occupancy flat, and it is correctly not counted) and an illegitimate one
    ACROSS questions.

    Cross-question gains are not ignored, they are just not a veto: they are
    reported in their own column, in the `gained_evidence_questions` detail,
    and named in the verdict when the gate fires with a non-negative net — the
    reader is told the losses coexist with gains and told why that does not
    excuse them. The aggregate gate is the instrument that measures net quality;
    this one measures the tail, and a tail instrument that nets is not one.

    **Breadth is the whole gate, so the floor is what stops it firing on
    noise.** Retrieval scoring here is deterministic, so ANY store mutation
    reshuffles something and single events are expected. One lost question is
    one displacement event: real, reported here in full, but not a pattern — it
    cannot distinguish "dreams displace evidence" from "the store changed". Two
    is still consistent with two unrelated one-offs. Three independent questions
    losing evidence is the smallest count at which the word "pattern" is
    defensible, and it is 3x the rate measured at the density that produced the
    passing run (1 lost question at 0.040% dream density), so the gate has
    honest headroom against today's baseline while firing at roughly 3x that
    density.

    Why the rate is 0.5%. On the 500-question LongMemEval-S split
    `ceil(0.005 x 500) = 3`, so the floor is what binds today and the rate
    changes nothing — deliberately. It exists so that a 5000-question split
    does not inherit a fixed count of 3, which would be hypersensitive at ten
    times the question volume. Displacement risk scales with the number of
    questions; the threshold should too.

    Scope: EVERY compared question counts, abstention included. The
    scored/abstention split is reported so a reader can see where the loss sits,
    but the gate does not exclude abstention questions — the displacement
    mechanism does not care which subset a question belongs to, and a gate
    restricted to the scored subset would reproduce exactly the blind spot that
    made the aggregate miss this. The measured run is the case in point: its one
    lost-evidence question was an abstention question.
    """
    threshold = metrics["threshold"]["effective"]
    lost = metrics["lost_evidence_count"]
    gained = metrics["gained_evidence_count"]
    net = metrics["net_evidence_delta"]
    compared = metrics["questions_compared"]
    cut = f"top-{metrics['k']}" if metrics["k"] is not None else "the compared hits"

    # SECOND AXIS: magnitude. Breadth alone cannot see a catastrophe confined to
    # one question — a question losing ALL of its evidence occupancy scores
    # lost=1, under any sensible breadth floor, and passes. That is the same
    # shape of blindness as the aggregate's (an effect real enough to ruin one
    # user's answer, averaged into invisibility), just on a different axis, so
    # refusing to add it would repeat the mistake this module was built to fix.
    # Deliberately strict — total loss only, not "most" — because a partial drop
    # is what the breadth axis is for and a second fuzzy threshold would just be
    # two ways to argue about noise.
    wiped = [q for q in metrics.get("lost_evidence_questions", [])
             if isinstance(q, dict) and q.get("evidence_after") == 0
             and (q.get("evidence_before") or 0) > 0]
    if wiped:
        names = ", ".join(sorted(str(q.get("question_id")) for q in wiped)[:5])
        return True, (
            f"EVIDENCE WIPED: {len(wiped)} question(s) lost ALL evidence from "
            f"{cut} ({names}) — breadth is not the only way this fails, and a "
            "total loss on one question is not noise at any sample size"
        )

    if lost > 0 and lost >= threshold:
        verdict = (
            f"EVIDENCE DISPLACEMENT: {lost} of {compared} question(s) lost an "
            f"evidence hit from {cut} — at or beyond the threshold "
            f"of {threshold} lost-evidence question(s), this is a pattern, not "
            "noise"
        )
        if net >= 0:
            verdict += (
                f". The net evidence delta is {net:+d} because {gained} OTHER "
                "question(s) gained evidence — that is not a defence: a gain on "
                "one question does not restore the evidence another question "
                "lost, and netting across questions is the averaging this gate "
                "exists to refuse"
            )
        else:
            verdict += f" (net evidence delta {net:+d})"
        return True, verdict
    detail = (
        f"{lost} lost-evidence question(s) of {compared} compared "
        f"(threshold {threshold}), net evidence delta {net:+d}, "
        f"{metrics['untagged_slot_delta']:+d} untagged slot(s)"
    )
    return False, f"OK: no evidence-displacement pattern — {detail}"


# ---------------------------------------------------------------------------
# The pure core.
# ---------------------------------------------------------------------------

def recall_provenance(after_run) -> dict:
    """What the 'after' leg's own record says about whether it recalled.

    `after_run` is that side's per-config score block (the dict `bench.report.
    attach_recall_counts` stamps `recall_counts` onto), or `None` when the
    caller offered no provenance at all. Reads `bench.common.completed_recalls`
    — the same function `bench.dream_ab.compare_runs` gates on, not a second
    implementation of it.

    `supplied=False` is kept distinct from a supplied-but-silent block: the
    first says the CALLER made no claim, the second says the RECORD makes none.
    Both come out `unverified`, but the warning text differs and so does the
    remedy.
    """
    if after_run is None:
        return _empty_provenance()
    completed, per_invocation = completed_recalls(after_run)
    if completed == 0:
        status = PROVENANCE_REFUSED
    elif completed is not None and per_invocation:
        status = PROVENANCE_CONFIRMED
    else:
        status = PROVENANCE_UNVERIFIED
    return {"status": status, "completed": completed,
            "per_invocation": per_invocation, "supplied": True}


def compare_displacement(
    before_rows, after_rows, evidence, *,
    k: int | None = None,
    known_sessions: dict | None = None,
    after_run: dict | None = None,
    min_lost_questions: int = MIN_LOST_QUESTIONS_DEFAULT,
    lost_question_rate: float = LOST_QUESTION_RATE_DEFAULT,
    rank_shift_warn: int = RANK_SHIFT_WARN_DEFAULT,
) -> dict:
    """Pure. Returns `{"metrics": {...}, "regressed": bool, "verdict": str,
    "warnings": [str]}` — the same envelope `bench.dream_ab.compare_runs`
    returns, so the two gates render and combine identically.

    `before_rows`/`after_rows` are per-question audit rows in the published
    shape (`report._load_recall_per_question`: `question_id`, `hits[]` of
    `{session_id, rank}`, `error`); the raw `work/recall_<config>.jsonl` rows
    are accepted too, since they carry the same two fields this reads.
    `evidence` maps question id -> that question's `answer_session_ids`.

    `after_run` is the 'after' side's per-config score block, carried ONLY for
    its `recall_counts` — the positive control. Omitting it is allowed (the
    pure primitive can be handed two hand-built row lists) but is never silent:
    a comparison with no provenance reports UNVERIFIED, because "nobody said
    whether this leg ran" and "this leg ran" are different claims.

    Loud failures, each returning `regressed=True` with an empty metrics block
    and a verdict naming the cause rather than a computed number:

    - the 'after' leg's final invocation completed 0 recalls — it recalled
      nothing, so its rows are an earlier invocation's artefacts and every slot
      comparison below would be a row against itself, which produces a table of
      perfect zeroes and the most reassuring verdict this tool can print,
    - either side's rows are not a list, contain a non-object row, a row with
      no usable `question_id`, or a duplicate `question_id`,
    - a row with no `hits` list, a hit that is not an object, a hit with no
      `session_id` KEY, or a hit whose declared `rank` disagrees with its slot,
    - the two sides do not cover the same question set,
    - a compared question has no entry in `evidence` (the partial-join refusal),
    - no question is left to compare.

    Questions that ERRORED on either side are excluded from the comparison and
    reported in `metrics["excluded_errored"]` with a warning — an errored recall
    returns no hits, so including it would report a fabricated total loss of
    evidence for a question that was never retrieved at all. That exclusion is
    announced, never silent.
    """
    # The positive control runs FIRST. An 'after' leg that recalled nothing
    # invalidates every number below it, so paying the per-question loop to
    # discover that would be work spent on an answer that cannot be used — and
    # a malformed-row verdict printed instead of "this leg did not run" points
    # the operator at the wrong repair.
    provenance = recall_provenance(after_run)
    if provenance["status"] == PROVENANCE_REFUSED:
        metrics = empty_metrics()
        metrics["k"] = k
        metrics["recall_provenance"] = provenance
        return _failure(
            "ERROR: not comparable — "
            + describe_zero_recalls(after_run, provenance["per_invocation"])
            + ". Its per-question rows are therefore an earlier invocation's "
            "artefacts, so every slot comparison here would be a row against "
            "itself — which reads as zero changed result sets, zero lost "
            "evidence and a clean gate",
            metrics,
        )

    before_idx, err = _index_rows(before_rows, "before")
    if err:
        return _failure(f"ERROR: not comparable — {err}")
    after_idx, err = _index_rows(after_rows, "after")
    if err:
        return _failure(f"ERROR: not comparable — {err}")

    if set(before_idx) != set(after_idx):
        only_before = sorted(set(before_idx) - set(after_idx))
        only_after = sorted(set(after_idx) - set(before_idx))
        return _failure(
            "ERROR: not comparable — the two runs cover different question "
            f"sets (before {len(before_idx)}, after {len(after_idx)}; "
            f"{len(only_before)} only in before e.g. {only_before[:3]}, "
            f"{len(only_after)} only in after e.g. {only_after[:3]})"
        )

    metrics = empty_metrics()
    metrics["k"] = k
    metrics["recall_provenance"] = provenance
    metrics["rank_shift"]["min_shift"] = rank_shift_warn
    metrics["threshold"] = {
        "min_lost_questions": min_lost_questions,
        "lost_question_rate": lost_question_rate,
        "effective": 0,
    }

    excluded: list[str] = []
    per_question: dict[str, dict] = {}
    for qid in sorted(before_idx):
        before_row, after_row = before_idx[qid], after_idx[qid]
        if before_row.get("error") or after_row.get("error"):
            excluded.append(qid)
            continue
        before_seq, err = _sessions(before_row, qid, "before", k)
        if err:
            return _failure(f"ERROR: not comparable — {err}")
        after_seq, err = _sessions(after_row, qid, "after", k)
        if err:
            return _failure(f"ERROR: not comparable — {err}")
        ev, err = _evidence_set(evidence, qid)
        if err:
            return _failure(f"ERROR: not comparable — {err}")
        known = None
        if known_sessions is not None:
            raw = known_sessions.get(qid)
            if raw is None:
                return _failure(
                    "ERROR: not comparable — a known-session map was supplied "
                    f"but has no entry for question {qid!r}"
                )
            known = frozenset(raw)
        per_question[qid] = analyse_question(before_seq, after_seq, ev, known)

    metrics["excluded_errored"] = excluded
    if not per_question:
        return _failure(
            "ERROR: not comparable — no question survived to be compared "
            f"({len(excluded)} excluded for having errored on one or both "
            "sides)",
            metrics,
        )

    _accumulate(metrics, per_question, known_sessions is not None)
    metrics["threshold"]["effective"] = effective_threshold(
        metrics["questions_compared"],
        min_lost_questions=min_lost_questions,
        lost_question_rate=lost_question_rate,
    )

    regressed, verdict = _gate_verdict(metrics)
    return {
        "metrics": metrics,
        "regressed": regressed,
        "verdict": verdict,
        "warnings": _warnings(metrics, regressed),
    }


def _accumulate(metrics: dict, per_question: dict[str, dict], have_known: bool) -> None:
    """Fold the per-question analyses into the reported metrics block."""
    compared = len(per_question)
    metrics["questions_compared"] = compared
    if have_known:
        metrics["foreign_session_slots_before"] = 0
        metrics["foreign_session_slots_after"] = 0

    for qid, q in sorted(per_question.items()):
        split = metrics["abstention"] if is_abstention(qid) else metrics["scored"]
        split["questions_compared"] += 1
        split["net_evidence_delta"] += q["evidence_delta"]

        if q["changed"]:
            metrics["changed_questions"].append(qid)
        metrics["evidence_hits_before"] += q["evidence_before"]
        metrics["evidence_hits_after"] += q["evidence_after"]
        metrics["untagged_slots_before"] += q["untagged_before"]
        metrics["untagged_slots_after"] += q["untagged_after"]
        if q["untagged_after"]:
            metrics["questions_with_untagged_slots_after"].append(qid)
        if have_known:
            metrics["foreign_session_slots_before"] += q["foreign_before"]
            metrics["foreign_session_slots_after"] += q["foreign_after"]

        if q["evidence_delta"] < 0:
            split["lost_evidence_count"] += 1
            metrics["lost_evidence_questions"].append(_detail_row(qid, q))
        elif q["evidence_delta"] > 0:
            split["gained_evidence_count"] += 1
            metrics["gained_evidence_questions"].append(_detail_row(qid, q))

    _rank_shift_stats(metrics, per_question)
    metrics["changed_count"] = len(metrics["changed_questions"])
    metrics["changed_pct"] = (100.0 * metrics["changed_count"] / compared) if compared else 0.0
    metrics["net_evidence_delta"] = metrics["evidence_hits_after"] - metrics["evidence_hits_before"]
    metrics["untagged_slot_delta"] = metrics["untagged_slots_after"] - metrics["untagged_slots_before"]
    metrics["lost_evidence_count"] = len(metrics["lost_evidence_questions"])
    metrics["gained_evidence_count"] = len(metrics["gained_evidence_questions"])


def _rank_shift_stats(metrics: dict, per_question: dict[str, dict]) -> None:
    """Rank degradation of evidence that STAYED in top-k.

    Why this is reported at all: without it, evidence moving from rank 1 to
    rank 9 inside k=10 produced `changed=1, lost=0, net=0`, verdict OK, and not
    one row in the report said anything had happened — while that question's
    MRR went 1.00 -> 0.11 and the aggregate moved by ~1e-3 over 500 questions.
    That is the same tail-versus-mean blindness the module was built for,
    reappearing one level down. Invisible is not an acceptable answer.

    Why it WARNS rather than gates, which is a deliberate choice and not
    timidity: this module's subject is evidence leaving the top-k the product
    actually spends, and a hit at rank 9 of 10 has not left it — the user still
    receives it. Reordering is also what dreams are FOR: a synthesized insight
    that legitimately outranks one raw episode pushes everything below it down
    by one, and a gate firing on that would fire on the feature working. The
    honest statement is that a rank shift with no loss is not demonstrably a
    regression, and a gate must only fire on what is demonstrable. So it is
    counted, named, and put in the reader's face.

    Why the shift threshold defaults to 3. One or two slots is the ordinary
    consequence of inserting a small number of points into a deterministic
    ranking — the noise floor, in exactly the sense the lost-question floor
    handles for occupancy. At three or more the evidence has moved a
    meaningful distance down the page, far enough that a reader should look at
    the question by name. `mean_shift` and `worst_shift` are reported
    unconditionally so the sub-threshold movement is never hidden either; only
    the by-name callout is thresholded.
    """
    block = metrics["rank_shift"]
    shifts = [(qid, q["best_rank_shift"]) for qid, q in sorted(per_question.items())
              if q["best_rank_shift"] is not None]
    block["questions"] = len(shifts)
    if not shifts:
        return
    block["mean_shift"] = sum(s for _, s in shifts) / len(shifts)
    worst_qid, worst = max(shifts, key=lambda pair: pair[1])
    block["worst_shift"] = worst
    # Only name a "worst" question when something actually moved down; the
    # deepest of a set of improvements is not a worst case.
    block["worst_question"] = worst_qid if worst > 0 else None
    block["improved_count"] = sum(1 for _, s in shifts if s < 0)
    block["degraded_questions"] = [
        {
            "question_id": qid,
            "abstention": is_abstention(qid),
            "rank_before": per_question[qid]["best_rank_before"],
            "rank_after": per_question[qid]["best_rank_after"],
            "shift": shift,
        }
        for qid, shift in shifts if shift >= block["min_shift"] and shift > 0
    ]
    block["degraded_count"] = len(block["degraded_questions"])


def _detail_row(qid: str, q: dict) -> dict:
    return {
        "question_id": qid,
        "abstention": is_abstention(qid),
        "evidence_before": q["evidence_before"],
        "evidence_after": q["evidence_after"],
        "delta": q["evidence_delta"],
        "untagged_slots_after": q["untagged_after"],
        "sessions": q["sessions"],
    }


def _provenance_warnings(metrics: dict) -> list[str]:
    """The UNVERIFIED half of the positive control.

    A refused leg never reaches here (it is a loud failure). What is left is
    "the record did not say" and "the record said only cumulatively" — neither
    is proof of a no-op, so neither may fail a comparison of two published
    records, and both must be impossible to overlook. Mirrors the two
    corresponding warnings in `bench.dream_ab.compare_runs`.
    """
    p = metrics["recall_provenance"]
    if p["status"] != PROVENANCE_UNVERIFIED:
        return []
    if not p["supplied"]:
        return [
            "UNVERIFIED: no recall-count provenance was supplied for the "
            "'after' rows, so this comparison cannot confirm they came from a "
            "leg that actually executed its recalls (pass the 'after' record's "
            "per-config score block as `after_run`; the CLIs do this for you)"
        ]
    if p["completed"] is None:
        return [
            "UNVERIFIED: the 'after' run records no recall counts, so this "
            "comparison cannot confirm it actually executed its recalls — its "
            "rows may be an earlier invocation's artefacts re-read from disk "
            "(re-run the 'after' leg with a harness that writes `recall_counts`)"
        ]
    return [
        "UNVERIFIED: the 'after' run records only a CUMULATIVE recall count "
        f"(completed={p['completed']} across every invocation of its run "
        "label), not a per-invocation one, so this comparison cannot rule out "
        "that its final invocation skipped every question and re-compared rows "
        "an earlier invocation had produced (re-run the 'after' leg with a "
        "harness that writes `completed_last_invocation`)"
    ]


def _warnings(metrics: dict, regressed: bool) -> list[str]:
    """Non-gating channel: things a reader must see that are not, on their own,
    a measured regression."""
    out: list[str] = _provenance_warnings(metrics)
    if metrics["excluded_errored"]:
        n = len(metrics["excluded_errored"])
        out.append(
            f"EXCLUDED: {n} question(s) errored on one or both sides and were "
            "left out of this comparison (an errored recall returns no hits, "
            "which would read as a total loss of evidence) — "
            f"e.g. {metrics['excluded_errored'][:3]}"
        )
    # Fires on the COUNT, not on the net — the gate no longer nets across
    # questions and neither may its early-warning line. A question that lost
    # evidence is worth reporting whether or not some other question gained.
    if not regressed and metrics["lost_evidence_count"] > 0:
        gained = metrics["gained_evidence_count"]
        out.append(
            f"BELOW GATE: {metrics['lost_evidence_count']} question(s) lost "
            f"evidence (net {metrics['net_evidence_delta']:+d} overall, "
            f"{gained} question(s) gained), under the threshold of "
            f"{metrics['threshold']['effective']} — reported, not gated. This "
            "is the tail the aggregate metrics cannot see; re-read it as dream "
            "density rises."
        )
    rank = metrics["rank_shift"]
    if rank["degraded_count"]:
        worst = rank["worst_question"]
        out.append(
            f"RANK DEGRADATION: {rank['degraded_count']} question(s) kept their "
            f"evidence but its best rank slipped by {rank['min_shift']} or more "
            f"slots (worst: {worst} {_shift_phrase(rank)}; mean shift across "
            f"{rank['questions']} question(s) with evidence on both sides "
            f"{rank['mean_shift']:+.2f}). Evidence still inside top-k has not "
            "been displaced OUT of it, so this does not gate — but it is what "
            "MRR and NDCG measure, and over hundreds of questions it is "
            "invisible in the aggregate."
        )
    return out


def _shift_phrase(rank: dict) -> str:
    rows = {r["question_id"]: r for r in rank["degraded_questions"]}
    row = rows.get(rank["worst_question"])
    if row is None:
        return f"{rank['worst_shift']:+d} slots"
    return f"rank {row['rank_before']} -> {row['rank_after']}"


# ---------------------------------------------------------------------------
# Run-record plumbing.
# ---------------------------------------------------------------------------

def _per_question_block(data) -> dict:
    block = data.get("per_question") if isinstance(data, dict) else None
    return block if isinstance(block, dict) else {}


def _config_k(data, config: str) -> int | None:
    retrieval = data.get("retrieval") if isinstance(data, dict) else None
    if not isinstance(retrieval, dict):
        return None
    scores = retrieval.get(config)
    if not isinstance(scores, dict):
        return None
    k = scores.get("k")
    return k if isinstance(k, int) and not isinstance(k, bool) else None


def common_configs(before, after) -> list[str]:
    """Recall configs with per-question rows in BOTH records.

    Exposed because a caller has to be able to ask "is there anything here to
    analyse?" without paying for the evidence join first — loading the 265 MB
    dataset to discover that a record predates `per_question` is a poor trade,
    and `bench.dream_ab` needs the distinction to tell "no displacement" apart
    from "no rows to look at".
    """
    return sorted((set(_per_question_block(before)) & set(_per_question_block(after)))
                  - {_QA_KEY})


def _config_score_block(data, config: str) -> dict:
    """The per-config `score_run` block, which is where `recall_counts` lives.

    Returns `{}` — never `None` — when the record has no such block. A record
    WAS supplied and it says nothing about its recalls, which is the
    "UNVERIFIED: the 'after' run records no recall counts" case. `None` is
    reserved for a caller that offered no provenance at all, and collapsing the
    two would tell an operator to change how they call the tool when the real
    remedy is to re-run the leg on a harness that writes counts.
    """
    retrieval = data.get("retrieval") if isinstance(data, dict) else None
    if not isinstance(retrieval, dict):
        return {}
    block = retrieval.get(config)
    return block if isinstance(block, dict) else {}


def compare_displacement_files(
    before, after, evidence, *,
    known_sessions: dict | None = None,
    min_lost_questions: int = MIN_LOST_QUESTIONS_DEFAULT,
    lost_question_rate: float = LOST_QUESTION_RATE_DEFAULT,
    rank_shift_warn: int = RANK_SHIFT_WARN_DEFAULT,
) -> dict[str, dict]:
    """Pure. Compares every recall config common to both run records. Returns
    `{config: compare_displacement(...)}`, or a single synthetic `{"error":
    {...}}` entry when nothing is comparable — the same loud shape
    `bench.dream_ab.compare_result_files` returns, so callers need no
    empty-result branch.

    `k` per config is taken from the record's own `retrieval[config]["k"]` and
    must agree between the two runs: a top-3 cut and a top-10 cut hold
    different numbers of slots, so displacement between them is not a
    measurement. When neither record states `k` the full hit list is used and
    the config is warned about.

    The 'after' record's per-config score block is passed through as the
    positive control's `after_run`. It is read PER CONFIG rather than once per
    record because `recall_counts` is stamped per config
    (`bench.report.attach_recall_counts`): a run can legitimately complete the
    `bench` config's recalls and skip every one of `defaults`', and refusing
    both configs on one config's zero would be as wrong as certifying both.
    """
    before_pq = _per_question_block(before)
    after_pq = _per_question_block(after)
    common = common_configs(before, after)
    if not common:
        return {
            "error": _failure(
                "ERROR: not comparable — no recall config has per-question rows "
                f"in both runs (before: {sorted(set(before_pq) - {_QA_KEY})}, "
                f"after: {sorted(set(after_pq) - {_QA_KEY})}). A run record "
                "written without a 'per_question' block cannot be analysed for "
                "displacement."
            )
        }

    out: dict[str, dict] = {}
    for config in common:
        before_k, after_k = _config_k(before, config), _config_k(after, config)
        if before_k is not None and after_k is not None and before_k != after_k:
            out[config] = _failure(
                f"ERROR: not comparable — k mismatch for config {config!r} "
                f"(before k={before_k!r}, after k={after_k!r})"
            )
            continue
        k = before_k if before_k is not None else after_k
        result = compare_displacement(
            before_pq[config], after_pq[config], evidence,
            k=k, known_sessions=known_sessions,
            after_run=_config_score_block(after, config),
            min_lost_questions=min_lost_questions,
            lost_question_rate=lost_question_rate,
            rank_shift_warn=rank_shift_warn,
        )
        if k is None:
            result["warnings"] = [
                "UNVERIFIED: neither run record states a top-k for config "
                f"{config!r}, so the full hit list was compared rather than a "
                "k-truncated one"
            ] + result["warnings"]
        out[config] = result
    return out


# ---------------------------------------------------------------------------
# Evidence resolution — the dataset join, from the record when possible.
# ---------------------------------------------------------------------------

def _merge_evidence(merged: dict[str, list], origins: dict[str, str],
                    stamped: dict, origin: str, subject: str) -> None:
    """Fold one stamped `answer_session_ids` map into `merged`, refusing to
    resolve a disagreement.

    One function, used for BOTH scopes — two configs of one record, and the two
    records of a pair. That is the point: the intra-record case already raised
    ("guessing would poison every number downstream") while the cross-record
    case silently took the last writer, so a `before` stamping `q1 -> [A]` and
    an `after` stamping `q1 -> [Z]` returned `{q1: [Z]}` with no error at all —
    the same defect, handled two opposite ways, one call site apart. Two
    sources that disagree about what the evidence IS are not comparable at
    either scope, and last-wins is the worst of the three available answers:
    it silently reclassifies which slots held evidence on BOTH sides.
    """
    for qid, ids in stamped.items():
        if qid in merged and sorted(merged[qid]) != sorted(ids):
            raise ValueError(
                f"{subject} about the evidence for question {qid!r} "
                f"({origin} says {ids!r}, {origins[qid]} said {merged[qid]!r})"
            )
        merged[qid] = list(ids)
        origins[qid] = origin


def _stamped_maps(record):
    """`(config, answer_session_ids)` for each config carrying the stamp."""
    retrieval = record.get("retrieval") if isinstance(record, dict) else None
    if not isinstance(retrieval, dict):
        return
    for config, scores in sorted(retrieval.items()):
        block = scores.get("displacement") if isinstance(scores, dict) else None
        stamped = block.get("answer_session_ids") if isinstance(block, dict) else None
        if isinstance(stamped, dict):
            yield config, stamped


def evidence_from_record(record) -> dict[str, list]:
    """`answer_session_ids` stamped into a run record by the scoring step
    (`retrieval[<config>]["displacement"]["answer_session_ids"]`).

    Returns `{}` when the record predates the stamp. Two configs disagreeing
    about the same question's evidence raises rather than picking one — they
    were scored from the same dataset, so a disagreement means one of them is
    not the dataset and guessing which would poison every number downstream.
    """
    merged: dict[str, list] = {}
    origins: dict[str, str] = {}
    for config, stamped in _stamped_maps(record):
        _merge_evidence(merged, origins, stamped, f"config {config!r}",
                        "run record disagrees with itself")
    return merged


def evidence_from_dataset(path: Path) -> tuple[dict[str, list], dict[str, set]]:
    """`(answer_session_ids, known_session_ids)` from the LongMemEval-S file.

    The second map is what enables `foreign_session_slots`: every session a
    question's namespace could legitimately return. It is available only on
    this path — the run-record stamp deliberately carries evidence ids only,
    because stamping ~50 haystack ids per question would add roughly a megabyte
    to every published record to detect a shape measured at zero.
    """
    rows = load_dataset(path)
    evidence = {r["question_id"]: list(r["answer_session_ids"]) for r in rows}
    known = {
        r["question_id"]: set(r["haystack_session_ids"]) | set(r["answer_session_ids"])
        for r in rows
    }
    return evidence, known


def resolve_evidence(before, after, *, dataset_path: Path | None = None,
                     default_dataset: Path | None = None) -> dict:
    """Where the `answer_session_ids` join comes from, in priority order.

    An explicit `dataset_path` wins (it is the only source that also yields the
    known-session map). Otherwise the two run records' own stamped block is
    used — that is the point of stamping it, since `data/` is gitignored while
    `results/` is committed, so a record must stay analysable on a machine that
    never downloaded the 265 MB dataset. Failing both, the default dataset path
    is tried if it happens to exist.

    Returns `{"evidence", "known_sessions", "source", "error"}`; `error` is a
    sentence naming what to do about it, never a silent empty map — including
    when the two records disagree with each other about a question's evidence,
    which is refused exactly as loudly as one record disagreeing with itself.
    """
    if dataset_path is not None:
        if not Path(dataset_path).exists():
            return {"evidence": None, "known_sessions": None, "source": None,
                    "error": f"--dataset {dataset_path} does not exist"}
        evidence, known = evidence_from_dataset(Path(dataset_path))
        return {"evidence": evidence, "known_sessions": known,
                "source": f"dataset {dataset_path}", "error": None}

    # Two scopes of disagreement, refused separately so each error names its own
    # remedy: a record inconsistent WITH ITSELF is a broken record, while two
    # records inconsistent with EACH OTHER were scored against different data.
    stamped: dict[str, list] = {}
    origins: dict[str, str] = {}
    for side, record in (("before", before), ("after", after)):
        try:
            per_record = evidence_from_record(record)
        except ValueError as exc:
            return {"evidence": None, "known_sessions": None, "source": None,
                    "error": f"the {side!r} record is unusable: {exc}"}
        try:
            _merge_evidence(stamped, origins, per_record,
                            f"the {side!r} record",
                            "the two run records disagree")
        except ValueError as exc:
            return {"evidence": None, "known_sessions": None, "source": None,
                    "error": (
                        f"{exc} — two records that disagree about what the "
                        "evidence IS are not comparable; re-score both legs "
                        "from the same dataset, or pass --dataset to override "
                        "the stamps with the file itself"
                    )}
    if stamped:
        return {"evidence": stamped, "known_sessions": None,
                "source": "run records' stamped 'displacement' block",
                "error": None}

    fallback = default_dataset if default_dataset is not None else DATA_DIR / "longmemeval_s.json"
    if Path(fallback).exists():
        evidence, known = evidence_from_dataset(Path(fallback))
        return {"evidence": evidence, "known_sessions": known,
                "source": f"dataset {fallback}", "error": None}

    return {
        "evidence": None, "known_sessions": None, "source": None,
        "error": (
            "no source of answer_session_ids — the run records carry no "
            "'displacement' block (they predate it) and no dataset was found "
            f"at {fallback}. Pass --dataset <longmemeval_s.json>, or re-run "
            "`python -m bench.report --run-label <label>` with a harness that "
            "stamps the block."
        ),
    }


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------

def _fmt_delta(before: int, after: int) -> str:
    return f"{before} -> {after} ({after - before:+d})"


def _render_detail(rows: list[dict], heading: str) -> list[str]:
    if not rows:
        return []
    lines = ["", heading]
    for row in rows:
        tags = " [abstention]" if row["abstention"] else ""
        parts = []
        for session in row["sessions"]:
            if session["delta"] < 0:
                parts.append(
                    f"{session['session_id']} x{-session['delta']} lost "
                    f"(held ranks {session['ranks_before']} before; attributed "
                    f"to rank(s) {session['lost_ranks_before']})"
                )
            else:
                parts.append(
                    f"{session['session_id']} x{session['delta']} gained "
                    f"(now at ranks {session['ranks_after']})"
                )
        lines.append(
            f"- `{row['question_id']}`{tags}: {row['evidence_before']} -> "
            f"{row['evidence_after']} evidence hits"
            + (f", {row['untagged_slots_after']} untagged slot(s) after" if row["untagged_slots_after"] else "")
            + ("; " + "; ".join(parts) if parts else "")
        )
    return lines


def _provenance_cell(p: dict) -> str:
    if p["status"] == PROVENANCE_CONFIRMED:
        return f"confirmed (final invocation completed {p['completed']} recalls)"
    if p["status"] == PROVENANCE_REFUSED:
        return "REFUSED (its final invocation completed 0 recalls)"
    if not p["supplied"]:
        return "UNVERIFIED (none supplied to this comparison)"
    if p["completed"] is None:
        return "UNVERIFIED (the record states no recall counts)"
    return f"UNVERIFIED (cumulative only: completed={p['completed']})"


def _rank_shift_cell(rank: dict) -> str:
    if not rank["questions"]:
        return "n/a (no question held evidence in top-k on both sides)"
    worst = (f", worst {rank['worst_shift']:+d} on {rank['worst_question']}"
             if rank["worst_question"] else "")
    return (f"mean {rank['mean_shift']:+.2f} slot(s) over {rank['questions']} "
            f"question(s){worst}")


def _render_rank_detail(rank: dict) -> list[str]:
    """The questions whose retained evidence slid down the page, by name.

    A number in a table is not the same as a row a reader can act on: the
    defect this closes was a question going rank 1 -> 9 with NO line anywhere
    in the report saying so.
    """
    if not rank["degraded_questions"]:
        return []
    lines = ["", f"Retained evidence that moved down by >= {rank['min_shift']} slot(s):"]
    for row in rank["degraded_questions"]:
        tags = " [abstention]" if row["abstention"] else ""
        lines.append(
            f"- `{row['question_id']}`{tags}: best evidence rank "
            f"{row['rank_before']} -> {row['rank_after']} ({row['shift']:+d}) "
            "— still in top-k, so not counted as a loss"
        )
    return lines


def _render_failure(name: str, analysis: dict) -> str:
    """A loud failure renders its cause, NOT a measurement table.

    `empty_metrics()` is all zeroes, so the full table under a refusal read
    `result sets changed 0 | questions that LOST evidence 0 | net +0` — the
    exact reassuring-looking output the refusal exists to prevent, printed
    directly above the sentence explaining that nothing was measured. A reader
    skimming tables sees the zeroes.
    """
    return "\n".join([
        f"### {name} — displacement",
        "",
        "| measure | value |",
        "|---|---|",
        "| questions compared | 0 — NOT ANALYSED |",
        f"| 'after' leg recall provenance | "
        f"{_provenance_cell(analysis['metrics']['recall_provenance'])} |",
        "",
        analysis["verdict"],
    ])


def render_markdown(analyses: dict[str, dict]) -> str:
    sections = []
    for name, analysis in analyses.items():
        m = analysis["metrics"]
        if analysis["regressed"] and not m["questions_compared"]:
            sections.append(_render_failure(name, analysis))
            continue
        k = m["k"] if m["k"] is not None else "all"
        rank = m["rank_shift"]
        lines = [
            f"### {name} — displacement",
            "",
            "| measure | value |",
            "|---|---|",
            f"| questions compared | {m['questions_compared']} |",
            f"| 'after' leg recall provenance | "
            f"{_provenance_cell(m['recall_provenance'])} |",
            f"| result sets changed | {m['changed_count']} ({m['changed_pct']:.2f}%) |",
            f"| evidence hits in top-{k} | "
            f"{_fmt_delta(m['evidence_hits_before'], m['evidence_hits_after'])} |",
            f"| questions that LOST evidence | {m['lost_evidence_count']} |",
            f"| questions that GAINED evidence | {m['gained_evidence_count']} |",
            f"| best evidence rank shift (retained evidence) | "
            f"{_rank_shift_cell(rank)} |",
            f"| questions whose best evidence rank worsened by >= "
            f"{rank['min_shift']} | {rank['degraded_count']} |",
            # NOT "dream-shaped". The row observes a shape; it cannot observe a
            # cause. `results/METHODOLOGY.md` documents graph-only hits taking
            # rank slots with session_id=None too, so a table reading
            # "untagged (dream-shaped) slots 0 -> 1 (+1)" asserted provenance
            # the data cannot support — and the table is what a reader sees,
            # whatever the docstring says. The footnote below carries the
            # caveat into the rendered output.
            f"| untagged slots (session_id=None) | "
            f"{_fmt_delta(m['untagged_slots_before'], m['untagged_slots_after'])} |",
        ]
        if m["foreign_session_slots_before"] is None:
            lines.append("| foreign-session slots | not computed (no dataset supplied) |")
        else:
            lines.append(
                "| foreign-session slots | "
                f"{_fmt_delta(m['foreign_session_slots_before'], m['foreign_session_slots_after'])} |"
            )
        lines.append(
            f"| gate threshold | {m['threshold']['effective']} lost-evidence "
            f"question(s) = max({m['threshold']['min_lost_questions']}, "
            f"{m['threshold']['lost_question_rate']:.3%} of "
            f"{m['questions_compared']}) |"
        )
        lines.append(
            f"| scored / abstention split | lost "
            f"{m['scored']['lost_evidence_count']} / "
            f"{m['abstention']['lost_evidence_count']}, net "
            f"{m['scored']['net_evidence_delta']:+d} / "
            f"{m['abstention']['net_evidence_delta']:+d} |"
        )
        lines.append("")
        lines.append(
            "> `untagged` counts slots whose hit carries no `lm_session:` tag. "
            "A dream insight has that shape — and so does a graph-only hit "
            "(`results/METHODOLOGY.md`). This row records the shape, not the "
            "cause: it is evidence a dream MAY have taken the slot, never proof "
            "that one did."
        )
        lines.extend(_render_detail(m["lost_evidence_questions"], "Lost evidence:"))
        lines.extend(_render_detail(m["gained_evidence_questions"], "Gained evidence:"))
        lines.extend(_render_rank_detail(rank))
        lines.append("")
        lines.append(analysis["verdict"])
        for warning in analysis.get("warnings") or []:
            lines.append(f"WARNING: {warning}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def refused_configs(analyses: dict[str, dict]) -> list[str]:
    """Config names whose 'after' leg positively recalled nothing.

    Exposed so both CLIs can name the refusal instead of describing it as an
    evidence displacement — `bench.dream_ab` composes this module and would
    otherwise print "dreams displaced evidence from top-k" for a leg that never
    ran, which is the actionable-but-wrong diagnosis this codebase already
    refuses to print elsewhere.
    """
    return sorted(
        name for name, analysis in analyses.items()
        if analysis["metrics"]["recall_provenance"]["status"] == PROVENANCE_REFUSED
    )


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.displacement")
    ap.add_argument("--before", required=True, help="path to the 'before' results/*.json")
    ap.add_argument("--after", required=True, help="path to the 'after' results/*.json")
    ap.add_argument(
        "--dataset", default=None,
        help="path to longmemeval_s.json — overrides the evidence stamped in "
             "the run records, and is the only source that also enables "
             "foreign-session detection",
    )
    ap.add_argument(
        "--min-lost-questions", type=int, default=MIN_LOST_QUESTIONS_DEFAULT,
        help="floor for the number of lost-evidence questions that gates "
             f"(default {MIN_LOST_QUESTIONS_DEFAULT})",
    )
    ap.add_argument(
        "--lost-question-rate", type=float, default=LOST_QUESTION_RATE_DEFAULT,
        help="fraction of compared questions that gates, whichever is larger "
             f"(default {LOST_QUESTION_RATE_DEFAULT}). The gate fires at "
             "max(--min-lost-questions, rate x compared), so TIGHTENING it "
             "means lowering both knobs",
    )
    ap.add_argument(
        "--rank-shift-warn", type=int, default=RANK_SHIFT_WARN_DEFAULT,
        help="how many slots an evidence hit that STAYED in top-k must slide "
             "down before the question is named in a RANK DEGRADATION warning "
             f"(default {RANK_SHIFT_WARN_DEFAULT}). This warns; it never gates",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    before = _load_json(args.before)
    after = _load_json(args.after)
    resolved = resolve_evidence(
        before, after,
        dataset_path=Path(args.dataset) if args.dataset else None,
    )
    if resolved["error"]:
        print(f"ERROR: {resolved['error']}")
        print("VERDICT: NOT RUN — displacement could not be computed")
        return 1
    print(f"Evidence source: {resolved['source']}")
    print()

    analyses = compare_displacement_files(
        before, after, resolved["evidence"],
        known_sessions=resolved["known_sessions"],
        min_lost_questions=args.min_lost_questions,
        lost_question_rate=args.lost_question_rate,
        rank_shift_warn=args.rank_shift_warn,
    )
    print(render_markdown(analyses))
    print()

    # The positive control's refusal is reported by NAME, before the
    # displacement verdict. Both are `regressed=True`, but "a dream took an
    # evidence slot" and "this leg never ran" have nothing in common except the
    # exit code, and printing the first for the second sends an operator to
    # audit a design change when the repair is to re-run the leg.
    refused = refused_configs(analyses)
    if refused:
        print(
            f"VERDICT: NOT CERTIFIED — config(s) {', '.join(refused)} could not "
            "be analysed because the 'after' leg completed no recalls; its rows "
            "are an earlier invocation's artefacts, so a clean displacement "
            "table here would mean nothing"
        )
        return 1
    # A `_failure` (incomparable rows, cross-record evidence disagreement, a
    # malformed record) is also `regressed=True` — it must never read as green —
    # but it is NOT the displacement finding. Naming it one sends an operator to
    # audit a design change when the repair is to fix the inputs. Distinguish by
    # the presence of the metrics a real analysis produces.
    analysed = {c: a for c, a in analyses.items() if a.get("metrics")}
    broken = [c for c, a in analyses.items() if a["regressed"] and not a.get("metrics")]
    if broken:
        print(
            f"VERDICT: NOT COMPARABLE — config(s) {', '.join(sorted(broken))} could "
            "not be analysed; see the error above. This is not a displacement "
            "finding: nothing was measured, so nothing is being claimed about it"
        )
        return 1
    if any(a["regressed"] for a in analysed.values()):
        print(
            "VERDICT: EVIDENCE DISPLACEMENT — dreams are taking top-k slots "
            "from real evidence at a rate the aggregate gate cannot see"
        )
        return 1
    if any(a.get("warnings") for a in analyses.values()):
        print(
            "VERDICT: OK (WITH WARNINGS) — no displacement pattern beyond the "
            "threshold, but read the WARNING lines above before treating this "
            "as a green gate"
        )
        return 0
    print("VERDICT: OK — no question lost evidence to a displaced slot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
