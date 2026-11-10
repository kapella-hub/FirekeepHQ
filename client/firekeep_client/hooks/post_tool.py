"""PostToolUse / PostToolUseFailure core — report how a tool call ended.

Ports scripts/multi-agent-postaction.sh. Resolves session_id IDENTICALLY to
pre_tool (state.resolve_session_id — the shared-resolution invariant, design
§6.2 pt 2), pops the action pre_tool queued, compares the current file sha256
against the pre-state snapshot, and POSTs the outcome to /agent/action/after.
Best-effort: ALWAYS returns 0 (never blocks the user's turn).

Two things were added 2026-11-09, because the outcome signal this hook exists
to produce was measured empty (50 evaluated sessions, zero recorded failures):

  * FAILURES. Claude Code fires PostToolUse only when a tool SUCCEEDS; a
    command that exits non-zero fires PostToolUseFailure instead, which the
    kit never registered. So every command this hook ever saw had succeeded,
    and `tool_response` carries no exit code to say otherwise. The adapter
    now routes PostToolUseFailure here too (`hook_event_name` tells them
    apart) and the exit status is read from the event's `error` text.
  * UNGATED COMMANDS. pre_tool keeps ordinary shell commands off the network
    — only runbook-matched ones reach /agent/action/before — so most commands
    had no queued action and this hook returned without reporting anything.
    Those now go to POST /agent/action/observed: one event carrying the
    program name and exit status, never the command line.
"""
from __future__ import annotations

import hashlib
import re

from firekeep_client import hooklog, resolver, state, transport
from firekeep_client.hooks import never_raise, runbooks
from firekeep_client.hooks.pre_tool import COMMAND_TOOLS

_HOOK = "post_tool"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
_FAILURE_EVENT = "PostToolUseFailure"

# Keys the harness may use for the real Bash exit status in tool_response.
# Claude Code's Bash tool_response is camelCase today (`stdout`, `stderr`,
# `interrupted`, `isImage`, and `exitCode` where provided); the snake_case and
# subprocess-style spellings are tolerated so a harness change degrades to
# "found it" rather than "silently None". First PRESENT key decides.
_EXIT_STATUS_KEYS = ("exit_code", "exitCode", "exit_status", "exitStatus",
                     "returncode", "returnCode")

# PostToolUseFailure's `error` for a shell command opens "Exit code N".
_ERROR_EXIT_RE = re.compile(r"\s*Exit code (-?\d{1,5})\b")

# The observed-outcome report is the only network call on the path of an
# ordinary command, so it is bounded hard and backs off on its own.
OBSERVED_TIMEOUT = 2.0
_OBSERVED_PAUSE_KEY = "observed_report_paused"
_PAUSE_UNSUPPORTED_SECONDS = 24 * 3600.0   # server predates the endpoint
_PAUSE_UNREACHABLE_SECONDS = 300.0         # server slow or down
_PROGRAM_MAX = 60
# Leading tokens that are not the program being run.
_WRAPPER_TOKENS = frozenset({"&", ".", "sudo", "time", "nohup", "exec", "command"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TOKEN_RE = re.compile(r"\"([^\"]*)\"|'([^']*)'|(\S+)")


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


def _exit_status_from_error(error: object) -> int | None:
    """Exit status from a PostToolUseFailure `error` string, else None.

    Only a leading "Exit code N" counts. A failure that reports no code (a
    timeout, a tool that threw) stays None — and a parsed 0 is refused, since
    a failure event cannot have succeeded and 0 is what commits runbook
    evidence."""
    if not isinstance(error, str):
        return None
    m = _ERROR_EXIT_RE.match(error)
    if not m:
        return None
    code = int(m.group(1))
    return code if code != 0 else None


def _program(command: object) -> str:
    """The program a command line runs — its first real token, basename only.

    This is ALL of the command that leaves the machine on the observed path:
    arguments carry paths, hostnames and tokens, and nothing downstream needs
    them to tell a failing test run from a failing deploy. "" when it cannot
    be worked out; never raises."""
    try:
        if not isinstance(command, str):
            return ""
        # A quoted token is one token: "C:\Program Files\Git\cmd\git.exe".
        for m in _TOKEN_RE.finditer(command):
            token = next(g for g in m.groups() if g is not None).strip("`()")
            if not token or token in _WRAPPER_TOKENS or _ENV_ASSIGNMENT_RE.match(token):
                continue
            name = token.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            return name[:_PROGRAM_MAX]
        return ""
    except Exception:  # noqa: BLE001
        return ""


def _report_observed(cfg, command: object, *, success: bool,
                     exit_status: int | None) -> None:
    """POST one ungated command outcome to /agent/action/observed.

    Sent only while the agent has a KEEP session — that is the id evals read,
    and an outcome filed anywhere else is a round trip nobody benefits from.
    Never raises. A server that predates the endpoint (404/405) pauses
    reporting for a day and one that cannot be reached for five minutes, so
    an old or slow Keep costs one failed call, not one per command."""
    try:
        if state.read_scratch(_OBSERVED_PAUSE_KEY):
            return
        agent = resolver.agent_id(cfg)
        keep_sid = state.keep_session_id(agent)
        if not keep_sid:
            return
        cep = resolver.resolve("cortex", cfg=cfg, session_id=keep_sid)
        transport.post_json(
            f"{cep.rest_base}/agent/action/observed",
            {"session_id": keep_sid, "agent_id": agent, "adapter": "shell-hook",
             "action": {"type": "run_command", "target": _program(command)},
             "outcome": {"success": success},
             "exit_status": exit_status},
            headers=cep.headers, verify=cep.verify, timeout=OBSERVED_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        status = getattr(e, "status", None)
        if status in (404, 405):
            pause, why = _PAUSE_UNSUPPORTED_SECONDS, "server has no /agent/action/observed"
        elif status is None:
            pause, why = _PAUSE_UNREACHABLE_SECONDS, f"unreachable: {e}"
        else:
            pause, why = 0.0, f"HTTP {status}: {e}"
        try:
            if pause:
                state.write_scratch(_OBSERVED_PAUSE_KEY, why[:200], ttl_seconds=pause)
            hooklog.log_failure(_HOOK, f"/agent/action/observed {why}"
                                + (f" — paused {int(pause)}s" if pause else ""))
        except Exception:  # noqa: BLE001
            pass


@never_raise(0)
def run(payload: dict) -> int:
    cfg = resolver.load_config()
    session_id = state.resolve_session_id(payload, cfg)

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    failed = payload.get("hook_event_name") == _FAILURE_EVENT
    interrupted = bool(payload.get("is_interrupt")) if failed else bool(
        isinstance(tool_response, dict) and tool_response.get("interrupted", False))
    is_command = tool_name in COMMAND_TOOLS

    # Pair by command hash for shell tools (review 2026-08-15): parallel calls
    # complete in arbitrary order, and a bare LIFO pop cross-attributed exit
    # statuses — enforcement evidence must reconcile the command that actually
    # ran. Edits pop the legacy hashless entries exactly as before.
    chash = ""
    if is_command:
        chash = runbooks.local_command_hash(tool_input.get("command") or "")
    action_id = state.pop_action(session_id, command_hash=chash)

    actual_changes: list[str] = []
    deviation_notes = None
    success = not failed
    exit_status: int | None = None

    if tool_name in _EDIT_TOOLS:
        file_path = (tool_input.get("file_path") or tool_input.get("filePath")
                     or tool_input.get("path") or "")
        success = success and bool(tool_response.get("success", True))
        if file_path and success and action_id:
            new_sha = _sha256(file_path)
            old_sha = state.read_prestate(action_id) or ""
            if new_sha and new_sha != old_sha:
                actual_changes = [file_path]
    elif is_command:
        if failed:
            exit_status = _exit_status_from_error(payload.get("error"))
            notes = payload.get("error")
        else:
            exit_status = _extract_exit_status(tool_response)
            if exit_status is not None:
                # A real exit status outranks the interrupted heuristic: a
                # command that ran to completion with a nonzero code was not
                # a success.
                success = exit_status == 0
            else:
                success = not interrupted
            notes = tool_response.get("stderr", "") if isinstance(tool_response, dict) else ""
        if isinstance(notes, str) and notes:
            deviation_notes = notes[:500]

    if not action_id:
        # Nothing was gated, so there is nothing to reconcile. An ordinary
        # command still has an outcome worth one event; an interrupted one
        # says nothing about the work (the user stopped it), and an edit with
        # no queued action means the gate itself was unreachable.
        if is_command and not interrupted:
            _report_observed(cfg, tool_input.get("command"),
                             success=success, exit_status=exit_status)
        return 0

    outcome = {"success": success, "actual_changes": actual_changes}
    if deviation_notes:
        outcome["deviation_notes"] = deviation_notes

    # Reconciled — drop the snapshot (bash parity: postaction unlinked it).
    state.delete_prestate(action_id)

    body: dict = {"action_id": action_id, "outcome": outcome}
    if is_command:
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
