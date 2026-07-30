"""PreCompact core: checkpoint the workspace, invalidate the shadow cursor,
stamp that a compaction happened. Best-effort, never blocking."""
from __future__ import annotations


def _record_mcp(monkeypatch):
    from firekeep_client.hooks import _mcp
    calls = []

    def fake_call(service, tool, args, **k):
        calls.append((service, tool, args))
        return {"status": "ok"}

    monkeypatch.setattr(_mcp, "call_tool", fake_call)
    return calls


class TestPrecompact:
    def test_bypass_returns_immediately_and_touches_nothing(self, client_env, monkeypatch):
        from firekeep_client import resolver
        from firekeep_client.hooks import precompact
        monkeypatch.setattr(resolver, "is_bypassed", lambda: True)
        calls = _record_mcp(monkeypatch)
        assert precompact.run({}) == {}
        assert calls == []          # personal mode must reach nothing

    def test_checkpoints_the_workspace_snapshot_to_bridge_scratch(self, client_env, monkeypatch):
        from firekeep_client.hooks import _git, precompact
        monkeypatch.setattr(_git, "workspace_snapshot", lambda *a, **k: "branch=main commits=3")
        calls = _record_mcp(monkeypatch)
        precompact.run({})
        updates = [a for s, t, a in calls if t == "ctx_update"]
        snap = [u for u in updates if u.get("key") == "workspace_snapshot"]
        assert len(snap) == 1
        assert snap[0]["category"] == "scratch"
        assert "branch=main" in snap[0]["content"]

    def test_bumps_the_shadow_epoch_via_ctx_update_not_a_new_tool(self, client_env, monkeypatch):
        from firekeep_client.hooks import precompact
        calls = _record_mcp(monkeypatch)
        precompact.run({})
        tools = {t for _, t, _ in calls}
        assert tools <= {"ctx_update"}, f"precompact must add no new tool surface: {tools}"
        epochs = [a for _, t, a in calls if t == "ctx_update" and a.get("key") == "shadow_epoch"]
        assert len(epochs) == 1
        assert epochs[0]["category"] == "scratch"

    def test_clears_the_local_shadow_cursor(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import precompact
        _record_mcp(monkeypatch)
        state.write_session_stash("tester", "personal", session_id="s1")
        state.write_shadow_cursor("tester", "personal", "cursor-abc")
        precompact.run({})
        assert state.read_shadow_cursor("tester", "personal") is None

    def test_emits_one_short_line_telling_the_agent_where_state_is(self, client_env, monkeypatch):
        from firekeep_client.hooks import precompact
        _record_mcp(monkeypatch)
        out = precompact.run({})
        assert "ctx_get_shadow" in out["systemMessage"]

    def test_never_raises_when_bridge_is_unreachable(self, client_env, monkeypatch):
        from firekeep_client.hooks import _mcp, precompact

        def boom(*a, **k):
            raise RuntimeError("bridge down")

        monkeypatch.setattr(_mcp, "call_tool", boom)
        assert precompact.run({}) == {} or isinstance(precompact.run({}), dict)

    def test_does_not_read_the_transcript_path(self, client_env, monkeypatch):
        """Pushing a raw transcript tail to the server is a privacy decision for a
        sold product, not an engineering one. Deliberately out of scope — this
        test is the guard that it stays out until that decision is made."""
        from firekeep_client.hooks import precompact
        calls = _record_mcp(monkeypatch)
        precompact.run({"transcript_path": "/tmp/should-not-be-read.jsonl"})
        blob = repr(calls)
        assert "should-not-be-read" not in blob
