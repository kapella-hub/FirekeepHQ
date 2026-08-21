"""Size ceilings on hook stdout, measured against deliberately hostile input.

Nothing in CI knew how large any hook's output could get. Two surfaces were
structurally unbounded when this was written (2026-08-21): the prompt core
renders relay task titles and channel message bodies verbatim, and the
session_start core renders whatever the briefing endpoint returns. Both are
server-supplied strings with no client-side trim, so "how big can this get" had
the answer "as big as someone's task title".

Why this matters even though hook stdout is currently CHEAP: Firekeep's dict
cores emit `{"systemMessage": ...}`, and on Claude Code that is a user-facing
line rather than model context — so today these bytes cost 0 model tokens. That
is precisely why a cap belongs here NOW. The moment any core moves to
`hookSpecificOutput.additionalContext` (which is what actually reaches the
model), every one of these strings starts being re-sent on every remaining turn
of the session. A cap added before that switch is free; a cap added after it is
a regression hunt.

The ceilings are measured p99 plus headroom, not aspirations. They are meant to
pass today and to fail loudly the day an unbounded surface actually runs away.
"""
from __future__ import annotations

import json

import pytest


# core -> ceiling in characters. Measured maxima on 2026-08-21:
# session_start 2,549 · prompt 999 · stop 847 · precompact 169.
CEILINGS = {
    "session_start": 3_500,
    "prompt": 2_000,
    "stop": 1_100,
    "precompact": 400,
}

# A task title nobody would write on purpose, and every hook will meet one day.
HOSTILE_TITLE = "fix the thing " * 800          # ~11 KB
HOSTILE_BODY = "context dump line. " * 900      # ~17 KB


def _patch_transport(monkeypatch, *, tasks=None, sessions=None, briefing=None):
    from firekeep_client import transport

    def fake_get(url, **k):
        if "/tasks" in url:
            return {"count": len(tasks or []), "tasks": tasks or []}
        if "/sessions" in url:
            return {"sessions": sessions or []}
        if "/briefing" in url:
            return briefing or {}
        return {}

    monkeypatch.setattr(transport, "get_json", fake_get)


def _patch_mcp(monkeypatch, *, messages=None):
    from firekeep_client.hooks import _mcp

    def fake_call(service, tool, args, **k):
        if tool == "relay_get_messages":
            return {"count": len(messages or []), "messages": messages or []}
        return {}

    monkeypatch.setattr(_mcp, "call_tool", fake_call)


def _emitted(result) -> str:
    """The bytes a hook actually writes to stdout."""
    if not result:
        return ""
    return json.dumps(result)


# --------------------------------------------------------------------------- #
# Static cores — constant text, so the ceiling is exact                        #
# --------------------------------------------------------------------------- #

def test_stop_output_is_within_budget(client_env, monkeypatch):
    from firekeep_client.hooks import stop
    from firekeep_client import state

    monkeypatch.setattr(state, "read_scratch", lambda *a, **k: None)
    monkeypatch.setattr(state, "write_scratch", lambda *a, **k: None)
    out = stop.run({})
    assert len(_emitted(out)) <= CEILINGS["stop"], (
        f"stop hook emitted {len(_emitted(out))} chars, over "
        f"{CEILINGS['stop']}. This text fires at EVERY assistant turn end."
    )


def test_precompact_output_is_within_budget(client_env):
    from firekeep_client.hooks import precompact
    out = precompact.run({})
    assert len(_emitted(out)) <= CEILINGS["precompact"]


# --------------------------------------------------------------------------- #
# Dynamic cores — server-supplied strings, so the ceiling is the point         #
# --------------------------------------------------------------------------- #

def test_prompt_output_is_bounded_under_hostile_relay_content(client_env, monkeypatch):
    """A 11KB task title and a 17KB channel message must not reach stdout whole.

    The task id and sender must SURVIVE the trim — they are how the agent asks
    for the full text (`relay_task_list`), which is the difference between a
    pointer and a loss.
    """
    from firekeep_client.hooks import prompt
    from firekeep_client import promptrecall

    monkeypatch.setattr(promptrecall, "nudge", lambda cfg, payload: "")
    _patch_transport(
        monkeypatch,
        sessions=[{"session_id": "s1", "goal": "g"}],
        tasks=[{"id": "task-1", "title": HOSTILE_TITLE, "creator": "someone"}],
    )
    _patch_mcp(
        monkeypatch,
        messages=[{"timestamp": "99999999", "sender": "other", "content": HOSTILE_BODY}],
    )

    out = prompt.run({"prompt": "do the thing"})
    emitted = _emitted(out)

    assert len(emitted) <= CEILINGS["prompt"], (
        f"prompt hook emitted {len(emitted)} chars from hostile relay content, "
        f"over {CEILINGS['prompt']}. Relay strings need a client-side trim."
    )
    if emitted:
        assert "task-1" in emitted, "the task id must survive the trim"


def test_session_start_output_is_bounded_under_a_hostile_briefing(
    client_env, monkeypatch
):
    """The briefing is server-rendered; the client still owns its own ceiling."""
    from firekeep_client.hooks import session_start

    _patch_transport(
        monkeypatch,
        briefing={
            "briefing_id": "b1",
            "text": HOSTILE_BODY,
            "profile": HOSTILE_TITLE,
        },
    )
    out = session_start.run({})
    emitted = _emitted(out)
    assert len(emitted) <= CEILINGS["session_start"], (
        f"session_start emitted {len(emitted)} chars from a hostile briefing, "
        f"over {CEILINGS['session_start']}."
    )


# --------------------------------------------------------------------------- #
# The ratchet itself                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("core_name", sorted(CEILINGS))
def test_every_dict_core_has_a_declared_ceiling(core_name):
    """Adding a dict core without a budget line should be impossible to miss."""
    from firekeep_client.hooks import __main__ as dispatcher

    assert core_name in dispatcher._DICT_CORES


def test_no_dict_core_is_missing_from_the_budget_table():
    from firekeep_client.hooks import __main__ as dispatcher

    # session_end deliberately emits nothing — "the session is over, there is
    # nobody left to read it" (session_end.py:91) — so it needs no ceiling.
    exempt = {"session_end"}
    missing = set(dispatcher._DICT_CORES) - set(CEILINGS) - exempt
    assert not missing, (
        f"dict cores with no stdout budget: {sorted(missing)}. Add a ceiling "
        "in CEILINGS above, sized from a measured p99 plus headroom."
    )
