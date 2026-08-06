"""Pure step matching. No I/O, no exceptions — this runs on the blocking
pre-edit path, where a raise costs a customer's edit and a slow call costs the
whole gate (the client's own timeout turns it into a silent skip)."""

from __future__ import annotations

import fnmatch
from typing import Any


# How many trailing path segments a repo-relative pattern is allowed to reach
# back over. Rebuilding EVERY suffix was O(len(target)^2) per index entry, and
# `Action.target` carries no max_length (its sibling `preview` is capped at
# 2048) — measured, a 20k-segment target cost 2.75s for ONE entry, on the
# blocking pre-edit path, from any caller holding an ordinary key. Suffixes are
# tried SHORTEST-FIRST because that is what an authored pattern matches; a
# pattern that needs to reach further than this is not repo-relative, and
# `_matches`' full-path fnmatch already covers a `*`-leading glob (fnmatch's `*`
# crosses `/`, so a suffix match by such a pattern implies a full-path match).
_MAX_SUFFIX_SEGMENTS = 16


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _matches(pattern: str, target: str, parts: list[str] | None = None) -> bool:
    """Glob against the full path and against a bounded set of path suffixes.

    Suffix matching is what lets a repo-relative pattern authored by a human
    match the absolute path a tool actually reports.
    """
    try:
        p = _norm(pattern)
        t = _norm(target)
        if not p or not t:
            return False
        if fnmatch.fnmatch(t, p):
            return True
        if parts is None:
            parts = t.split("/")
        stop = max(1, len(parts) - _MAX_SUFFIX_SEGMENTS)
        for i in range(len(parts) - 1, stop - 1, -1):
            if fnmatch.fnmatch("/".join(parts[i:]), p):
                return True
        return False
    except Exception:  # noqa: BLE001 — a hostile pattern must never raise here
        return False


def match_target(index: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    if not target:
        return []
    # Split once, not once per index entry: the index admits up to
    # PROCEDURE_MAX_SPECS entries per skill across every active skill.
    try:
        parts = _norm(target).split("/")
    except Exception:  # noqa: BLE001
        return []
    return [e for e in index if _matches(e.get("pattern", ""), target, parts)]


def missing_load_bearing(
    index: list[dict[str, Any]], skill_id: str, matched_order: int,
    observed_step_ids: set[str],
) -> list[dict[str, Any]]:
    """Load-bearing steps of THIS skill, earlier in the spec list than the one
    just matched, with no observation in this execution."""
    return [
        e for e in index
        if e.get("skill_id") == skill_id
        and e.get("load_bearing")
        and e.get("order", 0) < matched_order
        and e.get("step_id") not in observed_step_ids
    ]


def advisory_text(entry: dict[str, Any], missing: dict[str, Any],
                  stats: dict[str, Any] | None) -> str:
    """One pre-formatted line. The client joins only `message` and flattens
    advisories with '; ' (pre_tool.py), so anything a human needs must be here.

    Numbers are quoted ONLY when the hardening pass has earned them. With no
    stats the message says what is missing and invents nothing.
    """
    trigger = (entry.get("skill_trigger") or "this procedure").strip()
    step_text = (missing.get("step_text") or missing.get("step_id") or "an earlier step")
    base = (f"Procedure \"{trigger}\" — step \"{step_text}\" has no evidence "
            f"in this session.")
    row = (stats or {}).get(missing.get("step_id")) or {}
    observed = row.get("observed")
    executions = row.get("executions")
    if isinstance(observed, int) and isinstance(executions, int) and executions:
        base += f" Present in {observed} of {executions} recorded executions."
    return base
