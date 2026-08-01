"""Per-runtime graceful-degradation matrix (spec §6.5).

Honestly documents, per runtime, what is UNIVERSAL (via sidecar + MCP tools) vs
DEGRADED/DROPPED — no silent pretense that a runtime enforces what it can't. Each adapter
references the rendered fragment so a teammate sees exactly what their runtime guarantees.

DEVIATION FROM SPEC §6.5 (documented, not accidental): the spec's literal table lists
"sidecar" for the presence/heartbeat/snapshots/exit row across all three runtimes. Per the
T19 ownership reconciliation (see `firekeep_client.sidecar`'s module docstring), presence
lifecycle is owned by the HOOK CORES wherever the runtime has lifecycle hooks — Claude Code
AND kiro (the kiro adapter wires all five hook cores inline, incl. session_start/stop which
register/deregister presence; T23 landed after T19's decision, making kiro hook-capable).
Only codex is truly MCP-only: its presence path is the SIDECAR, which nothing auto-starts
yet — a codex user must run `firekeep-sidecar` manually until autostart lands (tracked).
Convention per row = delivery mechanism per runtime: claude/kiro="hook",
codex="sidecar (manual today)".
"""
from __future__ import annotations

RUNTIMES = ("claude", "kiro", "codex", "opencode")

# capability -> {runtime: level}
# opencode rows: hook-capable via the rendered JS plugin bridge
# (firekeep_client/adapters/opencode.py). VALIDATED live on opencode 1.14.22
# (2026-07-18, docs/OPENCODE-VALIDATION.md): pre-edit block is a HARD gate (the
# write tool aborted with the policy reason), prompt-core inbox surfaced, stop
# fired on session.deleted. Caveats: session.created publishes before plugins
# subscribe in `run` mode (bridge fires session_start from its first hook
# instead), and briefing/inbox text lands in opencode's console log, NOT the
# model context — opencode has no systemMessage channel.
MATRIX: dict[str, dict[str, str]] = {
    "briefing": {"claude": "hook", "kiro": "agentSpawn hook", "codex": "manual/memory_recall",
                 "opencode": "plugin (first event, console log only)"},
    "presence": {"claude": "hook", "kiro": "hook", "codex": "sidecar (manual today)",
                 "opencode": "plugin hooks"},
    # kiro (validated 2.12.1): the fs_write pre-edit hook FIRES (the agent-gateway before-call
    # runs + records), but kiro does not enforce its own exit-2 block — so it is advisory, not
    # a hard gate. See firekeep_client/adapters/kiro.py + docs/KIRO-VALIDATION.md.
    "pre_edit_block": {"claude": "guaranteed", "kiro": "advisory (fires, non-blocking on 2.12.1)", "codex": "none",
                       "opencode": "guaranteed (plugin throw, validated 1.14.22)"},
    # Only Claude exposes a compaction event; the other three runtimes have no
    # such lifecycle hook to wire, so this degrades honestly rather than silently.
    "precompact": {"claude": "hook", "kiro": "none", "codex": "none", "opencode": "none"},
    "reconcile": {"claude": "hooks", "kiro": "kiro pre/post hooks", "codex": "self-reported",
                  "opencode": "plugin pre/post hooks"},
    # Personal / bypass mode: the is_bypassed() gate (marker + FIREKEEP_BYPASS) works on
    # every runtime; only the /personal slash command is claude-specific. kiro/codex
    # toggle via the `firekeep personal` CLI (or `! firekeep personal`), or FIREKEEP_BYPASS at launch.
    "bypass": {
        "claude": "/personal command + firekeep personal CLI + FIREKEEP_BYPASS",
        "kiro": "firekeep personal CLI + FIREKEEP_BYPASS (no /personal command)",
        "codex": "firekeep personal CLI + FIREKEEP_BYPASS (sidecar honors the gate)",
        "opencode": "firekeep personal CLI + FIREKEEP_BYPASS (no /personal command)",
    },
}

LABELS = {
    "briefing": "Pre-flight briefing (GET /briefing)",
    "presence": "Presence / heartbeat / snapshots / exit",
    "pre_edit_block": "Guaranteed pre-edit blocking",
    "precompact": "PreCompact save",
    "reconcile": "Action reconcile (before/after)",
    "bypass": "Personal / bypass mode",
}


def capabilities(runtime: str) -> dict[str, str]:
    if runtime not in RUNTIMES:
        raise ValueError(f"unknown runtime: {runtime!r} (expected {'|'.join(RUNTIMES)})")
    return {cap: levels[runtime] for cap, levels in MATRIX.items()}


def render_matrix(runtime: str) -> str:
    caps = capabilities(runtime)
    lines = [f"# Firekeep capability contract — {runtime}", ""]
    for cap, level in caps.items():
        lines.append(f"- {LABELS[cap]}: {level}")
    return "\n".join(lines) + "\n"
