"""Enforced Runbooks Phase B — the session_start bundle handshake.

Spec ("Bundle"): the client fetches GET /procedures/bundle in its OWN REST
call — independent of the briefing, whose failure must not cost the bundle and
vice versa — stores it atomically as last-known-good, and POSTs
/procedures/bundle/ack {version} so the dashboard can report coverage honestly
("NOT ACTIVELY ENFORCED" when recent sessions lack acks). The ack is
reporting, not enforcement: its failure never costs the stored bundle.
"""
from __future__ import annotations


def _valid_bundle(version="v-abc123", workspace="ws-1"):
    return {"version": version, "workspace_id": workspace, "entries": [
        {"skill_id": "deploy-vps", "step_id": "s1", "pattern": "git push*",
         "mode": "advise", "load_bearing": False, "fail_posture": "open"},
    ]}


def _route(monkeypatch, *, bundle, briefing, posts):
    """URL-routed transport mocks: `bundle` / `briefing` are responses or
    callables that raise; every POST body lands in `posts`."""
    from firekeep_client import transport
    from firekeep_client.hooks import _mcp

    def fake_get(url, **k):
        target = bundle if url.endswith("/procedures/bundle") else briefing
        if callable(target):
            return target()
        return target

    def fake_post(url, body, **k):
        posts.append((url, body))
        return {"ok": True}

    monkeypatch.setattr(transport, "get_json", fake_get)
    monkeypatch.setattr(transport, "post_json", fake_post)
    monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})


def _boom():
    from firekeep_client import transport
    raise transport.TransportError("down")


class TestSyncBundle:
    def test_fetch_stores_and_acks(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import session_start

        posts = []
        _route(monkeypatch, bundle=_valid_bundle(), briefing={"rendered": "BRIEF"},
               posts=posts)
        out = session_start.run({"session_id": "sess-1"})

        assert out["systemMessage"].startswith("BRIEF")
        stored = state.read_runbook_bundle()
        assert stored["version"] == "v-abc123"
        assert stored["entries"][0]["skill_id"] == "deploy-vps"
        ack = [(u, b) for u, b in posts if u.endswith("/procedures/bundle/ack")]
        assert len(ack) == 1
        assert ack[0][1] == {"version": "v-abc123"}

    def test_bundle_failure_keeps_last_known_good_and_skips_ack(
            self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import session_start

        state.write_runbook_bundle(_valid_bundle(version="lkg"))
        posts = []
        _route(monkeypatch, bundle=_boom, briefing={"rendered": "BRIEF"}, posts=posts)
        out = session_start.run({})

        assert out["systemMessage"].startswith("BRIEF")  # briefing survived
        assert state.read_runbook_bundle()["version"] == "lkg"
        assert [u for u, _ in posts if "bundle/ack" in u] == []

    def test_invalid_bundle_payload_keeps_last_known_good(
            self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import session_start

        state.write_runbook_bundle(_valid_bundle(version="lkg"))
        posts = []
        # The briefing-shaped dict is what a wrong route/old server would return:
        # not a bundle, must not clobber the stored one.
        _route(monkeypatch, bundle={"rendered": "whoops"},
               briefing={"rendered": "BRIEF"}, posts=posts)
        session_start.run({})

        assert state.read_runbook_bundle()["version"] == "lkg"
        assert [u for u, _ in posts if "bundle/ack" in u] == []

    def test_ack_failure_keeps_the_stored_bundle(self, client_env, monkeypatch):
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, session_start

        def fake_get(url, **k):
            if url.endswith("/procedures/bundle"):
                return _valid_bundle(version="v-new")
            return {"rendered": "BRIEF"}

        def fail_post(url, body, **k):
            raise transport.TransportError("ack refused")

        monkeypatch.setattr(transport, "get_json", fake_get)
        monkeypatch.setattr(transport, "post_json", fail_post)
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
        out = session_start.run({})

        assert out["systemMessage"].startswith("BRIEF")
        assert state.read_runbook_bundle()["version"] == "v-new"

    def test_briefing_failure_does_not_cost_the_bundle(self, client_env, monkeypatch):
        """THE independence invariant, direction one: briefing down, bundle up."""
        from firekeep_client import state
        from firekeep_client.hooks import session_start

        posts = []
        _route(monkeypatch, bundle=_valid_bundle(version="v-kept"), briefing=_boom,
               posts=posts)
        out = session_start.run({})

        assert "Firekeep MCP servers are available" in out["systemMessage"]  # fallback
        assert state.read_runbook_bundle()["version"] == "v-kept"
        assert [u for u, _ in posts if u.endswith("/procedures/bundle/ack")]

    def test_bundle_failure_does_not_cost_the_briefing(self, client_env, monkeypatch):
        """Direction two: bundle down, briefing intact (already partially covered
        above, pinned here without a pre-seeded bundle)."""
        from firekeep_client import state
        from firekeep_client.hooks import session_start

        posts = []
        _route(monkeypatch, bundle=_boom, briefing={"rendered": "BRIEFING TEXT"},
               posts=posts)
        out = session_start.run({})

        assert out["systemMessage"].startswith("BRIEFING TEXT")
        assert state.read_runbook_bundle() is None  # nothing stored, nothing torn

    def test_sync_bundle_never_raises_even_if_state_write_explodes(
            self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import runbooks

        monkeypatch.setattr(state, "write_runbook_bundle",
                            lambda b: (_ for _ in ()).throw(RuntimeError("disk")))
        posts = []
        _route(monkeypatch, bundle=_valid_bundle(), briefing={"rendered": "x"},
               posts=posts)
        assert runbooks.sync_bundle(None) is None
