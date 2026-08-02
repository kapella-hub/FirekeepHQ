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

# Dedup window for any NON-authoritative distill marker (runtime session id, or
# the no-id sentinel). Only the Bridge stash id earns a permanent marker; see the
# enqueue block for why. Matches the session-stash TTL default (12h).
_FALLBACK_DEDUPE_TTL_SECONDS = 12 * 3600

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
    # Personal mode: skip ALL comms — no snapshot or distill enqueue reaches the
    # server for a personal session. The dispatcher routes `stop` here (rather
    # than short-circuiting it) so this stays self-handled.
    #
    # It deliberately does NOT clear the marker any more. Stop fires at EVERY
    # assistant turn end, so clearing here ended personal mode after turn 1 —
    # `/personal` protected exactly one turn and then silently rejoined team
    # logging, the opposite of what the user asked for. The documented
    # "auto-clears at session end" semantics now live in `session_end`, on the
    # event that actually means session end.
    if resolver.is_bypassed():
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
        # live: a 50-task backlog of per-turn duplicates). One per session.
        #
        # Dedup must NOT key on the stash id alone. The stash exists only once the
        # agent has called ctx_start_session, so a session that never did got
        # marker="" and `if marker and ...` short-circuited into re-enqueuing every
        # turn. Measured 2026-08-02: 193 of 200 queued tasks were per-turn duplicates
        # from unstamped sessions, while all 7 stamped sessions held exactly one each.
        # Keying dedup on a discretionary call is the hope-not-guarantee failure the
        # shim's X-Session-Id injection already fixed once.
        #
        # The runtime session id rides in the payload — no Bridge session, no network
        # call. It stays OUT of the `description` stamp deliberately: Night Shift keys
        # evidence by the BRIDGE session, so stamping a runtime id would forge a task
        # that looks distillable and is not. _safe_name() flattens it (untrusted).
        #
        # Only the authoritative stash key is permanent: reap_stale sweeps scratch by
        # declared expiry and NEVER by file age, so a permanent marker per runtime
        # session would leave one file per session forever. The no-id sentinel is
        # parenthesised so it cannot collide with a real session id.
        dedupe_id = sid or str(payload.get("session_id") or "")
        marker = f"distill_enqueued_{dedupe_id or '(none)'}@{profile}"
        marker_ttl = None if sid else _FALLBACK_DEDUPE_TTL_SECONDS
        if state.read_scratch(marker):
            return {"systemMessage": _MSG}
        resp = _mcp.call_tool("relay", "relay_task_post", task, cfg=cfg)
        # Mark only what actually landed. Relay tools report failure IN-BAND as
        # {"error": ...} at HTTP 200 — call_tool raises on JSON-RPC-level errors but
        # returns a tool-level error normally, which is why nightshift carries its own
        # _relay_ok(). Writing the marker regardless would dedup away a task that was
        # never created, losing that session's distillation for the marker's lifetime.
        if not (isinstance(resp, dict) and resp.get("error")):
            try:
                state.write_scratch(marker, "1", ttl_seconds=marker_ttl)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"distill enqueue failed: {e}")

    return {"systemMessage": _MSG}
