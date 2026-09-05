"""Best-effort bridge from Hands to the Keep.

Three things route through here: cortex's `action_before`/`action_after` for
provenance on the whole task, relay's lease so only one Hands session drives
a given machine at a time, and relay tasks reused as a human approval queue
(`hands_permit:<challenge>`) for the moments the local policy engine won't
decide on its own (see `policy.py`).

Hands must keep operating the desktop even when the Keep is unreachable,
slow, or the caller has deliberately gone offline (personal mode,
disconnected demos, tests) — so every method here is best-effort: a 5-second
timeout, `TransportError` or any other exception caught and logged via
`hooklog.log_failure`, and a `None`/no-op return rather than a raise. When
`offline` is set, no network call is attempted at all — not even a doomed
one — which matters for latency, not just correctness.

`call_tool` is imported by name (not through the `_mcp` module) specifically
so tests can `monkeypatch.setattr(keep, "call_tool", ...)`.
"""
from __future__ import annotations

import json
import os
from typing import Any

from firekeep_client import hooklog
from firekeep_client.hooks._mcp import call_tool
from firekeep_client.transport import TransportError

_TIMEOUT = 5.0
_DEFAULT_LEASE_TTL_MINUTES = 30


class KeepLink:
    def __init__(self, *, agent_id: str, machine_id: str, offline: bool | None = None):
        self.agent_id = agent_id
        self.machine_id = machine_id
        self.offline = offline if offline is not None else os.environ.get("FIREKEEP_HANDS_OFFLINE") == "1"
        self._fencing_token = 0
        self._lease_ttl_minutes = _DEFAULT_LEASE_TTL_MINUTES

    def _call(self, service: str, tool: str, arguments: dict) -> Any:
        """The one place that talks to the Keep: offline short-circuits
        before any call is attempted, and every failure mode (network,
        in-band MCP error, anything else) degrades to None."""
        if self.offline:
            return None
        try:
            return call_tool(service, tool, arguments, timeout=_TIMEOUT)
        except TransportError as exc:
            hooklog.log_failure("hands", f"{service}.{tool} failed: {exc}", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — best-effort: never raise into the caller
            hooklog.log_failure("hands", f"{service}.{tool} failed: {exc}", exc)
            return None

    def action_before(self, *, goal: str, task_id: str, apps: list[str]) -> str | None:
        result = self._call(
            "cortex",
            "action_before",
            {
                "action_type": "hands_task",
                "target": f"desktop:{self.machine_id}",
                "intent": goal,
                "success_criteria": "task ends with outcome=done",
                "confidence": 0.6,
            },
        )
        return result.get("action_id") if isinstance(result, dict) else None

    def action_after(self, action_id: str | None, outcome: str, summary: str) -> None:
        if action_id is None:
            return
        self._call(
            "cortex",
            "action_after",
            {"action_id": action_id, "outcome": outcome, "summary": summary},
        )

    def acquire_lease(self, ttl_minutes: int = _DEFAULT_LEASE_TTL_MINUTES) -> dict | None:
        self._lease_ttl_minutes = ttl_minutes
        result = self._call(
            "relay",
            "relay_lease",
            {"resource_id": f"hands:{self.machine_id}", "agent_id": self.agent_id, "ttl_minutes": ttl_minutes},
        )
        if not isinstance(result, dict):
            return None
        # relay's real response marks a lost race with "acquired": False (and
        # still hands back the OTHER holder's fencing_token, for visibility).
        # Default True when the key is absent — the brief's own fixture omits
        # it — but never let a losing result overwrite a token we already hold.
        if result.get("acquired", True):
            self._fencing_token = result.get("fencing_token", self._fencing_token)
        return result

    def renew_lease(self) -> None:
        self.acquire_lease(self._lease_ttl_minutes)

    def release_lease(self) -> None:
        self._call(
            "relay",
            "relay_release",
            {"resource_id": f"hands:{self.machine_id}", "agent_id": self.agent_id, "fencing_token": self._fencing_token},
        )

    def post_permit_task(
        self,
        *,
        challenge: str,
        title: str,
        classes: tuple[str, ...],
        task_id: str,
        step_index: int,
        expires_at: str,
    ) -> str | None:
        description = (
            f"Approve or deny '{title}' [{', '.join(classes) or 'none'}] "
            f"for task {task_id} step {step_index}; expires {expires_at}."
        )
        context = json.dumps(
            {
                "title": title,
                "classes": list(classes),
                "task_id": task_id,
                "step_index": step_index,
                "expires_at": expires_at,
            }
        )
        result = self._call(
            "relay",
            "relay_task_post",
            {
                "title": f"hands_permit:{challenge}",
                "assigner": self.agent_id,
                "description": description,
                "priority": "high",
                "context": context,
            },
        )
        if not isinstance(result, dict):
            return None
        task = result.get("task")
        return task.get("id") if isinstance(task, dict) else None

    def permit_task_state(self, challenge: str) -> str | None:
        result = self._call("relay", "relay_task_list", {"title": f"hands_permit:{challenge}", "limit": 1})
        if not isinstance(result, dict):
            return None
        tasks = result.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            return None
        status = tasks[0].get("status")
        if status == "completed":
            text = str(tasks[0].get("result") or "").strip().lower()
            return "approve" if text.startswith("approve") else "deny"
        if status in ("cancelled", "failed"):
            return "deny"
        if status in ("pending", "in-progress"):
            return "pending"
        return None

    def close_permit_task(self, task_id: str, result: str) -> None:
        self._call("relay", "relay_task_update", {"task_id": task_id, "status": "cancelled", "result": result})
