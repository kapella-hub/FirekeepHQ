"""SP1b §6.2: pre_tool + post_tool share temp-state AND resolve session_id
identically when it is ABSENT from the payload (the reconciliation landmine).

We do NOT monkeypatch resolve_session_id — that would mock away the thing under
test. Instead we mock the HTTP seam (transport.post_json / get_json) that the
real resolve_session_id and the gateway calls use, and let both cores run the
real resolution. Because the action pushed in pre_tool can only be popped in
post_tool when both resolve to the SAME id under the SAME FIREKEEP_CACHE_DIR, a
successful /agent/action/after POST proves the invariant end-to-end.
"""
from __future__ import annotations

import json


class TestPrePostIntegration:
    def test_shared_state_and_identical_session_resolution(
        self, client_env, monkeypatch, tmp_path
    ):
        from firekeep_client import state, transport
        from firekeep_client.hooks import _mcp, post_tool, pre_tool

        f = tmp_path / "target.py"
        f.write_text("before\n")

        # --- Mock the HTTP seams (URL-routed), NOT resolve_session_id itself. ---
        after_posts = []

        def fake_post(url, body, **k):
            if url.endswith("/agent/action/before"):
                return {"decision": "allow", "action_id": "act-int", "advisories": []}
            if url.endswith("/agent/action/after"):
                after_posts.append(body)
                return {"ok": True}
            # Bridge /mcp ctx_list_sessions — the seam resolve_session_id uses.
            return {"jsonrpc": "2.0", "id": 1, "result": {"content": [
                {"type": "text",
                 "text": json.dumps({"sessions": [{"session_id": "sess-int",
                                                   "goal": "g"}]})}]}}

        def fake_get(url, **k):
            # Bridge GET /sessions fallback shape (in case resolve uses REST).
            return {"sessions": [{"session_id": "sess-int", "goal": "g"}]}

        monkeypatch.setattr(transport, "post_json", fake_post)
        monkeypatch.setattr(transport, "get_json", fake_get)
        monkeypatch.setattr(_mcp, "call_tool", lambda service, tool, args, **k: {"held": False})

        # Payloads WITHOUT session_id -> both cores must fall back identically.
        pre_payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f)}}
        post_payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f)},
                        "tool_response": {"success": True}}

        cfg = None
        # Determinism of the shared resolver (explicit, cheap):
        assert state.resolve_session_id(pre_payload, cfg) == \
               state.resolve_session_id(post_payload, cfg)

        assert pre_tool.run(pre_payload) == 0
        # An action was queued under the resolved id (whatever it resolved to).
        f.write_text("after\n")
        assert post_tool.run(post_payload) == 0

        # The /after POST fired with the SAME action_id pre_tool pushed — only
        # possible if both cores resolved the same session_id and shared state.
        assert len(after_posts) == 1
        assert after_posts[0]["action_id"] == "act-int"
        assert after_posts[0]["outcome"]["actual_changes"] == [str(f)]

        # And it is truly consumed (shared queue, not duplicated per core).
        after_posts.clear()
        assert post_tool.run(post_payload) == 0
        assert after_posts == []
