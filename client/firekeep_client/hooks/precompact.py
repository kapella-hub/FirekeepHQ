"""PreCompact core — checkpoint before the context is compacted.

Claude is the only runtime that exposes a compaction event. Scope is deliberately
narrow: this hook fires BEFORE compaction but cannot read the agent's unstated
reasoning, so it CANNOT recover decisions the agent never wrote via ctx_update.
It does four cheap, certain things: checkpoint the workspace, invalidate the
shadow cursor (locally and server-side), stamp that a compaction happened, and
tell the agent in one line where its working state lives.

Budgeted like session_start (~15s) and best-effort throughout: a slow hook
stalls the customer mid-compaction, which is worse than a missed checkpoint.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from firekeep_client import hooklog, resolver
from firekeep_client.hooks import _git, _mcp, never_raise

_HOOK = "precompact"

_NOTICE = (
    "Context was compacted. Your plan, decisions, file knowledge and progress are "
    "in Bridge — call ctx_get_shadow() to restore them before asking the user to repeat anything."
)


# never_raise takes ONE argument: the safe default. The hook name for
# hooklog.log_failure is derived from the wrapped function's module.
@never_raise({})
def run(payload: dict) -> dict:
    # 1. Bypass gate FIRST — before any config resolution or network call.
    if resolver.is_bypassed():
        return {}

    # Identity resolution copied verbatim from hooks/session_start.py:73-75.
    # Called UNGUARDED on purpose: a malformed config raises ConfigError, which
    # @never_raise degrades to {} rather than crashing the caller.
    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)

    # 2. Workspace checkpoint — cheap, real, already implemented.
    try:
        snapshot = _git.workspace_snapshot()
        if snapshot:
            _mcp.call_tool("bridge", "ctx_update", {
                "category": "scratch", "key": "workspace_snapshot",
                "content": snapshot, "agent_id": agent,
            }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"workspace checkpoint failed: {e}")

    # 3. Invalidate the shadow cursor server-side. Load-bearing for the
    #    residency contract: after compaction the agent can no longer vouch
    #    for what is still in its context, so any cursor it holds is stale —
    #    bumping shadow_epoch makes Bridge's filter_since refuse it on the next
    #    ctx_get_shadow. Rides on ordinary ctx_update — no new MCP tool.
    try:
        _mcp.call_tool("bridge", "ctx_update", {
            "category": "scratch", "key": "shadow_epoch",
            "content": str(int(time.time() * 1000)), "agent_id": agent,
        }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"epoch bump failed: {e}")

    # 4. Stamp that a compaction occurred. This lands in the session scratch, so
    #    it is visible in every subsequent shadow restore — including the delta,
    #    which always sends scratch in full. See the note below on consumers.
    try:
        _mcp.call_tool("bridge", "ctx_update", {
            "category": "scratch", "key": "compacted_at",
            "content": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent,
        }, cfg=cfg)
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"compacted_at stamp failed: {e}")

    return {"systemMessage": _NOTICE}
