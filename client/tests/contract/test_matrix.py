import pytest

from firekeep_client.contract.matrix import RUNTIMES, capabilities, render_matrix

CAPS = {"briefing", "presence", "pre_edit_block", "precompact", "reconcile", "bypass"}


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
    overstate coverage, because `firekeep doctor` and the docs read from it.
    2026-07-29 it claimed claude="yes" while nothing rendered a PreCompact hook.
    It is now "hook" for claude because the claude adapter renders one and a
    precompact core exists — and still "none" everywhere else, because no other
    runtime exposes a compaction event at all."""
    assert capabilities("claude")["precompact"] == "hook"
    for runtime in ("kiro", "codex", "opencode"):
        assert capabilities(runtime)["precompact"] == "none", (
            f"{runtime} claims a precompact capability; that runtime exposes no "
            f"compaction event, so the claim would be false"
        )


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
