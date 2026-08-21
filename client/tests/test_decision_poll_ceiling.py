"""The long-poll ceiling is a token-cost setting, not just a latency setting.

`decision_board_check` returns `pending` when the human has not answered yet,
and the agent calls it again. Each of those calls is a full model turn, and a
model turn re-sends the entire conversation — so the ceiling does not cost the
~90 tokens of its own payload, it costs one whole context window per expiry.

Measured on this machine (2026-08-21) across 5 sessions and 13 boards: 197
poll-only turns, 93.5M raw token units, 9.8M price-weighted — about 49,800
weighted tokens per poll turn. Halving the number of expiries halves that, and
it costs nothing when the human is responsive: the poll loop returns the moment
an answer lands, so the ceiling only ever bounds the UNANSWERED case.

Why not simply raise it to minutes: the ceiling must survive the most
constrained host, not the one we happen to run on.

  * stdio MCP servers in Claude Code default to a 100,000,000 ms (~27.8 h)
    per-call timeout, i.e. effectively unbounded. That is the path the Firekeep
    gateway takes.
  * REMOTE HTTP/SSE MCP servers default to 60,000 ms in the same client, and
    that is the path `deploy/chatgpt-tunnel/` takes.
  * Other runtimes (codex, kiro, opencode) publish no number we have measured.

So 60 s is the tightest ceiling we can actually name, and the default must sit
safely under it with room for scheduling jitter and the synth call's own work.
`DECISION_POLL_SECONDS` remains the escape hatch for an operator who knows
their host tolerates more.
"""
from __future__ import annotations

import pytest

# firekeep_client.decision.server imports anyio (transitively, via mcp). The
# stdlib-only `client` CI job installs neither, so skip there and run for real
# in `client-transport`, which lists this file explicitly.
pytest.importorskip("anyio")

from firekeep_client.decision import server as decision_server  # noqa: E402


# The tightest per-call ceiling we have actually measured on a shipped host:
# remote HTTP/SSE MCP servers in Claude Code (stdio is ~27.8 h, effectively
# unbounded). Firekeep ships a remote path, so this is the binding constraint.
TIGHTEST_KNOWN_HOST_CEILING_SECONDS = 60.0

# Headroom for scheduling jitter, process wake-up and the response write.
MIN_HEADROOM_SECONDS = 5.0


def test_default_poll_ceiling_sits_under_the_tightest_known_host_ceiling():
    """A poll that outlives the host's timeout is worse than a short one.

    It does not merely cost a turn — the host cancels the call, so the agent
    gets an error instead of `pending` and cannot distinguish "no answer yet"
    from "board is gone".
    """
    assert decision_server._DEFAULT_POLL_SECONDS <= (
        TIGHTEST_KNOWN_HOST_CEILING_SECONDS - MIN_HEADROOM_SECONDS
    ), (
        f"_DEFAULT_POLL_SECONDS={decision_server._DEFAULT_POLL_SECONDS} leaves "
        f"less than {MIN_HEADROOM_SECONDS}s of headroom under the "
        f"{TIGHTEST_KNOWN_HOST_CEILING_SECONDS}s remote-MCP ceiling."
    )


def test_default_poll_ceiling_is_long_enough_to_be_worth_it():
    """Guard the OTHER direction: a short ceiling is a measured token cost.

    This is the assertion that keeps someone from quietly restoring 24 s. If a
    host is found that genuinely cannot hold the poll this long, lower it here
    deliberately and record the host — do not let it drift back.
    """
    assert decision_server._DEFAULT_POLL_SECONDS >= 45.0, (
        "poll ceiling regressed below 45s; each expiry costs a full context "
        "re-send (~49,800 weighted tokens measured 2026-08-21)."
    )


def test_env_override_still_wins():
    """An operator who knows their host can still tune it in both directions."""
    import os

    old = os.environ.get("DECISION_POLL_SECONDS")
    try:
        os.environ["DECISION_POLL_SECONDS"] = "12.5"
        assert decision_server._poll_seconds() == 12.5
        os.environ["DECISION_POLL_SECONDS"] = "not-a-number"
        assert decision_server._poll_seconds() == decision_server._DEFAULT_POLL_SECONDS
        del os.environ["DECISION_POLL_SECONDS"]
        assert decision_server._poll_seconds() == decision_server._DEFAULT_POLL_SECONDS
    finally:
        if old is None:
            os.environ.pop("DECISION_POLL_SECONDS", None)
        else:
            os.environ["DECISION_POLL_SECONDS"] = old


def test_poll_ceiling_stays_well_inside_the_board_ttl():
    """A poll must never outlive the board it is polling.

    If the ceiling ever exceeded the reaper horizon, a single call could span
    the board's own expiry and return `pending` for a board that no longer
    exists — the one state the tool contract says means "ask inline instead".
    """
    assert decision_server._DEFAULT_POLL_SECONDS < decision_server._DEFAULT_BOARD_TTL_SECONDS / 4
