"""`get_context`'s token budget must describe the thing it returns.

The bug this file pins (measured 2026-08-21 against the live 938-file index):
``get_context`` reported ``{"tokens_used": 3994, "tokens_budget": 4000,
"budget_utilization": 99.9}`` while returning 261,510 characters — 74,218
tokens, an 18.6x overshoot. The cause is that the budget counted ONLY the
``source`` bytes of each symbol. Every entry also carries an envelope — id,
kind, name, file, line, signature, summary — and on that call the envelope was
93.5% of the payload and was charged to nothing.

The second-order effect is worse than the arithmetic. With no ``focus`` the
candidate list is sorted smallest-symbol-first, on the reasoning that small
symbols are "more informative per token". Under source-only accounting a
15-byte symbol costs 3 tokens and its envelope is free, so the greedy fill
admits as many as it can find: 681 symbols, almost all of them envelope. The
tool optimised for the number of things it returned rather than the amount of
code, which is the opposite of what a context budget is for.

Charging the whole entry fixes both at once, and it should not need a change to
the sort: once a tiny symbol costs its ~90-token envelope instead of ~3 tokens,
smallest-first stops being pathological by itself.
``test_many_tiny_symbols_do_not_flood_the_budget`` is the assertion that says so.

Tolerances below are deliberately loose. The budget is an ESTIMATE — source is
still counted as ``byte_length // 4`` rather than tokenised, and JSON escaping
is not modelled — so these assert "honest to within a stated factor", never
"exact". A budget that is merely approximate is fine; one that is wrong by 18x
is not.
"""

import json

import pytest

from firekeep_symdex.parser import Symbol
from firekeep_symdex.storage import IndexStore
from firekeep_symdex.tools.get_context import get_context


REPO = "testowner/testrepo"

# How far over budget the SERIALIZED result may be. Covers the byte_length//4
# source approximation plus the JSON envelope of the outer result object.
OVERSHOOT_TOLERANCE = 1.35

# How far the reported tokens_used may sit from the real serialized size.
REPORTING_TOLERANCE = 0.35


def _build_index(tmp_path, *, count: int, body_size: int, name_prefix: str = "sym"):
    """An index of `count` symbols, each with `body_size` bytes of real source.

    Offsets are real: the file content is the concatenation of the bodies, so
    `get_symbol_content`'s seek+read returns exactly the intended slice.
    """
    store = IndexStore(base_path=str(tmp_path))
    file_name = "src/generated.py"

    bodies, symbols, offset = [], [], 0
    for i in range(count):
        body = (f"def {name_prefix}_{i}():\n").ljust(body_size, "#") + "\n"
        raw = body.encode()
        symbols.append(
            Symbol(
                id=f"gen::{name_prefix}_{i}",
                file=file_name,
                name=f"{name_prefix}_{i}",
                qualified_name=f"{name_prefix}_{i}",
                kind="function",
                language="python",
                signature=f"def {name_prefix}_{i}():",
                line=i + 1,
                end_line=i + 2,
                byte_offset=offset,
                byte_length=len(raw),
            )
        )
        bodies.append(body)
        offset += len(raw)

    store.save_index(
        owner="testowner",
        name="testrepo",
        source_files=[file_name],
        symbols=symbols,
        raw_files={file_name: "".join(bodies)},
        languages={"python": 1},
        references=[],
    )
    return store


def _serialized_tokens(result: dict) -> int:
    """What the result ACTUALLY costs on the wire, the way server.py sends it."""
    return len(json.dumps(result, separators=(",", ":"), default=str)) // 4


# --------------------------------------------------------------------------- #
# The budget describes the payload                                             #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("budget", [500, 1000, 4000])
def test_serialized_result_respects_the_budget(tmp_path, budget):
    """The real cost of the returned object stays near the stated budget."""
    _build_index(tmp_path, count=400, body_size=120)
    result = get_context(REPO, budget_tokens=budget, storage_path=str(tmp_path))

    actual = _serialized_tokens(result)
    assert actual <= budget * OVERSHOOT_TOLERANCE, (
        f"budget_tokens={budget} but the result serializes to {actual} tokens "
        f"({actual / budget:.1f}x). The budget must count the whole entry, not "
        f"just its source bytes."
    )


@pytest.mark.parametrize("budget", [500, 1000, 4000])
def test_reported_tokens_used_matches_reality(tmp_path, budget):
    """`_meta.tokens_used` must not lie to the agent reading it.

    Before the fix this reported 99.9% utilisation on a payload 18.6x the
    budget — an agent trusting it would keep asking for more.
    """
    _build_index(tmp_path, count=400, body_size=120)
    result = get_context(REPO, budget_tokens=budget, storage_path=str(tmp_path))

    reported = result["_meta"]["tokens_used"]
    actual = _serialized_tokens(result)
    drift = abs(reported - actual) / max(actual, 1)
    assert drift <= REPORTING_TOLERANCE, (
        f"_meta.tokens_used={reported} but the result is really {actual} tokens "
        f"({drift:.0%} off)."
    )


def test_budget_utilization_is_not_a_fiction(tmp_path):
    _build_index(tmp_path, count=400, body_size=120)
    result = get_context(REPO, budget_tokens=2000, storage_path=str(tmp_path))
    assert result["_meta"]["budget_utilization"] <= 110.0


# --------------------------------------------------------------------------- #
# The pathology this was really about                                          #
# --------------------------------------------------------------------------- #

def test_many_tiny_symbols_do_not_flood_the_budget(tmp_path):
    """1,000 near-empty symbols must not all be admitted under a 4k budget.

    This is the 681-symbol case from the live index. Each tiny symbol costs
    almost no SOURCE but a full envelope; if the envelope is free, the greedy
    fill takes every one of them and returns scaffolding instead of code.
    """
    _build_index(tmp_path, count=1000, body_size=8)
    result = get_context(REPO, budget_tokens=4000, storage_path=str(tmp_path))

    included = result["symbols_included"]
    actual = _serialized_tokens(result)
    assert actual <= 4000 * OVERSHOOT_TOLERANCE, (
        f"{included} tiny symbols serialized to {actual} tokens against a "
        f"4,000-token budget."
    )
    assert included < 1000, "every candidate was admitted — the envelope is still free"


def test_a_tighter_budget_returns_strictly_less(tmp_path):
    """Monotonicity — the budget is a control, not a decoration."""
    _build_index(tmp_path, count=400, body_size=120)
    small = get_context(REPO, budget_tokens=400, storage_path=str(tmp_path))
    large = get_context(REPO, budget_tokens=4000, storage_path=str(tmp_path))

    assert small["symbols_included"] < large["symbols_included"]
    assert _serialized_tokens(small) < _serialized_tokens(large)


def test_budget_still_admits_at_least_one_symbol(tmp_path):
    """A budget too small for any entry must not silently return nothing.

    The floor matters: `get_context` returning zero symbols looks to an agent
    like "this repo has no relevant code", which is a false negative, not a
    saving. Charging the envelope made this reachable — a symbol whose source
    alone used to fit exactly no longer does.
    """
    _build_index(tmp_path, count=50, body_size=400)
    result = get_context(REPO, budget_tokens=100, storage_path=str(tmp_path))
    assert result["symbols_included"] >= 1
    assert result["symbols"][0]["source"], "the floor entry must carry real source"


def test_the_floor_discloses_itself(tmp_path):
    """Overshooting the budget is acceptable; hiding it is not.

    An agent that cannot afford the overshoot has to be able to SEE that it
    happened, otherwise the one honest exception to the budget becomes a second
    silent lie in the same field this whole change exists to fix.
    """
    _build_index(tmp_path, count=50, body_size=400)
    result = get_context(REPO, budget_tokens=100, storage_path=str(tmp_path))

    meta = result["_meta"]
    assert meta.get("budget_floor_applied") is True
    assert "note" in meta and "budget_tokens" in meta["note"]


def test_no_floor_flag_when_the_budget_was_actually_respected(tmp_path):
    """The disclosure must not cry wolf on ordinary calls."""
    _build_index(tmp_path, count=400, body_size=120)
    result = get_context(REPO, budget_tokens=4000, storage_path=str(tmp_path))

    assert result["symbols_included"] > 1
    assert "budget_floor_applied" not in result["_meta"]
    assert "note" not in result["_meta"]


# --------------------------------------------------------------------------- #
# Nothing else moved                                                           #
# --------------------------------------------------------------------------- #

def test_entries_keep_the_fields_an_agent_reads(tmp_path):
    _build_index(tmp_path, count=50, body_size=200)
    result = get_context(REPO, budget_tokens=4000, storage_path=str(tmp_path))

    assert result["symbols_included"] >= 1
    entry = result["symbols"][0]
    for field in ("id", "kind", "name", "file", "line", "signature", "source"):
        assert field in entry, f"{field} disappeared from a get_context entry"
    assert entry["source"], "source must not be empty — it is the whole point"


def test_focus_path_is_budgeted_too(tmp_path):
    _build_index(tmp_path, count=400, body_size=120, name_prefix="handler")
    result = get_context(
        REPO, budget_tokens=1000, focus="handler", storage_path=str(tmp_path)
    )
    actual = _serialized_tokens(result)
    assert actual <= 1000 * OVERSHOOT_TOLERANCE, (
        f"focused call serialized to {actual} tokens against a 1,000 budget"
    )
    assert result["symbols_included"] >= 1
