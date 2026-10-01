"""The phone path: a pending permit becomes a relay task the human can
answer from the dashboard, wherever they are.

**Who answered is checked, not assumed (THREAT-MODEL row 12, §5.8).** Relay
stamps the verified principal of every task write — `created_by` when the
broker posts the permit task, `completed_by` when somebody resolves it — from
the API key the auth layer verified, never from a label the caller sets.
Before this, "the task says approve" meant only "somebody holding the
workspace key completed it", and the driving agent holds that key: it reaches
`relay_task_list`/`relay_task_update` through the same gateway, with the
same kit credential the broker posts with, and could approve its own step.

An approve is honoured only when `approval_refusal` finds nothing wrong:
both stamps present and authenticated, the same workspace, a completing
credential that is NOT the one that posted the task, and — when
`phone_approvers` is set — a completing credential on that list. Anything
else denies the permit (fail closed) with a log line naming the cause, and
the relay task is closed as `refused: <cause>` so the dashboard shows it. A
relay that predates the stamps, and a Keep with authentication off (every
caller is then the same anonymous owner), refuse every phone approval.

**Deny is never gated.** Refusing a step is the safe direction; anybody may.

**What is still trusted:** any OTHER credential in the workspace — a second
machine's kit key, a teammate's — unless `phone_approvers` pins the
approvers (typically to the dashboard's credential). A local one-time code
would not help: Hands can screenshot the screen the code is on.

Note what the phone path has that the chord path needs `notify.py` for: the
relay task carries the step's title and classes, so a person answering from
the dashboard is reading what they are approving.

`tick()` is one poll and is what the tests drive; `run()` is `tick()` in a
loop. Nothing in `tick()` may raise: it runs on a daemon thread whose death
would silently remove the phone path while `/health` still advertises it.
"""
from __future__ import annotations

import datetime
import logging
import threading

from firekeep_client import hooklog

from ..keep import answer_from_task
from .permits import PermitStore

log = logging.getLogger(__name__)

_PREDATES = (
    "relay did not record {who} — the Keep predates principal stamping on relay "
    "tasks; upgrade the server before using phone approvals"
)


def approval_refusal(created_by, completed_by, approvers: tuple[str, ...] = ()) -> str | None:
    """Why an approve on a permit task must NOT be honoured, or None if it may.

    `created_by` is relay's stamp for whoever posted the task (this broker,
    through the machine's kit key); `completed_by` is the stamp for whoever
    resolved it. Every branch that cannot prove "somebody other than the
    requester, in this workspace, authenticated" refuses."""
    if not isinstance(created_by, dict):
        return _PREDATES.format(who="who requested the permit")
    if not isinstance(completed_by, dict):
        return _PREDATES.format(who="who approved the permit")
    if created_by.get("authenticated") is not True or completed_by.get("authenticated") is not True:
        return (
            "the Keep has authentication disabled, so every caller is the same anonymous "
            "owner and an approval cannot be told apart from the agent's own; phone "
            "approvals need AUTH_ENABLED=true on the Keep"
        )
    approver = str(completed_by.get("credential_id") or "").strip()
    requester = str(created_by.get("credential_id") or "").strip()
    if not approver or not requester:
        return "the approval carries no credential id"
    if completed_by.get("workspace_id") != created_by.get("workspace_id"):
        return f"approved by credential {approver} from a different workspace"
    if approver == requester:
        return (
            f"approved by the requesting credential {approver} — this machine's own key, "
            "which the driving agent also holds"
        )
    if approvers and approver not in approvers:
        return f"approved by credential {approver}, which is not in phone_approvers"
    return None


class PhoneBridge(threading.Thread):
    def __init__(self, store: PermitStore, link, poll_s: float = 3.0,
                 approvers: tuple[str, ...] = ()):
        super().__init__(name="hands-phone-bridge", daemon=True)
        self.store = store
        self.link = link
        self.poll_s = float(poll_s)
        # Credential ids allowed to approve; empty means "any authenticated
        # credential in the workspace other than the requester's".
        self.approvers = tuple(approvers)
        # Not `_stop`: `threading.Thread` owns a private `_stop()` method that
        # `join()` calls on Python 3.11, and an Event under that name makes
        # every join raise "'Event' object is not callable".
        self._stop_event = threading.Event()
        # challenge -> relay task id, for tasks this bridge opened and has
        # not yet closed. Kept here rather than read back off the permit so
        # a swept permit still leaves a task to close.
        self._tasks: dict[str, str] = {}
        # Challenges the phone itself answered. Their relay task is already
        # resolved by the person who answered it; cancelling it afterwards
        # would overwrite their answer with "cancelled".
        self._answered_here: set[str] = set()
        # challenge -> why an approve on its task was refused. The task is
        # then closed with that reason rather than a bare "denied", so the
        # person looking at the dashboard sees why their tap did nothing.
        self._refused: dict[str, str] = {}

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        while not self._stop_event.is_set():
            self.tick()
            if self._stop_event.wait(self.poll_s):
                break

    def stop(self) -> None:
        self._stop_event.set()

    def tick(self) -> None:
        """Post, poll, close. Each step guards itself so a Keep that fails
        one call still gets the other two attempted this round."""
        for step in (self._post_new, self._poll_open, self._close_resolved):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - a poll must never kill the bridge
                hooklog.log_failure("hands", f"phone bridge {step.__name__} failed: {exc}", exc)

    # -- steps ------------------------------------------------------------

    def _post_new(self) -> None:
        for permit in self.store.pending():
            if permit.phone_task_id:
                continue
            try:
                task = self.link.post_permit_task(
                    challenge=permit.challenge,
                    title=permit.title,
                    classes=permit.classes,
                    task_id=permit.task_id,
                    step_index=permit.step_index,
                    expires_at=self._expires_at_iso(permit),
                )
            except Exception as exc:  # noqa: BLE001 - retried next tick
                hooklog.log_failure("hands", f"could not post permit task: {exc}", exc)
                continue
            task_id = task.get("id") if isinstance(task, dict) else None
            if task_id:
                permit.phone_task_id = str(task_id)
                self._tasks[permit.challenge] = str(task_id)
                log.debug("posted permit %s as relay task %s", permit.challenge, task_id)

    def _poll_open(self) -> None:
        for permit in self.store.pending():
            if not permit.phone_task_id:
                continue
            try:
                task = self.link.permit_task(permit.challenge, permit.phone_task_id)
            except Exception as exc:  # noqa: BLE001 - retried next tick
                hooklog.log_failure("hands", f"could not read permit task: {exc}", exc)
                continue
            # Only the task this bridge posted can answer for its permit.
            # `KeepLink.permit_task` already reads by id; checking again here
            # keeps a decoy out even if a link implementation does not.
            if not isinstance(task, dict) or task.get("id") != permit.phone_task_id:
                continue
            answer = answer_from_task(task)
            if answer not in ("approve", "deny"):
                continue
            if answer == "approve":
                refusal = approval_refusal(task.get("created_by"), task.get("completed_by"), self.approvers)
                if refusal is not None:
                    self._refuse(permit.challenge, refusal)
                    continue
            if self.store.decide(permit.challenge, answer, via="phone"):
                self._answered_here.add(permit.challenge)
                log.info("permit %s %sd from the dashboard", permit.challenge, answer)

    def _refuse(self, challenge: str, reason: str) -> None:
        """Deny the permit an untrusted approve arrived for. Denying rather
        than leaving it pending makes the refusal immediate and visible:
        the step fails now with a reason in the log, instead of expiring a
        minute later with none."""
        message = f"phone approval for permit {challenge} refused: {reason}"
        log.warning(message)
        hooklog.log_failure("hands", message)
        if self.store.decide(challenge, "deny", via="phone-refused"):
            self._refused[challenge] = reason

    def _close_resolved(self) -> None:
        for challenge, task_id in list(self._tasks.items()):
            permit = self.store.get(challenge)
            if permit is not None and permit.state == "pending":
                continue
            self._tasks.pop(challenge, None)
            if challenge in self._answered_here:
                self._answered_here.discard(challenge)
                continue
            refusal = self._refused.pop(challenge, None)
            if refusal is not None:
                result = f"refused: {refusal}"
            else:
                result = permit.state if permit is not None else "expired"
            try:
                self.link.close_permit_task(task_id, result)
            except Exception as exc:  # noqa: BLE001 - the task is stale either way
                hooklog.log_failure("hands", f"could not close permit task: {exc}", exc)

    # -- helpers ----------------------------------------------------------

    def _expires_at_iso(self, permit) -> str:
        """The permit's deadline as a wall-clock UTC timestamp. The store's
        clock is monotonic — meaningful for measuring, meaningless to show a
        human on a phone — so the remaining seconds are added to `now`."""
        remaining = max(0.0, permit.expires_at - self.store.now())
        when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=remaining)
        return when.strftime("%Y-%m-%dT%H:%M:%SZ")
