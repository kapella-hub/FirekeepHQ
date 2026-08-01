"""firekeep_client.hooks — CLI dispatcher: `python -m firekeep_client.hooks <core> [--block-exit N]`.

This is the ONLY thing that actually runs a hook core. Before this module
existed, `base.hook_command()` rendered `{python} -m firekeep_client.hooks.<core>`,
which merely IMPORTS the core module (no stdin-reading `__main__` block) and
exits 0 without ever calling `run()` — every rendered hook was silently dead:
no briefing systemMessage, and the pre_tool safety gate could never block.
This dispatcher closes that gap.

Contract:
  - argv[0] is the core name, one of {session_start, stop, session_end, prompt,
    precompact, pre_tool, post_tool}. Unknown/missing -> usage line to stderr,
    exit 0, hooklogged. A misconfigured hook must NEVER break the user's
    session — availability over enforcement.
  - stdin is read fully and parsed as JSON. Empty or malformed stdin ->
    payload defaults to `{}` (hooklogged) and the core still runs.
  - dict cores (session_start/stop/session_end/prompt/precompact): if `run(payload)` returns a
    truthy dict, it is printed as `json.dumps(result)` on stdout — Claude
    Code reads the hook's `systemMessage` from that stdout JSON. Always
    exits 0 (these cores never block).
  - int cores (pre_tool/post_tool): exits with the code `run(payload)`
    returns, EXCEPT: if `--block-exit N` was given and the code is nonzero,
    exit N instead. This is the adapter-level remap for Claude's PreToolUse
    gate, which blocks the tool call ONLY on exit code 2 — pre_tool.run()
    returns 1 for an agent-gateway block/rethink and 2 for a lease held by
    another agent, so without this remap a gateway 'block' decision (rc=1)
    would silently fall through as non-blocking. The claude adapter renders
    pre_tool with `--block-exit 2`; post_tool is rendered WITHOUT the flag
    (it always returns 0, so the remap would be a no-op).
  - The dispatcher itself never raises — any unexpected failure is caught,
    hooklogged, and swallowed to exit 0. stdlib only (SP1b import boundary).
  - Retired ``--profile NAME`` arguments from a pre-collapse rendered hook are
    accepted and ignored until the next adapter render removes them.
"""
from __future__ import annotations

import json
import sys

from firekeep_client import hooklog, resolver
from firekeep_client.hooks import (
    post_tool,
    pre_tool,
    precompact,
    prompt,
    session_end,
    session_start,
    stop,
)

# Shown (as a Claude systemMessage) on session_start/prompt while personal mode is on,
# so an active bypass is loud and hard to forget.
_BYPASS_MSG = (
    "⚠ PERSONAL MODE — Firekeep is bypassed this session. Nothing is logged or "
    "recalled, and you should NOT use firekeep_* memory/session tools. Run `firekeep personal "
    "off` (or /personal) to rejoin team mode; it also auto-clears when this session ends."
)

_CORE_MODULES = {
    "session_start": session_start,
    "stop": stop,
    "session_end": session_end,
    "prompt": prompt,
    "precompact": precompact,
    "pre_tool": pre_tool,
    "post_tool": post_tool,
}
_INT_CORES = frozenset({"pre_tool", "post_tool"})
_DICT_CORES = frozenset({"session_start", "stop", "session_end", "prompt", "precompact"})

# Cores that must run even while personal mode is ON, because they self-handle
# bypass and own end-of-session cleanup: `stop` clears the personal marker
# itself, and `session_end` must be free to decline comms without the dispatcher
# printing _BYPASS_MSG at a session nobody is reading any more.
_BYPASS_EXEMPT = frozenset({"stop", "session_end"})


def _usage() -> None:
    names = "|".join(_CORE_MODULES)
    print(f"usage: python -m firekeep_client.hooks <{names}> [--block-exit N]", file=sys.stderr)


def _parse_block_exit(rest: list[str]) -> int | None:
    for i, tok in enumerate(rest):
        if tok == "--block-exit" and i + 1 < len(rest):
            try:
                return int(rest[i + 1])
            except ValueError:
                return None
    return None


def _read_payload(hook_name: str) -> dict:
    """Never raises -- any stdin/JSON problem degrades to `{}` (hooklogged)."""
    try:
        raw = sys.stdin.read()
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(hook_name, f"stdin read failed: {e!r}")
        return {}
    if not raw.strip():
        hooklog.log_failure(hook_name, "empty stdin payload")
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        hooklog.log_failure(hook_name, f"malformed stdin JSON: {e!r}")
        return {}
    if not isinstance(data, dict):
        hooklog.log_failure(hook_name, f"stdin JSON was not an object: {type(data).__name__}")
        return {}
    return data


def _personal_text_command(payload: dict) -> str | None:
    """Handle `/personal [on|off|status|toggle]` typed as PLAIN CHAT TEXT.

    kiro has no slash-command surface — `/personal` typed there reaches the
    model (and this dispatcher) verbatim, doing nothing (field report,
    2026-07-14). Both kiro's userPromptSubmit and Claude's UserPromptSubmit
    carry the message as `payload["prompt"]` (kiro shape validated empirically
    on kiro-cli 2.12.1). Returns the systemMessage to show, or None when the
    prompt isn't a /personal command. Never raises.
    """
    try:
        text = payload.get("prompt", "")
        if not isinstance(text, str):
            return None
        parts = text.strip().split()
        if not parts or parts[0] != "/personal" or len(parts) > 2:
            return None
        action = parts[1].lower() if len(parts) == 2 else "toggle"
        if action not in ("on", "off", "status", "toggle"):
            return f"/personal: unknown action '{action}' — use on | off | status | toggle"
        if action == "on":
            resolver.set_personal(True)
        elif action == "off":
            resolver.set_personal(False)
        elif action == "toggle":
            resolver.set_personal(not resolver.is_personal())
        if resolver.is_personal():
            return (
                "⚠ PERSONAL MODE ON — Firekeep bypassed: nothing is logged or "
                "recalled, and firekeep_* tools should not be called. Auto-clears at "
                "session end; '/personal off' rejoins team mode."
            )
        if resolver.is_bypassed():  # marker off, but the FIREKEEP_BYPASS env tier is set
            return (
                "Personal marker cleared, but FIREKEEP_BYPASS is set in this process's "
                "environment — still bypassed. Unset FIREKEEP_BYPASS and restart to "
                "rejoin team mode."
            )
        return "Personal mode OFF — team mode (Firekeep active)."
    except Exception as e:  # noqa: BLE001 — a toggle failure must never break the session
        hooklog.log_failure("prompt", f"/personal text command failed: {e!r}")
        return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    core_name = argv[0] if argv else ""

    if core_name not in _CORE_MODULES:
        _usage()
        hooklog.log_failure("hooks", f"unknown or missing hook core: {core_name!r}")
        return 0

    # `/personal` as chat text: intercepted BEFORE the bypass gate below — while
    # personal mode is ON the prompt core is short-circuited, so an in-core
    # intercept could never toggle OFF again.
    payload: dict | None = None
    if core_name == "prompt":
        payload = _read_payload(core_name)
        personal_msg = _personal_text_command(payload)
        if personal_msg is not None:
            print(json.dumps({"systemMessage": personal_msg}))
            return 0

    # Personal / bypass mode: Firekeep goes dormant for this session. Checked LIVE
    # (every hook invocation re-reads the marker) so a mid-session `/personal` toggle
    # takes effect at once. `stop` and `session_end` are deliberately NOT
    # short-circuited here (see _BYPASS_EXEMPT) — they self-handle bypass: `stop`
    # clears the marker so personal mode auto-ends with the session, and both skip
    # their own Relay/Bridge comms rather than being skipped wholesale.
    try:
        if core_name not in _BYPASS_EXEMPT and resolver.is_bypassed():
            if core_name in _INT_CORES:
                return 0  # allow the edit; no policy/gateway call reaches the server
            print(json.dumps({"systemMessage": _BYPASS_MSG}))  # session_start / prompt
            return 0
    except Exception as e:  # noqa: BLE001 — a gate failure must never break the session
        hooklog.log_failure(core_name, f"bypass check failed: {e!r}")

    try:
        block_exit = _parse_block_exit(argv[1:])
        if payload is None:
            payload = _read_payload(core_name)
        result = _CORE_MODULES[core_name].run(payload)

        if core_name in _INT_CORES:
            code = result if isinstance(result, int) else 0
            if block_exit is not None and code != 0:
                return block_exit
            return code

        # dict core (session_start/stop/prompt).
        if result:
            print(json.dumps(result))
        return 0
    except resolver.ConfigMigrationConflict as e:
        hooklog.log_failure(core_name, f"config migration refused: {e}")
        print(json.dumps({"systemMessage": str(e)}))
        return 0
    except Exception as e:  # noqa: BLE001 — the dispatcher itself must never raise.
        hooklog.log_failure(core_name, f"dispatcher crashed: {e!r}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
