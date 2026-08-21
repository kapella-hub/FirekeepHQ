"""UserPromptSubmit core — poll tasks + tasks-channel, heartbeat, 5th snapshot,
proactive recall.

Ports scripts/multi-agent-poll.sh: poll pending tasks (Relay GET /tasks REST) and
the 'tasks' channel (relay_get_messages MCP), refresh presence heartbeat with the
active session_id+goal (Bridge GET /sessions REST), and every 5th prompt push a
git workspace snapshot to Bridge scratch. Returns a systemMessage ONLY when there
is NEWS — not merely a non-empty inbox: channel messages are filtered to
newer-than-last-shown (and never the agent's own broadcasts echoed back), pending
tasks only re-render when the set actually changes, and everything is one compact
line per item, duplicates collapsed. The raw-JSON-every-prompt behavior this
replaces re-injected the same five stale messages into context on every single
user message (field complaint, 2026-07-14).

The same discipline governs the newest addition, proactive recall
(`firekeep_client.promptrecall`): the prompt is embedded against team memory and
the few relevant, not-yet-seen memories join THIS systemMessage after the relay
content — or, far more often, nothing does. Both halves are optional, so the hook
returns {} only when both are silent.
"""
from __future__ import annotations

import hashlib
import urllib.parse

from firekeep_client import (
    hooklog,
    promptrecall,
    resolver,
    state,
    transport,
    worktree_snapshot,
)
from firekeep_client.hooks import _git, _mcp, never_raise

_HOOK = "prompt"

# The pending-task suppression digest is keyed by agent with no session
# component, so without an expiry an UNCHANGED task set stays suppressed across
# every future session on the machine — the customer silently stops being told
# about their own tasks, and only a change to the set can break the silence.
# Twelve hours matches the session-stash and personal-mode backstops: long enough
# that a working session is never re-nagged, short enough that the suppression
# cannot outlive the session that earned it.
_TASKS_DIGEST_TTL_SECONDS = 12 * 3600


def _dedup_lines(items: list[str]) -> list[str]:
    """Collapse consecutive-or-not duplicate lines into 'line (xN)'."""
    counts: dict[str, int] = {}
    order: list[str] = []
    for it in items:
        if it not in counts:
            order.append(it)
        counts[it] = counts.get(it, 0) + 1
    return [f"{it} (x{counts[it]})" if counts[it] > 1 else it for it in order]


def _task_line(task: dict) -> str:
    # The title is server-supplied free text with no length contract, so it is
    # trimmed to promptrecall's shared line budget. The id and creator stay
    # OUTSIDE the trim: a title over 200 chars is already a description, and the
    # id is how the agent asks relay_task_list for the rest. Pointer, not payload.
    title = promptrecall.trim_line(
        task.get("title") or task.get("description") or task.get("id") or "task"
    )
    tid = str(task.get("id") or "")
    creator = str(task.get("creator") or task.get("created_by") or "")
    line = f"- {title}"
    if tid:
        line += f" [{tid}]"
    if creator:
        line += f" (from {creator})"
    return line


@never_raise({})
def run(payload: dict) -> dict:
    cfg = resolver.load_config()
    agent = resolver.agent_id(cfg)

    inbox = []

    # 1. Pending tasks (Relay GET /tasks REST). Rendered ONLY when the pending
    # set changes — a stable todo list re-injected on every prompt is noise
    # (the briefing already surfaces it at session start).
    try:
        rep = resolver.resolve("relay", cfg=cfg)
        qs = urllib.parse.urlencode({"assignee": agent, "status": "pending", "limit": 5})
        tasks = transport.get_json(
            f"{rep.rest_base}/tasks?{qs}", headers=rep.headers, verify=rep.verify
        )
        rows = tasks.get("tasks", []) if isinstance(tasks, dict) else []
        if rows:
            digest = hashlib.sha256(
                "|".join(sorted(str(t.get("id", t)) for t in rows)).encode()
            ).hexdigest()[:16]
            digest_key = f"tasks_digest_{agent}"
            if state.read_scratch(digest_key) != digest:
                state.write_scratch(digest_key, digest,
                                    ttl_seconds=_TASKS_DIGEST_TTL_SECONDS)
                lines = _dedup_lines([_task_line(t) for t in rows])
                inbox.append(f"pending tasks ({len(rows)}):\n" + "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"GET /tasks failed: {e}")

    # 2. 'tasks' channel (relay_get_messages MCP — no REST route). Show only
    # messages NEWER than the last one already shown, and never the agent's own
    # broadcasts echoed back.
    try:
        msgs = _mcp.call_tool("relay", "relay_get_messages",
                              {"channel": "tasks", "limit": 5}, cfg=cfg)
        rows = msgs.get("messages", []) if isinstance(msgs, dict) else []
        seen_key = f"channel_seen_{agent}"
        raw_seen = state.read_scratch(seen_key)
        try:
            last_seen = float(raw_seen) if raw_seen else 0.0
        except ValueError:
            last_seen = 0.0
        fresh = []
        newest = last_seen
        for m in rows:
            try:
                ts = float(m.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            newest = max(newest, ts)
            if ts <= last_seen or str(m.get("sender") or "") == agent:
                continue
            sender = str(m.get("sender") or "?")
            # Same contract as _task_line: trim the body, keep the sender.
            content = promptrecall.trim_line(m.get("content"))
            fresh.append(f"- {content} — {sender}")
        if newest > last_seen:
            state.write_scratch(seen_key, repr(newest))
        if fresh:
            inbox.append(f"new channel messages ({len(fresh)}):\n"
                         + "\n".join(_dedup_lines(fresh)))
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"relay_get_messages failed: {e}")

    # 3. Active session_id + goal (Bridge GET /sessions REST) for the heartbeat.
    session_id, goal = "", ""
    try:
        bep = resolver.resolve("bridge", cfg=cfg)
        qs = urllib.parse.urlencode({"status": "active", "agent_id": agent, "limit": 1})
        sess = transport.get_json(
            f"{bep.rest_base}/sessions?{qs}", headers=bep.headers, verify=bep.verify
        )
        rows = sess.get("sessions", []) if isinstance(sess, dict) else []
        if rows:
            session_id = rows[0].get("session_id", "") or ""
            goal = rows[0].get("goal", "") or ""
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"GET /sessions failed: {e}")

    # 4. Heartbeat presence (best-effort).
    try:
        hb = {"agent_id": agent}
        if session_id:
            hb["session_id"] = session_id
        if goal:
            hb["goal"] = goal
        _mcp.call_tool("relay", "relay_heartbeat_presence", hb, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"relay_heartbeat_presence failed: {e}")

    # 5. Every 5th prompt: workspace snapshot -> Bridge scratch.
    try:
        key = f"poll_count_{agent}"
        raw = state.read_scratch(key)
        count = (int(raw) if raw and raw.isdigit() else 0) + 1
        state.write_scratch(key, str(count))
        if count % 5 == 0:
            # LOCAL, first and unconditionally: content, not `--stat`. The Bridge
            # payload below is `git diff --stat` — it records that work exists and its
            # exact size and none of its content, which is why 2026-08-02's loss was
            # unrecoverable. This one is a real copy, stays on the machine (a raw diff
            # holds whatever was being edited), and is not gated on session_id: work is
            # worth preserving whether or not the agent ever called ctx_start_session.
            root = worktree_snapshot.repo_root()
            if root is not None:
                worktree_snapshot.capture(root, reason="periodic (every 5th prompt)")
            if session_id:
                _mcp.call_tool(
                    "bridge", "ctx_update",
                    {"category": "scratch", "key": "workspace_snapshot",
                     "content": _git.workspace_snapshot(), "agent_id": agent},
                    cfg=cfg,
                )
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"snapshot failed: {e}")

    # 6. Proactive recall: embed THIS prompt against team memory and inject the
    # few genuinely relevant, not-yet-seen memories (firekeep_client.promptrecall).
    # Same news-only discipline as everything above it, and the same channel — the
    # user sees what the model was handed. Bounded and fail-open: nudge() never
    # raises and returns "" for every failure mode, so a slow or dead cortex costs
    # the hook nothing but its own timeout.
    recall_block = promptrecall.nudge(cfg, payload)

    if not inbox and not recall_block:
        return {}
    parts = []
    if inbox:
        parts.append("[relay] " + "\n".join(inbox))
    if recall_block:
        parts.append(recall_block)
    return {"systemMessage": "\n\n".join(parts)}
