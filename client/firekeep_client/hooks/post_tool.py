"""PostToolUse core — reconcile the pre-action prediction.

Ports scripts/multi-agent-postaction.sh. Resolves session_id IDENTICALLY to
pre_tool (state.resolve_session_id — the shared-resolution invariant, design
§6.2 pt 2), pops the action pre_tool queued, compares the current file sha256
against the pre-state snapshot, and POSTs the outcome to /agent/action/after.
Best-effort: ALWAYS returns 0 (never blocks the user's turn).
"""
from __future__ import annotations

import hashlib

from firekeep_client import hooklog, resolver, state, transport
from firekeep_client.hooks import never_raise

_HOOK = "post_tool"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _sha256(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


@never_raise(0)
def run(payload: dict) -> int:
    cfg = resolver.load_config()
    session_id = state.resolve_session_id(payload, cfg)

    action_id = state.pop_action(session_id)
    if not action_id:
        return 0  # no pre-hook ran for this turn — nothing to reconcile

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}

    actual_changes: list[str] = []
    deviation_notes = None
    success = True

    if tool_name in _EDIT_TOOLS:
        file_path = (tool_input.get("file_path") or tool_input.get("filePath")
                     or tool_input.get("path") or "")
        success = bool(tool_response.get("success", True))
        if file_path and success:
            new_sha = _sha256(file_path)
            old_sha = state.read_prestate(action_id) or ""
            if new_sha and new_sha != old_sha:
                actual_changes = [file_path]
    elif tool_name == "Bash":
        success = not tool_response.get("interrupted", False)
        stderr = tool_response.get("stderr", "") or ""
        if stderr:
            deviation_notes = stderr[:500]

    outcome = {"success": success, "actual_changes": actual_changes}
    if deviation_notes:
        outcome["deviation_notes"] = deviation_notes

    # Reconciled — drop the snapshot (bash parity: postaction unlinked it).
    state.delete_prestate(action_id)

    try:
        cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
        transport.post_json(
            f"{cep.rest_base}/agent/action/after",
            {"action_id": action_id, "outcome": outcome},
            headers=cep.headers, verify=cep.verify,
        )
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"/agent/action/after failed: {e}")
    return 0
