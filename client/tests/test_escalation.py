"""Escalation — the hook that tells a cheap main model when to consult Fable.

What these tests are FOR: the main model in this fleet is a cheap one, and the
escalation path that Claude Code ships (`/advisor`) cannot reach a non-Anthropic
upstream — so the decision to escalate has to be made here, in the client, and
stated to the model in words.

Two properties matter more than any other, and both are about restraint:

  * it speaks only on EVIDENCE it counted itself (consecutive failures, repeated
    edits to one file), never on a guess about difficulty;
  * it speaks ONCE per situation. The prompt core's own history is the reason —
    before the 2026-07-14 rewrite it re-injected the same five stale messages on
    every single prompt. A nudge that repeats itself is that failure again.

Everything here is local state; nothing touches the network, so the whole file
runs without a server.
"""
from __future__ import annotations

import json
import textwrap

import pytest

from firekeep_client import escalation, resolver, state


@pytest.fixture
def client_env(tmp_path, monkeypatch):
    """A tmp ~/.firekeep, mirroring test_promptrecall.py's fixture of the same name.
    Defined here rather than imported so this file runs without the hooks package's
    conftest."""
    cfg = tmp_path / "config"
    cfg.write_text(textwrap.dedent("""\
        [identity]
        agent_id = tester
        [server]
        kind = ports
        scheme = http
        host = 127.0.0.1
        verify_tls = false
    """))
    cache = tmp_path / "cache"
    cache.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setenv("FIREKEEP_CONFIG", str(cfg))
    monkeypatch.setenv("FIREKEEP_CACHE_DIR", str(cache))
    monkeypatch.setenv("FIREKEEP_LOG_DIR", str(logs))
    monkeypatch.delenv("FIREKEEP_AGENT_ID", raising=False)
    monkeypatch.delenv("FIREKEEP_NO_ESCALATION", raising=False)
    # The feature is gated on the main model being one that BENEFITS from
    # escalation (see TestModelGate). Every test in this file except that class
    # is about the counting/rendering behaviour, so the fixture puts the session
    # on a cheap model — which is the condition the feature exists for.
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-flash[1m]")
    # ...reached through the local router, which is what makes it a cheap session
    # and not a direct-to-Anthropic one. Set explicitly so these tests describe a
    # known environment rather than inheriting whatever the runner happens to be.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:47812")
    return {"tmp": tmp_path, "cfg": cfg, "cache": cache, "logs": logs, "agent": "tester"}


def _cfg():
    return resolver.load_config()


SESSION = "sess-1"


def _fail(tool="Bash", path=""):
    escalation.record_outcome(SESSION, tool_name=tool, file_path=path, success=False)


def _ok(tool="Bash", path=""):
    escalation.record_outcome(SESSION, tool_name=tool, file_path=path, success=True)


def _nudge():
    return escalation.nudge(_cfg(), {"session_id": SESSION})


class TestCounting:
    """The triggers are counters, not classifiers. These pin the arithmetic."""

    def test_a_clean_session_injects_nothing(self, client_env):
        assert _nudge() == ""

    def test_one_failure_is_not_enough(self, client_env):
        """A single failed command is ordinary work, not a reason to spend Fable."""
        _fail()
        assert _nudge() == ""

    def test_two_consecutive_failures_trigger(self, client_env):
        _fail()
        _fail()
        assert _nudge() != ""

    def test_a_success_resets_the_failure_streak(self, client_env):
        """fail, success, fail is not 'failed twice' — it is one failure either
        side of progress, and the model is not stuck."""
        _fail()
        _ok()
        _fail()
        assert _nudge() == ""

    def test_edit_thrash_triggers_on_the_third_edit_of_one_file(self, client_env):
        _ok(tool="Edit", path="src/foo.py")
        _ok(tool="Edit", path="src/foo.py")
        assert _nudge() == ""
        _ok(tool="Edit", path="src/foo.py")
        assert _nudge() != ""

    def test_edits_to_different_files_do_not_accumulate(self, client_env):
        """Thrash is about ONE file going in circles, not about editing in general."""
        _ok(tool="Edit", path="a.py")
        _ok(tool="Edit", path="b.py")
        _ok(tool="Edit", path="c.py")
        assert _nudge() == ""

    def test_a_passing_command_clears_the_edit_counters(self, client_env):
        """A green command is demonstrated progress, which is what 'without a clean
        pass' means — the file counter is not a lifetime edit tally."""
        _ok(tool="Edit", path="src/foo.py")
        _ok(tool="Edit", path="src/foo.py")
        _ok(tool="Bash")
        _ok(tool="Edit", path="src/foo.py")
        assert _nudge() == ""

    def test_a_failed_edit_counts_toward_both_triggers(self, client_env):
        _fail(tool="Edit", path="src/foo.py")
        _fail(tool="Edit", path="src/foo.py")
        assert _nudge() != ""


class TestQuiet:
    """It speaks once. Everything here is about not becoming the noise it warns about."""

    def test_the_nudge_fires_once_per_situation(self, client_env):
        _fail()
        _fail()
        assert _nudge() != ""
        assert _nudge() == ""

    def test_a_new_failure_re_arms_it(self, client_env):
        _fail()
        _fail()
        assert _nudge() != ""
        _fail()
        assert _nudge() != ""

    def test_env_kill_switch(self, client_env, monkeypatch):
        _fail()
        _fail()
        monkeypatch.setenv("FIREKEEP_NO_ESCALATION", "1")
        assert _nudge() == ""

    def test_config_kill_switch(self, client_env):
        client_env["cfg"].write_text(
            client_env["cfg"].read_text() + "[escalation]\nnudge = false\n")
        _fail()
        _fail()
        assert _nudge() == ""

    def test_blank_config_value_means_default_on_not_off(self, client_env):
        """A half-edited config gets the documented default, not silence."""
        client_env["cfg"].write_text(
            client_env["cfg"].read_text() + "[escalation]\nnudge =\n")
        _fail()
        _fail()
        assert _nudge() != ""

    def test_on_by_default(self, client_env):
        _fail()
        _fail()
        assert _nudge() != ""

    def test_the_state_carries_a_ttl_so_counters_cannot_outlive_the_session(
            self, client_env):
        import time

        _fail()
        _fail()
        assert _nudge() != ""
        ttl_file = state._scratch_ttl_file(escalation.state_key(SESSION))
        assert ttl_file.exists(), (
            "the counter declared no expiry — a stale streak would fire on a "
            "session that never earned it")
        ttl_file.write_text(str(time.time() - 1), encoding="utf-8")
        assert _nudge() == ""


class TestRender:
    def test_the_block_names_the_failure_count(self, client_env):
        _fail()
        _fail()
        _fail()
        assert "3" in _nudge()

    def test_the_block_names_the_thrashed_file(self, client_env):
        for _ in range(3):
            _ok(tool="Edit", path="src/foo.py")
        assert "src/foo.py" in _nudge()

    def test_the_block_names_the_escalation_target(self, client_env):
        """The whole point: the model must be told WHICH tool to reach for. A nudge
        that only says 'this is hard' is a feeling, not an instruction."""
        _fail()
        _fail()
        assert "fable-advisor" in _nudge()

    def test_the_block_is_labelled(self, client_env):
        _fail()
        _fail()
        assert _nudge().startswith("[escalation]")

    def test_never_raw_json(self, client_env):
        _fail()
        _fail()
        assert "{" not in _nudge()


class TestStandingPolicy:
    """The half that covers what a counter cannot see: architectural work is not
    detectable by counting, so it is stated as standing policy instead."""

    def test_names_architecture_and_planning(self, client_env):
        text = escalation.standing_policy(_cfg())
        assert "fable-advisor" in text
        assert "architecture" in text.lower()
        assert "planning" in text.lower()

    def test_silent_when_disabled(self, client_env, monkeypatch):
        monkeypatch.setenv("FIREKEEP_NO_ESCALATION", "1")
        assert escalation.standing_policy(_cfg()) == ""

    def test_is_rendered_for_the_model_channel(self, client_env):
        """It must be prose destined for a reader the model can act on — the
        dispatcher promotes a core's systemMessage to additionalContext only when
        the text is non-empty, so a blank policy stays a no-op."""
        assert escalation.standing_policy(_cfg()).strip() != ""


class TestModelGate:
    """The nudge is calibrated for a CHEAP main model, and is gated on one.

    Escalation only pays when the main model is the weak link: it turns a
    near-free DeepSeek call into a cheaper-but-good-enough answer. Point the same
    line at Claude and it inverts — it tells a strong model to consult a peer
    (circular when the main model IS Fable) and spends real money doing it. So the
    gate is an ALLOWLIST of models that benefit, and it fails QUIET: a model it
    cannot identify gets no nudge. That is the same direction the rest of this
    module fails in, for the same reason — a thing that writes into the user's
    context unasked should go dark, not loud.
    """

    @pytest.fixture
    def settings_file(self, tmp_path, monkeypatch):
        """Point the model lookup at a file we control, so a test never reads the
        machine's real ~/.claude/settings.json (which would make these results
        depend on whoever runs them)."""
        path = tmp_path / "claude-settings.json"
        monkeypatch.setattr(escalation, "_settings_paths", lambda: (path,))
        return path

    def _on_claude(self, settings_file, monkeypatch, model="claude-opus-5"):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        settings_file.write_text(json.dumps({"model": model}), encoding="utf-8")

    def test_silent_when_the_main_model_is_claude(self, client_env, monkeypatch,
                                                  settings_file):
        self._on_claude(settings_file, monkeypatch)
        _fail()
        _fail()  # evidence is present — the GATE is what must silence it
        assert _nudge() == ""

    def test_silent_for_every_claude_family_model(self, client_env, monkeypatch,
                                                  settings_file):
        for model in ("claude-opus-5", "claude-sonnet-5", "claude-fable-5-1",
                      "claude-haiku-4-5-20251001"):
            self._on_claude(settings_file, monkeypatch, model)
            _fail()
            assert _nudge() == "", model

    def test_speaks_when_the_main_model_is_deepseek(self, client_env, monkeypatch,
                                                    settings_file):
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        _fail()
        _fail()
        assert _nudge() != ""

    def test_the_env_var_wins_over_the_settings_file(self, client_env, monkeypatch,
                                                     settings_file):
        """ANTHROPIC_MODEL is the harness's own override for a session."""
        settings_file.write_text(json.dumps({"model": "claude-opus-5"}),
                                 encoding="utf-8")
        monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-flash[1m]")
        _fail()
        _fail()
        assert _nudge() != ""

    def test_silent_when_the_model_cannot_be_determined(self, client_env, monkeypatch,
                                                        settings_file):
        """Fail quiet: no model, no nudge. Guessing here would spend money on the
        one configuration where a wrong guess is expensive."""
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        assert not settings_file.exists()
        _fail()
        _fail()
        assert _nudge() == ""

    def test_an_unreadable_settings_file_is_not_an_error(self, client_env, monkeypatch,
                                                         settings_file):
        monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
        settings_file.write_text("{not json", encoding="utf-8")
        _fail()
        _fail()
        assert _nudge() == ""

    def test_the_allowlist_is_configurable(self, client_env, monkeypatch, settings_file):
        """For anyone running a different cheap model behind the same proxy."""
        client_env["cfg"].write_text(
            client_env["cfg"].read_text() + "[escalation]\nfor_models = gpt-, local-\n")
        self._on_claude(settings_file, monkeypatch, model="gpt-oss-120b")
        _fail()
        _fail()
        assert _nudge() != ""

    def test_a_configured_allowlist_replaces_the_default(self, client_env, monkeypatch,
                                                         settings_file):
        client_env["cfg"].write_text(
            client_env["cfg"].read_text() + "[escalation]\nfor_models = gpt-\n")
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        _fail()
        _fail()
        assert _nudge() == ""

    def test_the_standing_policy_is_gated_too(self, client_env, monkeypatch,
                                              settings_file):
        """It rides the session briefing on every session — on Claude it would be
        the same wrong advice, delivered unconditionally."""
        self._on_claude(settings_file, monkeypatch)
        assert escalation.standing_policy(_cfg()) == ""

    def test_silent_when_the_session_goes_straight_to_anthropic(self, client_env,
                                                                monkeypatch,
                                                                settings_file):
        """THE case that a model-name check alone gets wrong.

        The `claude-fable` escape hatch launches Claude Code with its own settings
        file (`--settings ~/.claude/anthropic-only.settings.json`) whose `model` is
        Fable. A hook cannot see that file, so the model lookup falls through to
        the GLOBAL settings — which say deepseek-flash. Reading only the model name
        therefore nudges a Fable session, exactly backwards.

        What IS visible is the file's `env` block: it sets ANTHROPIC_BASE_URL to
        api.anthropic.com, and session env is inherited by hook processes. So the
        endpoint, not the model name, is the reliable signal here.
        """
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        _fail()
        _fail()
        assert _nudge() == ""

    def test_silent_when_no_endpoint_override_is_present(self, client_env,
                                                         monkeypatch, settings_file):
        """An unset ANTHROPIC_BASE_URL means Claude Code's own default, which is
        Anthropic. Same conclusion, reached from the other direction."""
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        _fail()
        _fail()
        assert _nudge() == ""

    def test_speaks_when_the_session_is_behind_the_router(self, client_env,
                                                          monkeypatch, settings_file):
        """The configured DeepSeek setup: a cheap model reached through the local
        router. Do not require a specific port — any non-Anthropic endpoint is the
        signal."""
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:47812")
        _fail()
        _fail()
        assert _nudge() != ""

    def test_the_gate_is_about_the_model_not_the_user_switch(self, client_env,
                                                             monkeypatch, settings_file):
        """Two different questions: `is_enabled` is the user's off switch,
        `should_escalate` is whether this model benefits at all."""
        self._on_claude(settings_file, monkeypatch, model="deepseek-flash[1m]")
        assert escalation.is_enabled(_cfg()) is True
        assert escalation.should_escalate(_cfg()) is True
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-5")
        assert escalation.is_enabled(_cfg()) is True      # still switched on
        assert escalation.should_escalate(_cfg()) is False  # just not useful here


class TestFailOpen:
    def test_corrupt_state_reads_as_no_evidence(self, client_env):
        """A lost counter costs a missed nudge. Treating a parse failure as 'stuck'
        would fire an unnecessary Fable call, which is the louder failure."""
        state.write_scratch(escalation.state_key(SESSION), "{not json")
        assert _nudge() == ""

    def test_an_unwritable_counter_never_raises(self, client_env, monkeypatch):
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(state, "write_scratch", boom)
        _fail()
        _fail()  # must not raise
        assert _nudge() == ""

    def test_a_negative_or_absurd_count_is_tolerated(self, client_env):
        state.write_scratch(escalation.state_key(SESSION),
                            '{"fail_streak": -5, "edits": {"a.py": "banana"}}')
        assert _nudge() == ""

    def test_unknown_session_still_counts_rather_than_crashing(self, client_env):
        """resolve_session_id degrades to 'unknown' when it cannot resolve — the
        counters must still work there instead of raising."""
        escalation.record_outcome("unknown", tool_name="Bash", file_path="",
                                  success=False)
        escalation.record_outcome("unknown", tool_name="Bash", file_path="",
                                  success=False)
        assert escalation.nudge(_cfg(), {"session_id": "unknown"}) != ""


class TestPromptHookIntegration:
    """The wiring: the escalation block joins the prompt hook's one systemMessage."""

    def _patch_relay(self, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import _mcp

        monkeypatch.setattr(transport, "get_json", lambda *a, **k: {})
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})

    def test_it_merges_into_the_prompt_hook_message(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt

        self._patch_relay(monkeypatch)
        monkeypatch.setattr("firekeep_client.promptrecall.nudge", lambda *a, **k: "")
        _fail()
        _fail()
        msg = prompt.run({"prompt": "keep going", "session_id": SESSION})["systemMessage"]
        assert "[escalation]" in msg
        assert "fable-advisor" in msg

    def test_a_quiet_escalation_leaves_the_hook_silent(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt

        self._patch_relay(monkeypatch)
        monkeypatch.setattr("firekeep_client.promptrecall.nudge", lambda *a, **k: "")
        assert prompt.run({"prompt": "keep going", "session_id": SESSION}) == {}
