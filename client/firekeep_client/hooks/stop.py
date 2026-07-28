"""Stop core — workspace snapshot + distill enqueue + the completion reminder.

Ports scripts/debrief.sh, minus the presence deregister.

The deregister moved to `session_end` (see that module for the full why). Short
version: Stop fires at the end of EVERY assistant turn, not once per session --
this file's own comment below records finding that live -- so deregistering here
deleted presence at the end of turn 1, and `heartbeat_presence` is update-only
and therefore could never bring it back. Everything that remains here is
correctly per-turn: the snapshot is a periodic capture, and the distill enqueue
is already deduped to once per session by a scratch marker.
"""
from __future__ import annotations

from firekeep_client import hooklog, resolver, state
from firekeep_client.hooks import _git, _mcp, never_raise

_HOOK = "stop"

_MSG = (
    "Before ending: 1. If work is DONE call ctx_complete_session with an outcome "
    "summary. If this session had a hard-won fix, a non-obvious root cause, or a "
    "reusable technique, author it as a skill NOW with skill_create(trigger, "
    "symptoms, steps, gotchas, domain) — you hold the context and a capable model; "
    "the server does not synthesize skills for you. 2. If PAUSED leave the session "
    "active — it persists. 3. Store non-obvious learnings with memory_learn. 4. "
    "Update tasks (relay_task_update) and release leases (relay_release). Do NOT "
    "skip session completion — distilled learnings improve future recall."
)


@never_raise({})
def run(payload: dict) -> dict:
    # Personal mode: auto-clear it here (the user's chosen "clears at session end"
    # semantics) and skip ALL comms — no deregister, snapshot, or distill enqueue
    # reaches the server for a personal session. The dispatcher routes `stop` here
    # (rather than short-circuiting it) precisely so this cleanup always runs.
    if resolver.is_bypassed():
        resolver.set_personal(False)
        return {}

    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)

    # NB: the session stash is deliberately NOT cleared here — Stop fires every
    # turn, so clearing would drop X-Session-Id attribution after turn 1. The
    # stash is cleared at the next session_start (top), by the bridge tap on
    # ctx_complete/abandon, or by its TTL.
    #
    # The presence deregister that used to live here is now in `session_end`,
    # along with the registration-race guard it owned. Do not restore it: Stop is
    # a per-turn event, and deregistering per-turn is the bug session_end fixes.

    # 1. Workspace snapshot -> Bridge scratch (best-effort). Correctly per-turn:
    #    this is the periodic capture session resumption reads back.
    try:
        _mcp.call_tool(
            "bridge", "ctx_update",
            {"category": "scratch", "key": "workspace_snapshot",
             "content": _git.workspace_snapshot(), "agent_id": agent},
            cfg=cfg,
        )
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"ctx_update(workspace_snapshot) failed: {e}")

    # 2. Structural capture: enqueue a distill job so the session's durable facts
    #    become memory WITHOUT depending on the agent having called memory_learn.
    #    Drained later by a client agent (Fleet-as-GPU BET, out of scope here).
    #    relay_task_post's real params (relay/app/mcp_server.py) are
    #    title/assignee/assigner/description/priority/files/context -- there is no
    #    kind/metadata field, so the distill "kind" is the title, the agent is the
    #    assigner, and the workspace snapshot rides in context. Best-effort: a relay
    #    outage is swallowed here so it never blocks session end.
    try:
        # Stamp the session_id (from the bridge tap's stash) into the task so
        # Night Shift can reconstruct the session from replay/evals. Best-effort:
        # no stash -> no stamp -> the worker completes it as a legacy task.
        task = {"title": "distill_session", "assigner": agent,
                "context": _git.workspace_snapshot()}
        sid = ""
        try:
            stash = state.read_session_stash(agent, profile)
            if stash and stash.get("session_id"):
                sid = str(stash["session_id"])
                task["description"] = f"session_id={sid}"
        except Exception:  # noqa: BLE001 — a stash problem must not drop the enqueue
            pass
        # Stop fires at EVERY assistant turn end, not once per session — without
        # a marker an N-turn session enqueues N duplicate distill tasks (found
        # live: a 50-task backlog of per-turn duplicates). One per session; the
        # scratch marker rides the same TTL'd cache the stash uses. Sessions
        # with no stamped id can't be deduped (nothing to key on) and are
        # cleared by the worker as legacy anyway.
        marker = f"distill_enqueued_{sid}@{profile}" if sid else ""
        if marker and state.read_scratch(marker):
            return {"systemMessage": _MSG}
        _mcp.call_tool("relay", "relay_task_post", task, cfg=cfg)
        if marker:
            try:
                state.write_scratch(marker, "1")
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"distill enqueue failed: {e}")

    return {"systemMessage": _MSG}
