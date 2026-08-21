"""`scope_ask`'s long-poll ceiling is a token-cost setting.

Same defect and same fix as the decision board's own ceiling — see
`client/tests/test_decision_poll_ceiling.py` for the full measurement. In short:
when the human has not answered, the tool returns and the agent calls again, and
every one of those calls is a model turn that re-sends the entire conversation.
Measured 2026-08-21 on poll-only turns: ~49,800 price-weighted tokens per
expiry, against a payload of roughly 90 tokens.

The ceiling has to clear the tightest limit in the whole chain rather than the
one this process happens to see:

  * Claude Code -> STDIO MCP server (the gateway): ~27.8 h default per call.
  * Claude Code -> REMOTE HTTP/SSE MCP server (deploy/chatgpt-tunnel/): 60 s.
  * firekeep-shim -> this service: 300 s (shim.SSE_READ_TIMEOUT).

60 s binds, so the ceiling sits 10 s under it.
"""
from __future__ import annotations

from app import mcp_server


# The tightest measured per-call ceiling anywhere in the chain.
TIGHTEST_KNOWN_HOST_CEILING_SECONDS = 60
MIN_HEADROOM_SECONDS = 5

# The shim's own read timeout in front of this service.
SHIM_SSE_READ_TIMEOUT_SECONDS = 300


def _ceiling() -> int:
    return (
        mcp_server._SCOPE_ASK_POLL_ITERATIONS
        * mcp_server._SCOPE_ASK_POLL_INTERVAL_SECONDS
    )


def test_ceiling_clears_the_tightest_host_timeout():
    """Outliving the host timeout is worse than polling often.

    The host cancels the call, so the agent gets an error rather than a
    "not answered yet" — and cannot tell that apart from a dead board.
    """
    assert _ceiling() <= TIGHTEST_KNOWN_HOST_CEILING_SECONDS - MIN_HEADROOM_SECONDS, (
        f"scope_ask polls for {_ceiling()}s, leaving under {MIN_HEADROOM_SECONDS}s "
        f"of headroom below the {TIGHTEST_KNOWN_HOST_CEILING_SECONDS}s remote-MCP ceiling."
    )


def test_ceiling_is_long_enough_to_be_worth_it():
    """Guard the other direction — this is the assertion that stops a drift
    back to 24 s, which is what the measurement was taken against."""
    assert _ceiling() >= 45, (
        f"scope_ask poll ceiling regressed to {_ceiling()}s; each expiry costs a "
        "full context re-send (~49,800 weighted tokens measured 2026-08-21)."
    )


def test_ceiling_stays_inside_the_shim_read_timeout():
    """The shim sits between the agent and this service and reads with its own
    timeout. A poll that outlives it fails in the middle, not at the edge."""
    assert _ceiling() < SHIM_SSE_READ_TIMEOUT_SECONDS


def test_poll_interval_still_returns_promptly_when_answered():
    """The ceiling bounds the UNANSWERED case only.

    A responsive human must not wait the full window, so the granularity has to
    stay small — raising the ceiling by lengthening the interval would trade the
    token saving for latency and defeat the point.
    """
    assert mcp_server._SCOPE_ASK_POLL_INTERVAL_SECONDS <= 2
