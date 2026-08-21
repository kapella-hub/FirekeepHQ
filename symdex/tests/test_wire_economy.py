"""Wire economy: what a symdex tool result costs the model that reads it.

Every tool result is re-sent on every remaining turn of the session, so bytes
that carry no information are not paid once — they are paid once per remaining
turn. Two such classes were measured on the live index (2026-08-21) and are
guarded here:

  * ``json.dumps(..., indent=2)`` inflated every result 19.6-23.5% for zero
    information gain. JSON parses identically either way and no model reads an
    indented object more accurately; the only thing the whitespace bought was
    human debuggability of a raw wire log, which the debug log already gives.
  * the savings counters (``tokens_saved``, ``total_tokens_saved``,
    ``cost_avoided``, ``total_cost_avoided``) rode in the ``_meta`` envelope of
    eight tools at ~93 tokens per call. No decision an agent makes depends on
    the cumulative dollars a tool claims to have saved. The cumulative total is
    still recorded to ``~/.code-index/_savings.json`` — it just stops being
    charged to the model's context window.

Deliberately NOT guarded: ``truncated``, ``total_symbols``/
``total_symbols_available`` and ``timing_ms``. Those DO drive behaviour —
``truncated`` is how an agent learns its result set was cut and it must narrow —
and dropping them would trade tokens for correctness, which is the one trade
this file exists to prevent.
"""

import json
from pathlib import Path

import pytest

from firekeep_symdex import server
from firekeep_symdex.storage import token_tracker
from firekeep_symdex.tools.get_file_tree import get_file_tree
from firekeep_symdex.tools.index_folder import index_folder


# The four fields that must never reach the model again.
SAVINGS_KEYS = frozenset(
    {"tokens_saved", "total_tokens_saved", "cost_avoided", "total_cost_avoided"}
)

# Fields that must SURVIVE — an agent acts on these.
LOAD_BEARING_META = ("timing_ms",)

_FIXTURE = Path(__file__).parent / "fixtures" / "python"


@pytest.fixture
def indexed(tmp_path):
    """A real index over the python fixture, in a throwaway storage root.

    ``use_ai_summaries=False`` keeps it offline and sub-second; the point is to
    exercise a tool that actually builds a ``_meta`` envelope, which
    ``list_repos`` does not.
    """
    result = index_folder(
        str(_FIXTURE), use_ai_summaries=False, storage_path=str(tmp_path)
    )
    repo = result.get("repo")
    assert repo, f"fixture index produced no repo: {result}"
    return repo, str(tmp_path)


def _walk(obj):
    """Yield every dict nested anywhere in a JSON-shaped object."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


async def _call(name: str, **arguments):
    """Invoke a tool through the real server entry point and parse the wire text."""
    parts = await server.call_tool(name, arguments)
    assert len(parts) == 1, f"{name} returned {len(parts)} content parts, expected 1"
    return parts[0].text


@pytest.mark.asyncio
async def test_results_are_serialized_compactly():
    """The wire carries no pretty-printing whitespace.

    ``list_repos`` takes no required arguments and always returns a dict, which
    makes it the cheapest honest probe of the shared serialization funnel in
    ``server.call_tool`` — the one line every tool's result passes through.
    """
    text = await _call("list_repos")

    assert "\n" not in text, (
        "tool result contains newlines — the serialization funnel is still "
        "pretty-printing. Expected json.dumps(..., separators=(',', ':'))."
    )
    assert ": " not in text.replace('": "', '":"'), (
        "tool result contains ', ' / ': ' separators — not compact."
    )
    # Compact is a serialization change ONLY: the parsed object is unchanged.
    assert isinstance(json.loads(text), (dict, list))


@pytest.mark.asyncio
async def test_error_results_are_compact_too():
    """The unknown-tool and exception paths use the same compact serialization."""
    text = await _call("no_such_tool_exists")
    assert "\n" not in text
    assert json.loads(text)["error"].startswith("Unknown tool")


def test_no_savings_counters_in_a_meta_envelope(indexed):
    """A tool that builds a ``_meta`` envelope carries no savings telemetry.

    ``get_file_tree`` is one of the eight tools that merged ``cost_avoided``
    into its envelope, so it is the honest probe — before this change its
    ``_meta`` keys were exactly ``cost_avoided, file_count, timing_ms,
    tokens_saved, total_cost_avoided, total_tokens_saved``.
    """
    repo, storage = indexed
    result = get_file_tree(repo, storage_path=storage)

    for node in _walk(result):
        leaked = SAVINGS_KEYS & set(node)
        assert not leaked, (
            f"savings telemetry reached the model: {sorted(leaked)}. "
            "Record it to ~/.code-index/_savings.json and surface it in "
            "`firekeep doctor`, not in every tool result."
        )

    # The envelope still exists and still carries what an agent acts on.
    meta = result["_meta"]
    for key in LOAD_BEARING_META:
        assert key in meta, f"{key} was dropped — that is a behaviour change, not a saving"
    assert meta["file_count"] >= 1


def test_no_tool_module_imports_the_cost_helper():
    """Structural guard so a NEW tool cannot reintroduce the counters.

    The runtime test above can only cover tools it can cheaply invoke. This one
    covers all of them at once, and it is the check that survives someone adding
    a ninth tool by copying the envelope out of an existing one.
    """
    tools_dir = Path(server.__file__).parent / "tools"
    offenders = {
        path.name: [
            f"{n}: {line.strip()}"
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "cost_avoided" in line
        ]
        for path in sorted(tools_dir.glob("*.py"))
    }
    offenders = {name: hits for name, hits in offenders.items() if hits}
    assert not offenders, (
        "tool modules still reference cost_avoided:\n"
        + "\n".join(f"  {name}: {hits}" for name, hits in offenders.items())
    )


def test_savings_still_accrue_to_disk(tmp_path):
    """Dropping the counters from the wire must not stop the disk total.

    The meter is the product's own evidence that symdex pays for itself; it
    keeps accruing, it just stops being billed to the context window.
    """
    assert token_tracker.get_total_saved(str(tmp_path)) == 0
    total = token_tracker.record_savings(1_000, str(tmp_path))
    assert total == 1_000
    assert token_tracker.get_total_saved(str(tmp_path)) == 1_000

    token_tracker.record_savings(500, str(tmp_path))
    assert token_tracker.get_total_saved(str(tmp_path)) == 1_500


def test_cost_avoided_helper_survives_for_the_doctor():
    """`cost_avoided` stays importable — it moved audiences, it was not deleted.

    It is the right function for `firekeep doctor` to call when a human asks
    what symdex has saved. Deleting it would force the next author to rewrite
    the pricing table from memory.
    """
    out = token_tracker.cost_avoided(1_000_000, 2_000_000)
    assert set(out) == {"cost_avoided", "total_cost_avoided"}
    assert out["cost_avoided"]["claude_opus"] > 0
    assert out["total_cost_avoided"]["claude_opus"] > out["cost_avoided"]["claude_opus"]
