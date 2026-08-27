"""Size ceilings on the text Firekeep puts in every prompt, forever.

`FIREKEEP_INSTRUCTIONS` is rendered into the user's CLAUDE.md / AGENTS.md and
`GATEWAY_INSTRUCTIONS` rides the MCP handshake into the system prompt. Both sit
in the cached prefix of every session on the machine, so a character added here
is not paid once — it is paid on every turn of every session until someone
removes it.

That makes growth here uniquely easy to miss and uniquely expensive. Measured
2026-08-21: the rendered block is 6,440 chars (~1,610 tok), and it had roughly
doubled from 3,084 chars in the preceding 22 days — one section at a time, each
individually reasonable, none of them reviewed as a spend.

This file does NOT argue any current text should be shorter. Every ceiling
below is the measured size plus deliberate headroom, so it passes today and
keeps passing through ordinary editing. Its whole job is to convert the NEXT
large addition from a side effect into a decision: when it fails, the fix is
usually to raise the number in the same commit that adds the section — and to
have looked at the cost while doing it.

Deliberately NOT asserted: that any block is under some "ideal" size. Two of
these blocks exist because of measured failures — base.py records that tool
descriptions alone did not make `decision_board` fire, and that the handshake
channel was added after an armed experiment found the recall trigger silent.
Cutting them to hit a number would trade tokens for the behaviour they were
written to produce, which is the trade this file exists to prevent.
"""
from __future__ import annotations

import pytest

from firekeep_client.adapters import base


# name -> (ceiling_chars, measured at last re-baseline: 2026-08-21, or
# 2026-08-27 for the rows the Communicating section moved)
# Headroom is ~12% on the composed blocks and ~15% on the smaller constants:
# enough for a paragraph of ordinary editing, not enough for a new section.
BUDGETS = {
    "MEMORY_INSTRUCTIONS": (3_800, 3_356),
    # 2026-08-27, the deliberate spend this file exists to force a look at:
    # the team owner asked for fleet-wide conciseness guidance in the client.
    # ~304 tok/turn always-on, accepted — the guidance exists to SHRINK the
    # other side of every exchange, and a response trimmed by ~a paragraph
    # repays it immediately.
    "COMMUNICATION_INSTRUCTIONS": (1_400, 1_215),
    "DECISION_INSTRUCTIONS": (2_050, 1_782),
    "KNOWLEDGE_INGEST_INSTRUCTIONS": (1_500, 1_298),
    "MCP_SERVER_INSTRUCTIONS": (1_450, 1_251),
    "GATEWAY_INSTRUCTIONS": (1_650, 1_440),
    "CHAT_INSTRUCTIONS": (1_250, 1_061),
    # The composed blocks — what actually lands in a user's instruction file.
    "FIREKEEP_INSTRUCTIONS": (8_650, 7_748),
    "GENERIC_INSTRUCTIONS": (8_650, 7_670),
}

# The total always-on Firekeep surface on a default Claude Code install:
# the rendered instruction file block + the MCP handshake text. ~2,300 tok today
# (was ~1,970 before the Communicating section, 2026-08-27).
TOTAL_ALWAYS_ON_CEILING_CHARS = 10_300


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_instruction_block_is_within_budget(name):
    ceiling, measured = BUDGETS[name]
    text = getattr(base, name)
    assert isinstance(text, str), f"{name} is not a string"
    assert len(text) <= ceiling, (
        f"{name} is {len(text)} chars, over its {ceiling}-char ceiling "
        f"(was {measured} on 2026-08-21).\n"
        f"This text is re-sent on EVERY turn of EVERY session. Before raising "
        f"the ceiling, check the addition earns ~{(len(text) - measured) // 4} "
        f"tokens per turn, forever — then raise it in this same commit."
    )


@pytest.mark.parametrize("name", sorted(BUDGETS))
def test_budget_has_not_drifted_far_below_its_ceiling(name):
    """A ceiling far above reality stops being a ratchet.

    If a block shrinks a lot, the recorded baseline is stale and the headroom
    has silently become a licence. Re-baseline rather than leaving slack.
    """
    ceiling, measured = BUDGETS[name]
    text = getattr(base, name)
    assert len(text) >= measured * 0.6, (
        f"{name} shrank from {measured} to {len(text)} chars — re-baseline "
        f"BUDGETS[{name!r}] so the ratchet keeps biting."
    )


def test_total_always_on_surface_is_within_budget():
    """The number that actually matters: everything Firekeep adds to a prompt."""
    total = len(base.FIREKEEP_INSTRUCTIONS) + len(base.GATEWAY_INSTRUCTIONS)
    assert total <= TOTAL_ALWAYS_ON_CEILING_CHARS, (
        f"Firekeep's always-on prompt surface is {total} chars "
        f"(~{total // 4} tok/turn), over the "
        f"{TOTAL_ALWAYS_ON_CEILING_CHARS}-char ceiling."
    )


def test_composed_block_is_the_sum_of_its_parts():
    """Guard the composition, so a budget on the parts bounds the whole.

    Without this, someone could add a fourth section straight into
    FIREKEEP_INSTRUCTIONS and only the composed ceiling would notice.
    """
    parts = (
        base.MEMORY_INSTRUCTIONS,
        base.COMMUNICATION_INSTRUCTIONS,
        base.DECISION_INSTRUCTIONS,
        base.KNOWLEDGE_INGEST_INSTRUCTIONS,
    )
    for part in parts:
        assert part in base.FIREKEEP_INSTRUCTIONS
    joined = sum(len(p) for p in parts)
    overhead = len(base.FIREKEEP_INSTRUCTIONS) - joined
    assert 0 <= overhead <= 400, (
        f"FIREKEEP_INSTRUCTIONS carries {overhead} chars beyond its four known "
        "sections — a new section was added inline. Give it its own constant "
        "and its own budget line."
    )
