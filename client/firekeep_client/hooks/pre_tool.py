"""PreToolUse core — lease check + agent-gateway pre-action gate.

Ports scripts/multi-agent-precheck.sh. THE BLOCKING CONTRACT (design §6.3):
  0 -> allow/warn (proceed; on allow/warn also record pre-state)
  1 -> gateway block|rethink (advisory on stderr)
  2 -> file leased by ANOTHER agent (advisory on stderr)
Server unreachable -> 0 (availability over enforcement) but hooklog.

ADAPTER CONTRACT: Claude's PreToolUse process gate blocks ONLY on exit code 2;
exit 1 is non-blocking. The adapter MUST map BOTH 1 and 2 to a blocking exit or
the gateway 'block' is silently defeated at the seam.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from firekeep_client import hooklog, resolver, state, transport
from firekeep_client.hooks import _mcp, destructive, never_raise

_HOOK = "pre_tool"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _action_type(tool_name: str) -> str:
    if tool_name in _EDIT_TOOLS:
        return "edit_file"
    if tool_name == "Bash":
        return "run_command"
    return "other"


def _target(action_type: str, tool_input: dict) -> str:
    if action_type == "edit_file":
        return tool_input.get("file_path") or tool_input.get("path") or ""
    if action_type == "run_command":
        return tool_input.get("command") or ""
    return ""


def _resource_id(file_path: str) -> str:
    path = os.path.normpath(file_path).replace("\\", "/").lstrip("/")
    return path.replace("/", ".")


def _sha256(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "MISSING"


@never_raise(0)
def run(payload: dict) -> int:
    # Bash parity: precheck reaped >1h-old orphan snapshots near the TOP of
    # every invocation — before any early return (no-target/lease-block/
    # gateway-block paths must not skip cleanup). Best-effort, never raises.
    state.reap_stale()

    cfg = resolver.load_config()
    profile = resolver.active_profile(cfg)
    agent = resolver.agent_id(cfg, profile)

    tool_name = payload.get("tool_name", "")
    # 3-key fallback mirrors the bash original: MCP/JSON-RPC-framed callers
    # (Codex/kiro adapters) use "arguments" — dropping it would silently bypass
    # BOTH the lease check and the gateway for those payload shapes.
    tool_input = (payload.get("tool_input") or payload.get("input")
                  or payload.get("arguments") or {})
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except (ValueError, TypeError):
            tool_input = {}
    action_type = _action_type(tool_name)
    target = _target(action_type, tool_input)

    if action_type == "edit_file" and not target:
        return 0  # nothing resolvable to guard

    # Shell commands: snapshot-then-allow, and return here so Bash never reaches the
    # gateway below. Routing every Bash call through a 5s network gate that fails open
    # would put latency on the hottest tool and still wave through the one command that
    # matters whenever Cortex is slow. `_action_type` has mapped Bash -> run_command
    # since this file was written; only the adapter's ^(Edit|Write)$ matcher kept the
    # branch unreachable, which is why `git checkout -- cortex/app/` was never seen.
    if action_type == "run_command":
        if target:
            note = destructive.guard(target)
            if note:
                print(note, file=sys.stderr)
        return 0

    session_id = state.resolve_session_id(payload, cfg)

    # 1. Lease check (edit_file only). Held by ANOTHER agent -> block (2).
    if action_type == "edit_file" and target:
        rid = _resource_id(target)
        if rid:
            try:
                lease = _mcp.call_tool("relay", "relay_lease_status",
                                       {"resource_id": rid}, cfg=cfg)
                if (isinstance(lease, dict) and lease.get("held")
                        and lease.get("holder_id") != agent):
                    holder = lease.get("holder_id", "unknown")
                    print(f"BLOCKED: {target} is leased by {holder}. Coordinate via "
                          f"relay_get_messages/relay_task_list before editing.",
                          file=sys.stderr)
                    return 2
            except Exception as e:  # noqa: BLE001
                hooklog.log_failure(_HOOK, f"relay_lease_status failed: {e}")

    # 2. Agent-gateway pre-action gate (Cortex REST). Unreachable -> allow (0).
    try:
        cep = resolver.resolve("cortex", cfg=cfg, session_id=session_id)
        # adapter MUST be one of the server's Adapter literals ("shell-hook",
        # "mcp", "rest") — anything else 422s and the fail-open path would make
        # the gateway gate permanently inert (caught by task review vs the real
        # cortex/app/agent_gateway/models.py).
        resp = transport.post_json(
            f"{cep.rest_base}/agent/action/before",
            {"session_id": session_id, "agent_id": agent, "adapter": "shell-hook",
             "action": {"type": action_type, "target": target}},
            headers=cep.headers, verify=cep.verify,
        )
    except Exception as e:  # noqa: BLE001
        hooklog.log_failure(_HOOK, f"/agent/action/before unreachable: {e}")
        return 0

    decision = (resp.get("decision") if isinstance(resp, dict) else "allow") or "allow"
    advisories = resp.get("advisories", []) if isinstance(resp, dict) else []
    reasons = "; ".join(a.get("message", "") for a in advisories if a.get("message"))
    action_id = resp.get("action_id", "") if isinstance(resp, dict) else ""

    if decision in ("block", "rethink"):
        print(f"[firekeep pre_tool] {decision}: {reasons}", file=sys.stderr)
        return 1
    # The live gateway remaps warn->allow before responding (service.py) and
    # carries the warn-tier advisories in the payload — surface them whenever
    # present instead of gating on a "warn" decision that can never arrive.
    if reasons:
        print(f"[firekeep pre_tool] warn: {reasons}", file=sys.stderr)

    # allow / warn: record pre-state for post_tool reconciliation.
    if action_id:
        if action_type == "edit_file":
            state.write_prestate(action_id, _sha256(target))
        state.push_action(session_id, action_id)
    return 0
