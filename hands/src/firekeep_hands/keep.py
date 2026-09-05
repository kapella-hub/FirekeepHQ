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
from dataclasses import dataclass
from typing import Any

from firekeep_client import hooklog, resolver
from firekeep_client.hooks._mcp import call_tool
from firekeep_client.transport import TransportError

_TIMEOUT = 5.0
_DEFAULT_LEASE_TTL_MINUTES = 30

# cortex's `action_type` vocabulary is a CLOSED Literal —
# `ActionType = Literal["edit_file", "run_command", "call_api", "delete",
# "other"]` in `cortex/app/agent_gateway/models.py` — and `Action.type` is
# typed with it, so FastAPI answers anything else with a 422. A Hands task is
# none of the first four, so it goes in as "other" and says what it really is
# in `preview`.
#
# This is not a tidiness fix. Sending "hands_task" made every POST 422; the
# MCP tool catches `HTTPStatusError` and returns `_format_error(exc)`, a
# STRING, which `call_tool` hands back without raising — so `action_id` was
# always None, `action_after` no-opped, and the `block` veto three documents
# describe could never fire. `tests/test_keep.py` holds a tripwire that parses
# cortex's own models.py so this cannot drift back silently.
_ACTION_TYPE = "other"
_HANDS_MARKER = "hands_task"
# Two more closed limits from the same file: `Action.preview` is
# `Field(max_length=2048)` and `Prediction.intent` is `Field(max_length=512)`.
# Either one exceeded is a second 422 queued behind the first.
_MAX_PREVIEW = 2048
_MAX_INTENT = 512


@dataclass(frozen=True)
class KeepDecision:
    """What cortex said about a proposed action.

    `decision` is cortex's own vocabulary — "allow", "rethink" or "block"
    (`cortex/app/agent_gateway/models.py`) — or None when there was no
    answer at all: offline, unreachable, or a reply that did not carry one.
    None is NOT a block. Hands has to keep working with no Keep, so only an
    explicit refusal refuses.

    `reason` is the advisory text cortex sent with it, joined. The response
    has no single "reason" field; the advisories are where a human-readable
    explanation lives, and a block with nothing to show the human would be a
    refusal they cannot act on."""

    decision: str | None
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return self.decision == "block"


def _decision_from(result: Any) -> KeepDecision:
    if not isinstance(result, dict):
        return KeepDecision(None)
    decision = result.get("decision")
    if not isinstance(decision, str):
        return KeepDecision(None)
    advisories = result.get("advisories")
    messages = []
    if isinstance(advisories, list):
        for advisory in advisories:
            if isinstance(advisory, dict):
                message = str(advisory.get("message") or "").strip()
                if message:
                    messages.append(message)
    return KeepDecision(decision, "; ".join(messages))


def _no_keep_configured() -> bool:
    """True when this machine has no Keep to talk to.

    `resolver.resolve` is the cheapest honest answer: it reads the config file
    and raises `ConfigError` for a missing file, a missing `[server]` section
    or a malformed entry, and it performs no network I/O — so this costs one
    stat and a parse, not a timeout. Any other exception counts the same way;
    a resolver that cannot say where the Keep is means there is no Keep."""
    try:
        resolver.resolve("relay")
    except Exception:  # noqa: BLE001 - every failure means "no Keep here"
        return True
    return False


class KeepLink:
    def __init__(self, *, agent_id: str, machine_id: str, offline: bool | None = None, session_id: str = ""):
        self.agent_id = agent_id
        self.machine_id = machine_id
        # An explicit argument always wins. Otherwise offline is the env
        # switch OR simply having nowhere to call: an unconfigured machine
        # that thinks it is online advertises a phone approval path it does
        # not have and retries a doomed post every few seconds per permit.
        if offline is not None:
            self.offline = offline
        else:
            self.offline = os.environ.get("FIREKEEP_HANDS_OFFLINE") == "1" or _no_keep_configured()
        self.session_id = session_id
        # What the Keep said about the last `action_before`. Kept as an
        # attribute rather than folded into the return value so the existing
        # `-> action_id | None` contract stays intact for every caller that
        # only wants the id.
        self.last_decision = KeepDecision(None)
        self._fencing_token = 0
        self._holds_lease = False
        self._lease_ttl_minutes = _DEFAULT_LEASE_TTL_MINUTES
        # One reclaim per link. A live peer sharing this agent id must not be
        # robbed of the lease over and over by a retrying caller.
        self._reclaimed_once = False

    def _call(self, service: str, tool: str, build_arguments) -> Any:
        """The one place that talks to the Keep: offline short-circuits
        before anything else is attempted, and every failure mode — network,
        an in-band MCP error, or a caller's OWN argument construction raising
        (e.g. `", ".join(...)` on a non-str element) — degrades to None
        rather than escaping into the caller.

        `build_arguments` is a zero-arg callable, invoked HERE inside the
        guarded region rather than in the caller's frame: a dict literal
        passed as an ordinary argument is evaluated before this method is
        even entered, which is exactly how an earlier version of this class
        broke its own "never raises" contract (`action_before`/
        `post_permit_task` building a dict inline, with a `", ".join(...)`
        inside it, in their own frame)."""
        if self.offline:
            return None
        try:
            arguments = build_arguments()
            result = call_tool(service, tool, arguments, timeout=_TIMEOUT)
        except TransportError as exc:
            hooklog.log_failure("hands", f"{service}.{tool} failed: {exc}", exc)
            return None
        except Exception as exc:  # noqa: BLE001 — best-effort: never raise into the caller
            hooklog.log_failure("hands", f"{service}.{tool} failed: {exc}", exc)
            return None
        # An in-band MCP error is a STRING, not a raise: the cortex and relay
        # tools catch `HTTPStatusError`/`RequestError` and return formatted
        # text, and `call_tool` passes it through verbatim. Every caller here
        # tests `isinstance(result, dict)` and degrades to None, so without
        # this line a 422 or a 500 was completely invisible — which is exactly
        # how `action_type: "hands_task"` survived to a shipped branch. The
        # body is what names the fault, so it is what gets logged.
        if result is not None and not isinstance(result, dict):
            hooklog.log_failure(
                "hands",
                f"{service}.{tool} answered with {type(result).__name__}, not an object: "
                f"{str(result)[:300]}",
            )
            return None
        return result

    def action_before(self, *, goal: str, task_id: str, apps: list[str]) -> str | None:
        """cortex/app/mcp_server.py:1542 (`action_before`) is the source of
        truth for this call's shape, not the simplified
        `action_before(action_type, target, intent, success_criteria,
        confidence)` shorthand documented elsewhere (e.g. this repo's
        CLAUDE.md) — the real tool REQUIRES `session_id` and `agent_id` (no
        defaults) and types `success_criteria` as a list, not a string.
        `action_type` is `"other"` because cortex's `ActionType` is a closed
        Literal that has never had a Hands member (see `_ACTION_TYPE` above);
        the marker and `apps` both ride in `preview`, which is the only free
        text field in that shape. `session_id` falls back to `task_id` when
        this KeepLink wasn't given one — either way it must be non-empty for
        the call to validate server-side. `apps` elements are coerced with
        `str()` before joining — this method must not raise on a caller
        passing e.g. a list of ints — and both free-text fields are truncated
        to the maxima cortex declares, because over either one the whole
        request 422s and the Keep records nothing at all."""
        def build():
            joined = ", ".join(str(a) for a in apps)
            preview = f"{_HANDS_MARKER} · apps: {joined}" if joined else _HANDS_MARKER
            return {
                "session_id": self.session_id or task_id,
                "agent_id": self.agent_id,
                "action_type": _ACTION_TYPE,
                "target": f"desktop:{self.machine_id}",
                "preview": preview[:_MAX_PREVIEW],
                "intent": str(goal)[:_MAX_INTENT],
                "success_criteria": ["task ends with outcome=done"],
                "confidence": 0.6,
            }
        result = self._call("cortex", "action_before", build)
        # The full `ActionBeforeResponse` comes back, not just an id. The
        # decision half lands on `last_decision` for a caller that wants to
        # honour a block; the id is still the return value.
        self.last_decision = _decision_from(result)
        return result.get("action_id") if isinstance(result, dict) else None

    def action_after(self, action_id: str | None, outcome: str, summary: str) -> None:
        """cortex/app/mcp_server.py:1599 (`action_after`) wants a boolean
        `success`, not the `outcome`/`summary` strings this method takes from
        its caller — `outcome == "done"` is the boolean, and both strings are
        folded into `deviation_notes` (`Outcome.deviation_notes` is
        `Field(max_length=512)`; truncated to 500 here to match the ledger's
        own summary length, which keeps it inside that cap as well)."""
        if action_id is None:
            return
        self._call(
            "cortex",
            "action_after",
            lambda: {
                "action_id": action_id,
                "success": outcome == "done",
                "deviation_notes": f"{outcome}: {summary}"[:500],
            },
        )

    def _lease_call(self, ttl_minutes: int) -> Any:
        return self._call(
            "relay",
            "relay_lease",
            lambda: {"resource_id": f"hands:{self.machine_id}", "agent_id": self.agent_id, "ttl_minutes": ttl_minutes},
        )

    def _is_our_own_stale_lease(self, result: Any) -> bool:
        return (
            isinstance(result, dict)
            and result.get("acquired") is False
            and result.get("held_by") == self.agent_id
        )

    def acquire_lease(self, ttl_minutes: int = _DEFAULT_LEASE_TTL_MINUTES,
                      reclaim_own: bool = True) -> dict | None:
        """Take the machine lease, reclaiming one this agent left behind.

        A Hands server that exits without `hands_task_end` — the runtime
        closed, the process crashed, the driver walked away on a
        `needs_permit` — leaves its lease held for the full TTL, and the next
        run on the same machine is refused for half an hour by its own dead
        predecessor. Observed on real hardware, which is why this exists.

        The reclaim is deliberately narrow. It fires only when relay says the
        holder is THIS agent id (the resource id already pins the machine),
        only once per link, and it passes the holder's fencing token straight
        to `relay_release` without ever adopting it into `self._fencing_token`
        — another agent's token must never end up in ours.

        The residual risk is stated plainly: two LIVE Hands servers on one
        machine sharing a `NEXUS_AGENT_ID` are indistinguishable from one
        live and one dead, so the second would take the lease from the first
        rather than be refused. Relay's answer carries no liveness signal to
        separate them. See the task report for the PID-in-holder-id fix that
        would."""
        self._lease_ttl_minutes = ttl_minutes
        result = self._lease_call(ttl_minutes)
        if reclaim_own and not self._reclaimed_once and self._is_our_own_stale_lease(result):
            self._reclaimed_once = True
            token = result.get("fencing_token")
            hooklog.log_failure(
                "hands",
                f"reclaiming the hands:{self.machine_id} lease left by our own "
                f"agent {self.agent_id!r} (fencing token {token})",
            )
            self._call(
                "relay",
                "relay_release",
                lambda: {"resource_id": f"hands:{self.machine_id}",
                         "agent_id": self.agent_id, "fencing_token": token},
            )
            result = self._lease_call(ttl_minutes)
        if not isinstance(result, dict):
            return None
        # relay's real response marks a lost race with "acquired": False (and
        # still hands back the OTHER holder's fencing_token, for visibility
        # only). Default True when the key is absent — the brief's own
        # fixture omits it — but never let a losing result overwrite a token
        # we already hold, and remember that we don't currently hold the
        # lease so release_lease() below knows to send nothing.
        if result.get("acquired", True):
            self._fencing_token = result.get("fencing_token", self._fencing_token)
            self._holds_lease = True
        else:
            self._holds_lease = False
        return result

    def renew_lease(self) -> None:
        """Extends the TTL on the lease we currently hold via relay's actual
        renewal primitive, `relay_heartbeat(resource_id, fencing_token,
        agent_id)` (`relay/app/mcp_server.py:434`) — re-calling `relay_lease`
        while we already hold it only returns holder info, it does not
        extend the TTL. No-op when we don't currently hold a lease (no
        fencing token to renew)."""
        if not self._holds_lease:
            return
        self._call(
            "relay",
            "relay_heartbeat",
            lambda: {"resource_id": f"hands:{self.machine_id}", "fencing_token": self._fencing_token, "agent_id": self.agent_id},
        )

    def release_lease(self) -> bool:
        """No-op if we don't currently hold the lease — e.g. right after a
        lost `acquire_lease()` race — rather than send `relay_release` with a
        stale or foreign `fencing_token`, which would be worse than doing
        nothing.

        Returns whether a release was actually sent, so a caller reporting
        the shutdown to a human can say which of the two happened instead of
        claiming a release it may never have made."""
        if not self._holds_lease:
            return False
        self._call(
            "relay",
            "relay_release",
            lambda: {"resource_id": f"hands:{self.machine_id}", "agent_id": self.agent_id, "fencing_token": self._fencing_token},
        )
        self._holds_lease = False
        return True

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
        """`classes` elements are coerced with `str()` before joining into
        `description` — this must not raise on a caller passing e.g. a tuple
        of ints, the same failure mode `action_before` had for `apps`."""
        def build():
            classes_text = ", ".join(str(c) for c in classes) or "none"
            description = (
                f"Approve or deny '{title}' [{classes_text}] "
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
            return {
                "title": f"hands_permit:{challenge}",
                "assigner": self.agent_id,
                "description": description,
                "priority": "high",
                "context": context,
            }
        result = self._call("relay", "relay_task_post", build)
        if not isinstance(result, dict):
            return None
        task = result.get("task")
        return task.get("id") if isinstance(task, dict) else None

    def permit_task_state(self, challenge: str) -> str | None:
        result = self._call("relay", "relay_task_list", lambda: {"title": f"hands_permit:{challenge}", "limit": 1})
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
        self._call("relay", "relay_task_update", lambda: {"task_id": task_id, "status": "cancelled", "result": result})
