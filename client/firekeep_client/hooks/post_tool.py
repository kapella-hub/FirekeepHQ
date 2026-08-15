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
from firekeep_client.hooks import never_raise, runbooks

_HOOK = "post_tool"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}

# Keys the harness may use for the real Bash exit status in tool_response.
# Claude Code's Bash tool_response is camelCase today (`stdout`, `stderr`,
# `interrupted`, `isImage`, and `exitCode` where provided); the snake_case and
# subprocess-style spellings are tolerated so a harness change degrades to
# "found it" rather than "silently None". First PRESENT key decides.
_EXIT_STATUS_KEYS = ("exit_code", "exitCode", "exit_status", "exitStatus",
                     "returncode", "returnCode")


def _sha256(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _extract_exit_status(tool_response: object) -> int | None:
    """The REAL Bash exit status where the harness provides one, else None.

    Enforced Runbooks (spec 2026-08-15, "Allow is not success"): command
    evidence commits ONLY on a reconcile carrying exit_status == 0, so an
    absent or unparseable status must stay None — None is NEVER coerced to 0,
    because an unknown status is not success. Accepted values: int (bool is
    NOT an exit status — True must never become 1), integral float, or a
    string of digits. The first key present in the response decides; if its
    value is unparseable the answer is None, not the next key's guess."""
    if not isinstance(tool_response, dict):
        return None
    for key in _EXIT_STATUS_KEYS:
        if key not in tool_response:
            continue
        v = tool_response[key]
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v.is_integer():
            return int(v)
        if isinstance(v, str):
            try:
                return int(v.strip(), 10)
            except (ValueError, TypeError):
                return None
        return None
    return None


@never_raise(0)
def run(payload: dict) -> int:
    cfg = resolver.load_config()
    session_id = state.resolve_session_id(payload, cfg)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}

    # Pair by command hash for Bash (review 2026-08-15): parallel Bash calls
    # complete in arbitrary order, and a bare LIFO pop cross-attributed exit
    # statuses — enforcement evidence must reconcile the command that actually
    # ran. Edits pop the legacy hashless entries exactly as before.
    chash = ""
    if tool_name == "Bash":
        chash = runbooks.local_command_hash(tool_input.get("command") or "")
    action_id = state.pop_action(session_id, command_hash=chash)
    if not action_id:
        return 0  # no pre-hook entry for this tool call — nothing to reconcile

    actual_changes: list[str] = []
    deviation_notes = None
    success = True
    exit_status: int | None = None

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
        exit_status = _extract_exit_status(tool_response)
        if exit_status is not None:
            # A real exit status outranks the interrupted heuristic: a command
            # that ran to completion with a nonzero code was not a success.
            success = exit_status == 0
        else:
            success = not tool_response.get("interrupted", False)
        stderr = tool_response.get("stderr", "") or ""
        if stderr:
            deviation_notes = stderr[:500]

    outcome = {"success": success, "actual_changes": actual_changes}
    if deviation_notes:
        outcome["deviation_notes"] = deviation_notes

    # Reconciled — drop the snapshot (bash parity: postaction unlinked it).
    state.delete_prestate(action_id)

    body: dict = {"action_id": action_id, "outcome": outcome}
    if tool_name == "Bash":
        # Enforced Runbooks wire contract: ActionAfterRequest gains optional
        # `exit_status`. Sent as an explicit null when unknown — the server
        # treats unknown as not-success (attempt recorded, nothing satisfied),
        # and `success` alone no longer commits command evidence. Old servers
        # ignore the extra field. Edit reconciles keep their exact old shape.
        body["exit_status"] = exit_status

    try:
        cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
        transport.post_json(
            f"{cep.rest_base}/agent/action/after",
            body,
            headers=cep.headers, verify=cep.verify,
        )
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"/agent/action/after failed: {e}")
    return 0
