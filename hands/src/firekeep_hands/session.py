"""The Hands state machine: one task, one budget, one ledger, one gate.

Every module under `firekeep_hands` meets here. `routing` turns a requested
action into something a backend can actually do, `policy` says whether a
human has to approve it first, the `broker` is the only thing that can grant
that approval, `evidence` records what happened, and `keep` tells the Keep a
task started and ended. This module is the order those run in, and the order
is the safety property — a protected step is refused unless a permit minted
for *that exact step* is consumed, once, before anything reaches the desktop.

Three rules are worth stating plainly because everything else follows from
them:

1. **A permit is bound to the action.** The challenge id is derived from
   machine, session, task, step index and a hash of the action dict. Change
   any of them and the id changes, so a permit approved for "Send" cannot be
   replayed on "Delete", or on the same button one step later.
2. **The human is shown what was routed, never what was asked for.** The
   permit title is built here from the observed control's own name and the
   window's app — a model that puts reassuring text in its action payload
   cannot get that text in front of the person holding the keyboard.
3. **Nothing here raises into a tool result the model cannot read.** `act`
   and `browser_op` return `{"ok": False, "error": "<code>: <message>"}`;
   the lifecycle methods raise `HandsError` and the MCP layer converts it.
   A model that gets an exception cannot recover; one that gets an error
   string can.

There is deliberately no auto-remember path in this release. A permit is still
spent on exactly one step, and `firekeep hands allow` is the only thing that
writes a standing allowance into `policy.json`. The single exception is
task-scoped and dies with the task: consuming a `boundary` permit adds the app
(or, for a navigation, the host) that was approved to `self.task_apps`, so the
human is asked once to enter an app rather than once per click inside it.
Nothing about that reaches disk, another task, or `policy.remembered`.
"""
from __future__ import annotations

import datetime as dt
import secrets
import sys
import time
from urllib.parse import urlsplit

from firekeep_client import hooklog

from . import ids, paths
from .backends.base import Control, HandsError, Observation, Rect, WindowInfo
from .broker.client import BrokerClient
from .evidence import Ledger, prune
from .policy import BROWSER_APP, decide
from .routing import Routed, route

_DETAILS = ("summary", "controls", "screenshot")
_OUTCOMES = ("done", "failed", "abandoned")
_BROWSER_DIRECT = ("open", "tabs", "read", "find", "click", "fill", "screenshot")
_BROWSER_NAVIGATE = ("navigate", "open_url")

# A permit request that never returns to the requester within a single tool
# call is useless, and MCP clients time a call out somewhere around a minute.
_MAX_WAIT_S = 55
# Renew the machine lease every this many steps. The lease TTL is 30 minutes;
# a task doing 10 steps takes far less, so this is cheap insurance against a
# long task quietly losing the machine half way through.
_RENEW_EVERY = 10
# What a human should see in a permit prompt: enough to recognise the button,
# not enough for an app (or a model) to write a paragraph into their face.
_TITLE_LIMIT = 60

_BROKER_DOWN = (
    "approval broker unreachable — protected step refused; run `firekeep hands status`"
)

# The DOM probe names a password field by its type; the classifier knows the
# native spelling. Mapping it here is what makes a web login field and a
# Windows PasswordBox the same kind of target.
_BROWSER_ROLES = {"password": "PasswordBox"}
_FILLABLE_ROLES = frozenset({"password", "input", "textarea", "textbox", "searchbox", "combobox"})

# Mirrors `policy._CREDENTIAL_ROLES`. Duplicated rather than imported because
# redaction must not depend on a private name in another module — and because
# it has to hold even when `decide` DROPPED the credential class thanks to a
# remembered allowance. A human choosing not to be asked again is not a human
# choosing to write their password into a file.
_CREDENTIAL_ROLES = frozenset({"PasswordBox", "AXSecureTextField"})
_SECRET_KEYS = ("text", "value")
_SECRET_KINDS = ("type", "set_value", "browser.fill")
_REDACTED = "<redacted:credential>"

# What a browser or navigation call is allowed to contribute to a tool
# result. Merging its whole payload would let a page-derived key overwrite
# `ok`, `error`, `route` or `step_index` — the fields the runtime steers on.
_BROWSER_RESULT_KEYS = frozenset(
    {"loaded", "url", "title", "text", "controls", "tabs", "screenshot_png"}
)
# A `find` that returns hundreds of controls is not a better answer, it is a
# context window spent. The perception budget in `HandsConfig` bounds the
# native side; this bounds the two the caller can name directly.
_FIND_LIMIT_CEILING = 50


def _clean(text: object, limit: int = _TITLE_LIMIT) -> str:
    """Printable, whitespace-collapsed and length-capped.

    Everything that reaches a permit prompt goes through here. Control names
    come from whatever application drew them and URLs come from the runtime,
    so neither is trusted to be a single tidy line — a name containing
    newlines or terminal escapes would be rendered by the broker exactly as
    given."""
    cleaned = " ".join("".join(c for c in str(text) if c.isprintable()).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


class HandsSession:
    """One runtime's connection to one desktop. Not thread-safe by design:
    the MCP server drives it from a single dedicated worker thread (Windows
    UI Automation binds to the first thread that touches it), so a lock here
    would protect against a caller that must not exist."""

    def __init__(self, *, backend, broker: "BrokerClient | None", link,
                 browser, config, policy, session_id: str):
        self.backend = backend
        self.broker = broker
        self.link = link
        self.browser = browser
        self.config = config
        self.policy = policy
        self.session_id = session_id
        self.machine_id = ids.machine_id()

        self.task_id: str | None = None
        self.goal: str = ""
        self.task_apps: list[str] = []
        # Hosts this task has had approved, kept apart from `task_apps` on
        # purpose: one list made `apps=["intranet"]` clear a navigation to
        # `http://intranet/`, because a name is a name once both live in the
        # same bag. An app declaration says which programs are in scope; where
        # the browser may GO is the domain allowlist plus this.
        self.task_hosts: list[str] = []
        self.ledger: Ledger | None = None
        self.action_id: str | None = None
        self.step_index = 0
        self.last_obs: Observation | None = None
        self.last_task: dict | None = None
        # What the last click/invoke/set_value targeted. `type` carries no ref
        # of its own — the keystrokes land wherever focus happens to be — so
        # this is the only thing that can tell the classifier a password box
        # is about to receive them. Best-effort by construction: a human (or
        # the app) can move focus between the two steps and Hands cannot know.
        self._focus_hint: Control | None = None
        # The last page scan, by ref, and the page it came from. Browser steps
        # are classified from these the way native ones are classified from
        # the observation.
        self._browser_controls: dict[str, dict] = {}
        self._browser_page: dict[str, str] = {"url": "", "title": ""}

    # -- status ------------------------------------------------------------

    def status(self) -> dict:
        """What Hands can do on this machine *right now* — the answer to
        "why was that refused?" before it is asked. The broker's `listeners`
        map is reported verbatim rather than summarised: `phone` reads `off`,
        `offline` or `active`, and those three mean different things to the
        person deciding whether to walk away from the keyboard."""
        broker = self._ensure_broker()
        health = broker.health() if broker is not None else None
        listeners = dict(health.get("listeners") or {}) if health else {}
        task = None
        if self.task_id is not None:
            task = {
                "task_id": self.task_id,
                "goal": self.goal,
                "apps": list(self.task_apps),
                # Both lists are shown because both grow: a consumed boundary
                # permit widens one or the other, and "why was that allowed?"
                # is answered by seeing what this task has been let into.
                "hosts": list(self.task_hosts),
                "step_index": self.step_index,
                "steps_left": max(0, self.config.max_steps - self.step_index),
                "evidence": str(self.ledger.dir) if self.ledger else None,
                # Same reason it is in `task_start`'s result: null here on a
                # link that is not offline means the Keep never took the task.
                "action_id": self.action_id,
            }
        return {
            "ok": True,
            "platform": sys.platform,
            "backend": self.backend.name,
            "permissions": self.backend.permissions(),
            "broker": ({"running": True, "chord": health.get("chord"),
                        "listeners": listeners, "pending": health.get("pending")}
                       if health else {"running": False}),
            "approvals": self._approvals_text(health, listeners),
            "task": task,
            "last_task": self.last_task,
            "policy": {
                "apps": len(self.policy.apps),
                "domains": len(self.policy.domains),
                "remembered": len(self.policy.remembered),
            },
            "keep": {"offline": bool(getattr(self.link, "offline", True))},
            "limits": {
                "max_steps": self.config.max_steps,
                "max_nodes": self.config.max_nodes,
                "permit_ttl_s": self.config.permit_ttl_s,
                "evidence_retention_days": self.config.evidence_retention_days,
            },
        }

    def _approvals_text(self, health: dict | None, listeners: dict) -> str:
        """The phone sentence is present in both branches on purpose. Someone
        reading this because a step was refused needs to know both ways of
        approving one exist, and a machine with no broker running is exactly
        where someone is deciding how to set approvals up."""
        if health is None:
            return (
                "No approval broker is running, so every protected step is refused. "
                "Start it with `firekeep-hands-broker run`. Phone approvals are off by "
                "default; turn them on with `firekeep hands config set phone_approvals "
                "true` and restart the broker. docs/guides/hands.md explains what that "
                "trusts."
            )
        return (
            f"A protected step needs the {health.get('chord')} chord on this keyboard "
            f"(listener: {listeners.get('chord', 'unknown')}). Phone approvals are "
            f"{listeners.get('phone', 'off')}; turn them on with "
            "`firekeep hands config set phone_approvals true` and restart the broker. "
            "docs/guides/hands.md explains what that trusts."
        )

    # -- task lifecycle ----------------------------------------------------

    def task_start(self, goal: str, apps: list[str] | None = None) -> dict:
        """Open a task: prune aged evidence, mint a task id, open the ledger,
        take the machine lease, and tell the Keep.

        A refused lease means another Hands session is already driving this
        desktop, and two of them interleaving actions on one screen is worse
        than not starting at all — so this raises rather than proceeding with
        a lease that enforces nothing. A lease call that merely *failed*
        (offline, Keep unreachable) returns None and is not treated as a
        refusal; Hands has to keep working with no Keep."""
        if self.task_id is not None:
            raise HandsError("busy", f"task {self.task_id} is still open; call hands_task_end first")
        prune(paths.evidence_root(), older_than_days=self.config.evidence_retention_days)

        task_id = "h-" + secrets.token_hex(6)
        task_apps = [str(a) for a in (apps or [])]
        ledger = Ledger(task_id, goal=goal, apps=task_apps,
                        machine_id=self.machine_id, session_id=self.session_id)
        lease = self.link.acquire_lease()
        if isinstance(lease, dict) and lease.get("acquired") is False:
            # Close the directory we just opened rather than leave an empty
            # task behind: a refused start is itself worth a record.
            ledger.close("abandoned", "another Hands session holds this machine's lease")
            raise HandsError("busy", _lease_refusal(lease))

        self.task_id = task_id
        self.goal = goal
        self.task_apps = task_apps
        self.ledger = ledger
        self.step_index = 0
        self.last_obs = None
        self.action_id = self.link.action_before(goal=goal, task_id=task_id, apps=task_apps)
        # Before the block check on purpose: a task cortex refused still has an
        # action record on the Keep, and the evidence directory should name it.
        ledger.note_keep_action(self.action_id)

        # The Keep gets a say in whether this task starts at all. Only an
        # explicit "block" stops it: no answer, an unreachable Keep, or
        # "rethink" all proceed, because Hands must keep working offline and
        # a silent Keep is not a refusing one. No `action_after` is sent for
        # a blocked task — cortex refused it, so there is no outcome to
        # reconcile.
        decision = getattr(self.link, "last_decision", None)
        if decision is not None and getattr(decision, "blocked", False):
            reason = decision.reason or "the Keep blocked this task"
            # The reset belongs in a `finally` for the same reason it does in
            # `task_end`: a raising `ledger.close` here would leave the
            # session believing a task is open moments after the lease that
            # backed it was handed back.
            try:
                self.link.release_lease()
                ledger.close("abandoned", f"blocked by the Keep: {reason}")
            except Exception as exc:  # noqa: BLE001 — the refusal is what matters
                hooklog.log_failure("hands", f"cleanup after a Keep block failed: {exc}", exc)
            finally:
                self._reset()
            raise HandsError("blocked", reason)
        return {
            "ok": True,
            "task_id": task_id,
            "goal": goal,
            "apps": task_apps,
            "evidence": str(ledger.dir),
            "max_steps": self.config.max_steps,
            # A dict, matching `status()`'s `keep` — and carrying the action
            # id, which is the only thing that distinguishes a Keep that
            # ACCEPTED this task from one that is merely reachable. A
            # connectivity flag said "online" for a whole release while every
            # `action_before` was being rejected as a 422 and turned into an
            # error string nobody read; `action_id: null` on a link that is
            # not offline is that failure, visible in the tool result.
            "keep": {
                "online": not getattr(self.link, "offline", True),
                "action_id": self.action_id,
            },
            # The same id, top level, under the name the live-smoke checklist
            # asks for. One fact spelled twice is a small price for a script
            # written against either spelling working — and the nesting is what
            # makes `keep` mean the same thing here as it does in `status()`.
            "keep_action_id": self.action_id,
        }

    def task_end(self, outcome: str, summary: str = "") -> dict:
        """Close the ledger, tell the Keep how it went, release the machine.

        The browser is deliberately left open: a human may be part way
        through a login in it, and closing it would throw that away.

        Releasing the machine does NOT depend on the evidence write
        succeeding. A read-only disk or a locked file would otherwise strand
        the lease for its full thirty minutes and lock the human out of their
        own desktop over a bookkeeping failure — so the close is attempted,
        reported if it fails, and the lease comes back either way."""
        if self.task_id is None:
            raise HandsError("no_task", "no task is open")
        if outcome not in _OUTCOMES:
            raise HandsError("invalid_action", f"outcome must be one of {list(_OUTCOMES)}")
        evidence_error = None
        try:
            self.ledger.close(outcome, summary)
            steps = len(self.ledger.steps())
        except Exception as exc:  # noqa: BLE001 — the lease must come back regardless
            hooklog.log_failure("hands", f"could not close ledger {self.task_id}: {exc}", exc)
            evidence_error, steps = f"the evidence ledger did not close: {exc}", self.step_index
        result = {
            "ok": True,
            "task_id": self.task_id,
            "outcome": outcome,
            "summary": summary,
            "steps": steps,
            "evidence": str(self.ledger.dir),
        }
        if evidence_error:
            result["evidence_error"] = evidence_error
        try:
            self.link.action_after(self.action_id, outcome, summary)
        finally:
            # The release sits in the `finally` alongside the reset, not
            # after `action_after` in the try: telling the Keep how it went
            # is bookkeeping, and bookkeeping must never be what decides
            # whether the human gets their machine back. Guarded separately
            # so a failure here cannot replace the exception it is unwinding.
            try:
                released = bool(self.link.release_lease())
            except Exception as exc:  # noqa: BLE001 — the lease is the point
                hooklog.log_failure("hands", f"lease release failed: {exc}", exc)
                released = False
            # Say which of the two happened rather than let a caller assume:
            # "not held" is the normal answer offline, and the difference
            # matters to whoever is reading a shutdown log.
            result["lease"] = "released" if released else "not held"
            self.last_task = dict(result)
            self._reset()
        return result

    def abandon(self, summary: str = "server shut down with the task open") -> dict | None:
        """Close an open task on the way out, and never raise doing it.

        This is what the MCP server calls in its shutdown `finally`. Without
        it a run that ends any way other than `hands_task_end` — the runtime
        closed, the process killed, the driver walking away on a
        `needs_permit` — leaves the machine lease held for its full TTL, and
        the NEXT run on this machine is refused for half an hour by its own
        dead predecessor. Observed on real hardware, not a hypothetical.

        Returns the closing summary, or None when there was no task. Any
        failure is logged and swallowed: shutdown is not a place to raise."""
        if self.task_id is None:
            return None
        # Captured up front: `task_end` clears `task_id` in its own `finally`,
        # so reading it in the handler below logged "abandon failed for None".
        task_id = self.task_id
        try:
            return self.task_end("abandoned", summary)
        except Exception as exc:  # noqa: BLE001 — a teardown must not raise
            hooklog.log_failure("hands", f"abandon failed for {task_id}: {exc}", exc)
            try:
                self.link.release_lease()
            except Exception as release_exc:  # noqa: BLE001 — the lease is the point
                hooklog.log_failure("hands", f"lease release failed: {release_exc}", release_exc)
            self._reset()
            return None

    def _reset(self) -> None:
        self.task_id = None
        self.goal = ""
        self.task_apps = []
        self.task_hosts = []
        self.ledger = None
        self.action_id = None
        self.step_index = 0
        self.last_obs = None
        self._focus_hint = None
        self._browser_controls = {}
        self._browser_page = {"url": "", "title": ""}

    # -- perception --------------------------------------------------------

    def observe(self, *, detail: str = "controls", app: str | None = None,
                region: list[int] | None = None, max_nodes: int | None = None) -> dict:
        """Scan a window and remember the result: `last_obs` is what every
        later `ref` resolves against, so an observation is what makes acting
        possible at all.

        Requires an open task, and for accountability rather than
        bookkeeping: an observation is a tree — and possibly a screenshot —
        of the human's own screen, which may leave this machine for a cloud
        model. It has to sit inside a declared task with a ledger and a
        lease. Looking still costs no step; only `hands_status` works with no
        task at all."""
        guard = self._no_task()
        if guard is not None:
            return guard
        if detail not in _DETAILS:
            raise HandsError("invalid_action", f"detail must be one of {list(_DETAILS)}")
        observation = self.backend.observe(
            app=app,
            region=Rect(*region) if region else None,
            # Clamped to the configured ceiling, never merely defaulted to it:
            # `max_nodes` is multiplied into the backend's walk allowance, and
            # that walk runs on the one thread every other tool call queues
            # behind. A caller asking for a million nodes gets the budget.
            max_nodes=_clamp(max_nodes, self.config.max_nodes, self.config.max_nodes),
            text_budget=self.config.text_budget,
            screenshot=detail == "screenshot",
            max_width=self.config.screenshot_max_width,
        )
        self.last_obs = observation
        result = {
            "ok": True,
            "detail": detail,
            "window": _window_json(observation.window),
            "control_count": len(observation.controls),
            "text": observation.text,
            "truncated": observation.truncated,
        }
        if detail != "summary":
            result["controls"] = [_control_json(c) for c in observation.controls]
        if observation.screenshot_png is not None:
            result["screenshot_png"] = observation.screenshot_png
        return result

    def find(self, query: str, *, role: str | None = None, app: str | None = None,
             limit: int = 10) -> dict:
        """Search the current window (or a named app) by control text.

        Requires an open task for the same reason `observe` does — this reads
        the human's screen.

        The matches are folded into `last_obs` rather than returned and
        forgotten: `route` only resolves refs it can see in the current
        observation, so a ref this hands out would otherwise be dead on
        arrival. Existing entries are kept; a control that both calls found
        keeps the *newer* entry, since its rect may have moved."""
        guard = self._no_task()
        if guard is not None:
            return guard
        matches = self.backend.find(query, role=role, app=app,
                                    limit=_clamp(limit, 10, _FIND_LIMIT_CEILING))
        self._merge_into_observation(matches)
        return {"ok": True, "count": len(matches),
                "controls": [_control_json(c) for c in matches]}

    def _merge_into_observation(self, controls: list[Control]) -> None:
        if not controls:
            return
        if self.last_obs is None:
            self.last_obs = Observation(
                generation=0, window=self.backend.active_window(),
                controls=list(controls), text="", screenshot_png=None, truncated=False,
            )
            return
        fresh = {c.ref: c for c in controls}
        merged = [fresh.pop(c.ref, c) for c in self.last_obs.controls]
        self.last_obs.controls = merged + list(fresh.values())

    # -- acting ------------------------------------------------------------

    def act(self, action: dict, *, permit: str | None = None) -> dict:
        """One step. The whole gate, in the order the design fixes it.

        Routing failures (a stale ref, a shape Hands does not accept) return
        before anything is recorded — nothing happened, so nothing is a step
        and nothing costs budget. Everything from the permit check onward
        does cost a step, including a backend failure: a step that was
        attempted is part of the record whether or not it worked.

        That last rule is why execution is wrapped in a bare `except
        Exception` and why `_advance` sits in a `finally`. The exceptions a
        real backend throws are not all `HandsError` — `uiautomation` raises
        `comtypes.COMError` straight out of a pattern call — and letting one
        past this point would leave a consumed permit, an action that may
        well have run, no ledger line, an uncounted step, and a stale
        observation still holding refs. Every one of those is worse than a
        recorded failure."""
        guard = self._step_guard()
        if guard is not None:
            return guard

        try:
            routed = route(action, self.last_obs)
        except HandsError as exc:
            return _error(exc)

        window = self.backend.active_window()
        control = routed.control
        if control is None and routed.kind == "type":
            # Typing has no target of its own; the focus hint is what makes
            # "type into the password box you just clicked" a credential step.
            control = self._focus_hint
        decision = decide(action, control, window, action.get("url"),
                          self.policy, self.task_apps, task_hosts=self.task_hosts)

        permit_record = None
        if decision.verdict == "permit":
            gate = self._gate(action, routed, window, decision, permit)
            if "error" in gate or "needs_permit" in gate:
                return gate
            permit_record = gate["permit"]
            self._widen(gate.get("widen"), decision)

        # A navigation is a browser step even though it arrives through
        # `act`, so its evidence comes from the page, not the desktop.
        in_browser = routed.route == "browser"
        before = self._capture(via_browser=in_browser) if permit_record is not None else None
        index = self.step_index
        outcome, error, extra, evidence_error = "ok", None, None, None
        try:
            try:
                extra = self._execute(routed, window)
            except HandsError as exc:
                outcome, error = "error", f"{exc.code}: {exc}"
            except Exception as exc:  # noqa: BLE001 — see the docstring: a step
                # that was attempted is a step that gets recorded and counted,
                # whatever the backend threw. `uiautomation` raises bare
                # `comtypes.COMError`s from its pattern calls, and a field type
                # routing does not know about would raise a TypeError here.
                hooklog.log_failure("hands", f"step {index} failed: {exc}", exc)
                outcome, error = "error", f"backend: {exc}"
            after = self._capture(via_browser=in_browser) if permit_record is not None else None
            if outcome == "ok":
                self._update_focus_hint(routed)
            evidence_error = self._record(
                index, _redact(action, decision.classes, control), routed.route,
                decision.classes, permit_record, before, after, outcome, error,
            )
        finally:
            self._advance()
        result = {"ok": outcome == "ok", "step_index": index, "route": routed.route,
                  "classes": list(decision.classes), "error": error}
        result.update(_browser_payload(extra))
        if evidence_error:
            result["evidence_error"] = evidence_error
        return result

    def _record(self, index, action, route_, classes, permit, before, after,
                outcome, error) -> str | None:
        """Append the step to the ledger. Returns a message if the write
        failed — evidence is best-effort at the moment of writing, because a
        full disk must not turn a step that ran into a step the caller is
        told failed. It IS reported back rather than swallowed: a caller
        acting on an unrecorded step should know the record is missing."""
        try:
            self.ledger.record(
                step_index=index, action=action, route=route_, classes=classes,
                permit=permit, before_png=before, after_png=after,
                outcome=outcome, error=error,
            )
        except Exception as exc:  # noqa: BLE001 — never lose the step over the record
            hooklog.log_failure("hands", f"could not ledger step {index}: {exc}", exc)
            return f"step {index} ran but was not recorded: {exc}"
        return None

    def _gate(self, action: dict, routed: Routed, window: WindowInfo | None,
              decision, permit: str | None) -> dict:
        """The permit half of `act`, returning either `{"permit": ...}` once
        an approval has been consumed, or the refusal to hand back.

        Fails closed in four distinct ways, three of them naming the broker,
        because they are different things to fix: no broker at all, a broker
        that stopped answering between the health check and the request, a
        permit that is not (or is no longer) approved, and a permit already
        standing under a description that is not the one just computed here
        (see `_tampered`)."""
        broker = self._ensure_broker()
        if broker is None:
            return {"ok": False, "error": _BROKER_DOWN, "classes": list(decision.classes)}

        challenge = ids.challenge_id_for(
            self.machine_id, self.session_id, self.task_id,
            self.step_index, ids.action_hash(action),
        )
        title, widen_apps, widen_hosts = self._titled(routed, window, decision.classes)
        needs = {
            "challenge": challenge,
            "title": title,
            "classes": list(decision.classes),
            "reason": decision.reason,
            "expires_in_s": self.config.permit_ttl_s,
        }

        if permit != challenge:
            reply = broker.request(challenge=challenge, title=title,
                                   classes=list(decision.classes),
                                   task_id=self.task_id, step_index=self.step_index)
            if isinstance(reply, dict) and reply.get("state") in ("unreachable", "error"):
                # The broker answered /health and then stopped. Drop it so the
                # next protected step re-probes, and refuse this one rather
                # than send the model to wait on a permit nobody is holding.
                self.broker = None
                return {"ok": False, "error": _BROKER_DOWN, "classes": list(decision.classes)}
            tampered = self._tampered(reply, challenge, title, decision.classes)
            if tampered is not None:
                return tampered
            return {"ok": False, "needs_permit": needs}

        # The same check on the retry path, and it is not redundant. This
        # branch never calls `request`, so nothing above has looked at what the
        # broker is actually holding — and a permit that EXPIRED unanswered
        # stops being live, which lets the next `request` mint a fresh one with
        # a fresh title. Wait out the TTL, re-register the same challenge with
        # a sentence of your choosing, and the human chords on that.
        tampered = self._tampered(broker.get(challenge), challenge, title, decision.classes)
        if tampered is not None:
            return tampered

        if not broker.consume(challenge):
            return {"ok": False, "error": "permit not approved, expired or already used",
                    "needs_permit": needs}

        granted = broker.get(challenge) or {}
        # The widening travels with the permit, carrying exactly the names the
        # title just put in front of the human. It is NOT recomputed later:
        # that is precisely how the two drifted apart.
        return {"permit": {"challenge": challenge, "via": granted.get("via")},
                "widen": {"apps": widen_apps, "hosts": widen_hosts}}

    def _tampered(self, reply: object, challenge: str, title: str,
                  classes: tuple[str, ...]) -> dict | None:
        """Refuse when the permit the broker is holding does not describe the
        step we are about to run. None when it does — or when there is no
        permit at all, which `consume` refuses on its own.

        This is what keeps rule 2 of this module's docstring true against a
        process running as the same user. `PermitStore.request` is idempotent
        while a permit is live: it returns the EXISTING permit, title and all.
        Every input to the challenge id is reachable by such a process —
        `machine_id` is a file, `session_id` is in `evidence/<task>/task.json`,
        `task_id` and `step_index` come back in tool results, and `broker.json`
        gives it the token — so it can pre-register the challenge for a future
        `money` step under the sentence "open example.com in the browser", let
        Hands ask for that step, and have the toast, `pending.json` and
        `firekeep hands status` all show the human ITS sentence. The permit
        stays bound to the real action, so this was never a way to act
        unapproved; it was a way to obtain informed-looking consent for
        something else, which is precisely the property the permit text
        exists to establish.

        It cannot be defeated by getting in first, because the honest title is
        recomputed here, on this side, every time."""
        if not isinstance(reply, dict):
            return None
        their_title = reply.get("title")
        their_classes = list(reply.get("classes") or [])
        if their_title == title and their_classes == list(classes):
            return None
        hooklog.log_failure(
            "hands",
            f"permit {challenge} already existed describing something else: "
            f"{their_title!r} {their_classes} vs {title!r} {list(classes)} — refusing",
        )
        return {
            "ok": False,
            "error": ("permit_tampered: a permit for this step already existed with a "
                      "different description; refusing"),
            "classes": list(classes),
        }

    def _widen(self, widen: object, decision) -> None:
        """Add what a consumed `boundary` permit approved to this task's scope,
        for the life of this task only.

        Without it, making `boundary` the catch-all it is documented to be
        would demand a chord per click in an undeclared app — which is not
        security, it is a queue of prompts nobody reads by the fifth one. With
        it the human is asked once, "may this task operate Mail", and the
        answer holds until `hands_task_end`.

        `widen` comes from `_gate`, which got it from `_titled` — the same call
        that built the sentence the human read. It is deliberately NOT
        recomputed here from `policy.boundary_apps`, which is what this used to
        do: that function reports the control's app AND the window's app, while
        the title named only the window's, so a model could foreground a
        declared app, reach an undeclared one by `hands_find(app=…)`, and have
        the human approve a sentence about Notepad while the task gained Excel.
        Deriving the widening from anything but the title reopens that.

        Apps and hosts are separate for the same reason they are separate in
        `policy.boundary_apps`: a name in the app list must not clear a
        navigation to a host of that name.

        Three things it deliberately is not. It is not `policy.remembered`:
        nothing is written to disk and the next task starts from what it
        declares. It does not widen any other class — a `send` inside the
        now-declared app still needs its own permit, because the app is the
        scope, not the permission. And it never adds a name the human was not
        shown."""
        if "boundary" not in decision.classes or not isinstance(widen, dict):
            return
        for name in widen.get("apps") or ():
            if name and not _already(name, self.task_apps):
                self.task_apps.append(name)
        for host in widen.get("hosts") or ():
            if host and not _already(host, self.task_hosts):
                self.task_hosts.append(host)

    def _permit_title(self, routed: Routed, window: WindowInfo | None,
                      classes: tuple[str, ...] = ()) -> str:
        return self._titled(routed, window, classes)[0]

    def _titled(self, routed: Routed, window: WindowInfo | None,
                classes: tuple[str, ...]) -> tuple[str, list[str], list[str]]:
        """`(title, apps, hosts)` — the sentence the human is shown, and
        EXACTLY the names that sentence puts in front of them.

        One function returning all three because they have to agree. The
        widening used to recompute its own list from `policy.boundary_apps`,
        which returns the control's app AND the window's app; the title named
        only the window's. So a model could foreground a declared app, reach
        an undeclared one with `hands_find(app="Excel")`, click it, and the
        human would approve `invoke "Save" in Notepad` while the task silently
        gained Excel. The title now names the control's app when it differs,
        with the window as context, and the widening is whatever this returned.

        Built from the ROUTED step — never from a caller-supplied string —
        because the broker renders whatever it is given and this is the one
        sentence the approval rests on.

        Three kinds have no observed control to name, only a string the
        runtime chose: an app to open, an app to focus, a chord to press.
        Those are still shown, because refusing to say WHICH app would make
        the prompt useless — but they are rendered in their own shape,
        `... the runtime asked for: "..."`, and never in the
        `invoke "X" in Y` form reserved for something Hands actually
        observed. A human seeing the second form is looking at a real
        control; the first form is a request, and reads like one."""
        window_app = str(window.app or "") if window is not None else ""
        where = _clean(window_app or (window.title if window else ""), 40) or "this machine"
        if routed.control is not None:
            if routed.route == "browser":
                # "in browser" tells the human nothing they need. Which SITE
                # is about to be ordered from is the whole question, and a
                # navigation prompt already names its host — a click prompt
                # should not be vaguer than the navigation that reached it.
                # The suffix appears only when this step is a crossing, so the
                # sentence names the thing the task is about to gain.
                title = f'{routed.kind} "{_clean(routed.control.name)}" on {self._browser_host()}'
                if "boundary" in classes:
                    title += " (in the browser)"
                return title, [BROWSER_APP], []
            control_app = str(routed.control.app or "")
            target = control_app or window_app
            title = f'{routed.kind} "{_clean(routed.control.name)}" in {_clean(target, 40) or where}'
            if control_app and window_app and control_app.lower() != window_app.lower():
                # The control is not in the window that is in front. Say both:
                # the app being operated is what the approval is about, and the
                # foreground is what the human is actually looking at.
                title += f" (window: {_clean(window_app, 40)})"
            return title, ([target] if target else []), []
        if routed.kind == "open_url":
            host = urlsplit(str(routed.payload.get("url", ""))).hostname or ""
            return (f"open {_clean(host or 'an unnamed host')} in the browser",
                    [], ([host] if host else []))
        if routed.kind == "open_app":
            app = str(routed.payload.get("app", ""))
            return (f'open the app the runtime asked for: "{_clean(app)}"',
                    ([app] if app.strip() else []), [])
        if routed.kind == "focus_app":
            app = str(routed.payload.get("app", ""))
            return ("switch to the app the runtime asked for: "
                    f'"{_clean(app)}"', ([app] if app.strip() else []), [])
        if routed.kind == "key":
            return (f'press the keys the runtime asked for: '
                    f'"{_clean(routed.payload.get("chord", ""), 24)}" in {where}',
                    ([window_app] if window_app else []), [])
        if routed.kind == "type":
            return f"type text into {where}", ([window_app] if window_app else []), []
        if routed.kind == "clipboard_set":
            return f"set the clipboard on {where}", ([window_app] if window_app else []), []
        return f"{routed.kind} in {where}", ([window_app] if window_app else []), []

    def _execute(self, routed: Routed, window: WindowInfo | None) -> dict | None:
        """Routed step -> backend call. `routing` already chose accessibility
        over pixels and resolved every ref, so this is a dispatch table and
        nothing more; no decision is taken here.

        Returns whatever the call knows that the caller does not — today only
        `open_url`, whose `{"url", "title", "loaded"}` is merged into the tool
        result. A navigation that finished without the load event is still a
        step that happened (`outcome: "ok"`); `loaded: False` is how the
        runtime learns to look before it acts."""
        kind, payload, point = routed.kind, routed.payload, routed.point
        if kind == "invoke":
            if routed.route == "accessibility":
                self.backend.invoke(routed.control)
            else:
                self.backend.click(point)
        elif kind == "set_value":
            if routed.route == "accessibility":
                self.backend.set_value(routed.control, payload["value"])
            else:
                self.backend.click(point)
                self.backend.key("cmd+a" if sys.platform == "darwin" else "ctrl+a")
                self.backend.type_text(payload["value"])
        elif kind == "click":
            self.backend.click(point, button=payload.get("button", "left"),
                               double=bool(payload.get("double", False)))
        elif kind == "type":
            self.backend.type_text(payload["text"])
        elif kind == "key":
            self.backend.key(payload["chord"])
        elif kind == "scroll":
            self.backend.scroll(point or _window_centre(window), payload["dy"])
        elif kind == "focus_app":
            self.backend.focus_app(payload["app"])
        elif kind == "open_app":
            self.backend.open_app(payload["app"])
        elif kind == "open_url":
            result = self._require_browser().navigate(payload["url"])
            # A new document: every ref from the old one is gone, and the
            # probe's generation counter has reset behind us.
            self._browser_controls = {}
            self._remember_page(result)
            return result if isinstance(result, dict) else None
        elif kind == "clipboard_set":
            self.backend.clipboard_set(payload["text"])
        else:  # wait
            time.sleep(max(0.0, min(10.0, float(payload["seconds"]))))
        return None

    def _update_focus_hint(self, routed: Routed) -> None:
        """Remember what a step targeted, so the next `type` can be judged by
        where the keystrokes are about to land.

        Only the three kinds that move focus set it. `focus_app`/`open_app`
        clear it: focus has gone somewhere this session knows nothing about,
        and a stale hint is worse than none — it would let a `type` be judged
        against a control in a window that is no longer in front."""
        if routed.kind in ("click", "invoke", "set_value") and routed.control is not None:
            self._focus_hint = routed.control
        elif routed.kind in ("focus_app", "open_app"):
            self._focus_hint = None

    def _capture(self, *, via_browser: bool = False, tab: str | None = None) -> bytes | None:
        """A before/after screenshot for a protected step — best effort.

        A browser step is photographed through the browser, on the tab the
        step targets: the page is what the human approved, and on a machine
        where the browser is not the foreground window — or where the step
        named a background tab — anything else would show the wrong thing.

        The permit has already been consumed by the time this runs, so a
        machine that cannot screenshot (no `mss`, no Screen Recording
        permission, a CDP transport that just dropped) must not turn the
        human's approval into a refusal. A failure is logged and the ledger
        line simply carries no image."""
        try:
            if via_browser:
                return self._require_browser().screenshot(
                    tab=tab, max_width=self.config.screenshot_max_width)
            observation = self.backend.observe(
                app=None, region=None, max_nodes=1, text_budget=0,
                screenshot=True, max_width=self.config.screenshot_max_width,
            )
        except Exception as exc:  # noqa: BLE001 — evidence must never break the step
            hooklog.log_failure("hands", f"screenshot capture failed: {exc}", exc)
            return None
        return observation.screenshot_png

    # -- the browser -------------------------------------------------------

    def browser_op(self, op: str, **kwargs) -> dict:
        """One browser step, gated exactly like a native one.

        Navigation goes back through `act` so the boundary class applies to a
        URL the way it would to any other action. `click` and `fill` are
        classified here instead: a web page's "Place order" is the same
        decision as a native one, and a password `<input>` is the same
        credential target as a `PasswordBox` — the surface a button is drawn
        on is not a reason to ask the human less often.

        The synthetic `Control` those two are judged against comes from the
        last page scan, so a ref this session has not seen is refused rather
        than run unclassified: the descriptor and the ref age together, and
        acting on a ref with no descriptor would be acting with no classifier.

        Everything else runs directly, and every op is a ledgered step
        against the same budget: a task that clicks its way through a page
        has done that many things, whatever surface it did them on."""
        if op in _BROWSER_NAVIGATE:
            url = kwargs.get("url")
            if not url:
                return {"ok": False, "error": "invalid_action: navigate needs a url"}
            return self.act({"kind": "open_url", "url": url}, permit=kwargs.get("permit"))

        guard = self._step_guard()
        if guard is not None:
            return guard
        if op not in _BROWSER_DIRECT:
            return {"ok": False, "error": f"invalid_action: unknown browser op {op!r}"}

        ledger_action = _browser_action(op, kwargs)
        classes: tuple[str, ...] = ()
        permit_record = None
        control = None
        if op in ("click", "fill"):
            gated = self._gate_browser(op, kwargs, ledger_action)
            if "error" in gated or "needs_permit" in gated:
                return gated
            classes, permit_record, control = gated["classes"], gated["permit"], gated["control"]

        tab = kwargs.get("tab")
        index = self.step_index
        payload: dict = {}
        # Non-empty classes means the permit gate ran and a human approved,
        # so this step gets the same before/after pair a native protected
        # step does — photographed through the browser.
        before = self._capture(via_browser=True, tab=tab) if classes else None
        outcome, error, evidence_error = "ok", None, None
        try:
            try:
                browser = self._require_browser()
                if op == "open":
                    payload = browser.open()
                elif op == "tabs":
                    payload = {"tabs": browser.tabs()}
                elif op == "read":
                    payload = browser.read(tab=tab, budget=self.config.text_budget)
                elif op == "find":
                    payload = {"controls": browser.find(
                        str(kwargs.get("query", "")), tab=tab,
                        limit=_clamp(kwargs.get("limit"), 10, _FIND_LIMIT_CEILING))}
                elif op == "click":
                    browser.click(str(kwargs.get("ref", "")), tab=tab)
                elif op == "fill":
                    browser.fill(str(kwargs.get("ref", "")), str(kwargs.get("text", "")), tab=tab)
                else:  # screenshot
                    payload = {"screenshot_png": browser.screenshot(
                        tab=tab, max_width=self.config.screenshot_max_width)}
            except HandsError as exc:
                outcome, error = "error", f"{exc.code}: {exc}"
            except Exception as exc:  # noqa: BLE001 — same rule as `act`: an
                # attempted step is a recorded, counted step. The CDP
                # transport raises its own errors, not only HandsError.
                hooklog.log_failure("hands", f"browser step {index} failed: {exc}", exc)
                outcome, error = "error", f"backend: {exc}"
            after = self._capture(via_browser=True, tab=tab) if classes else None
            evidence_error = self._record(
                index, _redact(ledger_action, classes, control), "browser",
                classes, permit_record, before, after, outcome, error,
            )
        finally:
            self._advance()
        if outcome == "ok":
            if op == "find":
                self._browser_controls = {
                    str(c.get("ref")): c for c in payload.get("controls", []) if c.get("ref")
                }
                self._refresh_page_url(tab)
            elif op in ("read", "open", "tabs"):
                self._remember_page(payload if op == "read" else _first_tab(payload))
            elif op in ("click", "fill") and control is not None:
                self._focus_hint = control
        result = {"ok": outcome == "ok", "step_index": index, "route": "browser",
                  "op": op, "classes": list(classes), "error": error}
        result.update(_browser_payload(payload))
        if evidence_error:
            result["evidence_error"] = evidence_error
        return result

    def _gate_browser(self, op: str, kwargs: dict, ledger_action: dict) -> dict:
        """Classify a browser `click`/`fill` and, if it is protected, run it
        through the same permit gate a native step goes through.

        The action handed to `policy.decide` is deliberately spelled as its
        native equivalent (`click` / `set_value`) — the classifier keys
        `send` off click-like kinds and `credential` off `set_value` into a
        password role, and a web button should reach exactly those rules. The
        hash the permit is bound to is still the real browser action, so the
        permit cannot be replayed on the native surface or on a different op."""
        ref = str(kwargs.get("ref", ""))
        control = self._browser_control(ref)
        if control is None:
            return {"ok": False, "error":
                    f"stale_ref: {ref!r} is not in the current page scan; "
                    "run hands_browser op=find again"}
        if op == "click":
            policy_action = {"kind": "click", "ref": ref}
        else:
            policy_action = {"kind": "set_value", "ref": ref, "value": str(kwargs.get("text", ""))}
        window = WindowInfo(BROWSER_APP, self._browser_page.get("title", ""), 0, Rect(0, 0, 0, 0))
        url = self._browser_page.get("url")
        # `browser_step=True` is what lets the reserved `browser` token count
        # here and nowhere else — a NATIVE window whose process happens to be
        # called "browser" (Yandex ships `browser.exe`) must not be cleared by
        # a declaration that was about the web.
        decision = decide(policy_action, control, window, url, self.policy, self.task_apps,
                          task_hosts=self.task_hosts, browser_step=True)
        if decision.verdict != "permit":
            return {"classes": decision.classes, "permit": None, "control": control}
        routed = Routed(policy_action["kind"], "browser", control, None, {})
        gate = self._gate(ledger_action, routed, window, decision, kwargs.get("permit"))
        if "error" in gate or "needs_permit" in gate:
            return gate
        # A consumed `boundary` here approved operating the browser at all, so
        # the rest of the task's clicks in it are not re-prompted. Which SITE
        # it may reach is unaffected: that is the domain allowlist's job plus
        # this task's approved hosts, and every navigation still goes through
        # both.
        self._widen(gate.get("widen"), decision)
        return {"classes": decision.classes, "permit": gate["permit"], "control": control}

    def _browser_control(self, ref: str) -> Control | None:
        """The last scan's description of `ref`, as a `Control` the classifier
        can read. None when this session never saw that ref — which is a
        refusal, not a fall-through to running it unclassified.

        `href` stands in for `value` only on an element with no accessible
        name: an unlabelled link is best described by where it goes, but a
        labelled one must be judged by its label. Folding the URL in
        regardless would classify a link to documentation about `remove` as
        destructive and turn ordinary reading into an approval queue."""
        found = self._browser_controls.get(ref)
        if found is None:
            return None
        role = str(found.get("role", ""))
        name = str(found.get("name", ""))
        value = str(found.get("value", "")) or (str(found.get("href", "")) if not name else "")
        rect = list(found.get("rect") or [0, 0, 0, 0])[:4]
        while len(rect) < 4:
            rect.append(0)
        return Control(
            ref=ref,
            role=_BROWSER_ROLES.get(role, role),
            name=name,
            value=value,
            rect=Rect(*(int(v) for v in rect)),
            app=BROWSER_APP,
            patterns=("Value",) if role in _FILLABLE_ROLES else ("Invoke",),
        )

    def _browser_host(self) -> str:
        host = urlsplit(str(self._browser_page.get("url", ""))).hostname
        return _clean(host or "an unnamed site", 40)

    def _refresh_page_url(self, tab: str | None) -> None:
        """Ask the browser where it currently is, cheaply.

        A `find` is often the only browser call a task makes before clicking,
        and the probe's result carries no URL — so without this the permit
        prompt for that click could not name the site, which is the one thing
        it most needs to say. `current_url` is a single `Target.getTargetInfo`
        call, no page evaluation.

        The page title is dropped whenever the URL changed: a title from the
        previous page is worse than none, because the classifier reads it the
        way it reads a window title."""
        try:
            url = str(self._require_browser().current_url(tab))
        except Exception as exc:  # noqa: BLE001 — a missing URL is not a failed step
            hooklog.log_failure("hands", f"could not read the current url: {exc}", exc)
            return
        previous = self._browser_page
        # Compare without the fragment: a single-page app that opens its
        # "Confirm payment" dialog on a hash route (`/pay` -> `/pay#confirm`)
        # is still the same page, and dropping its title there would blind the
        # classifier exactly where it needs the title most.
        import urllib.parse
        same_page = (urllib.parse.urldefrag(url).url
                     == urllib.parse.urldefrag(str(previous.get("url") or "")).url)
        title = previous.get("title", "") if same_page else ""
        self._browser_page = {"url": url, "title": title}

    def _remember_page(self, data: object) -> None:
        """The page a browser step last reported. It is the browser's
        equivalent of a window title, and the classifier reads it the way it
        reads one — a "Confirm payment" page names what a bare "OK" button
        will not. The URL is kept separately and deliberately NOT used as the
        title: a path is not a description, and matching destructive words
        against one would flag every click on a documentation page."""
        if isinstance(data, dict) and ("url" in data or "title" in data):
            self._browser_page = {"url": str(data.get("url") or ""),
                                  "title": str(data.get("title") or "")}

    def _require_browser(self):
        if self.browser is None:
            raise HandsError("unsupported", "no browser is available in this session")
        return self.browser

    # -- permits -----------------------------------------------------------

    def request_permit(self, challenge: str, wait_s: int = 45) -> dict:
        """Block until the human answers, or `wait_s` (capped at 55s so the
        call returns inside an MCP client's timeout).

        The permit this unblocks authorises the single step it was minted for
        and nothing else — there is no auto-remember in this release, and
        nothing here writes to `policy.json`. The one thing a consumed permit
        does carry forward is task-scoped: approving a `boundary` step adds
        that app (or host) to the apps this task declared, so the next click
        inside it is not a second prompt. It dies with the task."""
        broker = self._ensure_broker()
        if broker is None:
            return {"state": "unavailable", "challenge": challenge, "error": _BROKER_DOWN}
        timeout = min(max(0, int(wait_s or 0)), _MAX_WAIT_S)
        reply = broker.wait(challenge, timeout) or {}
        return {
            "state": reply.get("state"),
            "via": reply.get("via"),
            "challenge": challenge,
            "expires_in_s": reply.get("expires_in_s"),
        }

    def _ensure_broker(self) -> "BrokerClient | None":
        """Re-probe once when we have no broker: it is started at logon by a
        per-user `Run` registry value on Windows or a LaunchAgent on macOS,
        so it may well come up after the MCP server did, and
        a session that gave up at start-up would refuse every protected step
        for the rest of its life."""
        if self.broker is None:
            self.broker = BrokerClient.from_disk()
        return self.broker

    # -- step bookkeeping --------------------------------------------------

    def _no_task(self) -> dict | None:
        """Everything but `hands_status` needs a declared task behind it — a
        ledger to record into and a lease saying this machine is ours."""
        if self.task_id is None:
            return {"ok": False, "error": "no_task: call hands_task_start first"}
        return None

    def _step_guard(self) -> dict | None:
        guard = self._no_task()
        if guard is not None:
            return guard
        if self.step_index >= self.config.max_steps:
            return {"ok": False, "error":
                    f"budget: this task has used its {self.config.max_steps} step budget; "
                    "call hands_task_end"}
        return None

    def _advance(self) -> None:
        """Close out a step: renew the lease periodically, count it, and drop
        the observation. That last one is the important half — anything Hands
        just did may have moved or destroyed the very controls it was looking
        at, so every ref minted before this point is now suspect and the
        runtime is made to look again.

        The page scan is deliberately NOT dropped here, and the difference is
        not laziness. A native `hands_observe` costs no step, so "look again,
        then retry with the permit" lands on the same step index and the
        approval still fits the action. A browser `find` IS a step, so
        clearing per step would move the index between the refusal and the
        retry and make a browser permit impossible to spend. The probe keeps
        its own guard anyway: refs carry the scan generation they were minted
        in, a navigation resets that counter, and `data-hands-ref` resolves to
        the specific element it was set on rather than a position — so a ref
        this session still holds a description for is a ref the page still
        agrees is that element. `_execute` drops the scan on navigation, where
        the whole document changes underneath it."""
        if self.step_index and self.step_index % _RENEW_EVERY == 0:
            self.link.renew_lease()
        self.step_index += 1
        self.last_obs = None


# -- JSON shapes -----------------------------------------------------------


def _error(exc: HandsError) -> dict:
    return {"ok": False, "error": f"{exc.code}: {exc}"}


def _already(name: str, names: list[str]) -> bool:
    """Case-insensitive membership, matching `policy._named`'s rule — a task
    widened with "Notepad" must not gain "notepad" as a second entry."""
    needle = str(name).lower()
    return any(needle == str(n).lower() for n in names)


def _redact(action: dict, classes: tuple[str, ...], control: Control | None) -> dict:
    """The ledger's copy of an action, with a typed secret replaced.

    Only the ledger copy: the challenge the human approved is hashed from the
    real action, and the real action is what runs. Evidence should say a
    password was typed into that field at that moment — it should not be the
    place the password ends up living for `evidence_retention_days`."""
    if action.get("kind") not in _SECRET_KINDS:
        return action
    role = control.role if control is not None else None
    if "credential" not in classes and role not in _CREDENTIAL_ROLES:
        return action
    return {k: (_REDACTED if k in _SECRET_KEYS else v) for k, v in action.items()}


def _lease_refusal(lease: dict) -> str:
    """Who has the machine and until when.

    relay answers a lost race with `{"acquired": False, "held_by": …,
    "expires_in": <seconds>}` (`relay/app/leases.py`), so the wall-clock
    expiry is computed here — a countdown in seconds is not something a human
    reading an error message can act on. Both fields degrade to a plain
    "another session" / "it lapses" if relay ever stops sending them."""
    holder = str(lease.get("held_by") or "another session")
    try:
        seconds = max(0, int(lease.get("expires_in")))
    except (TypeError, ValueError):
        return (f"hands is leased by {holder} — wait for it to lapse or end that session")
    expires_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    return (f"hands is leased by {holder} until {expires_at} — "
            "wait for it to lapse or end that session")


def _browser_payload(payload: object) -> dict:
    """The part of a browser result that may be merged into a tool result."""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if k in _BROWSER_RESULT_KEYS}


def _clamp(value: object, default: int, ceiling: int) -> int:
    """A caller-supplied count, bounded. `None` means "use the default";
    anything that is not a whole number is a refusal rather than a silent
    fallback, because a caller that asked for `"lots"` has a bug and should
    be told so."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise HandsError("invalid_action", f"expected a whole number, not {type(value).__name__}")
    return max(1, min(value, ceiling))


def _first_tab(payload: object) -> dict:
    """The current tab out of an `open`/`tabs` result, for the page context."""
    tabs = payload.get("tabs") if isinstance(payload, dict) else None
    return tabs[0] if isinstance(tabs, list) and tabs and isinstance(tabs[0], dict) else {}


def _control_json(control: Control) -> dict:
    return {
        "ref": control.ref,
        "role": control.role,
        "name": control.name,
        "value": control.value,
        "rect": [control.rect.x, control.rect.y, control.rect.w, control.rect.h],
        "app": control.app,
        "enabled": control.enabled,
    }


def _window_json(window: WindowInfo | None) -> dict | None:
    if window is None:
        return None
    return {
        "app": window.app,
        "title": window.title,
        "pid": window.pid,
        "rect": [window.rect.x, window.rect.y, window.rect.w, window.rect.h],
        "elevated": window.elevated,
    }


def _window_centre(window: WindowInfo | None) -> tuple[int, int]:
    if window is None:
        raise HandsError("not_found", "there is no active window to scroll")
    return window.rect.center()


def _browser_action(op: str, kwargs: dict) -> dict:
    """What a browser step looks like in the ledger. `permit` is dropped —
    it is bookkeeping, not part of what was done."""
    action = {"kind": f"browser.{op}"}
    action.update({k: v for k, v in kwargs.items() if k != "permit" and v is not None})
    return action
