"""Nothing leaves the machine unless the operator asked for it.

`token_tracker` holds the only outbound network call in this package: a
fire-and-forget POST of a savings delta to a community counter. Its docstring
used to state the opposite of the code — "shared anonymously by default",
disabled with `JCODEMUNCH_SHARE_SAVINGS=0` — while `record_savings` has only
ever fired on `FIREKEEP_SYMDEX_SHARE_STATS == "1"`. The behaviour was the safe
one; the documentation named a default and an environment variable that do not
exist, in software that is sold.

That is a worse failure than a wrong default would be. A reader auditing what
this package sends had exactly one place to look and it told them the wrong
thing, in the more alarming direction. These tests pin BOTH the behaviour and
the documentation, so the two cannot drift apart again silently.
"""

import pathlib

import pytest

from firekeep_symdex.storage import token_tracker


ENV_FLAG = "FIREKEEP_SYMDEX_SHARE_STATS"


@pytest.fixture
def captured(monkeypatch):
    """Intercept the outbound share at its own function boundary."""
    calls = []
    monkeypatch.setattr(
        token_tracker, "_share_savings",
        lambda delta, anon_id: calls.append((delta, anon_id)),
    )
    return calls


# --------------------------------------------------------------------------- #
# Behaviour                                                                    #
# --------------------------------------------------------------------------- #

def test_nothing_is_sent_by_default(tmp_path, captured, monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    token_tracker.record_savings(5_000, str(tmp_path))
    assert captured == [], "savings were shared with no opt-in"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "true", "yes", "2"])
def test_only_the_exact_string_1_opts_in(tmp_path, captured, monkeypatch, value):
    """Fail closed on anything ambiguous — `true` must not enable a network call.

    A flag that accepts several spellings is a flag someone enables by accident.
    """
    monkeypatch.setenv(ENV_FLAG, value)
    token_tracker.record_savings(5_000, str(tmp_path))
    assert captured == [], f"{ENV_FLAG}={value!r} triggered a share"


def test_opting_in_shares_only_the_delta_and_an_anonymous_id(
    tmp_path, captured, monkeypatch
):
    monkeypatch.setenv(ENV_FLAG, "1")
    token_tracker.record_savings(5_000, str(tmp_path))

    assert len(captured) == 1
    delta, anon_id = captured[0]
    assert delta == 5_000
    assert isinstance(anon_id, str) and len(anon_id) >= 32
    # The id is random and persistent, never derived from anything identifying.
    assert "/" not in anon_id and "\\" not in anon_id


def test_a_zero_delta_sends_nothing_even_when_opted_in(
    tmp_path, captured, monkeypatch
):
    monkeypatch.setenv(ENV_FLAG, "1")
    token_tracker.record_savings(0, str(tmp_path))
    assert captured == []


def test_recording_still_works_with_telemetry_off(tmp_path, monkeypatch):
    """The local meter is the product feature; the share is not."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert token_tracker.record_savings(1_234, str(tmp_path)) == 1_234
    assert token_tracker.get_total_saved(str(tmp_path)) == 1_234


# --------------------------------------------------------------------------- #
# Documentation — the half that was actually wrong                             #
# --------------------------------------------------------------------------- #

def _module_doc() -> str:
    return pathlib.Path(token_tracker.__file__).read_text(encoding="utf-8")[:4000]


def test_the_docstring_names_the_real_environment_variable():
    doc = token_tracker.__doc__ or ""
    assert ENV_FLAG in doc, (
        "the module docstring must name the flag that actually gates the "
        "network call — a reader auditing outbound traffic looks here first"
    )


def test_the_docstring_does_not_name_the_retired_flag():
    assert "JCODEMUNCH_SHARE_SAVINGS" not in (token_tracker.__doc__ or ""), (
        "JCODEMUNCH_SHARE_SAVINGS never gated anything in this package"
    )


def test_the_docstring_does_not_claim_sharing_is_on_by_default():
    doc = (token_tracker.__doc__ or "").lower()
    assert "opt-in" in doc
    assert "by default" not in doc or "off by default" in doc, (
        "the docstring must not describe the share as default-on"
    )


def test_the_gating_flag_appears_exactly_where_expected_in_source():
    """If the gate moves, this test drags the docstring along with it."""
    src = _module_doc()
    assert src.count(ENV_FLAG) >= 2, (
        f"{ENV_FLAG} should appear in both the docstring and the gate; if you "
        "moved the check, update the docstring in the same commit"
    )
