"""Pure step matching. No I/O, no exceptions — this runs on the blocking
pre-edit path, where a raise costs a customer's edit and a slow call costs the
whole gate (the client's own timeout turns it into a silent skip)."""

from __future__ import annotations

import fnmatch
from typing import Any


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _matches(pattern: str, target: str) -> bool:
    """Glob against the full path and against every path suffix.

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
        parts = t.split("/")
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), p):
                return True
        return False
    except Exception:  # noqa: BLE001 — a hostile pattern must never raise here
        return False


def match_target(index: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    if not target:
        return []
    return [e for e in index if _matches(e.get("pattern", ""), target)]


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
