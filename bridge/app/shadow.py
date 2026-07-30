"""Shadow context assembly — generates the Markdown document."""

from __future__ import annotations

from typing import Any


def assemble_shadow(data: dict[str, Any], *, omitted: dict[str, Any] | None = None) -> str:
    """Assemble a shadow context Markdown document from session data.

    Section order is fixed: Plan, Decisions, Files Known, Progress, Scratchpad.

    `omitted` is filter_since's omission report. When present, a section whose entries
    were withheld renders a line SAYING SO instead of the "none recorded" placeholder.
    A delta must never let a reader conclude the omitted content does not exist — that
    inference is the degradation, not the omission.
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
        lines.append(plan)
    elif omitted and omitted.get("plan"):
        lines.append(
            "*Plan unchanged - delivered earlier in this conversation. "
            "Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No plan set*")
    lines.append("")

    # Decisions
    lines.append("### Decisions")
    decisions = data.get("decisions", [])
    if decisions:
        for d in decisions:
            ts = d.get("timestamp", "")
            time_part = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"- [{time_part}] {d.get('content', '')}")
    elif omitted and omitted.get("decisions"):
        lines.append(
            f"*{omitted['decisions']} earlier decision(s) omitted - delivered earlier in "
            "this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No decisions recorded*")
    lines.append("")

    # Files Known
    lines.append("### Files Known")
    files = data.get("files", {})
    if files:
        for path, info in sorted(files.items()):
            summary = info.get("summary", "") if isinstance(info, dict) else str(info)
            lines.append(f"- **{path}** — {summary}")
    elif omitted and omitted.get("files"):
        lines.append(
            f"*{omitted['files']} earlier file(s) omitted - delivered earlier in "
            "this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No files tracked*")
    lines.append("")

    # Progress
    lines.append("### Progress")
    progress = data.get("progress", [])
    if progress:
        for p in progress:
            ts = p.get("timestamp", "")
            time_part = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"- [{time_part}] {p.get('content', '')}")
    elif omitted and omitted.get("progress"):
        lines.append(
            f"*{omitted['progress']} earlier progress entry(s) omitted - delivered earlier "
            "in this conversation. Call ctx_get_shadow() with no arguments for the full document.*"
        )
    else:
        lines.append("*No progress logged*")
    lines.append("")

    # Scratchpad
    lines.append("### Scratchpad")
    scratch = data.get("scratch", {})
    if scratch:
        for k, v in sorted(scratch.items()):
            lines.append(f"- {k}: {v}")
    else:
        lines.append("*Empty*")

    # Relevant Past Experience (proactive recall)
    proactive = data.get("proactive_memories", [])
    if proactive:
        lines.append("")
        lines.append("### Relevant Past Experience")
        for m in proactive:
            score = m.get("score", 0)
            content = m.get("content", "")
            lines.append(f"- [{score:.2f}] {content}")

    return "\n".join(lines)
