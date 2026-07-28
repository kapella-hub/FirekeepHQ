"""SessionEnd core — presence deregister at REAL session end.

Why this exists as its own core rather than living in `stop`:

`stop` is wired to Claude's Stop event, which fires at the end of EVERY assistant
turn, not once per session (stop.py's own comment records finding this live: "a
50-task backlog of per-turn duplicates"). Deregistering there meant presence was
deleted at the end of a session's FIRST turn and could never come back --
`presence.heartbeat_presence` is update-only, returning
`{"refreshed": False, "reason": "not_registered"}` at HTTP 200 with no "error"
key, so `prompt`'s heartbeat could neither resurrect it nor report the failure.
Net effect: `relay_who_is_online` and the dashboard Presence view were empty in
steady state, and Cortex crash detection -- which infers death from ABSENT
presence -- flagged live sessions as crashed.

Claude's SessionEnd is the event that actually carries the turn-vs-session
distinction the Stop payload lacks, so the deregister moves here verbatim,
race guard included.

Runtime coverage, deliberate:
  - claude   : SessionEnd -> here. Stop no longer deregisters.
  - opencode : `session.deleted` IS real session end (adapters/opencode.py), so
               its plugin runs `stop` then this core.
  - kiro     : deliberately NOT wired, and this is final rather than interim.
               Probed 2026-07-28 on kiro-cli 2.12.1 (KIRO-VALIDATION.md rows 7-8):
               its `stop` fires PER TURN like Claude's (3 prompts in one session ->
               agentSpawn 1, userPromptSubmit 3, stop 3), so kiro carried this same
               bug and is fixed by the same change. But kiro has no session-end
               event, and its hook payload carries NO session id (keys are only
               cwd / hook_event_name / prompt|assistant_response), so there is
               neither an event to hang a deregister on nor an id to key a
               once-per-session marker by. Consequence is bounded and benign:
               presence is one key per agent_id with idempotent overwrite
               (relay/app/presence.py:27-29), so kiro leaves at most ONE idle
               record per agent, reclaimed by that agent's next agentSpawn and
               filtered from who_is_online by status. Strictly better than
               presence vanishing after turn 1.

SessionEnd does not fire on a hard kill, so a crashed session still leaves that
same single bounded record. That is the honest justification for an orphan
reaper, which is a separate decision and deliberately not bundled here.
"""
from __future__ import annotations

from firekeep_client import hooklog, resolver, state
from firekeep_client.hooks import _mcp, never_raise

_HOOK = "session_end"


@never_raise({})
def run(payload: dict) -> dict:
    # Personal mode: clear the marker (the documented "auto-clears at session
    # end" semantics) and skip ALL comms. This moved here from `stop`, which
    # fires at EVERY assistant turn end and therefore ended personal mode after
    # turn 1 -- `/personal` protected one turn, then silently rejoined team
    # logging.
    #
    # set_personal(False) clears the MARKER tier only. The FIREKEEP_BYPASS env
    # tier is startup-scoped and deliberately not clearable from here.
    #
    # kiro consequence, accepted: it has no session-end event (see the runtime
    # notes above), so personal mode there persists until the
    # FIREKEEP_PERSONAL_TTL_HOURS backstop (default 12h) or an explicit
    # `firekeep personal off`. That is what the TTL exists for, and it cannot
    # fail silently: session_start prints a loud PERSONAL MODE banner every
    # session and `firekeep doctor` carries a warning row.
    if resolver.is_bypassed():
        resolver.set_personal(False)
        return {}

    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)

    # Race guard, moved verbatim from stop.py: skip the deregister if a NEWER
    # session registered under this agent_id within the window, else a
    # just-started session loses the presence it just claimed. Still meaningful
    # here -- a new session can legitimately start while this one is ending.
    # Shared keying authority: state.should_deregister/clear_registered, the
    # SAME scratch key session_start.py's mark_registered writes and the
    # sidecar's independent registration guard reads.
    do_dereg = state.should_deregister(agent, profile=profile)
    state.clear_registered(agent, profile=profile)  # consume the mark either way

    if do_dereg:
        try:
            _mcp.call_tool("relay", "relay_deregister", {"agent_id": agent}, cfg=cfg)
        except Exception as e:  # noqa: BLE001
            hooklog.log_failure(_HOOK, f"relay_deregister failed: {e}")

    # No systemMessage: the session is over, there is nobody left to read it.
    return {}
