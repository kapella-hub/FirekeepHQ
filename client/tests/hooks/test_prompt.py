"""SP1b hook core: prompt — poll tasks/channel, heartbeat, 5th-prompt snapshot.

News-only contract (2026-07-14 rewrite after the raw-JSON-every-prompt field
complaint): channel messages render only when NEWER than the last shown and
never when self-sent; pending tasks only when the set changes; everything one
compact line per item.
"""
from __future__ import annotations


def _patch_transport(monkeypatch, *, tasks=None, sessions=None):
    from firekeep_client import transport

    task_rows = tasks or []

    def fake_get(url, **k):
        if "/tasks" in url:
            return {"count": len(task_rows), "tasks": task_rows}
        if "/sessions" in url:
            return {"sessions": sessions or []}
        return {}

    monkeypatch.setattr(transport, "get_json", fake_get)


def _record_mcp(monkeypatch, *, messages=None):
    from firekeep_client.hooks import _mcp
    calls = []
    rows = messages or []

    def fake_call(service, tool, args, **k):
        calls.append((tool, args))
        if tool == "relay_get_messages":
            return {"count": len(rows), "messages": rows}
        return {}

    monkeypatch.setattr(_mcp, "call_tool", fake_call)
    return calls


class TestPrompt:
    def test_empty_inbox_returns_no_message(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(monkeypatch, sessions=[{"session_id": "s1", "goal": "g"}])
        calls = _record_mcp(monkeypatch)
        out = prompt.run({})
        assert out == {}
        assert "relay_heartbeat_presence" in [t for t, _ in calls]

    def test_new_pending_tasks_render_compactly(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(
            monkeypatch,
            tasks=[{"id": "task-1", "title": "fix ingress"},
                   {"id": "task-2", "title": "ship 0.1.13"}],
            sessions=[{"session_id": "s1", "goal": "g"}],
        )
        _record_mcp(monkeypatch)
        out = prompt.run({})
        msg = out["systemMessage"]
        assert msg.startswith("[relay] ")
        assert "pending tasks (2):" in msg
        assert "- fix ingress [task-1]" in msg
        assert "{" not in msg  # never raw JSON again

    def test_unchanged_pending_tasks_are_silent_on_the_next_prompt(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(
            monkeypatch,
            tasks=[{"id": "task-1", "title": "fix ingress"}],
            sessions=[{"session_id": "s1", "goal": "g"}],
        )
        _record_mcp(monkeypatch)
        assert "pending tasks" in prompt.run({})["systemMessage"]
        assert prompt.run({}) == {}  # same set -> no re-injection

    def test_unchanged_pending_tasks_are_re_announced_once_the_digest_ttl_lapses(
            self, client_env, monkeypatch):
        """The suppression digest key carries no session component, so before it
        declared a TTL an unchanged pending-task set was suppressed FOREVER —
        across every future session on that machine. The customer silently
        stopped being told about their own tasks, and only a CHANGE to the task
        set could break the silence."""
        import time

        from firekeep_client import state
        from firekeep_client.hooks import prompt

        _patch_transport(
            monkeypatch,
            tasks=[{"id": "task-1", "title": "fix ingress"}],
            sessions=[{"session_id": "s1", "goal": "g"}],
        )
        _record_mcp(monkeypatch)
        assert "pending tasks" in prompt.run({})["systemMessage"]
        assert prompt.run({}) == {}          # suppressed while fresh

        digests = list((state.cache_dir() / "scratch").glob("tasks_digest_*"))
        assert len(digests) == 1, f"expected one digest marker, found {digests}"
        key = digests[0].name
        assert state._scratch_ttl_file(key).exists(), (
            "the digest declared no expiry — it would suppress the customer's "
            "tasks forever"
        )
        state._scratch_ttl_file(key).write_text(str(time.time() - 1), encoding="utf-8")

        assert "pending tasks" in prompt.run({})["systemMessage"]

    def test_channel_shows_only_new_messages_and_never_self(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(monkeypatch, sessions=[{"session_id": "s1", "goal": "g"}])
        _record_mcp(monkeypatch, messages=[
            {"content": "New task: distill_session", "sender": "tester", "timestamp": 100.0},
            {"content": "review my MR please", "sender": "colleague", "timestamp": 99.0},
        ])
        out = prompt.run({})
        msg = out["systemMessage"]
        # self-broadcast filtered; the colleague's message shows once
        assert "distill_session" not in msg
        assert "- review my MR please — colleague" in msg
        # second run: nothing newer -> silent
        assert prompt.run({}) == {}

    def test_channel_duplicates_collapse(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(monkeypatch, sessions=[{"session_id": "s1", "goal": "g"}])
        _record_mcp(monkeypatch, messages=[
            {"content": "New task: distill_session", "sender": "other", "timestamp": float(i)}
            for i in range(1, 6)
        ])
        msg = prompt.run({})["systemMessage"]
        assert msg.count("distill_session") == 1
        assert "(x5)" in msg

    def test_heartbeat_carries_session_id_and_goal(self, client_env, monkeypatch):
        from firekeep_client.hooks import prompt
        _patch_transport(monkeypatch, sessions=[{"session_id": "sX", "goal": "the goal"}])
        calls = _record_mcp(monkeypatch)
        prompt.run({})
        hb = next(a for t, a in calls if t == "relay_heartbeat_presence")
        assert hb["session_id"] == "sX"
        assert hb["goal"] == "the goal"

    def test_snapshot_on_fifth_prompt(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import prompt
        state.write_scratch("poll_count_tester", "4")  # this call -> 5
        _patch_transport(monkeypatch, sessions=[{"session_id": "s1", "goal": "g"}])
        calls = _record_mcp(monkeypatch)
        prompt.run({})
        assert "ctx_update" in [t for t, _ in calls]

    def test_no_snapshot_off_cycle(self, client_env, monkeypatch):
        from firekeep_client import state
        from firekeep_client.hooks import prompt
        state.write_scratch("poll_count_tester", "1")  # this call -> 2
        _patch_transport(monkeypatch, sessions=[{"session_id": "s1", "goal": "g"}])
        calls = _record_mcp(monkeypatch)
        prompt.run({})
        assert "ctx_update" not in [t for t, _ in calls]
