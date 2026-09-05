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

There is deliberately no auto-remember path in this release: an approval is
good for exactly one step, and `firekeep hands allow` is the only thing that
writes a standing allowance into `policy.json`.
"""
from __future__ import annotations

import secrets
import sys
import time
from urllib.parse import urlsplit

from firekeep_client import hooklog

from . import ids, paths
from .backends.base import Control, HandsError, Observation, Rect, WindowInfo
from .broker.client import BrokerClient
from .evidence import Ledger, prune
from .policy import decide
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
        self.ledger: Ledger | None = None
        self.action_id: str | None = None
        self.step_index = 0
        self.last_obs: Observation | None = None
        self.last_task: dict | None = None

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
                "step_index": self.step_index,
                "steps_left": max(0, self.config.max_steps - self.step_index),
                "evidence": str(self.ledger.dir) if self.ledger else None,
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
        if health is None:
            return (
                "No approval broker is running, so every protected step is refused. "
                "Start it with `firekeep-hands-broker run`."
            )
        phone = listeners.get("phone", "off")
        return (
            f"A protected step needs the {health.get('chord')} chord on this keyboard "
            f"(listener: {listeners.get('chord', 'unknown')}). Phone approvals are "
            f"{phone}; turn them on with `firekeep hands config set phone_approvals true` "
            "and restart the broker. docs/guides/hands.md explains what that trusts."
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
            raise HandsError(
                "busy",
                "another Hands session holds this machine's lease; wait for it to finish "
                "(the lease expires on its own) or check `firekeep hands status`",
            )

        self.task_id = task_id
        self.goal = goal
        self.task_apps = task_apps
        self.ledger = ledger
        self.step_index = 0
        self.last_obs = None
        self.action_id = self.link.action_before(goal=goal, task_id=task_id, apps=task_apps)
        return {
            "ok": True,
            "task_id": task_id,
            "goal": goal,
            "apps": task_apps,
            "evidence": str(ledger.dir),
            "max_steps": self.config.max_steps,
            "keep": "offline" if getattr(self.link, "offline", True) else "online",
        }

    def task_end(self, outcome: str, summary: str = "") -> dict:
        """Close the ledger, tell the Keep how it went, release the machine.

        The browser is deliberately left open: a human may be part way
        through a login in it, and closing it would throw that away."""
        if self.task_id is None:
            raise HandsError("no_task", "no task is open")
        if outcome not in _OUTCOMES:
            raise HandsError("invalid_action", f"outcome must be one of {list(_OUTCOMES)}")
        self.ledger.close(outcome, summary)
        result = {
            "ok": True,
            "task_id": self.task_id,
            "outcome": outcome,
            "summary": summary,
            "steps": len(self.ledger.steps()),
            "evidence": str(self.ledger.dir),
        }
        self.link.action_after(self.action_id, outcome, summary)
        self.link.release_lease()
        self.last_task = dict(result)
        self._reset()
        return result

    def _reset(self) -> None:
        self.task_id = None
        self.goal = ""
        self.task_apps = []
        self.ledger = None
        self.action_id = None
        self.step_index = 0
        self.last_obs = None

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
            max_nodes=int(max_nodes or self.config.max_nodes),
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
        matches = self.backend.find(query, role=role, app=app, limit=int(limit))
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
        attempted is part of the record whether or not it worked."""
        guard = self._step_guard()
        if guard is not None:
            return guard

        try:
            routed = route(action, self.last_obs)
        except HandsError as exc:
            return _error(exc)

        window = self.backend.active_window()
        decision = decide(action, routed.control, window, action.get("url"),
                          self.policy, self.task_apps)

        permit_record = None
        if decision.verdict == "permit":
            gate = self._gate(action, routed, window, decision, permit)
            if "error" in gate or "needs_permit" in gate:
                return gate
            permit_record = gate["permit"]

        before = self._capture() if permit_record is not None else None
        outcome, error, extra = "ok", None, None
        try:
            extra = self._execute(routed, window)
        except HandsError as exc:
            outcome, error = "error", f"{exc.code}: {exc}"
        after = self._capture() if permit_record is not None else None

        index = self.step_index
        self.ledger.record(
            step_index=index, action=action, route=routed.route,
            classes=decision.classes, permit=permit_record,
            before_png=before, after_png=after, outcome=outcome, error=error,
        )
        self._advance()
        result = {"ok": outcome == "ok", "step_index": index, "route": routed.route,
                  "classes": list(decision.classes), "error": error}
        if extra:
            result.update(extra)
        return result

    def _gate(self, action: dict, routed: Routed, window: WindowInfo | None,
              decision, permit: str | None) -> dict:
        """The permit half of `act`, returning either `{"permit": ...}` once
        an approval has been consumed, or the refusal to hand back.

        Fails closed in three distinct ways, all of them naming the broker,
        because they are three different things to fix: no broker at all, a
        broker that stopped answering between the health check and the
        request, and a permit that is not (or is no longer) approved."""
        broker = self._ensure_broker()
        if broker is None:
            return {"ok": False, "error": _BROKER_DOWN, "classes": list(decision.classes)}

        challenge = ids.challenge_id_for(
            self.machine_id, self.session_id, self.task_id,
            self.step_index, ids.action_hash(action),
        )
        title = self._permit_title(routed, window)
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
            return {"ok": False, "needs_permit": needs}

        if not broker.consume(challenge):
            return {"ok": False, "error": "permit not approved, expired or already used",
                    "needs_permit": needs}

        granted = broker.get(challenge) or {}
        return {"permit": {"challenge": challenge, "via": granted.get("via")}}

    def _permit_title(self, routed: Routed, window: WindowInfo | None) -> str:
        """What the human sees. Built from the ROUTED step — the control the
        observation actually resolved and the window it lives in — never from
        a caller-supplied string, because the broker renders whatever it is
        given and this is the one sentence the approval rests on."""
        where = _clean(window.app or window.title, 40) if window is not None else "this machine"
        if routed.control is not None:
            return f'{routed.kind} "{_clean(routed.control.name)}" in {where}'
        if routed.kind == "open_url":
            host = urlsplit(str(routed.payload.get("url", ""))).hostname or "an unnamed host"
            return f"open {_clean(host)} in the browser"
        if routed.kind in ("open_app", "focus_app"):
            return f'{routed.kind} {_clean(routed.payload.get("app", ""))}'
        if routed.kind == "key":
            return f'press {_clean(routed.payload.get("chord", ""), 24)} in {where}'
        if routed.kind == "type":
            return f"type text into {where}"
        if routed.kind == "clipboard_set":
            return f"set the clipboard on {where}"
        return f"{routed.kind} in {where}"

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
            return result if isinstance(result, dict) else None
        elif kind == "clipboard_set":
            self.backend.clipboard_set(payload["text"])
        else:  # wait
            time.sleep(max(0.0, min(10.0, float(payload["seconds"]))))
        return None

    def _capture(self) -> bytes | None:
        """A before/after screenshot for a protected step — best effort.

        The permit has already been consumed by the time this runs, so a
        machine that cannot screenshot (no `mss`, no Screen Recording
        permission) must not turn the human's approval into a refusal. A
        failure is logged and the ledger line simply carries no image."""
        try:
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
        """One browser step.

        Navigation goes back through `act` so the boundary class in
        `policy.py` applies to it exactly as it would to any other action —
        a URL is the one browser operation that can leave the task's declared
        ground. The rest run directly, and are still ledgered steps against
        the same budget: a task that clicks its way through a page has done
        that many things, whatever surface it did them on."""
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

        tab = kwargs.get("tab")
        payload: dict = {}
        outcome, error = "ok", None
        try:
            browser = self._require_browser()
            if op == "open":
                payload = browser.open()
            elif op == "tabs":
                payload = {"tabs": browser.tabs()}
            elif op == "read":
                payload = browser.read(tab=tab, budget=self.config.text_budget)
            elif op == "find":
                payload = {"controls": browser.find(str(kwargs.get("query", "")), tab=tab,
                                                    limit=int(kwargs.get("limit", 10)))}
            elif op == "click":
                browser.click(str(kwargs.get("ref", "")), tab=tab)
            elif op == "fill":
                browser.fill(str(kwargs.get("ref", "")), str(kwargs.get("text", "")), tab=tab)
            else:  # screenshot
                payload = {"screenshot_png": browser.screenshot(
                    tab=tab, max_width=self.config.screenshot_max_width)}
        except HandsError as exc:
            outcome, error = "error", f"{exc.code}: {exc}"

        index = self.step_index
        self.ledger.record(
            step_index=index, action=_browser_action(op, kwargs), route="browser",
            classes=(), permit=None, before_png=None, after_png=None,
            outcome=outcome, error=error,
        )
        self._advance()
        result = {"ok": outcome == "ok", "step_index": index, "route": "browser",
                  "op": op, "error": error}
        result.update(payload)
        return result

    def _require_browser(self):
        if self.browser is None:
            raise HandsError("unsupported", "no browser is available in this session")
        return self.browser

    # -- permits -----------------------------------------------------------

    def request_permit(self, challenge: str, wait_s: int = 45) -> dict:
        """Block until the human answers, or `wait_s` (capped at 55s so the
        call returns inside an MCP client's timeout).

        Approving here never widens anything: there is no auto-remember in
        this release, so the permit authorises the single step it was minted
        for and nothing else."""
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
        """Re-probe once when we have no broker: it is started by a logon
        task / LaunchAgent and may well come up after the MCP server did, and
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
        runtime is made to look again."""
        if self.step_index and self.step_index % _RENEW_EVERY == 0:
            self.link.renew_lease()
        self.step_index += 1
        self.last_obs = None


# -- JSON shapes -----------------------------------------------------------


def _error(exc: HandsError) -> dict:
    return {"ok": False, "error": f"{exc.code}: {exc}"}


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
