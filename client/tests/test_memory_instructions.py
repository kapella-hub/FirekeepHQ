"""The cognitive protocol must reach every runtime's instruction layer.

The failure this guards
-----------------------
A user asked their agent "deploy to my vps". The agent said it did not know what
the VPS was. The user said "look at your memories"; the agent called
``memory_recall``; the answer was the FIRST result at 100% confidence, complete
with the IP, ssh-as-root and the checkout path.

Storage and retrieval were perfect. **Nothing triggered them.**

The rendered instruction block — the only surface with proven model delivery on any
runtime — contained exactly two sections, decision-board and knowledge-ingest. No
"recall before", no ``memory_learn``, no ``vault_retrieve``. The session-start
briefing does say "then memory_recall", but once, before the agent has anything to
recall against; it cannot look up a VPS before the user has mentioned one.

It stayed invisible because the author's own machine behaved correctly — he had
hand-written those rules into his personal ``CLAUDE.md`` years earlier, so the
product's own instruction layer was never the thing being exercised.

Why these assertions and not others
-----------------------------------
Tool descriptions are deliberately NOT tested as a trigger. ``memory_recall``'s
description already states its trigger correctly and still did not fire; this repo
proved the same for ``decision_board`` in client 0.1.11, which is why
``DECISION_INSTRUCTIONS`` exists at all. Only the instruction layer counts.
"""
from __future__ import annotations

import re

import pytest

from firekeep_client.adapters.base import (
    FIREKEEP_INSTRUCTIONS,
    GATEWAY_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS,
    MEMORY_INSTRUCTIONS,
)


class TestTheRecallTriggerExists:
    """Each assertion names a capability a customer paid for."""

    @pytest.mark.parametrize(
        "tool",
        ["memory_recall", "memory_learn", "vault_retrieve", "vault_store",
         "skill_recall", "skill_create", "ctx_update", "ctx_get_shadow",
         "ctx_complete_session"],
    )
    def test_the_block_names_the_tool(self, tool):
        assert tool in FIREKEEP_INSTRUCTIONS, (
            f"{tool} has no trigger in the rendered instruction layer. A tool the "
            f"agent is never told to call is a tool that does not exist."
        )

    def test_not_knowing_is_stated_as_the_trigger(self):
        """The specific wording that fixes the observed failure. A vague
        'use memory when relevant' has no edge a model can evaluate."""
        low = MEMORY_INSTRUCTIONS.lower()
        assert "before you answer" in low or "before answering" in low, (
            "the recall instruction must fire BEFORE the model answers, not as "
            "general advice"
        )
        assert "i don't know" in low, (
            "the instruction must name the exact failure mode — answering "
            "'I don't know' about the user's own systems without recalling first"
        )

    def test_it_gives_an_observable_test_not_an_exhortation(self):
        """'Can I name this from THIS conversation?' is checkable per turn.
        'Remember to use memory' is not."""
        low = MEMORY_INSTRUCTIONS.lower()
        assert "this conversation" in low, (
            "the trigger must be a test against the current turn's context"
        )

    def test_history_words_are_enumerated(self):
        """The second trigger class: a user referring to past work."""
        low = MEMORY_INSTRUCTIONS.lower()
        found = [w for w in ("again", "still", "last time", "how did we") if w in low]
        assert len(found) >= 3, f"too few history-word triggers enumerated: {found}"

    def test_the_ctx_update_rule_is_countable(self):
        """'As you work' is unfalsifiable, so it gets skipped. A number is not."""
        assert re.search(r"three or more|3\+|three\b", MEMORY_INSTRUCTIONS, re.I), (
            "the ctx_update cadence must be countable, or the model cannot tell "
            "whether it is complying"
        )

    def test_secrets_are_routed_away_from_memory(self):
        """A credential in plain-text memory is a security regression, and the
        vault is the only correct destination."""
        low = MEMORY_INSTRUCTIONS.lower()
        assert "vault_store" in low and "never" in low, (
            "the block must state that secrets go to the vault and NEVER to "
            "memory_learn"
        )


class TestMemoryComesFirst:
    def test_memory_is_the_first_section(self):
        """It governs ordinary turns; the other sections fire on rarer, specific
        situations. Ordering is the cheapest available prioritisation."""
        sections = re.findall(r"^## (.+)$", FIREKEEP_INSTRUCTIONS, re.M)
        assert sections, "no sections found"
        assert "Memory" in sections[0], f"Memory is not first: {sections}"

    def test_the_other_sections_survived(self):
        """A regression that ADDED memory by dropping the decision board would
        pass every test above."""
        sections = re.findall(r"^## (.+)$", FIREKEEP_INSTRUCTIONS, re.M)
        joined = " ".join(sections)
        assert "Decision Board" in joined
        assert "Knowledge Ingest" in joined


class TestEveryRuntimeGetsIt:
    """One rendered block, four runtimes. Codex is the one that had NOTHING."""

    @pytest.mark.parametrize("runtime", ["claude", "kiro", "opencode", "codex"])
    def test_the_adapter_renders_the_instruction_block(self, runtime):
        import inspect

        from firekeep_client.adapters import get_adapter

        adapter = get_adapter(runtime)
        src = inspect.getsource(type(adapter))
        assert "FIREKEEP_INSTRUCTIONS" in src or "_render_instructions" in src, (
            f"the {runtime} adapter renders no instruction block, so a {runtime} "
            f"user gets the MCP tools and no word about when to use them"
        )

    def test_codex_writes_an_agents_file(self):
        """Codex has NO hook surface, so the instruction file is its only channel.
        It previously rendered MCP servers and nothing else, while
        docs/SETUP-CODEX.md described an AGENTS.md that was never written."""
        from firekeep_client.adapters import get_adapter

        adapter = get_adapter("codex")
        path = adapter._instructions_path()
        assert path.name == "AGENTS.md", f"unexpected codex instruction path: {path}"

    def test_codex_render_is_wired_not_merely_defined(self, tmp_path, monkeypatch):
        """A _render_instructions that render() never calls is dead code."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        from firekeep_client.adapters import get_adapter

        get_adapter("codex").render(venv_bin=tmp_path / "vbin")
        agents = tmp_path / ".codex" / "AGENTS.md"
        assert agents.is_file(), "codex render() did not write AGENTS.md"
        text = agents.read_text(encoding="utf-8")
        assert "memory_recall" in text, "the written block carries no recall trigger"


class TestTheMcpHandshakeCarriesItToo:
    """Defence in depth. The server-side `instructions=` needs no adapter, so it
    reaches clients the render path cannot — including a user who deletes the
    block from their own instruction file."""

    def test_the_short_form_states_the_trigger(self):
        low = MCP_SERVER_INSTRUCTIONS.lower()
        assert "memory_recall" in low
        assert "before" in low, "the short form must still state WHEN to recall"

    def test_the_receiving_end_carries_action_before(self):
        """Round-2 Correction 2: f23133a put the action_before paragraph in
        Cortex's FastMCP `_INSTRUCTIONS`, which NO kit runtime ever receives —
        the gateway discards backend `instructions=` during discovery and serves
        GATEWAY_INSTRUCTIONS instead, which carried no action_before paragraph.
        `test_every_server_passes_instructions` below asserts the backends SEND
        instructions; this asserts what an agent actually RECEIVES. Both the
        short form and the gateway handshake it flows into must carry the
        declare-before-acting protocol, or the second delivery channel of the
        armed 0/32 experiment is dead again."""
        for received in (MCP_SERVER_INSTRUCTIONS, GATEWAY_INSTRUCTIONS):
            assert "action_before" in received
            assert "action_after" in received
            assert "confidence" in received, (
                "the calibration half — stated confidence scored against "
                "reality — is what makes the declaration honest"
            )

    def test_gateway_instructions_embed_the_short_form(self):
        """GATEWAY_INSTRUCTIONS is derived from MCP_SERVER_INSTRUCTIONS by
        construction; if that derivation is ever unpicked, the handshake agents
        receive silently loses every protocol line the short form carries."""
        assert MCP_SERVER_INSTRUCTIONS.rstrip() in GATEWAY_INSTRUCTIONS

    def test_it_stays_short(self):
        """Sent once per session, but it competes with everything else in the
        handshake. Long instructions are skimmed."""
        n = len(MCP_SERVER_INSTRUCTIONS.splitlines())
        assert n <= 20, f"MCP handshake instructions grew to {n} lines; keep it tight"

    @pytest.mark.parametrize(
        "module",
        ["cortex/app/mcp_server.py", "bridge/app/mcp_server.py",
         "relay/app/mcp_server.py", "sentinel/app/mcp_server.py"],
    )
    def test_every_server_passes_instructions(self, module):
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        src = (root / module).read_text(encoding="utf-8")
        assert "instructions=" in src, (
            f"{module} constructs FastMCP without instructions=, so its clients "
            f"receive tools with no protocol"
        )
