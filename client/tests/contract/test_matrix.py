import contextlib
import importlib

import pytest

from firekeep_client.adapters import claude
from firekeep_client.contract import matrix
from firekeep_client.contract.matrix import (
    RUNTIMES,
    capabilities,
    render_matrix,
)

CAPS = {"briefing", "proactive_recall", "presence", "pre_edit_block", "precompact",
        "reconcile", "bypass"}

# Captured at import, before any test swaps the adapter's table out from under it.
REAL_CLAUDE_HOOKS = claude.CLAUDE_HOOKS


@contextlib.contextmanager
def rebuilt_against_adapter_hooks(rows):
    """Rebuild `matrix` against a substitute claude hook table, then put it back.

    `matrix.py` reads `CLAUDE_HOOKS` once, at import, to compute its cell -- so the
    only way to observe whether the cell TRACKS the adapter is to change the adapter
    and re-import. Reload rather than reach into `MATRIX`: it re-runs the real
    module-level expression, which is the thing under test.

    Restoring is not politeness, and the `finally` is load-bearing rather than
    decorative. `importlib.reload` re-executes into the SAME module dict, so a
    reloaded `MATRIX` is what every already-imported reference (this file's
    `capabilities`, included) resolves at call time -- and the swapped
    `claude.CLAUDE_HOOKS` is process-global to every other test file too. Measured
    2026-08-01 by deleting the two restore lines: this file still went 10/10 green
    (the last swap installs a table that happens to agree with reality), while
    `test_session_end.py::test_claude_adapter_wires_sessionend` and
    `test_kit_smoke.py::test_kit_hangs_together` failed downstream. A test that
    passes by wrecking its neighbours is the defect it was written to catch.
    """
    original = claude.CLAUDE_HOOKS
    claude.CLAUDE_HOOKS = rows
    try:
        importlib.reload(matrix)
        yield matrix
    finally:
        claude.CLAUDE_HOOKS = original
        importlib.reload(matrix)


def test_all_runtimes_have_full_capability_set():
    for rt in RUNTIMES:
        assert set(capabilities(rt)) == CAPS


def test_pre_edit_block_degrades_per_runtime():
    # kiro validated against 2.12.1: the fs_write hook fires but kiro does not enforce its
    # exit-2 block, so pre-edit blocking is advisory, not guaranteed (docs/KIRO-VALIDATION.md).
    assert capabilities("claude")["pre_edit_block"] == "guaranteed"
    assert capabilities("kiro")["pre_edit_block"] == "advisory (fires, non-blocking on 2.12.1)"
    assert capabilities("codex")["pre_edit_block"] == "none"
    # opencode VALIDATED live (1.14.22, docs/OPENCODE-VALIDATION.md): the write tool
    # call aborted with the policy engine's block reason and the file was untouched —
    # a hard gate, unlike kiro's advisory fire-only hook.
    assert capabilities("opencode")["pre_edit_block"] == "guaranteed (plugin throw, validated 1.14.22)"


def test_precompact_is_claimed_only_where_the_kit_renders_it():
    """Corrected twice, for the same reason each time: the matrix must never
    overstate what the kit delivers. 2026-07-29 it claimed claude="yes" while
    nothing rendered a PreCompact hook. It is now "hook" for claude because the
    claude adapter renders one and a precompact core exists — and still "none"
    everywhere else, because no other runtime exposes a compaction event at all.
    Those three stay hand-authored on purpose: their value is a fact about the
    RUNTIMES, not about our code, so there is nothing here to derive it from.

    This checks TODAY'S VALUES only. That the claude cell tracks the adapter
    rather than restating it is a separate property, bound by the test below.

    (What reads the matrix: nothing in the kit imports this module at runtime —
    `render_matrix` has no caller outside these tests. Its audience is a human
    reading the file or the fragment, which is exactly why a false cell can sit
    here undetected; correctness here rests on this suite alone.)
    """
    assert capabilities("claude")["precompact"] == "hook"
    for runtime in ("kiro", "codex", "opencode"):
        assert capabilities(runtime)["precompact"] == "none", (
            f"{runtime} claims a precompact capability; that runtime exposes no "
            f"compaction event, so the claim would be false"
        )


def test_precompact_claim_is_derived_from_the_adapter_not_asserted():
    """The structural fix for the failure the test above records twice.

    Both earlier corrections were humans re-typing a constant to match reality.
    `MATRIX["precompact"]["claude"]` is now COMPUTED from claude.py's
    `CLAUDE_HOOKS`, so deleting the `("PreCompact", "precompact", None, 15)` row
    from the adapter degrades the cell to "none" on its own, matrix.py untouched.

    Binding that requires moving the ADAPTER and re-importing the matrix. An
    earlier version of this test instead called `_precompact_claude` directly on a
    row-less table -- which proves the helper computes, not that `MATRIX` calls
    it. Re-hardcoding the cell to "hook" while leaving the helper defined-and-
    unused kept the whole file green: the exact regression the derivation exists
    to prevent, invisible to the test named after it.

    Both directions are checked, so no hardcoded constant survives: a re-typed
    "hook" fails the row-less case, a re-typed "none" fails the row-present case.
    """
    without_precompact = tuple(h for h in REAL_CLAUDE_HOOKS if h[0] != "PreCompact")
    with rebuilt_against_adapter_hooks(without_precompact) as reloaded:
        assert reloaded.MATRIX["precompact"]["claude"] == "none", (
            "the claude cell did not follow the adapter: the hook table it was "
            "built from has no PreCompact row, yet the matrix still claims one. "
            "The cell has been re-hardcoded -- restore the "
            "`_precompact_claude(CLAUDE_HOOKS)` call, which exists precisely so "
            "the matrix cannot claim a hook the kit does not render."
        )

    precompact_only = tuple(h for h in REAL_CLAUDE_HOOKS if h[0] == "PreCompact")
    assert precompact_only, "the claude adapter renders no PreCompact hook at all"
    with rebuilt_against_adapter_hooks(precompact_only) as reloaded:
        assert reloaded.MATRIX["precompact"]["claude"] == "hook"

    # The swap is undone, so the rest of the suite reads the real adapter — and
    # `capabilities` was bound before the reloads, which is the reference that
    # would go stale if restoration were merely assumed.
    assert matrix.MATRIX["precompact"]["claude"] == "hook"
    assert capabilities("claude")["precompact"] == "hook"


def test_presence_hook_for_hook_capable_sidecar_for_mcp_only():
    # T19 decided hooks-for-claude/sidecar-for-mcp-only; T23 then made kiro
    # hook-capable (its adapter wires all 5 hook cores inline, incl.
    # session_start/stop which register/deregister presence) — so kiro is
    # "hook" too. Only codex is truly MCP-only, and nothing auto-starts the
    # sidecar yet, so its value honestly says "(manual today)".
    assert capabilities("claude")["presence"] == "hook"
    assert capabilities("kiro")["presence"] == "hook"
    assert capabilities("codex")["presence"] == "sidecar (manual today)"
    # opencode is hook-capable via the rendered JS plugin bridge (event hook ->
    # session_start/prompt/stop cores), so presence is plugin-owned, not sidecar.
    assert capabilities("opencode")["presence"] == "plugin hooks"


def test_proactive_recall_fires_only_where_the_runtime_delivers_prompt_text():
    """Pushed recall needs the prompt TEXT — no text, nothing to embed.

    Claude Code and kiro both hand `prompt` to their submit hook, so both push on
    every prompt. The other three do not, and their cells name the DIFFERENT
    reasons on purpose: codex and generic are MCP-only (no hook surface at all),
    while opencode is hook-capable and still cannot do this, because its bridge
    maps `session.idle`, an event that carries no prompt. Collapsing those into one
    "none" would tell a reader that opencode support is a wiring job when it is a
    protocol limit — the overstatement this file exists to prevent, inverted.
    """
    assert capabilities("claude")["proactive_recall"] == "per-prompt push"
    assert capabilities("kiro")["proactive_recall"] == "per-prompt push"
    assert capabilities("codex")["proactive_recall"] == "none (no hooks)"
    assert capabilities("opencode")["proactive_recall"] == "none (no prompt text)"
    assert capabilities("generic")["proactive_recall"] == "none (no hooks)"


def test_reconcile_levels():
    assert capabilities("claude")["reconcile"] == "hooks"
    assert capabilities("kiro")["reconcile"] == "kiro pre/post hooks"
    assert capabilities("codex")["reconcile"] == "self-reported"
    assert capabilities("opencode")["reconcile"] == "plugin pre/post hooks"


def test_render_matrix_contains_runtime_and_labels():
    frag = render_matrix("codex")
    assert "codex" in frag
    assert "Guaranteed pre-edit blocking: none" in frag
    assert "Action reconcile (before/after): self-reported" in frag


def test_unknown_runtime_raises():
    with pytest.raises(ValueError):
        capabilities("emacs")
    with pytest.raises(ValueError):
        render_matrix("emacs")


def test_matrix_contains_no_retired_profile_pin_capability():
    for runtime in RUNTIMES:
        assert "profile_pin" not in capabilities(runtime)


def test_bypass_is_claude_only_slash_command():
    # The gate works on every runtime; only claude gets the rendered /personal command.
    assert "/personal command" in capabilities("claude")["bypass"]
    assert "no /personal command" in capabilities("kiro")["bypass"]
    assert "/personal command" not in capabilities("codex")["bypass"]
    assert "no /personal command" in capabilities("opencode")["bypass"]
    assert "no /personal command" in capabilities("generic")["bypass"]


def test_generic_column_is_honestly_degraded():
    """The floor tier: MCP tools and the instruction protocol, nothing that
    rides a hook — because a client we ship no adapter for exposes none. Every
    cell here must UNDERSTATE rather than overstate; this file's whole hazard is
    a cell that claims a capability the kit cannot deliver."""
    caps = capabilities("generic")
    assert caps["briefing"] == "none (MCP only)"
    assert caps["proactive_recall"] == "none (no hooks)"
    assert caps["pre_edit_block"] == "none"
    assert caps["precompact"] == "none"
    assert caps["presence"] == "sidecar (manual today)"
    assert caps["reconcile"] == "self-reported"
    assert "no /personal command" in caps["bypass"]
