"""PostToolUseFailure core — the event a FAILED tool call actually arrives on.

This core exists because of a measurement, not a design. Claude Code dispatches a
failed tool call to `PostToolUseFailure`, a DIFFERENT hook event from
`PostToolUse`; the client had no hook on it at all until 2026-09-13, so every
failed tool call was invisible. Measured with a sentinel: a marker written into
the escalation scratch file by a shell that is not a Claude Code tool (so no hook
could fire), then `exit 1` through the real Bash tool, then read back intact —
`post_tool` had not run. The bundle confirms both events are first-class
(`executePostToolUseHooks` and `executePostToolUseFailureHooks`).

Two consequences, and this core addresses the first:

  * `escalation`'s failure-streak trigger could never fire, because nothing ever
    told it a command had failed. That is what this hook fixes.
  * `post_tool`'s own Bash reconciliation — `exit_status`, the stderr
    `deviation_notes` — had only ever executed for SUCCESSFUL commands, and a
    failed command's queued action was never popped. That is a PRE-EXISTING
    concern in `post_tool`'s territory, deliberately NOT folded in here: this
    core is kept single-purpose so its blast radius is one counter.

The payload differs from PostToolUse in the one way that matters: there is no
`tool_response`. The failure carries `error` (a string) and `is_interrupt`
(optional bool). An interrupt is the user cancelling a tool call — the work did
not go wrong, it was abandoned — so it is excluded rather than counted as
evidence that the agent is stuck.

Never raises, always returns 0. A hook that cannot block must not be able to fail
either: counting is best-effort bookkeeping on a path the user is waiting on.
"""
from __future__ import annotations

from firekeep_client import escalation, resolver, state
from firekeep_client.hooks import never_raise

_HOOK = "post_tool_failure"


def _file_path(tool_input: object) -> str:
    """The edited file, from whichever key the harness used. Tolerant of a
    non-dict `tool_input` — a malformed payload costs a path, never the hook."""
    if not isinstance(tool_input, dict):
        return ""
    return (tool_input.get("file_path") or tool_input.get("filePath")
            or tool_input.get("path") or "")


@never_raise(0)
def run(payload: dict) -> int:
    cfg = resolver.load_config()
    session_id = state.resolve_session_id(payload, cfg)

    if payload.get("is_interrupt"):
        return 0

    tool_name = payload.get("tool_name") or ""
    escalation.record_outcome(
        session_id,
        tool_name=str(tool_name),
        file_path=_file_path(payload.get("tool_input")),
        success=False,
    )
    return 0
