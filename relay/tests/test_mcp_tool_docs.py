"""Guard: no MCP tool description ships a truncated sentence.

A tool description is the product surface an agent reads before deciding whether
and how to call the tool. A sentence that stops mid-clause doesn't just waste
tokens — it deletes the explanation the agent needed, and the agent cannot tell
that anything is missing.

The check: inside a docstring paragraph, a line that ends without terminal
punctuation followed by a line starting a new capitalized sentence means the
first sentence was truncated. Wrapped sentences continue in lowercase, so they
do not trip it.
"""

from __future__ import annotations

import inspect

from app import mcp_server

_SENTENCE_ENDINGS = ".!?:,)\"`'"


def _dangling_fragments(doc: str | None) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    lines = [ln.rstrip() for ln in (doc or "").split("\n")]
    for first, second in zip(lines, lines[1:]):
        a, b = first.strip(), second.strip()
        if not a or not b:
            continue
        if a.startswith("Args") or a.endswith(":"):
            continue  # section headers and arg labels are not sentences
        if a[-1] in _SENTENCE_ENDINGS:
            continue
        if b[:1].isupper():
            hits.append((a, b))
    return hits


def _tools() -> list[tuple[str, object]]:
    return [
        (name, obj)
        for name, obj in sorted(vars(mcp_server).items())
        if inspect.iscoroutinefunction(obj) and not name.startswith("_")
    ]


def test_no_tool_description_ends_a_sentence_mid_clause():
    broken = {
        name: _dangling_fragments(obj.__doc__)
        for name, obj in _tools()
        if _dangling_fragments(obj.__doc__)
    }
    assert not broken, (
        "Truncated sentence(s) in MCP tool description(s): "
        + "; ".join(f"{n}: {frag[0][0]!r} -> {frag[0][1]!r}" for n, frag in broken.items())
    )


def test_relay_register_drops_the_orphan_noun_phrase_and_keeps_every_fact():
    """The specific defect this guard was written for. 'The presence entry' was
    an orphan noun phrase with no predicate — it stated nothing, so removing it
    costs the agent no information. Every surrounding fact must survive: this is
    a deletion of three words that said nothing, not a trim of the description."""
    doc = mcp_server.relay_register.__doc__ or ""
    assert "The presence entry" not in doc
    for surviving_fact in (
        "Register this agent as online",
        "Call this at session start",
        "heartbeat within 10 minutes",
        "there is no auto-expiry",
    ):
        assert surviving_fact in doc, f"lost information: {surviving_fact!r}"
