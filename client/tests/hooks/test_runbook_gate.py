"""Enforced Runbooks Phase B — the pre_tool escalation gate.

Spec: docs/superpowers/specs/2026-08-15-enforced-runbooks-design.md.

The three postures under test, in rising order of importance:
  no match          -> exactly today's behaviour, ZERO network.
  advise/require_ack-> escalate; network failure fails OPEN (hooklog + 1 line).
  block             -> escalate; EVERY failure shape fails CLOSED (exit 2) —
                       review finding 3: rc starts at 2 before any network I/O,
                       only an authenticated allow lowers it to 0, and the
                       outer @never_raise(0) must never observe an exception
                       from the branch (which would fail it OPEN).
"""
from __future__ import annotations

import json
import sys
import time

import pytest


ADVISE = {"skill_id": "deploy-vps", "step_id": "push", "pattern": "git push*",
          "mode": "advise", "load_bearing": False, "fail_posture": "open"}
REQUIRE_ACK = {"skill_id": "deploy-vps", "step_id": "update", "pattern": "bash update.sh*",
               "mode": "require_ack", "load_bearing": True, "fail_posture": "open"}
BLOCK = {"skill_id": "deploy-vps", "step_id": "up", "pattern": "docker compose up*",
         "mode": "block", "load_bearing": True, "fail_posture": "closed"}


def _seed(state, *entries, version="v1"):
    assert state.write_runbook_bundle(
        {"version": version, "workspace_id": "ws-1", "entries": list(entries)})


def _make_stale(state):
    f = state.cache_dir() / "runbooks" / "ws-1.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    data["fetched_at"] = time.time() - state.RUNBOOK_BUNDLE_TTL_SECONDS - 10
    f.write_text(json.dumps(data), encoding="utf-8")


def _no_network(monkeypatch):
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp

    def forbidden(*a, **k):
        raise AssertionError("this path must make ZERO network calls")

    monkeypatch.setattr(transport, "get_json", forbidden)
    monkeypatch.setattr(transport, "post_json", forbidden)
    monkeypatch.setattr(_mcp, "call_tool", forbidden)


def _bash(command, **extra):
    payload = {"tool_name": "Bash", "tool_input": {"command": command},
               "session_id": "s"}
    payload.update(extra)
    return payload


class TestNoMatchZeroNetwork:
    def test_no_bundle_at_all_allows_with_zero_network(self, client_env, monkeypatch):
        from firekeep_client.hooks import pre_tool
        _no_network(monkeypatch)
        # No session_id: a session-resolution attempt would hit the forbidden
        # transport too — proving the no-bundle path resolves nothing.
        assert pre_tool.run({"tool_name": "Bash",
                             "tool_input": {"command": "git push"}}) == 0

    def test_bundle_present_but_no_match_allows_with_zero_network(
            self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE, REQUIRE_ACK, BLOCK)
        _no_network(monkeypatch)
        assert pre_tool.run({"tool_name": "Bash",
                             "tool_input": {"command": "ls -la"}}) == 0

    def test_fresh_bundle_match_does_not_refetch(self, client_env, monkeypatch):
        """Only staleness triggers a bundle GET; a fresh bundle escalates with
        exactly one POST and no GET."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        gets = []
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: gets.append(url) or {})
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "a1",
                                                    "advisories": []})
        assert pre_tool.run(_bash("git push origin main")) == 0
        assert gets == []


class TestEscalationContract:
    def test_match_escalates_with_the_pinned_wire_shape(self, client_env, monkeypatch,
                                                        capsys):
        from firekeep_client import state
        from firekeep_client import transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        seen = {}

        def fake_post(url, body, **k):
            seen["url"] = url
            seen["body"] = body
            seen["kwargs"] = k
            return {"decision": "allow", "action_id": "act-r1",
                    "advisories": [{"code": "procedure_step_missing",
                                    "message": "backup has no evidence"}]}

        monkeypatch.setattr(transport, "post_json", fake_post)
        rc = pre_tool.run(_bash("git push origin main", cwd="/work/repo"))

        assert rc == 0
        assert seen["url"].endswith("/agent/action/before")
        assert seen["body"]["adapter"] == "shell-hook"
        assert seen["body"]["session_id"] == "s"
        assert seen["body"]["action"]["type"] == "run_command"
        assert seen["body"]["action"]["target"] == "git push origin main"
        # cwd rides in the payload for audit (spec: "cwd sent for audit").
        assert seen["body"]["action"]["cwd"] == "/work/repo"
        # EXPLICIT 5s timeout — the transport default is 10s.
        assert seen["kwargs"]["timeout"] == 5.0
        # allow + advisory -> exit 0 with the advisory on stderr.
        assert "backup has no evidence" in capsys.readouterr().err
        # The action was queued for post_tool's /after reconcile — paired by
        # command hash (review 2026-08-15: parallel Bash calls must not
        # cross-attribute exit statuses).
        from firekeep_client.hooks import runbooks as _rb
        assert state.pop_action(
            "s",
            command_hash=_rb.local_command_hash("git push origin main")) == "act-r1"

    def test_allow_without_advisory_is_silent(self, client_env, monkeypatch, capsys):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "a2",
                                                    "advisories": []})
        assert pre_tool.run(_bash("git push")) == 0
        assert "[firekeep pre_tool]" not in capsys.readouterr().err

    @pytest.mark.parametrize("decision,expected_rc", [
        ("allow", 0), ("rethink", 1), ("block", 2),
    ])
    def test_verdict_to_exit_code_mapping(self, client_env, monkeypatch,
                                          decision, expected_rc):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": decision,
                                                    "action_id": "a3",
                                                    "advisories": [
                                                        {"message": "why"}]})
        assert pre_tool.run(_bash("git push")) == expected_rc

    def test_blocked_verdict_pushes_no_action(self, client_env, monkeypatch):
        """A blocked command never executes, so PostToolUse never fires — an
        action queued here would be popped by the NEXT unrelated reconcile."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "block",
                                                    "action_id": "a4",
                                                    "advisories": []})
        assert pre_tool.run(_bash("git push")) == 2
        assert state.pop_action("s") is None


class TestAdvisoryFailOpen:
    @pytest.mark.parametrize("entry", [ADVISE, REQUIRE_ACK])
    def test_network_failure_fails_open_with_one_stderr_line(
            self, client_env, monkeypatch, capsys, entry):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, entry)
        cmd = "git push" if entry is ADVISE else "bash update.sh --now"

        def boom(*a, **k):
            raise transport.TransportError("cortex down")

        monkeypatch.setattr(transport, "post_json", boom)
        assert pre_tool.run(_bash(cmd)) == 0
        err = capsys.readouterr().err
        assert err.count("\n") == 1  # exactly one line
        assert "fail open" in err
        log = (client_env["logs"] / "hooks.log").read_text(encoding="utf-8")
        assert "runbook escalation unavailable" in log

    def test_malformed_response_fails_open_for_advise(self, client_env, monkeypatch):
        """The OPEN posture keeps the edit-path leniency: a garbage response is
        not a verdict, and advisory runbooks do not block on garbage."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        monkeypatch.setattr(transport, "post_json", lambda *a, **k: "garbage")
        assert pre_tool.run(_bash("git push")) == 0


class TestBlockFailClosed:
    """Review finding 3 — the branch that must be perfect."""

    def test_authenticated_allow_lowers_to_zero(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "b1",
                                                    "advisories": [
                                                        {"code": "runbook_evaluated",
                                                         "message": ""}]})
        assert pre_tool.run(_bash("docker compose up -d")) == 0
        from firekeep_client.hooks import runbooks as _rb
        assert state.pop_action(
            "s", command_hash=_rb.local_command_hash("docker compose up -d")) == "b1"

    def test_allow_with_advisory_prints_warn(self, client_env, monkeypatch, capsys):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "b2",
                                                    "advisories": [
                                                        {"code": "runbook_evaluated",
                                                         "message": ""},
                                                        {"message": "proceed carefully"}]})
        assert pre_tool.run(_bash("docker compose up -d")) == 0
        assert "proceed carefully" in capsys.readouterr().err

    def test_allow_without_the_evaluated_marker_fails_closed(
            self, client_env, monkeypatch, capsys):
        """Review 2026-08-15: a bare allow is a degraded server (unreadable
        index, internal exception), not a verdict. Block mode refuses it."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "allow",
                                                    "action_id": "b9",
                                                    "advisories": []})
        assert pre_tool.run(_bash("docker compose up -d")) == 2
        assert "without evaluating" in capsys.readouterr().err

    def test_destructive_note_on_hostile_stderr_still_gates(
            self, client_env, monkeypatch):
        """Review 2026-08-15 (the MAJOR): the destructive-guard note prints
        OUTSIDE the fail-closed branch; a raising stderr there escaped to
        @never_raise(0) and exited 0 for a command a block-mode runbook was
        about to refuse. The note is advisory; the gate is not."""
        import sys
        from firekeep_client import state, transport
        from firekeep_client.hooks import destructive, pre_tool

        class Hostile:
            def write(self, *a, **k):
                raise ValueError("stderr is gone")
            def flush(self):
                raise ValueError("stderr is gone")

        _seed(state, BLOCK)
        monkeypatch.setattr(destructive, "guard",
                            lambda cmd: "snapshotted uncommitted work")
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: (_ for _ in ()).throw(
                                OSError("server down")))
        monkeypatch.setattr(sys, "stderr", Hostile())
        # destructive note raises on print -> must be swallowed; the gate then
        # runs, its escalation fails, and block mode fails CLOSED — never 0.
        assert pre_tool.run(_bash("docker compose up -d")) == 2

    def test_rethink_maps_to_1_and_block_to_2(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "rethink",
                                                    "advisories": [
                                                        {"message": "ack needed"}]})
        assert pre_tool.run(_bash("docker compose up -d")) == 1
        monkeypatch.setattr(transport, "post_json",
                            lambda url, body, **k: {"decision": "block",
                                                    "advisories": [
                                                        {"message": "backup missing"}]})
        assert pre_tool.run(_bash("docker compose up -d")) == 2

    @pytest.mark.parametrize("failure", [
        "http_500", "timeout", "dns", "oserror", "runtimeerror", "valueerror",
    ])
    def test_every_network_failure_shape_exits_2(self, client_env, monkeypatch,
                                                 capsys, failure):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)

        exc = {
            "http_500": transport.TransportError(
                "POST .../agent/action/before failed: 500 Internal Server Error",
                status=500),
            "timeout": transport.TransportError(
                "POST .../agent/action/before timed out after 5.0s"),
            "dns": transport.TransportError(
                "POST .../agent/action/before unreachable: getaddrinfo failed"),
            "oserror": OSError("connection reset"),
            "runtimeerror": RuntimeError("unexpected"),
            "valueerror": ValueError("bad payload"),
        }[failure]

        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(transport, "post_json", boom)
        assert pre_tool.run(_bash("docker compose up -d")) == 2
        err = capsys.readouterr().err
        assert "deploy-vps" in err            # names the runbook
        assert "fail-closed" in err           # states the posture

    @pytest.mark.parametrize("resp", [
        None, [], {}, "a string", 42,
        {"decision": "weird"},
        {"decision": None},
        {"advisories": [{"message": "no decision key"}]},
    ])
    def test_malformed_response_is_not_an_allow(self, client_env, monkeypatch,
                                                capsys, resp):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(transport, "post_json", lambda *a, **k: resp)
        assert pre_tool.run(_bash("docker compose up -d")) == 2
        err = capsys.readouterr().err
        assert "deploy-vps" in err
        assert "fail-closed" in err

    def test_exception_before_the_network_call_still_exits_2(
            self, client_env, monkeypatch):
        """Session resolution is inside the branch: its failure maps to 2 too."""
        from firekeep_client import state
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        monkeypatch.setattr(state, "resolve_session_id",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        _no_network(monkeypatch)  # nothing should be reached, and nothing may leak
        assert pre_tool.run(_bash("docker compose up -d")) == 2

    def test_never_raise_never_observes_the_branch(self, client_env, monkeypatch):
        """THE invariant: calling the UNDECORATED core directly (bypassing
        @never_raise(0)) with a failing escalation must return 2 — not raise.
        If the branch leaked, never_raise would have swallowed it to exit 0 and
        the block-mode runbook would fail OPEN, silently."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)

        def boom(*a, **k):
            raise transport.TransportError("down")

        monkeypatch.setattr(transport, "post_json", boom)
        rc = pre_tool.run.__wrapped__(_bash("docker compose up -d"))
        assert rc == 2
        log = (client_env["logs"] / "hooks.log").read_text(encoding="utf-8")
        assert "failed CLOSED" in log
        assert "run() crashed" not in log  # never_raise saw nothing

    def test_exits_2_even_when_hooklog_and_stderr_are_hostile(
            self, client_env, monkeypatch):
        """Beyond-contract hostility: hooklog raises, stderr's write raises.
        The branch must still return 2 without leaking an exception."""
        from firekeep_client import hooklog, state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)

        def boom(*a, **k):
            raise transport.TransportError("down")

        class BrokenStderr:
            def write(self, *_a):
                raise OSError("stderr gone")

            def flush(self):
                raise OSError("stderr gone")

        monkeypatch.setattr(transport, "post_json", boom)
        monkeypatch.setattr(hooklog, "log_failure",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("log dead")))
        monkeypatch.setattr(sys, "stderr", BrokenStderr())
        # Undecorated core: a leak becomes a test error, not a swallowed 0.
        assert pre_tool.run.__wrapped__(_bash("docker compose up -d")) == 2


class TestStalenessRefetch:
    def test_stale_block_match_refetch_success_retired_pattern_allows(
            self, client_env, monkeypatch):
        """The fresh, authenticated bundle says no runbook governs the command
        any more — nothing left to enforce, zero escalation."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        _make_stale(state)
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"version": "v2", "workspace_id": "ws-1",
                                              "entries": []})
        monkeypatch.setattr(transport, "post_json",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("no escalation for a retired pattern")))
        assert pre_tool.run(_bash("docker compose up -d")) == 0
        # And the refreshed bundle was stored as the new last-known-good.
        assert state.read_runbook_bundle()["version"] == "v2"

    def test_stale_block_match_refetch_demoted_to_advise_fails_open(
            self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        _make_stale(state)
        demoted = dict(BLOCK, mode="advise", fail_posture="open")
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"version": "v2", "workspace_id": "ws-1",
                                              "entries": [demoted]})

        def boom(*a, **k):
            raise transport.TransportError("down")

        monkeypatch.setattr(transport, "post_json", boom)
        # Demoted while we were stale: the advisory posture applies -> open.
        assert pre_tool.run(_bash("docker compose up -d")) == 0

    def test_stale_block_match_refetch_failure_keeps_fail_closed(
            self, client_env, monkeypatch, capsys):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, BLOCK)
        _make_stale(state)

        def get_boom(*a, **k):
            raise transport.TransportError("bundle endpoint down")

        def post_boom(*a, **k):
            raise transport.TransportError("gateway down")

        monkeypatch.setattr(transport, "get_json", get_boom)
        monkeypatch.setattr(transport, "post_json", post_boom)
        assert pre_tool.run(_bash("docker compose up -d")) == 2
        assert "fail-closed" in capsys.readouterr().err
        # Last-known-good survived the failed refetch.
        assert state.read_runbook_bundle()["version"] == "v1"

    def test_stale_advise_match_promoted_to_block_enforces_fresh_mode(
            self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        _make_stale(state)
        promoted = dict(ADVISE, mode="block", fail_posture="closed")
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"version": "v2", "workspace_id": "ws-1",
                                              "entries": [promoted]})

        def boom(*a, **k):
            raise transport.TransportError("down")

        monkeypatch.setattr(transport, "post_json", boom)
        assert pre_tool.run(_bash("git push origin main")) == 2

    def test_stale_advise_match_refetch_failure_fails_open(
            self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import pre_tool
        _seed(state, ADVISE)
        _make_stale(state)

        def boom(*a, **k):
            raise transport.TransportError("down")

        monkeypatch.setattr(transport, "get_json", boom)
        monkeypatch.setattr(transport, "post_json", boom)
        assert pre_tool.run(_bash("git push")) == 0


class TestDestructiveGuardStillFirst:
    def test_guard_runs_before_escalation_and_its_note_survives_a_block(
            self, client_env, monkeypatch, capsys):
        """The existing snapshot-then-allow check is UNCHANGED and runs first;
        the runbook gate runs after it and may still block."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import destructive, pre_tool
        _seed(state, BLOCK)
        order = []

        def fake_guard(command, cwd=None):
            order.append("guard")
            return "firekeep: snapshotted uncommitted work before `x`"

        def fake_post(url, body, **k):
            order.append("escalate")
            return {"decision": "block", "advisories": [{"message": "no backup"}]}

        monkeypatch.setattr(destructive, "guard", fake_guard)
        monkeypatch.setattr(transport, "post_json", fake_post)
        rc = pre_tool.run(_bash("docker compose up -d"))

        assert rc == 2
        assert order == ["guard", "escalate"]
        err = capsys.readouterr().err
        assert "snapshotted uncommitted work" in err  # the note still printed
        assert "no backup" in err

    def test_guard_still_snapshots_on_a_real_dirty_repo_with_no_bundle(
            self, client_env, monkeypatch, tmp_path):
        """End-to-end sanity with the REAL guard (no mocks): a destructive
        command on a dirty tree still snapshots and allows, bundle absent."""
        import subprocess
        from firekeep_client.hooks import pre_tool

        repo = tmp_path / "proj"
        repo.mkdir()
        git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        subprocess.run([*git, "init", "-q"], cwd=str(repo), check=False)
        (repo / "a.py").write_text("committed\n", encoding="utf-8")
        subprocess.run([*git, "add", "-A"], cwd=str(repo), check=False)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=str(repo), check=False)
        (repo / "a.py").write_text("uncommitted\n", encoding="utf-8")
        monkeypatch.setenv("FIREKEEP_SNAPSHOT_DIR", str(tmp_path / "snaps"))
        monkeypatch.chdir(repo)
        _no_network(monkeypatch)

        assert pre_tool.run({"tool_name": "Bash",
                             "tool_input": {"command": "git checkout -- ."}}) == 0
