"""Hook core: session_end — presence deregister at REAL session end.

The race-guard cases here moved verbatim from test_stop.py when the deregister
moved off Stop. Stop fires at every assistant turn end, so deregistering there
deleted presence after turn 1 and heartbeat (update-only) could not restore it.
"""
from __future__ import annotations

import time

_MARK = "presence_registered_tester@personal"


def _record_calls(monkeypatch):
    from firekeep_client.hooks import _mcp
    calls = []
    monkeypatch.setattr(_mcp, "call_tool",
                        lambda service, tool, args, **k: (calls.append((tool, args)) or {}))
    return calls


class TestSessionEnd:
    def test_deregisters_when_no_prior_registration(self, client_env, monkeypatch):
        from firekeep_client.hooks import session_end
        calls = _record_calls(monkeypatch)
        session_end.run({})
        assert "relay_deregister" in [t for t, _ in calls]

    def test_deregisters_when_registration_is_stale(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import session_end
        state.write_scratch(_MARK, str(int(time.time()) - 60))
        calls = _record_calls(monkeypatch)
        session_end.run({})
        assert "relay_deregister" in [t for t, _ in calls]

    def test_skips_deregister_within_race_window(self, client_env, monkeypatch):
        """A newer session registered under this agent_id moments ago — tearing
        its presence down would drop a live session offline."""
        from firekeep_client import state
        from firekeep_client.hooks import session_end
        state.write_scratch(_MARK, str(int(time.time())))
        calls = _record_calls(monkeypatch)
        session_end.run({})
        assert "relay_deregister" not in [t for t, _ in calls]

    def test_consumes_the_mark_either_way(self, client_env, monkeypatch):
        """Consumed whether or not we deregistered — parity with the original
        stop.py behaviour, so a mark can never outlive the session that wrote it."""
        from firekeep_client import state
        from firekeep_client.hooks import session_end
        state.write_scratch(_MARK, str(int(time.time())))   # the SKIP path
        _record_calls(monkeypatch)
        session_end.run({})
        assert state.read_scratch(_MARK) is None

    def test_returns_no_system_message(self, client_env, monkeypatch):
        """The session is over; there is nobody left to read a systemMessage."""
        from firekeep_client.hooks import session_end
        _record_calls(monkeypatch)
        assert session_end.run({}) == {}

    def test_personal_mode_sends_nothing(self, client_env, monkeypatch):
        """Bypass is self-handled: no Relay comms leave a personal session."""
        from firekeep_client import resolver, state
        from firekeep_client.hooks import session_end
        monkeypatch.setattr(resolver, "is_bypassed", lambda: True)
        state.write_scratch(_MARK, str(int(time.time()) - 60))  # would otherwise deregister
        calls = _record_calls(monkeypatch)
        session_end.run({})
        assert calls == []
        assert state.read_scratch(_MARK) is not None   # registration mark untouched

    def test_personal_mode_auto_clears_the_marker(self, client_env, monkeypatch):
        """The documented "auto-clears at session end" semantics, on the event that
        actually means session end. This moved off `stop`, which fires every turn
        and therefore ended personal mode after turn 1."""
        from firekeep_client import resolver
        from firekeep_client.hooks import session_end

        resolver.set_personal(True)
        assert resolver.is_personal() is True
        calls = _record_calls(monkeypatch)

        session_end.run({})

        assert calls == []                               # still no comms
        assert resolver.is_personal() is False
        assert not resolver.personal_marker_path().exists()

    def test_never_raises(self, client_env, monkeypatch):
        """@never_raise({}) — a hook must never break the caller's process."""
        from firekeep_client.hooks import _mcp, session_end

        def boom(*a, **k):
            raise RuntimeError("relay down")

        monkeypatch.setattr(_mcp, "call_tool", boom)
        assert session_end.run({}) == {}


class TestDispatcherWiring:
    def test_registered_as_a_dict_core(self):
        from firekeep_client.hooks import __main__ as dispatcher
        assert "session_end" in dispatcher._CORE_MODULES
        assert "session_end" in dispatcher._DICT_CORES
        assert "session_end" not in dispatcher._INT_CORES

    def test_exempt_from_the_bypass_short_circuit(self):
        """It self-handles bypass. If the dispatcher short-circuited it, it would
        print _BYPASS_MSG at a session nobody is reading any more."""
        from firekeep_client.hooks import __main__ as dispatcher
        assert "session_end" in dispatcher._BYPASS_EXEMPT
        assert "stop" in dispatcher._BYPASS_EXEMPT

    def test_claude_adapter_wires_sessionend(self):
        from firekeep_client.adapters.claude import CLAUDE_HOOKS
        events = {e: core for e, core, _m, _t in CLAUDE_HOOKS}
        assert events["SessionEnd"] == "session_end"
        assert events["Stop"] == "stop"      # Stop still wired, just no longer deregistering
