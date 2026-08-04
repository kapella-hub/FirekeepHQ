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
and `overall`) and decides whether "after" regressed relative to "before".
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


def compare_runs(before: dict, after: dict, *, tolerance: float = 0.005) -> dict:
    """Pure. Returns `{"deltas": {metric: after-before}, "regressed": bool,
    "verdict": str}`.

    Defensive by construction, not by inspection — every doubtful shape
    fails LOUD (`regressed=True`, empty `deltas`, a `verdict` that says
    exactly what made the runs incomparable) rather than computing a
    number that looks like a real delta:

    - a missing/malformed `overall` on either side,
    - a `k` mismatch between the two runs (they measured different top-k
      cuts — a delta between them is not meaningful),
    - none of the three gated metrics present in both `overall` dicts.

    A single metric missing from one side (but the others present) is not
    fatal — it is simply omitted from `deltas` and the gate runs on
    whatever metrics both sides actually reported.
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
        }

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

    return {"deltas": deltas, "regressed": regressed, "verdict": verdict}


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
    print("VERDICT: OK — no regression beyond tolerance; safe to ship dreaming enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
