"""Hook text written FOR the model has to reach the model.

Firekeep's dict cores return {"systemMessage": ...}. On Claude Code that is a
line shown to the HUMAN; the channel that reaches the model is
`hookSpecificOutput.additionalContext`. Measured 2026-08-21 by comparing every
SessionStart hook attachment in one session: the three emitting
`additionalContext` were verbatim in the model's context, the two emitting
`systemMessage` (Firekeep's pre-flight briefing, the symdex banner) were absent.
Same event, same session, one variable. Claude Code's docs agree — "To surface a
message to the user on any platform, return systemMessage", against "for
SessionStart and UserPromptSubmit Claude Code adds plain-text stdout as context
that Claude can see and act on".

The text was always written for the model. The briefing opens "You are
agent-...", the proactive recall pushes memories for the agent to use, and the
bypass notice says "you should NOT use firekeep_* tools" — an instruction with
no meaning for a human reader. All of it was going to the terminal.

Scope is deliberately narrow, and the narrowness is the point:

  * ONLY `session_start` and `prompt` switch. Those are the two events Claude
    Code documents as accepting model-facing context. `stop`, `precompact` and
    `session_end` keep emitting systemMessage alone, because nobody has verified
    that those events have a model-facing channel at all and inventing one from
    a plausible-looking shape is how the original bug happened.

  * ONLY the `claude` runtime switches. kiro, opencode and the rest use
    different mechanisms that have not been measured. Guessing their shape would
    repeat the same mistake in a new place.

  * systemMessage is KEPT alongside. It costs nothing extra — only
    additionalContext enters the context window — and it is what keeps the human
    seeing what their agent was just handed.
"""
from __future__ import annotations

import io
import json

import pytest

from firekeep_client.hooks import __main__ as dispatcher


MODEL_FACING_CORES = ("session_start", "prompt")
HUMAN_ONLY_CORES = ("stop", "precompact", "session_end")

EXPECTED_EVENT = {
    "session_start": "SessionStart",
    "prompt": "UserPromptSubmit",
}


def _run(monkeypatch, capsys, core: str, *, runtime="claude", text="hello from the hook"):
    """Drive the real dispatcher and return whatever it printed, parsed."""
    monkeypatch.setattr(
        dispatcher._CORE_MODULES[core], "run", lambda payload: {"systemMessage": text}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    argv = [core] if runtime is None else [core, "--runtime", runtime]
    rc = dispatcher.main(argv)
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out) if out else None)


# --------------------------------------------------------------------------- #
# The fix                                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("core", MODEL_FACING_CORES)
def test_model_facing_cores_reach_the_model(client_env, monkeypatch, capsys, core):
    rc, out = _run(monkeypatch, capsys, core)
    assert rc == 0

    hso = out.get("hookSpecificOutput")
    assert hso, (
        f"{core} emitted no hookSpecificOutput — its text still goes only to the "
        "human's terminal, which is the bug this test exists for"
    )
    assert hso["hookEventName"] == EXPECTED_EVENT[core]
    assert hso["additionalContext"] == "hello from the hook"


@pytest.mark.parametrize("core", MODEL_FACING_CORES)
def test_the_human_still_sees_it(client_env, monkeypatch, capsys, core):
    """Dual channel. Only additionalContext costs context window."""
    _, out = _run(monkeypatch, capsys, core)
    assert out["systemMessage"] == "hello from the hook"
    assert out["hookSpecificOutput"]["additionalContext"] == out["systemMessage"]


# --------------------------------------------------------------------------- #
# The narrowness                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("core", HUMAN_ONLY_CORES)
def test_unverified_events_are_left_alone(client_env, monkeypatch, capsys, core):
    """stop / precompact / session_end keep systemMessage only.

    Not an oversight. Claude Code documents SessionStart and UserPromptSubmit as
    taking model-facing context; it does not document these, and nobody has
    measured them. Emitting a plausible-looking shape at an event that ignores
    it would put the text back where it started while looking fixed.
    """
    _, out = _run(monkeypatch, capsys, core)
    if out is None:
        pytest.skip(f"{core} emitted nothing for this payload")
    assert "hookSpecificOutput" not in out
    assert out["systemMessage"] == "hello from the hook"


@pytest.mark.parametrize("runtime", ["kiro", "opencode", "codex", "generic"])
def test_other_runtimes_are_unchanged(client_env, monkeypatch, capsys, runtime):
    _, out = _run(monkeypatch, capsys, "session_start", runtime=runtime)
    assert "hookSpecificOutput" not in out, (
        f"{runtime} got a Claude Code-shaped payload; its channel has not been "
        "measured and its shape may differ"
    )
    assert out["systemMessage"] == "hello from the hook"


def test_a_hook_with_no_runtime_flag_is_unchanged(client_env, monkeypatch, capsys):
    """An OLD rendered hook command carries no --runtime and must keep working."""
    _, out = _run(monkeypatch, capsys, "session_start", runtime=None)
    assert "hookSpecificOutput" not in out
    assert out["systemMessage"] == "hello from the hook"


# --------------------------------------------------------------------------- #
# Edges                                                                        #
# --------------------------------------------------------------------------- #

def test_a_silent_core_stays_silent(client_env, monkeypatch, capsys):
    """The prompt core returns {} on most turns; it must not start emitting."""
    monkeypatch.setattr(dispatcher._CORE_MODULES["prompt"], "run", lambda p: {})
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    dispatcher.main(["prompt", "--runtime", "claude"])
    assert capsys.readouterr().out.strip() == ""


def test_a_core_returning_no_systemmessage_gets_no_context_block(
    client_env, monkeypatch, capsys
):
    """Only text destined for a reader is promoted — never an arbitrary dict."""
    monkeypatch.setattr(
        dispatcher._CORE_MODULES["session_start"], "run", lambda p: {"decision": "block"}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    dispatcher.main(["session_start", "--runtime", "claude"])
    out = json.loads(capsys.readouterr().out.strip())
    assert "hookSpecificOutput" not in out
    assert out["decision"] == "block"


def test_output_stays_within_the_stdout_budget(client_env, monkeypatch, capsys):
    """The budgets in test_output_budget.py become load-bearing once this lands.

    Before the switch, hook text cost 0 model tokens because it never reached
    the model. From here it is re-sent on every remaining turn, so the caps stop
    being hygiene and start being the thing that keeps this affordable.
    """
    _, out = _run(monkeypatch, capsys, "session_start")
    emitted = json.dumps(out)
    # Dual channel roughly doubles the STDOUT bytes; only one copy reaches the
    # model, so the meaningful figure is additionalContext, not the envelope.
    assert len(out["hookSpecificOutput"]["additionalContext"]) <= 3_500
