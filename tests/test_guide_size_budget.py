"""`CLAUDE.md` is a prompt prefix, not documentation. Budget it like one.

WHY THIS EXISTS. The root guide reached **264 KB / ~71,000 tokens** — roughly 35% of a
200k context window consumed before anyone typed anything, on every session, in every
project directory. Working inside `cortex/` loaded `cortex/CLAUDE.md` on top of it, for
~88,000 tokens of instructions before the first tool call.

It got there honestly, and that is the point: the file was so thorough that each agent
added its section in the same register to match, which is a loop that only ever grows.
The largest single section (37 KB, Living Procedures) was written by an agent one hour
before this test. Nothing was wrong with any individual addition; what was missing was a
budget, so "match the surrounding quality" had no counterweight.

WHAT BELONGS IN THE PREFIX. One test: *would an agent do the wrong thing in the next five
minutes without this line?* Architecture, ports, commands, the change-consistency
checklist, "never change the embedding dim without a collection rebuild" — yes. "Here is
what we measured on 2026-08-04 and why we chose 45s" — no. That is a decision record,
it is read on demand, and it lives in `docs/guides/`.

The archaeology is NOT less valuable than the rules; it is the best thing in this
repository. It is simply not worth re-reading on every unrelated task, and moving it out
cost nothing — the split was byte-for-byte lossless.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Bytes, not tokens: bytes are what a test can measure without a tokenizer, and the
# ratio is stable enough (~3.7 chars/token on this prose) for a budget.
BUDGETS = {
    # Loaded in EVERY session in this repository.
    "CLAUDE.md": 32_000,
    # Loaded ON TOP of the above whenever work happens inside cortex/. Set just above
    # its current size: a ratchet that stops further growth. It has not been split yet
    # and should be — see the note in the failure message.
    "cortex/CLAUDE.md": 66_000,
}


@pytest.mark.parametrize("rel,budget", sorted(BUDGETS.items()))
def test_guide_stays_within_its_prompt_budget(rel: str, budget: int) -> None:
    path = REPO / rel
    size = path.stat().st_size
    assert size <= budget, (
        f"{rel} is {size:,} bytes (~{round(size / 3.7):,} tokens), over its "
        f"{budget:,}-byte budget by {size - budget:,}.\n\n"
        "This file is loaded into the prompt of every session, so its size is a tax on "
        "every unrelated task and any edit to it invalidates the cached prefix. Do NOT "
        "raise the budget to make this pass.\n\n"
        "Move the new material to docs/guides/<area>.md and leave a pointer. Keep only "
        "what changes what an agent DOES in its first five minutes: interfaces, "
        "defaults that bite, and the rules that must never be broken. Rationale, "
        "measurements and the history of what failed first belong in the guide, where "
        "they are read by someone working on that area."
    )


def test_every_guide_referenced_from_the_index_exists() -> None:
    """A pointer to a file that is not there is worse than no pointer: the reader
    concludes the detail was deleted rather than moved."""
    guide = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    import re

    refs = set(re.findall(r"docs/guides/([a-z0-9-]+\.md)", guide))
    assert refs, "CLAUDE.md no longer points at docs/guides/ — did the index get dropped?"
    missing = sorted(r for r in refs if not (REPO / "docs" / "guides" / r).exists())
    assert not missing, f"CLAUDE.md points at guides that do not exist: {missing}"


def test_no_guide_is_orphaned() -> None:
    """Every file in docs/guides/ is reachable from the index. An unreferenced guide is
    invisible: nobody browses a directory they were never told about."""
    import re

    guide = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    refs = set(re.findall(r"docs/guides/([a-z0-9-]+\.md)", guide))
    on_disk = {p.name for p in (REPO / "docs" / "guides").glob("*.md")}
    orphans = sorted(on_disk - refs)
    assert not orphans, (
        f"docs/guides/ contains files the root guide never links: {orphans}. "
        "Add them to the Feature guides table."
    )
