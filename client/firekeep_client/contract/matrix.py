"""Per-runtime graceful-degradation matrix (spec §6.5).

Honestly documents, per runtime, what is UNIVERSAL (via sidecar + MCP tools) vs
DEGRADED/DROPPED — no silent pretense that a runtime enforces what it can't.

WHO READS THIS: humans, and only humans. Nothing in the kit imports this module at
runtime — `render_matrix` has no caller outside `client/tests/contract/test_matrix.py`,
no adapter reads the fragment, and `firekeep doctor` does not consult it. That is worth
stating plainly, because it is the whole hazard: a false cell here reaches a teammate
through the file itself and nothing else can contradict it, so the tests are the only
thing standing between a wrong cell and a wrong belief. (Whether a module with no
runtime caller should keep shipping is a separate question, not settled here.)

DEVIATION FROM SPEC §6.5 (documented, not accidental): the spec's literal table lists
"sidecar" for the presence/heartbeat/snapshots/exit row across all three runtimes. Per the
T19 ownership reconciliation (see `firekeep_client.sidecar`'s module docstring), presence
lifecycle is owned by the HOOK CORES wherever the runtime has lifecycle hooks — Claude Code
AND kiro (the kiro adapter wires all five hook cores inline, incl. session_start/stop which
register/deregister presence; T23 landed after T19's decision, making kiro hook-capable).
codex and generic are the MCP-only runtimes: their presence path is the SIDECAR, which
nothing auto-starts yet — such a user must run `firekeep-sidecar` manually until autostart
lands (tracked). Convention per row = delivery mechanism per runtime: claude/kiro="hook",
codex/generic="sidecar (manual today)".

MOSTLY hand-authored, which is a known hazard: a cell that describes the kit can drift
away from the kit and nothing notices (it did -- see `_precompact_claude`). Where a cell's
truth is mechanically checkable against the code it describes, DERIVE it instead of typing
it. Today exactly one cell qualifies; the pattern generalises to any row whose value is a
statement about something this repo renders (e.g. the pre_edit_block matchers, which live
in each adapter's hook table) and deliberately does NOT generalise to rows stating facts
about the runtimes themselves, which have no in-repo source of truth.
"""
from __future__ import annotations

# Import direction: contract -> adapters, and it must stay that way. `adapters.claude`
# imports only `adapters.base`, which is stdlib-only at module level, so this costs one
# small package init and keeps this module import-light. NOTHING under `adapters/` may
# import `contract.matrix` -- that would close the loop into a cycle. If an adapter ever
# needs to render the fragment, move the fact (not the import) rather than adding a lazy
# import here to paper over it.
from firekeep_client.adapters.claude import CLAUDE_HOOKS

# `generic` is any MCP client the kit ships no bespoke adapter for
# (firekeep_client/adapters/generic.py). Its column is the FLOOR: the MCP tools
# and the instruction protocol, and nothing that rides a hook — a client we know
# nothing about exposes no hook surface to wire. It is listed last because it is
# what a runtime degrades TO, not a peer of the four.
RUNTIMES = ("claude", "kiro", "codex", "opencode", "generic")


def _precompact_claude(hooks: tuple[tuple[str, str, str | None, int], ...]) -> str:
    """Derive the precompact/claude cell from the claude adapter's OWN hook table.

    This row is computed, not typed, because a hand-authored one lied: on
    2026-07-29 the matrix claimed `precompact: {"claude": "yes"}` while nothing in
    the kit rendered a PreCompact hook, and the test of the day enforced the false
    claim rather than catching it. Nothing tied the claim to the thing it described,
    so it could drift silently and it did. Now it cannot: delete the
    `("PreCompact", "precompact", None, 15)` row from `CLAUDE_HOOKS` and this cell
    degrades to "none" by itself, with this file untouched.

    Deliberately narrow. The other three runtimes' "none" stays hand-authored,
    because no compaction event exists for them to render -- that is a fact about
    those runtimes, not about our code, and there is nothing here to derive it from.
    """
    return "hook" if any(event == "PreCompact" for event, *_rest in hooks) else "none"

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
                 "opencode": "plugin (first event, console log only)",
                 "generic": "none (MCP only)"},
    "presence": {"claude": "hook", "kiro": "hook", "codex": "sidecar (manual today)",
                 "opencode": "plugin hooks", "generic": "sidecar (manual today)"},
    # kiro (validated 2.12.1): the fs_write pre-edit hook FIRES (the agent-gateway before-call
    # runs + records), but kiro does not enforce its own exit-2 block — so it is advisory, not
    # a hard gate. See firekeep_client/adapters/kiro.py + docs/KIRO-VALIDATION.md.
    "pre_edit_block": {"claude": "guaranteed", "kiro": "advisory (fires, non-blocking on 2.12.1)", "codex": "none",
                       "opencode": "guaranteed (plugin throw, validated 1.14.22)",
                       "generic": "none"},
    # Only Claude exposes a compaction event; the other three runtimes have no
    # such lifecycle hook to wire, so this degrades honestly rather than silently.
    # The claude cell is DERIVED from the adapter that renders the hook (see
    # _precompact_claude) so it cannot claim a hook the kit does not render; the
    # other three are hand-authored because there is nothing to derive them from.
    "precompact": {"claude": _precompact_claude(CLAUDE_HOOKS), "kiro": "none",
                   "codex": "none", "opencode": "none", "generic": "none"},
    "reconcile": {"claude": "hooks", "kiro": "kiro pre/post hooks", "codex": "self-reported",
                  "opencode": "plugin pre/post hooks", "generic": "self-reported"},
    # Personal / bypass mode: the is_bypassed() gate (marker + FIREKEEP_BYPASS) works on
    # every runtime; only the /personal slash command is claude-specific. kiro/codex
    # toggle via the `firekeep personal` CLI (or `! firekeep personal`), or FIREKEEP_BYPASS at launch.
    "bypass": {
        "claude": "/personal command + firekeep personal CLI + FIREKEEP_BYPASS",
        "kiro": "firekeep personal CLI + FIREKEEP_BYPASS (no /personal command)",
        "codex": "firekeep personal CLI + FIREKEEP_BYPASS (sidecar honors the gate)",
        "opencode": "firekeep personal CLI + FIREKEEP_BYPASS (no /personal command)",
        "generic": "firekeep personal CLI + FIREKEEP_BYPASS (no /personal command)",
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
