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
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The three metrics the regression gate watches, per the design brief.
# `mrr` is still reported in `deltas` for visibility but deliberately does
# NOT gate — the brief specifies Recall@k / Coverage@k / NDCG@k only.
_GATE_METRICS = ("recall_at_k", "coverage_at_k", "ndcg_at_k")
_ALL_METRICS = ("recall_at_k", "coverage_at_k", "mrr", "ndcg_at_k")


_CUMULATIVE_KEY = "completed"
_LAST_INVOCATION_KEY = "completed_last_invocation"


def _as_count(value) -> int | None:
    """An int that is not a bool, else `None`. `True` is an `int` in Python and
    would otherwise read as `completed=1`."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _recall_counts(run: dict) -> dict:
    """The run's `recall_counts` block, or `{}`. Never raises.

    Sibling of `_completed_recalls`, which reads the ONE number that gates.
    This reads the rest of the block so a failure can name its own shape:
    `completed == 0` has causes with opposite remedies (all-skipped is a resume
    or run-label problem; all-errored is a backend problem) and the block
    already distinguishes them.
    """
    counts = run.get("recall_counts") if isinstance(run, dict) else None
    return counts if isinstance(counts, dict) else {}


def _completed_recalls(run: dict) -> tuple[int | None, bool]:
    """`(completed, per_invocation)` — how many recalls the gate should read.

    `completed` is `None` when the run record does not say (a record produced
    before `recall_counts` was written, or a bare
    `work/<label>/scores_<config>.json`). `None` and `0` must never collapse
    into each other: `0` is a positive statement that nothing was recalled,
    while `None` is an absence of evidence — treating the latter as the former
    would make the comparator unable to read its own published history.

    `per_invocation` says which figure it is. `completed_last_invocation` is
    preferred and is the only one that can actually gate: `completed`
    accumulates over every invocation of a run label, so a leg re-run under
    the SAME label recalls nothing yet still reports the first invocation's
    large total. When only the cumulative figure exists (a record written
    before it was recorded) it is still read — `0` is unambiguous either way —
    but the caller downgrades to a warning, because a same-label no-op re-run
    is indistinguishable from a single honest run in that record.
    """
    counts = run.get("recall_counts") if isinstance(run, dict) else None
    if not isinstance(counts, dict):
        return None, False
    last = _as_count(counts.get(_LAST_INVOCATION_KEY))
    if last is not None:
        return last, True
    return _as_count(counts.get(_CUMULATIVE_KEY)), False


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
        whose = (
            "the 'after' run's final invocation recorded completed=0 recalls"
            if per_invocation else
            "the 'after' run recorded completed=0 recalls"
        )
        # Name the SHAPE of the zero, do not assume it. completed=0 has at least
        # two causes with opposite remedies, and the counts entry already
        # distinguishes them at no cost:
        #   skipped>0, errored=0 -> resume/label hygiene (the defect this gate
        #     was built for: an "after" leg that re-scored an earlier run's rows)
        #   errored>0            -> the recalls RAN and FAILED; the stack was
        #     down or unreachable, and nothing was skipped or re-scored
        # Asserting the first unconditionally sends an operator whose Cortex was
        # down to go and audit their run labels. A diagnosis that is wrong in a
        # legible way is worse than no diagnosis, because it is actionable.
        counts = _recall_counts(after)
        errored = counts.get("errored") if isinstance(counts, dict) else None
        skipped = counts.get("skipped") if isinstance(counts, dict) else None
        if isinstance(errored, int) and not isinstance(errored, bool) and errored > 0:
            cause = (
                f"{errored} recall(s) ERRORED and none completed — the queries "
                "ran and failed, so this is a backend/connectivity problem, not "
                "a resume or run-label one"
            )
        elif isinstance(skipped, int) and not isinstance(skipped, bool) and skipped > 0:
            cause = (
                f"all {skipped} question(s) were already on disk and skipped — "
                "its scores are a re-score of artefacts from an earlier run, "
                "not a measurement of this one"
            )
        else:
            cause = (
                "it executed no recalls, and the record does not say whether "
                "they were skipped or errored"
            )
        return {
            "deltas": {},
            "regressed": True,
            "verdict": f"ERROR: not comparable — {whose} ({cause})",
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


def _build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bench.dream_ab")
    ap.add_argument("--before", required=True, help="path to the 'before' results/*.json")
    ap.add_argument("--after", required=True, help="path to the 'after' results/*.json")
    ap.add_argument("--tolerance", type=float, default=0.005)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)

    before = _load_json(args.before)
    after = _load_json(args.after)
    comparisons = compare_result_files(before, after, tolerance=args.tolerance)

    print(render_markdown(comparisons))
    print()

    regressed = any(cmp["regressed"] for cmp in comparisons.values())
    if regressed:
        print("VERDICT: REGRESSION — dreaming must not ship enabled (see above)")
        return 1
    # A warning does not fail the gate — every warning below is a shape the
    # comparator could not rule out, not a measured regression — but it must be
    # impossible to read the final line and miss it.
    if any(cmp.get("warnings") for cmp in comparisons.values()):
        print(
            "VERDICT: OK (WITH WARNINGS) — no gate metric regressed, but the "
            "comparison could not be fully verified; read the WARNING lines "
            "above before treating this as a green gate"
        )
        return 0
    print("VERDICT: OK — no regression beyond tolerance; safe to ship dreaming enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
