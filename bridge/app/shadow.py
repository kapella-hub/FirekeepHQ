"""Shadow context assembly — generates the Markdown document."""

from __future__ import annotations

from typing import Any


def _rows(value: Any) -> list[Any]:
    """A list-shaped section, defensively.

    A container that is not a list is rendered as ONE literal row rather than
    iterated: iterating a dict yields its KEYS and silently discards every value,
    which is precisely the omission this module exists to prevent. See the module
    note in `assemble_shadow` for why totality matters here at all.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _stamped_line(entry: Any) -> str:
    """One `- [HH:MM] content` row for a decisions/progress entry.

    An entry that is not the `{timestamp, content}` shape is rendered literally
    instead of being dropped or crashed on.
    """
    if not isinstance(entry, dict):
        return f"- {entry!r}"
    ts = entry.get("timestamp") or ""
    if not isinstance(ts, str):
        ts = str(ts)
    time_part = ts[11:16] if len(ts) > 16 else ts
    return f"- [{time_part}] {entry.get('content', '')}"


def _sorted_pairs(mapping: dict[Any, Any]) -> list[tuple[Any, Any]]:
    """Mapping items in key order, tolerating keys that do not compare.

    `sorted(m.items())` raises TypeError on mixed key types. Keying on `str(k)` is
    byte-identical for the all-string keys Redis actually produces (dict keys are
    unique, so the value half of the tuple is never reached as a tiebreak).
    """
    return sorted(mapping.items(), key=lambda kv: str(kv[0]))


def assemble_shadow(data: dict[str, Any], *, omitted: dict[str, Any] | None = None) -> str:
    """Assemble a shadow context Markdown document from session data.

    Section order is fixed: Plan, Decisions, Files Known, Progress, Scratchpad.

    `omitted` is filter_since's omission report. When present, a section whose entries
    were withheld renders a line SAYING SO instead of the "none recorded" placeholder.
    A delta must never let a reader conclude the omitted content does not exist — that
    inference is the degradation, not the omission.

    THIS FUNCTION IS TOTAL (M1). It is the floor of the post-compaction lifeline:
    `ctx_get_shadow` answers every doubtful path with `assemble_shadow(data)` — the
    full, unfiltered document — so a renderer that can itself raise is not a floor at
    all, and the guarantee would bottom out in a traceback handed to an agent that has
    just lost its working state. No arrangement of JSON values — which is the whole
    domain, since `get_session_data` builds `data` out of `json.loads` — may turn it
    into an exception. Totality is bought without silence: an unrecognised container
    or entry is rendered literally, never skipped, because rendering nothing is the
    affirmative denial ("*No decisions recorded*") the omission machinery below
    exists to avoid.
    """
    lines: list[str] = []

    # Header
    lines.append(f"## Session: {data.get('goal', 'unknown')}")
    lines.append(
        f"**Status**: {data.get('status', 'unknown')} | "
        f"**Started**: {data.get('created_at', '')} | "
        f"**Updated**: {data.get('updated_at', '')}"
    )
    lines.append("")

    # Plan
    lines.append("### Plan")
    plan = data.get("plan", "")
    if plan:
        lines.append(plan if isinstance(plan, str) else str(plan))
    elif omitted and omitted.get("plan"):
        lines.append(
            "*Plan unchanged - delivered earlier in this conversation. "
            "Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No plan set*")
    lines.append("")

    # Decisions
    # Four states: has entries / has omissions are independent axes -- a delta that
    # KEPT some entries but withheld others must show both, not just the kept ones
    # (an `elif` here would silently swallow the disclosure whenever anything survived
    # the high-water filter, which is the common case: a fully-empty section only
    # happens when nothing changed at all since the cursor).
    lines.append("### Decisions")
    decisions = _rows(data.get("decisions", []))
    if decisions:
        for d in decisions:
            lines.append(_stamped_line(d))
    if omitted and omitted.get("decisions"):
        lines.append(
            f"*{omitted['decisions']} earlier decision(s) omitted - delivered earlier in "
            "this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    elif not decisions:
        lines.append("*No decisions recorded*")
    lines.append("")

    # Files Known
    lines.append("### Files Known")
    files = data.get("files", {})
    if isinstance(files, dict):
        for path, info in _sorted_pairs(files):
            summary = info.get("summary", "") if isinstance(info, dict) else str(info)
            lines.append(f"- **{path}** — {summary}")
    elif files:
        lines.append(f"- {files!r}")
    if omitted and omitted.get("files"):
        lines.append(
            f"*{omitted['files']} earlier file(s) omitted - delivered earlier in "
            "this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    elif not files:
        lines.append("*No files tracked*")
    lines.append("")

    # Progress
    lines.append("### Progress")
    progress = _rows(data.get("progress", []))
    if progress:
        for p in progress:
            lines.append(_stamped_line(p))
    if omitted and omitted.get("progress"):
        lines.append(
            f"*{omitted['progress']} earlier progress entry(s) omitted - delivered earlier "
            "in this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    elif not progress:
        lines.append("*No progress logged*")
    lines.append("")

    # Scratchpad
    lines.append("### Scratchpad")
    scratch = data.get("scratch", {})
    if isinstance(scratch, dict) and scratch:
        for k, v in _sorted_pairs(scratch):
            lines.append(f"- {k}: {v}")
    elif scratch:
        lines.append(f"- {scratch!r}")
    else:
        lines.append("*Empty*")

    # Relevant Past Experience (proactive recall)
    proactive = _rows(data.get("proactive_memories", []))
    if proactive:
        lines.append("")
        lines.append("### Relevant Past Experience")
        for m in proactive:
            if not isinstance(m, dict):
                lines.append(f"- {m!r}")
                continue
            score = m.get("score", 0)
            content = m.get("content", "")
            try:
                score_text = f"{score:.2f}"
            except (TypeError, ValueError):
                score_text = str(score)
            lines.append(f"- [{score_text}] {content}")

    return "\n".join(lines)
