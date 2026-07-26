"""Shadow context assembly — generates the Markdown document."""

from __future__ import annotations

from typing import Any


def assemble_shadow(data: dict[str, Any]) -> str:
    """Assemble a shadow context Markdown document from session data.

    Section order is fixed: Plan, Decisions, Files Known, Progress, Scratchpad.
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
    lines.append(plan if plan else "*No plan set*")
    lines.append("")

    # Decisions
    lines.append("### Decisions")
    decisions = data.get("decisions", [])
    if decisions:
        for d in decisions:
            ts = d.get("timestamp", "")
            time_part = ts[11:16] if len(ts) > 16 else ts
            lines.append(f"- [{time_part}] {d.get('content', '')}")
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
