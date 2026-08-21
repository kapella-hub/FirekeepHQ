"""A hard backstop against results that cannot fit in any context window.

Measured on the live 938-file index (2026-08-21, AFTER compact serialization):
``export_index`` returns 525,879 tokens — 262.9% of a 200k context window. That
call cannot succeed. It does not make a session more expensive, it ends one, and
it sits in the default 98-tool surface where any agent can reach it.

Two design choices worth stating, because both look wrong from one angle:

REFUSE, DO NOT TRUNCATE. The obvious fix is to cut the payload at N tokens and
send the prefix. That is the more dangerous option here: a truncated index or
file tree makes the agent believe a symbol does not exist, and a false negative
on "does this already exist" defeats the entire purpose of a code index. A
refusal cannot cause that — the agent knows it received nothing and must narrow.
So the ceiling never ships partial data.

A HIGH CEILING, NOT A TIGHT ONE. This is a backstop against unusable results,
not a token optimisation. ``find_dead_code`` (49,454 tok) and
``get_import_graph`` (32,006 tok) are expensive but usable, and refusing them
would be a regression for anyone who wants them. Whether those deserve their own
narrowing is a separate product judgment; this file only guarantees that no
result is ever large enough to destroy the session that asked for it.

The refusal names only narrowing parameters the tool ACTUALLY accepts, derived
from its signature — a suggestion to pass ``path_prefix`` to a tool with no such
parameter would send the agent into a retry loop, which is worse than the
original problem.
"""

import inspect
import json

import pytest

from firekeep_symdex import server


def _big(chars: int) -> dict:
    return {"payload": "x" * chars}


def test_a_normal_result_passes_through_untouched():
    small = {"repo": "a/b", "symbols": [{"name": "f"}], "_meta": {"timing_ms": 1.0}}
    assert json.loads(server._wire(small)) == small


def test_an_oversized_result_is_refused_not_truncated():
    payload = _big(server._max_result_chars() + 1)
    out = json.loads(server._wire(payload, tool_name="export_index"))

    assert "error" in out, "an oversized result must announce itself as an error"
    # The refusal carries NO fragment of the original payload — that is the
    # whole point: partial data is what creates a false negative.
    assert "payload" not in out
    assert "xxxx" not in json.dumps(out)


def test_the_refusal_says_how_big_and_how_big_is_allowed():
    payload = _big(server._max_result_chars() + 1)
    out = json.loads(server._wire(payload, tool_name="export_index"))

    assert out["result_tokens"] > out["max_result_tokens"]
    assert out["tool"] == "export_index"


def test_the_refusal_names_only_real_narrowing_parameters():
    """export_index accepts path_prefix/include_summaries; it has no `focus`."""
    payload = _big(server._max_result_chars() + 1)
    out = json.loads(server._wire(payload, tool_name="export_index"))

    suggested = set(out.get("narrow_with") or [])
    assert suggested, "a refusal with no way forward is just a broken tool"

    params = set(inspect.signature(
        server._TOOLS["export_index"]["handler"]).parameters)
    assert suggested <= params, (
        f"refusal suggested parameters export_index does not accept: "
        f"{sorted(suggested - params)}"
    )
    assert "path_prefix" in suggested


def test_a_tool_with_different_levers_gets_different_advice():
    payload = _big(server._max_result_chars() + 1)
    out = json.loads(server._wire(payload, tool_name="get_import_graph"))
    suggested = set(out.get("narrow_with") or [])
    params = set(inspect.signature(
        server._TOOLS["get_import_graph"]["handler"]).parameters)
    assert suggested <= params
    assert "file_path" in suggested


def test_refusal_is_itself_small_and_compact():
    payload = _big(server._max_result_chars() + 1)
    text = server._wire(payload, tool_name="export_index")
    assert "\n" not in text
    assert len(text) < 1200, "the refusal must not itself be a large result"


def test_ceiling_is_env_tunable(monkeypatch):
    monkeypatch.setenv("FIREKEEP_SYMDEX_MAX_RESULT_TOKENS", "10")
    assert server._max_result_chars() == 40
    out = json.loads(server._wire(_big(100), tool_name="export_index"))
    assert "error" in out

    monkeypatch.setenv("FIREKEEP_SYMDEX_MAX_RESULT_TOKENS", "not-a-number")
    assert server._max_result_chars() == server._DEFAULT_MAX_RESULT_TOKENS * 4


def test_default_ceiling_admits_todays_expensive_but_usable_tools():
    """Guard the OTHER direction — the backstop must not become a tight budget.

    find_dead_code (49,454 tok) and get_import_graph (32,006 tok) measured on
    the live index are expensive and legitimate. If someone lowers the ceiling
    to "save tokens", these start failing and the tool stops working rather than
    costing less.
    """
    assert server._DEFAULT_MAX_RESULT_TOKENS >= 60_000


def test_default_ceiling_actually_catches_the_known_bomb():
    """export_index measured 525,879 tokens on the live index."""
    assert server._DEFAULT_MAX_RESULT_TOKENS < 525_879


@pytest.mark.asyncio
async def test_ceiling_applies_through_the_real_call_path():
    """The guard lives in the funnel every tool result passes through."""
    parts = await server.call_tool("list_repos", {})
    assert len(parts) == 1
    # A normal call is unaffected and still parses.
    assert isinstance(json.loads(parts[0].text), (dict, list))
