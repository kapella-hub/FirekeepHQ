"""Dreaming A/B comparator — the regression gate.

Why this exists: the audit found the existing Cortex eval surface cannot
detect a dreaming regression. `_memory_freshness_at_recall` averages
`RecallResponse.score`, which is `max(sources[].score)` after min-max
normalisation — i.e. pinned to 1.0 by construction, regardless of whether
recall actually got worse. The only instrument that can tell whether
Dreaming helps or hurts retrieval is this LongMemEval-S harness. Dreaming
ships enabled ONLY on a measured non-regression against this comparator —
see the README section "Dreaming A/B".

`compare_runs` is the pure core: it takes two Task-5-shaped `score_run`
results (`bench.score_retrieval.score_run`'s return shape — top-level `k`
and `overall`, plus the `recall_counts` block `bench.run` stamps onto each
config in the published run-record) and decides whether "after" regressed
relative to "before". The counts are the positive control: an "after" leg
whose LAST INVOCATION recorded `completed=0` recalled nothing, so its scores
are a re-score of artefacts an earlier invocation produced, and the comparison
is refused rather than reported as a flawless +0.0000. The cumulative
`completed` cannot serve as that control — it survives a same-label re-run
that did nothing — so it is carried as provenance only.
`compare_result_files`/`main` are the practical wrapper: a real `bench.run`
invocation produces a full run-record (`results/<ts>-<label>.json`, one
`score_run` per recall config nested under `retrieval`), so the CLI accepts
either that shape or a bare `score_run` (e.g. `work/scores_<config>.json`)
and compares every recall config common to both files.

The CLI runs a SECOND, independent gate alongside this one:
`bench.displacement`. The aggregate comparison here watches four means and
cannot see a tail — a dream taking a top-k slot that held real evidence moves
`ndcg_at_k` by ~1e-5, four orders of magnitude under the 0.005 tolerance, so a
design that leaks evidence at that rate passes this gate indefinitely. That is
not hypothetical: it is what the first measured A/B did. Both gates run, both
can fail the exit code, and neither subsumes the other.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench import displacement
from bench.common import (
    RECALL_LAST_INVOCATION_KEY as _LAST_INVOCATION_KEY,
    completed_recalls as _completed_recalls,
    describe_zero_recalls as _describe_zero_recalls,
)

# The three metrics the regression gate watches, per the design brief.
# `mrr` is still reported in `deltas` for visibility but deliberately does
# NOT gate — the brief specifies Recall@k / Coverage@k / NDCG@k only.
_GATE_METRICS = ("recall_at_k", "coverage_at_k", "ndcg_at_k")
_ALL_METRICS = ("recall_at_k", "coverage_at_k", "mrr", "ndcg_at_k")

# The zero-recall positive control lives in `bench.common` — `bench.displacement`
# needs the identical control and re-implementing it there is how this defect
# came back once already. The names above are kept so `compare_runs` reads as it
# always did; the implementations are shared, not copied.


def compare_runs(before: dict, after: dict, *, tolerance: float = 0.005) -> dict:
    """Pure. Returns `{"deltas": {metric: after-before}, "regressed": bool,
    "verdict": str, "warnings": [str]}`.

    Defensive by construction, not by inspection — every doubtful shape
    fails LOUD (`regressed=True`, empty `deltas`, a `verdict` that says
    exactly what made the runs incomparable) rather than computing a
    number that looks like a real delta:

    - a missing/malformed `overall` on either side,
    - an 'after' run whose final invocation recorded `completed=0` recalls
      — it recalled nothing, so its scores are a re-score of artefacts some
      earlier invocation produced and the delta between them is an artifact,
      not evidence,
    - a `k` mismatch between the two runs (they measured different top-k
      cuts — a delta between them is not meaningful),
    - none of the three gated metrics present in both `overall` dicts.

    A single metric missing from one side (but the others present) is not
    fatal — it is simply omitted from `deltas` and the gate runs on
    whatever metrics both sides actually reported.

    `warnings` is a separate, non-gating channel for things that are
    suspicious but genuinely possible — see the identical-metrics note at
    the bottom of this function.
    """
    before_overall = before.get("overall") if isinstance(before, dict) else None
    after_overall = after.get("overall") if isinstance(after, dict) else None
    if not isinstance(before_overall, dict) or not isinstance(after_overall, dict):
        return {
            "deltas": {},
            "regressed": True,
            "verdict": (
                "ERROR: not comparable — 'before' and/or 'after' is missing "
                "a valid 'overall' key"
            ),
            "warnings": [],
        }

    # The positive control. A run whose recall stage completed nothing did not
    # measure the store; it re-scored rows on disk. Bit-identical metrics and a
    # +0.0000 delta on every gate metric is the most reassuring-looking output
    # this tool can print, so this must fail LOUD rather than pass quietly.
    #
    # It reads the LAST INVOCATION's count, not the label's cumulative total —
    # the reachable no-op is "run the leg, enable dreaming, run the identical
    # command again", which recalls nothing while the cumulative total still
    # reports the first invocation's work. A re-run after a leg legitimately
    # finished is equally not evidence about the store as it stands now, and is
    # equally refused.
    after_completed, per_invocation = _completed_recalls(after)
    if after_completed == 0:
        return {
            "deltas": {},
            "regressed": True,
            "verdict": (
                "ERROR: not comparable — "
                + _describe_zero_recalls(after, per_invocation)
            ),
            "warnings": [],
        }

    before_k, after_k = before.get("k"), after.get("k")
    if before_k is not None and after_k is not None and before_k != after_k:
        return {
            "deltas": {},
            "regressed": True,
            "verdict": (
                f"ERROR: not comparable — k mismatch "
                f"(before k={before_k!r}, after k={after_k!r})"
            ),
            "warnings": [],
        }

    deltas = {
        metric: after_overall[metric] - before_overall[metric]
        for metric in _ALL_METRICS
        if metric in before_overall and metric in after_overall
    }

    gate_available = [m for m in _GATE_METRICS if m in deltas]
    if not gate_available:
        return {
            "deltas": deltas,
            "regressed": True,
            "verdict": (
                "ERROR: none of the gate metrics "
                f"({', '.join(_GATE_METRICS)}) are present in both runs — "
                "cannot verify non-regression"
            ),
            "warnings": [],
        }

    # Non-gating channel. Only reachable when the 'after' run did not
    # positively demonstrate that its final invocation recalled — a run that
    # did is trusted, and one that reported zero already failed above.
    warnings: list[str] = []
    if after_completed is None:
        warnings.append(
            "UNVERIFIED: the 'after' run records no recall counts, so this "
            "comparison cannot confirm it actually executed its recalls "
            "(re-run the 'after' leg with a harness that writes "
            "`recall_counts`)"
        )
    elif not per_invocation:
        warnings.append(
            "UNVERIFIED: the 'after' run records only a CUMULATIVE recall "
            f"count (completed={after_completed} across every invocation of "
            "its run label), not a per-invocation one, so this comparison "
            "cannot rule out that its final invocation skipped every question "
            "and re-scored rows an earlier invocation had produced (re-run "
            "the 'after' leg with a harness that writes "
            f"`{_LAST_INVOCATION_KEY}`)"
        )
    # Deliberately a WARNING, not a regression. Retrieval scoring is
    # deterministic, so an unchanged store legitimately produces bit-identical
    # metrics — "identical" is genuinely ambiguous between "nothing changed"
    # and "nothing ran", and the last-invocation count above is what
    # distinguishes them mechanically. This line is only the fallback signal
    # for records that cannot supply it; making it gate would fail honest
    # no-change results, and it must never fire for a run that demonstrably
    # recalled (hence the `per_invocation` guard).
    if not per_invocation and deltas and all(d == 0 for d in deltas.values()):
        warnings.append(
            "SUSPECT: every metric is bit-identical between the two runs "
            "— consistent with the 'after' leg having re-scored the "
            "'before' leg's artefacts rather than recalling anything "
            "(also consistent with a genuine no-change result)"
        )

    offenders = [m for m in gate_available if deltas[m] < -tolerance]
    regressed = bool(offenders)

    if regressed:
        detail = ", ".join(
            f"{m} dropped by {-deltas[m]:.4f} "
            f"({before_overall[m]:.4f} -> {after_overall[m]:.4f})"
            for m in offenders
        )
        verdict = f"REGRESSION: {detail} (tolerance {tolerance})"
    else:
        watched = ", ".join(
            f"{m} {deltas[m]:+.4f}" for m in gate_available
        )
        verdict = f"OK: no gate metric dropped beyond tolerance {tolerance} ({watched})"

    return {
        "deltas": deltas,
        "regressed": regressed,
        "verdict": verdict,
        "warnings": warnings,
    }


def _extract_score_runs(data: dict) -> dict[str, dict]:
    """Normalise a loaded result JSON into `{config_name: score_run}`.

    A `bench.run` run-record (`results/<ts>-<label>.json`) nests one
    `score_run` per recall config under `retrieval`. A bare `score_run`
    (e.g. `work/scores_<config>.json`, or the shape the brief's tests pass
    directly) has `overall` at the top level — that gets wrapped under the
    synthetic name `"result"` so it flows through the same comparison path.
    Anything else yields no configs, which `compare_result_files` reports
    as a loud "nothing common" error rather than crashing here.
    """
    if not isinstance(data, dict):
        return {}
    retrieval = data.get("retrieval")
    if isinstance(retrieval, dict) and retrieval:
        return retrieval
    if isinstance(data.get("overall"), dict):
        return {"result": data}
    return {}


def compare_result_files(before: dict, after: dict, *, tolerance: float = 0.005) -> dict[str, dict]:
    """Pure. Compares every recall config common to both `before`/`after`
    (each either a full run-record or a bare `score_run`). Returns
    `{config_name: compare_runs(...)}`; when no config is common to both,
    returns a single synthetic `{"error": {...}}` entry — same loud-failure
    shape as `compare_runs`, so callers (the CLI) don't need a separate
    empty-result branch.
    """
    before_runs = _extract_score_runs(before)
    after_runs = _extract_score_runs(after)
    common = sorted(set(before_runs) & set(after_runs))
    if not common:
        return {
            "error": {
                "deltas": {},
                "regressed": True,
                "verdict": (
                    "ERROR: not comparable — no recall config is common to "
                    f"both runs (before: {sorted(before_runs)}, "
                    f"after: {sorted(after_runs)})"
                ),
                "warnings": [],
            }
        }
    return {
        name: compare_runs(before_runs[name], after_runs[name], tolerance=tolerance)
        for name in common
    }


def render_markdown(comparisons: dict[str, dict]) -> str:
    sections = []
    for name, cmp in comparisons.items():
        lines = [f"### {name}", "", "| metric | delta |", "|---|---|"]
        for metric, delta in cmp["deltas"].items():
            lines.append(f"| {metric} | {delta:+.4f} |")
        if not cmp["deltas"]:
            lines.append("| (none) | — |")
        lines.append("")
        lines.append(cmp["verdict"])
        for warning in cmp.get("warnings") or []:
            lines.append(f"WARNING: {warning}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# Prefix of the warning `displacement_section` raises when the displacement
# gate refused a config outright. `main` reads it to name WHICH gate failed;
# keeping it a module constant is what stops that from being a loose string
# match on prose that a later edit could silently break.
NOT_CERTIFIED_MARKER = "DISPLACEMENT GATE REFUSED"


def displacement_section(before: dict, after: dict, *, dataset_path=None,
                         min_lost_questions: int = displacement.MIN_LOST_QUESTIONS_DEFAULT,
                         lost_question_rate: float = displacement.LOST_QUESTION_RATE_DEFAULT,
                         rank_shift_warn: int = displacement.RANK_SHIFT_WARN_DEFAULT,
                         ) -> tuple[str, bool, list[str]]:
    """`(markdown, regressed, warnings)` for the displacement half of the gate.

    Kept out of `compare_runs`/`compare_result_files` on purpose: those two
    functions are the aggregate gate, they are exercised by their own tests as
    a fixed contract, and a second gate bolted into their return shape would
    make one failure indistinguishable from the other. This composes at the CLI
    instead, where two independent verdicts can both be printed and both feed
    the exit code.

    A record with no `per_question` block, or no reachable source of
    `answer_session_ids`, does not fail the run: it returns `regressed=False`
    with a WARNING saying in as many words that the displacement gate DID NOT
    RUN. That is deliberate and it is the same call `compare_runs` makes for a
    record carrying no `recall_counts` — `data/` is gitignored while `results/`
    is committed, so refusing to compare two published records on a machine
    that never downloaded the 265 MB dataset would break the tool for its most
    common use. The cost is that a silent absence is possible, which is why the
    warning names the gate by name and the CLI's final line reads
    "OK (WITH WARNINGS)". `python -m bench.displacement` makes the opposite
    call and exits non-zero, because there displacement is the whole job.

    Records that DO carry rows and are nonetheless incomparable (a `k`
    mismatch, a malformed row, an 'after' leg that completed no recalls) still
    fail — that is a broken comparison, not a missing capability, and the
    aggregate gate refuses the same shapes.

    The zero-recall refusal is surfaced through `warnings` with the
    `NOT_CERTIFIED_MARKER` prefix as well as through `regressed`, because
    `regressed=True` alone cannot tell `main` WHICH failure it is, and the two
    have different remedies: a displacement pattern means the design leaks
    evidence, an uncertified leg means the measurement never happened.

    The row check runs BEFORE evidence resolution so a record predating
    `per_question` does not pay for a 265 MB dataset load to learn there is
    nothing to analyse.
    """
    if not displacement.common_configs(before, after):
        return "", False, [
            "DISPLACEMENT GATE DID NOT RUN: neither run record carries "
            "per-question recall rows for a config the other also has, so "
            "there is nothing to compare slot by slot (a record written before "
            "`per_question` existed, or one assembled without it)"
        ]
    resolved = displacement.resolve_evidence(before, after, dataset_path=dataset_path)
    if resolved["error"]:
        return "", False, [
            "DISPLACEMENT GATE DID NOT RUN: " + resolved["error"]
        ]
    analyses = displacement.compare_displacement_files(
        before, after, resolved["evidence"],
        known_sessions=resolved["known_sessions"],
        min_lost_questions=min_lost_questions,
        lost_question_rate=lost_question_rate,
        rank_shift_warn=rank_shift_warn,
    )
    markdown = (
        f"Evidence source: {resolved['source']}\n\n"
        + displacement.render_markdown(analyses)
    )
    regressed = any(a["regressed"] for a in analyses.values())
    warnings = [w for a in analyses.values() for w in (a.get("warnings") or [])]
    # A refused config is a loud failure, so it carries no warnings of its own
    # (`_failure` returns an empty list by contract). Surface it here, first, so
    # the composed CLI can name it — and so a reader skimming the warning block
    # cannot miss that a config was not analysed at all.
    refused = displacement.refused_configs(analyses)
    if refused:
        warnings.insert(0, (
            f"{NOT_CERTIFIED_MARKER}: config(s) {', '.join(refused)} were not "
            "analysed — the 'after' leg completed no recalls, so its "
            "per-question rows are an earlier invocation's artefacts and a "
            "clean displacement table would be a comparison of those rows with "
            "themselves"
        ))
    return markdown, regressed, warnings


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.dream_ab")
    ap.add_argument("--before", required=True, help="path to the 'before' results/*.json")
    ap.add_argument("--after", required=True, help="path to the 'after' results/*.json")
    ap.add_argument("--tolerance", type=float, default=0.005)
    ap.add_argument(
        "--dataset", default=None,
        help="path to longmemeval_s.json for the displacement gate's evidence "
             "join (defaults to the block stamped in the run records, then to "
             "data/longmemeval_s.json)",
    )
    ap.add_argument(
        "--min-lost-questions", type=int,
        default=displacement.MIN_LOST_QUESTIONS_DEFAULT,
        help="displacement gate: floor for the number of questions that must "
             "lose an evidence hit before it fires "
             f"(default {displacement.MIN_LOST_QUESTIONS_DEFAULT})",
    )
    ap.add_argument(
        "--lost-question-rate", type=float,
        default=displacement.LOST_QUESTION_RATE_DEFAULT,
        help="displacement gate: fraction of compared questions that fires it, "
             f"whichever is larger (default {displacement.LOST_QUESTION_RATE_DEFAULT})",
    )
    ap.add_argument(
        "--rank-shift-warn", type=int,
        default=displacement.RANK_SHIFT_WARN_DEFAULT,
        help="displacement report: slots an evidence hit that stayed in top-k "
             "must slide down before its question is named in a RANK "
             f"DEGRADATION warning (default {displacement.RANK_SHIFT_WARN_DEFAULT}; "
             "warns, never gates)",
    )
    ap.add_argument(
        "--no-displacement", action="store_true",
        help="skip the displacement gate entirely (the aggregate gate still runs)",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    before = _load_json(args.before)
    after = _load_json(args.after)
    comparisons = compare_result_files(before, after, tolerance=args.tolerance)

    print(render_markdown(comparisons))
    print()

    aggregate_regressed = any(cmp["regressed"] for cmp in comparisons.values())
    warned = any(cmp.get("warnings") for cmp in comparisons.values())

    displaced = False
    not_certified = False
    if args.no_displacement:
        print(
            "## Evidence displacement\n\n"
            "SKIPPED (--no-displacement) — the aggregate metrics above cannot "
            "detect a dream displacing evidence from top-k."
        )
        warned = True
    else:
        markdown, displaced, dwarnings = displacement_section(
            before, after,
            dataset_path=Path(args.dataset) if args.dataset else None,
            min_lost_questions=args.min_lost_questions,
            lost_question_rate=args.lost_question_rate,
            rank_shift_warn=args.rank_shift_warn,
        )
        not_certified = any(w.startswith(NOT_CERTIFIED_MARKER) for w in dwarnings)
        print("## Evidence displacement\n")
        if markdown:
            # Per-config WARNING lines are already rendered inside the tables,
            # attached to the config they belong to; re-printing the collected
            # list here would attribute every warning to every config. The
            # refusal marker is the exception: it is synthesised OUTSIDE the
            # tables (a refused config is a loud failure and carries no warnings
            # of its own), so without this it would be printed nowhere at all.
            print(markdown)
            for warning in dwarnings:
                if warning.startswith(NOT_CERTIFIED_MARKER):
                    print(f"\nWARNING: {warning}")
        else:
            for warning in dwarnings:
                print(f"WARNING: {warning}")
        warned = warned or bool(dwarnings)
    print()

    # Two independent gates. Naming which one failed is the whole point of
    # running both: an aggregate regression and an evidence-displacement
    # pattern have different causes and different remedies.
    if aggregate_regressed or displaced:
        failed = []
        if aggregate_regressed:
            failed.append("aggregate retrieval metrics regressed")
        if not_certified:
            # Never call this "dreams displaced evidence". The leg did not run;
            # nothing was measured, in either direction.
            failed.append(
                "the displacement gate refused to certify the 'after' leg — it "
                "completed no recalls"
            )
        elif displaced:
            failed.append("dreams displaced evidence from top-k")
        print(
            f"VERDICT: REGRESSION ({'; '.join(failed)}) — dreaming must not "
            "ship enabled (see above)"
        )
        return 1
    # A warning does not fail the gate — every warning below is a shape the
    # comparator could not rule out, not a measured regression — but it must be
    # impossible to read the final line and miss it.
    if warned:
        print(
            "VERDICT: OK (WITH WARNINGS) — no gate metric regressed and no "
            "displacement pattern fired, but the comparison could not be fully "
            "verified; read the WARNING lines above before treating this as a "
            "green gate"
        )
        return 0
    print("VERDICT: OK — no regression beyond tolerance; safe to ship dreaming enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
