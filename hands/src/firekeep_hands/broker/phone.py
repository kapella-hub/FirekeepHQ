"""The phone path: a pending permit becomes a relay task the human can
answer from the dashboard, wherever they are.

**This path is OFF by default (`phone_approvals`, default False) and this is
the reason.** Relay records no actor on a task update — `relay/app/tasks.py`
`update_task` writes status, result and assignee and never the completing
principal — so "the task says approve" means "somebody holding the workspace
key completed it", not "a person decided". The driving agent holds that key
and reaches `relay_task_list` and `relay_task_update` through the same MCP
surface it already has, so with this on it can post a step for approval,
complete its own `hands_permit:` task with the result `approve`, and be
granted the permit within one poll. A local one-time code would not fix it
either: Hands can screenshot the screen the code is on. Turning
`phone_approvals` on means deliberately trusting every holder of the
workspace key, and it stays off until relay records who completed a task
(PR2). The chord path has no such hole — it needs a physical keystroke.

Given that it is on, the bridge only ever copies an answer across:
`permit_task_state` returning anything other than the two literals
`"approve"` and `"deny"` (a task still open, a Keep that is down, a garbled
reply) leaves the permit pending.

`tick()` is one poll and is what the tests drive; `run()` is `tick()` in a
loop. Nothing in `tick()` may raise: it runs on a daemon thread whose death
would silently remove the phone path while `/health` still advertises it.
"""
from __future__ import annotations

import datetime
import logging
import threading

from firekeep_client import hooklog

from .permits import PermitStore

log = logging.getLogger(__name__)


class PhoneBridge(threading.Thread):
    def __init__(self, store: PermitStore, link, poll_s: float = 3.0):
        super().__init__(name="hands-phone-bridge", daemon=True)
        self.store = store
        self.link = link
        self.poll_s = float(poll_s)
        self._stop = threading.Event()
        # challenge -> relay task id, for tasks this bridge opened and has
        # not yet closed. Kept here rather than read back off the permit so
        # a swept permit still leaves a task to close.
        self._tasks: dict[str, str] = {}
        # Challenges the phone itself answered. Their relay task is already
        # resolved by the person who answered it; cancelling it afterwards
        # would overwrite their answer with "cancelled".
        self._answered_here: set[str] = set()

    # -- the loop ---------------------------------------------------------

    def run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            if self._stop.wait(self.poll_s):
                break

    def stop(self) -> None:
        self._stop.set()

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
                task_id = self.link.post_permit_task(
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
            if task_id:
                permit.phone_task_id = str(task_id)
                self._tasks[permit.challenge] = str(task_id)
                log.debug("posted permit %s as relay task %s", permit.challenge, task_id)

    def _poll_open(self) -> None:
        for permit in self.store.pending():
            if not permit.phone_task_id:
                continue
            try:
                answer = self.link.permit_task_state(permit.challenge)
            except Exception as exc:  # noqa: BLE001 - retried next tick
                hooklog.log_failure("hands", f"could not read permit task: {exc}", exc)
                continue
            if answer not in ("approve", "deny"):
                continue
            if self.store.decide(permit.challenge, answer, via="phone"):
                self._answered_here.add(permit.challenge)
                log.info("permit %s %sd from the dashboard", permit.challenge, answer)

    def _close_resolved(self) -> None:
        for challenge, task_id in list(self._tasks.items()):
            permit = self.store.get(challenge)
            if permit is not None and permit.state == "pending":
                continue
            self._tasks.pop(challenge, None)
            if challenge in self._answered_here:
                self._answered_here.discard(challenge)
                continue
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
