"""SP1b hook cores: the _mcp.call_tool helper + the session_start core."""
from __future__ import annotations

import json

import pytest


class TestMcpCallTool:
    def test_call_tool_unwraps_text_content(self, client_env, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import _mcp

        seen = {}

        def fake_post(url, body, **k):
            seen["url"] = url
            seen["name"] = body["params"]["name"]
            seen["args"] = body["params"]["arguments"]
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps({"status": "registered"})}]}}

        monkeypatch.setattr(transport, "post_json", fake_post)
        out = _mcp.call_tool("relay", "relay_register", {"agent_id": "x"})
        assert out == {"status": "registered"}
        assert seen["url"] == "http://127.0.0.1:8050/mcp"
        assert seen["name"] == "relay_register"
        assert seen["args"] == {"agent_id": "x"}

    def test_call_tool_raises_on_inband_error(self, client_env, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import _mcp

        monkeypatch.setattr(transport, "post_json", lambda *a, **k: {
            "jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})
        with pytest.raises(transport.TransportError):
            _mcp.call_tool("relay", "relay_register", {"agent_id": "x"})


class TestSessionStart:
    def test_returns_rendered_briefing_and_registers(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, session_start

        cap = {}

        def fake_get(url, **k):
            cap["url"] = url
            cap["headers"] = k.get("headers")
            return {"rendered": "=== PRE-FLIGHT BRIEFING ===\nhi", "degraded": False}

        reg = {}

        def fake_call(service, tool, args, **k):
            reg["service"] = service
            reg["tool"] = tool
            reg["args"] = args
            return {"status": "registered"}

        monkeypatch.setattr(transport, "get_json", fake_get)
        monkeypatch.setattr(_mcp, "call_tool", fake_call)

        out = session_start.run({})
        assert out["systemMessage"].startswith("=== PRE-FLIGHT BRIEFING ===")
        assert cap["url"].startswith("http://127.0.0.1:8100/briefing?")
        assert "agent_id=tester" in cap["url"]
        assert cap["headers"]["X-Agent-Id"] == "tester"
        assert reg["service"] == "relay"
        assert reg["tool"] == "relay_register"
        assert reg["args"]["agent_id"] == "tester"
        # registration epoch pinned for stop.py's race guard
        assert state.read_scratch("presence_registered_tester@personal") is not None

    def test_falls_back_when_briefing_unreachable(self, client_env, monkeypatch):
        from firekeep_client import transport
        from firekeep_client.hooks import _mcp, session_start

        def boom(*a, **k):
            raise transport.TransportError("cortex down")

        monkeypatch.setattr(transport, "get_json", boom)
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {"status": "registered"})
        out = session_start.run({})
        assert "Firekeep MCP servers are available" in out["systemMessage"]

    def test_briefing_id_stashed_and_stale_cleared(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, session_start

        # A leftover stash from a previous session must be cleared, not inherited.
        state.write_session_stash("tester", "personal", session_id="OLD-sess")
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"rendered": "BRIEF", "briefing_id": "brf-xyz"})
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})

        session_start.run({})

        stash = state.read_session_stash("tester", "personal")
        assert stash["briefing_id"] == "brf-xyz"
        assert "session_id" not in stash  # OLD-sess was cleared before the write

    def test_stale_stash_cleared_even_without_briefing_id(self, client_env, monkeypatch):
        """A new session must never inherit a crashed session's stale id — the
        clear is unconditional at the top, not gated on briefing_id presence."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, session_start

        state.write_session_stash("tester", "personal", session_id="STALE-crashed")
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"rendered": "BRIEF"})  # no briefing_id
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
        session_start.run({})
        assert state.read_session_stash("tester", "personal") is None

    def test_stale_stash_cleared_even_when_briefing_fetch_fails(self, client_env, monkeypatch):
        """Cortex down at start must not let a stale id ride the new session."""
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, session_start

        state.write_session_stash("tester", "personal", session_id="STALE-crashed")

        def boom(*a, **k):
            raise transport.TransportError("cortex down")

        monkeypatch.setattr(transport, "get_json", boom)
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
        session_start.run({})
        assert state.read_session_stash("tester", "personal") is None


class TestUpdateNudge:
    """Board 2026-07-14: 'briefing nudge' — one line when a newer client exists,
    at most one manifest fetch per day, silence on every failure shape."""

    def _fake_env(self, monkeypatch, *, latest):
        from firekeep_client import autoupdate, transport, updater
        from firekeep_client.hooks import _mcp
        monkeypatch.setattr(transport, "get_json",
                            lambda url, **k: {"rendered": "BRIEFING"})
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
        monkeypatch.setattr(updater, "dist_base", lambda cfg: "https://reg.example")
        # Never launch a REAL background `firekeep update` from a test — record instead.
        # Return True (an update is 'in flight'), which is what drives the message.
        self.spawn_calls = []

        def _spawn(cfg, latest, today):
            self.spawn_calls.append((latest, today))
            return True

        monkeypatch.setattr(autoupdate, "maybe_spawn", _spawn)
        calls = []

        class _M:
            version = latest

        def fake_fetch(base, **k):
            calls.append(base)
            if latest is None:
                raise OSError("dist host unreachable")
            return _M()

        monkeypatch.setattr(updater, "fetch_manifest", fake_fetch)
        return calls

    def test_auto_update_runs_in_background_by_default(self, client_env, monkeypatch):
        # Default (auto-update ON): a background update is spawned and the message
        # says so — NOT the manual 'run: firekeep update' nudge.
        from firekeep_client.hooks import session_start
        self._fake_env(monkeypatch, latest="99.0.0")
        out = session_start.run({})
        assert "updating client in background" in out["systemMessage"]
        assert "99.0.0" in out["systemMessage"]
        assert self.spawn_calls == [("99.0.0", __import__("datetime").date.today().isoformat())]

    def test_manual_nudge_when_auto_update_opted_out(self, client_env, monkeypatch):
        from firekeep_client.hooks import session_start
        monkeypatch.setenv("FIREKEEP_NO_AUTO_UPDATE", "1")
        self._fake_env(monkeypatch, latest="99.0.0")
        out = session_start.run({})
        assert "client update available" in out["systemMessage"]
        assert "run: firekeep update" in out["systemMessage"]
        assert self.spawn_calls == [], "opted out: no background update may spawn"

    def test_no_nudge_when_current(self, client_env, monkeypatch):
        from firekeep_client import __version__
        from firekeep_client.hooks import session_start
        self._fake_env(monkeypatch, latest=__version__)
        out = session_start.run({})
        assert "update available" not in out["systemMessage"]
        assert "updating client" not in out["systemMessage"]
        assert self.spawn_calls == []  # nothing newer -> no spawn

    def test_manifest_fetched_at_most_once_per_day(self, client_env, monkeypatch):
        from firekeep_client.hooks import session_start
        calls = self._fake_env(monkeypatch, latest="99.0.0")
        session_start.run({})
        session_start.run({})
        session_start.run({})
        assert len(calls) == 1  # cached — one fetch, three messages
        assert "99.0.0" in session_start.run({})["systemMessage"]

    def test_fetch_failure_is_silent_and_cached(self, client_env, monkeypatch):
        from firekeep_client.hooks import session_start
        calls = self._fake_env(monkeypatch, latest=None)  # fetch raises
        out = session_start.run({})
        assert "update available" not in out["systemMessage"]
        session_start.run({})
        assert len(calls) == 1  # failure cached: no retry storm on a dead host
