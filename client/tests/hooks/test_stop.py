"""SP1b hook core: stop — race-guarded deregister + final workspace snapshot."""
from __future__ import annotations

import time


def _record_calls(monkeypatch):
    from firekeep_client.hooks import _mcp
    calls = []
    monkeypatch.setattr(_mcp, "call_tool",
                        lambda service, tool, args, **k: (calls.append((tool, args)) or {}))
    return calls


class TestGitHelper:
    def test_workspace_snapshot_shape(self, client_env):
        from firekeep_client.hooks import _git
        snap = _git.workspace_snapshot()
        assert "branch:" in snap
        assert "recent_commits:" in snap
        assert "staged_files:" in snap

    def test_workspace_snapshot_respects_cwd(self, client_env, monkeypatch):
        """SP1b Task 19 Part C: the sidecar's duplicate _collect_snapshot is
        dedup'd onto this helper via an optional cwd param -- both sidecar and
        stop/prompt now share ONE git-snapshot implementation."""
        from firekeep_client.hooks import _git

        seen_cwd = []

        class _Result:
            returncode = 0
            stdout = "ok"

        def fake_run(cmd, **kwargs):
            seen_cwd.append(kwargs.get("cwd"))
            return _Result()

        monkeypatch.setattr(_git.subprocess, "run", fake_run)

        _git.workspace_snapshot(cwd="/some/workdir")

        assert seen_cwd  # git was invoked at least once
        assert all(c == "/some/workdir" for c in seen_cwd)

    def test_workspace_snapshot_default_cwd_is_none(self, client_env, monkeypatch):
        """Backward-compat: callers that don't pass cwd (stop.py, prompt.py)
        get subprocess's own default (current process cwd), unchanged."""
        from firekeep_client.hooks import _git

        seen_cwd = []

        class _Result:
            returncode = 0
            stdout = "ok"

        def fake_run(cmd, **kwargs):
            seen_cwd.append(kwargs.get("cwd"))
            return _Result()

        monkeypatch.setattr(_git.subprocess, "run", fake_run)

        _git.workspace_snapshot()

        assert seen_cwd
        assert all(c is None for c in seen_cwd)


class TestStop:
    def test_stop_never_deregisters_presence(self, client_env, monkeypatch):
        """REGRESSION GUARD for the turn-1 presence bug.

        Stop fires at EVERY assistant turn end, so deregistering here deleted
        presence at the end of turn 1 -- and relay heartbeat_presence is
        update-only (returns {"refreshed": False} at HTTP 200, which no client
        detects), so nothing could ever restore it. The deregister now lives in
        `session_end`. If it reappears here, that bug is back.

        The no-prior-mark case is used deliberately: it is the one that used to
        FORCE a deregister, because should_deregister() returns True when the
        mark is absent.
        """
        from firekeep_client.hooks import stop
        calls = _record_calls(monkeypatch)
        out = stop.run({})
        tools = [t for t, _ in calls]
        assert "relay_deregister" not in tools
        assert "ctx_update" in tools          # snapshot is correctly per-turn, still pushed
        assert "systemMessage" in out

    def test_stop_does_not_consume_the_registration_mark(self, client_env, monkeypatch):
        """The mark belongs to session_end. If stop consumed it, session_end's
        race guard would later read "no mark" -> deregister, and clobber the
        presence of a newer session that had just taken over this agent_id."""
        from firekeep_client import state
        from firekeep_client.hooks import stop
        key = "presence_registered_tester@personal"
        state.write_scratch(key, str(int(time.time())))
        _record_calls(monkeypatch)
        stop.run({})
        assert state.read_scratch(key) is not None

    def test_normal_stop_does_not_clear_session_stash(self, client_env, monkeypatch):
        """Stop fires every turn, not at session end — clearing the session
        stash here would drop X-Session-Id attribution for turns 2..N."""
        from firekeep_client import state
        from firekeep_client.hooks import stop
        state.write_session_stash("tester", "personal", session_id="sess-live")
        _record_calls(monkeypatch)
        stop.run({})
        assert state.read_session_stash("tester", "personal")["session_id"] == "sess-live"

    def test_stop_enqueues_distill_job(self, client_env, monkeypatch):
        """N=1 structural capture: stop enqueues a Relay distill task carrying the
        workspace snapshot, so the session's durable facts become memory WITHOUT
        depending on the agent having called memory_learn. relay_task_post's real
        params (relay/app/mcp_server.py) are title/assigner/context (no kind/metadata),
        so the distill 'kind' is the title, the agent is the assigner, and the snapshot
        rides in context."""
        from firekeep_client.hooks import stop
        calls = _record_calls(monkeypatch)
        stop.run({})
        distill = [a for t, a in calls
                   if t == "relay_task_post" and a.get("title") == "distill_session"]
        assert distill                          # a distill task was enqueued
        assert distill[0].get("assigner") == "tester"
        assert distill[0].get("context")        # carries the workspace snapshot

    def test_distill_task_carries_session_id_from_stash(self, client_env, monkeypatch):
        """Night Shift (0.1.23) reconstructs the session from replay/evals — it
        needs the session_id. The stash (written by the bridge tap) is the only
        place stop can get it; the stamp rides in the task description."""
        from firekeep_client import state
        from firekeep_client.hooks import stop
        state.write_session_stash("tester", "personal", session_id="sess-live")
        calls = _record_calls(monkeypatch)
        stop.run({})
        distill = [a for t, a in calls
                   if t == "relay_task_post" and a.get("title") == "distill_session"]
        assert "session_id=sess-live" in distill[0].get("description", "")

    def test_distill_task_without_stash_has_no_session_stamp(self, client_env, monkeypatch):
        from firekeep_client.hooks import stop
        calls = _record_calls(monkeypatch)
        stop.run({})
        distill = [a for t, a in calls
                   if t == "relay_task_post" and a.get("title") == "distill_session"]
        assert "session_id=" not in (distill[0].get("description") or "")

    def test_distill_enqueued_once_per_session_not_per_turn(self, client_env, monkeypatch):
        """Claude's Stop event fires at EVERY assistant turn end — without a
        marker, an N-turn session enqueues N duplicate distill tasks
        (wf_02954176 review; the live backlog held 50). One task per session."""
        from firekeep_client import state
        from firekeep_client.hooks import stop
        state.write_session_stash("tester", "personal", session_id="sess-multi")
        calls = _record_calls(monkeypatch)
        stop.run({})
        state.write_session_stash("tester", "personal", session_id="sess-multi")
        stop.run({})
        distill = [a for t, a in calls if t == "relay_task_post"
                   and a.get("title") == "distill_session"]
        assert len(distill) == 1

    def test_distill_reenqueued_for_a_new_session(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import stop
        state.write_session_stash("tester", "personal", session_id="sess-a")
        calls = _record_calls(monkeypatch)
        stop.run({})
        state.write_session_stash("tester", "personal", session_id="sess-b")
        stop.run({})
        distill = [a for t, a in calls if t == "relay_task_post"
                   and a.get("title") == "distill_session"]
        assert len(distill) == 2

    def test_stop_never_raises_when_relay_unreachable(self, client_env, monkeypatch):
        """The enqueue is best-effort: a relay outage is swallowed via try/except so
        session end still returns the completion reminder (not the @never_raise {})."""
        from firekeep_client.hooks import _mcp, stop

        def _boom(service, tool, args, **k):
            raise RuntimeError("relay down")

        monkeypatch.setattr(_mcp, "call_tool", _boom)
        out = stop.run({})
        assert "systemMessage" in out


class TestStopPersonalMode:
    """A personal-mode TURN end: make ZERO server calls, and leave personal mode ON.

    Stop fires at every assistant turn end, so clearing the marker here ended
    personal mode after turn 1 — `/personal` protected exactly one turn and then
    silently rejoined team logging. The auto-clear lives in `session_end` now.
    """

    def test_bypassed_stop_makes_no_calls_and_LEAVES_PERSONAL_ON(self, client_env, monkeypatch):
        from firekeep_client import resolver, state
        from firekeep_client.hooks import stop

        resolver.set_personal(True)
        state.write_session_stash("tester", "personal", session_id="sess-live")
        calls = _record_calls(monkeypatch)

        out = stop.run({})

        assert out == {}                              # no completion reminder for a personal session
        assert calls == []                            # nothing reached Relay/Bridge
        # THE REGRESSION GUARD: personal mode must survive a turn boundary.
        assert resolver.is_personal() is True
        assert resolver.personal_marker_path().exists()
        # Stop fires PER TURN (Claude 'Stop' event) — it must NOT clear the
        # session stash, or attribution dies after turn 1. Session-end clearing
        # is the tap's job (complete/abandon) + session_start's top-clear + TTL.
        assert state.read_session_stash("tester", "personal") is not None

    def test_env_bypass_stop_skips_comms_without_a_marker(self, client_env, monkeypatch):
        from firekeep_client.hooks import stop

        monkeypatch.setenv("FIREKEEP_BYPASS", "1")  # env-only; no marker to clear
        calls = _record_calls(monkeypatch)

        out = stop.run({})

        assert out == {}
        assert calls == []
