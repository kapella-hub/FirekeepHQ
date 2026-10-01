import logging

import pytest

from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.phone import PhoneBridge, approval_refusal

# The credential the broker posts with: this machine's kit key. The driving
# agent reaches relay through the same gateway with the same key.
REQUESTER = {"workspace_id": "ws", "member_id": "member-owner",
             "credential_id": "cred-kit", "authenticated": True}
# A different credential in the same workspace — the dashboard's, say.
HUMAN = {"workspace_id": "ws", "member_id": "member-owner",
         "credential_id": "cred-dashboard", "authenticated": True}


class FakeLink:
    """Relay as the bridge sees it: tasks keyed by challenge, each carrying
    the principal stamps relay would put on it."""

    def __init__(self, created_by=REQUESTER):
        self.posted = []
        self.tasks = {}
        self.closed = []
        self.created_by = created_by

    def post_permit_task(self, **kw):
        self.posted.append(kw)
        task = {"id": "task-" + kw["challenge"], "status": "pending", "title": "hands_permit:" + kw["challenge"]}
        if self.created_by is not None:
            task["created_by"] = dict(self.created_by)
        self.tasks[kw["challenge"]] = task
        return dict(task)

    def permit_task(self, challenge, task_id):
        task = self.tasks.get(challenge)
        if task is None or task["id"] != task_id:
            return None
        return dict(task)

    def answer(self, challenge, status, result=None, by=HUMAN):
        task = self.tasks[challenge]
        task["status"] = status
        task["result"] = result
        if by is None:
            task.pop("completed_by", None)
        else:
            task["completed_by"] = dict(by)

    def close_permit_task(self, task_id, result):
        self.closed.append((task_id, result))


def _bridge(link=None, **kw):
    store = PermitStore(ttl_s=60)
    link = link or FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01, **kw)
    store.request(challenge="c", title="Send", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    return store, link, bridge


def test_bridge_posts_polls_and_decides():
    store, link, bridge = _bridge()
    assert link.posted[0]["challenge"] == "c" and store.get("c").phone_task_id == "task-c"
    link.answer("c", "completed", "approve")
    bridge.tick()
    assert store.get("c").state == "approved" and store.get("c").via == "phone"


def test_bridge_closes_task_when_permit_resolves_elsewhere():
    store, link, bridge = _bridge()
    store.decide("c", "approve", via="chord")
    bridge.tick()
    assert link.closed == [("task-c", "approved")]


# --- the approver must be somebody other than the requester (THREAT-MODEL #12)


def test_an_approval_by_the_requesting_credential_is_refused(caplog):
    """The hole PR2 closes: the driving agent holds the same kit key the
    broker posted with, so it can complete its own permit task. Relay now
    stamps who completed it, and the same credential is refused."""
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve", by=REQUESTER)
    with caplog.at_level(logging.WARNING):
        bridge.tick()
    permit = store.get("c")
    assert permit.state == "denied" and permit.via == "phone-refused"
    assert "requesting credential" in caplog.text
    bridge.tick()
    assert link.closed and link.closed[0][0] == "task-c"
    assert link.closed[0][1].startswith("refused:")


def test_an_approval_with_no_completing_principal_is_refused(caplog):
    """A relay that predates principal stamping returns no `completed_by`.
    That is indistinguishable from the agent approving itself, so it fails
    closed — with a message that says why."""
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve", by=None)
    with caplog.at_level(logging.WARNING):
        bridge.tick()
    assert store.get("c").state == "denied"
    assert "predates" in caplog.text


def test_a_relay_that_stamps_no_requester_refuses_every_approval(caplog):
    store, link, bridge = _bridge(FakeLink(created_by=None))
    link.answer("c", "completed", "approve", by=HUMAN)
    with caplog.at_level(logging.WARNING):
        bridge.tick()
    assert store.get("c").state == "denied"
    assert "predates" in caplog.text


def test_an_unauthenticated_keep_refuses_every_approval(caplog):
    """With auth off every caller is the same anonymous owner: nobody can be
    told apart from the agent, so no approval is honoured."""
    anon = {"workspace_id": "ws", "member_id": "member-owner",
            "credential_id": "anonymous", "authenticated": False}
    store, link, bridge = _bridge(FakeLink(created_by=anon))
    link.answer("c", "completed", "approve", by={**anon, "credential_id": "other"})
    with caplog.at_level(logging.WARNING):
        bridge.tick()
    assert store.get("c").state == "denied"
    assert "authentication" in caplog.text


def test_an_approval_from_another_workspace_is_refused():
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve", by={**HUMAN, "workspace_id": "elsewhere"})
    bridge.tick()
    assert store.get("c").state == "denied"


def test_an_approval_with_a_blank_credential_is_refused():
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve", by={**HUMAN, "credential_id": ""})
    bridge.tick()
    assert store.get("c").state == "denied"


def test_a_different_authenticated_credential_is_accepted():
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve", by=HUMAN)
    bridge.tick()
    assert store.get("c").state == "approved" and store.get("c").via == "phone"


def test_phone_approvers_pins_who_may_approve():
    """With `phone_approvers` set, only those credentials count — the
    dashboard's, typically — so a teammate's or a second machine's key is
    refused even though it is a different credential."""
    store, link, bridge = _bridge(approvers=("cred-dashboard",))
    link.answer("c", "completed", "approve", by={**HUMAN, "credential_id": "cred-teammate"})
    bridge.tick()
    assert store.get("c").state == "denied"

    store, link, bridge = _bridge(approvers=("cred-dashboard",))
    link.answer("c", "completed", "approve", by=HUMAN)
    bridge.tick()
    assert store.get("c").state == "approved"


@pytest.mark.parametrize("by", [REQUESTER, None, {**HUMAN, "authenticated": False}])
def test_a_deny_is_honoured_whoever_sent_it(by):
    """Deny is the safe direction: anyone may refuse a step."""
    store, link, bridge = _bridge()
    link.answer("c", "cancelled", "deny", by=by)
    bridge.tick()
    assert store.get("c").state == "denied" and store.get("c").via == "phone"


def test_a_decoy_task_under_the_same_title_is_ignored():
    """The bridge reads the task it posted, by id. A second task under the
    same `hands_permit:` title is not ours, whoever completed it."""
    class DecoyLink(FakeLink):
        def permit_task(self, challenge, task_id):
            if task_id != "task-c":
                return None
            return {"id": "task-decoy", "status": "completed", "result": "approve",
                    "created_by": dict(HUMAN), "completed_by": {**HUMAN, "credential_id": "cred-x"}}
    store, link, bridge = _bridge(DecoyLink())
    bridge.tick()
    assert store.get("c").state == "pending"


def test_approval_refusal_names_the_cause():
    assert approval_refusal(REQUESTER, HUMAN) is None
    assert "requesting credential" in approval_refusal(REQUESTER, REQUESTER)
    assert "predates" in approval_refusal(REQUESTER, None)
    assert "predates" in approval_refusal(None, HUMAN)
    assert "phone_approvers" in approval_refusal(REQUESTER, HUMAN, approvers=("cred-other",))


# --- additions -------------------------------------------------------------


def test_each_permit_is_posted_once_and_each_task_closed_once():
    store, link, bridge = _bridge()
    bridge.tick()
    bridge.tick()
    assert len(link.posted) == 1
    store.decide("c", "deny", via="chord")
    bridge.tick()
    bridge.tick()
    assert link.closed == [("task-c", "denied")]


def test_a_phone_deny_denies_the_permit():
    store, link, bridge = _bridge()
    link.answer("c", "cancelled", "deny")
    bridge.tick()
    assert store.get("c").state == "denied" and store.get("c").via == "phone"


def test_a_permit_the_phone_resolved_is_not_also_cancelled():
    """The dashboard already closed that relay task by approving it; cancelling
    it afterwards would overwrite the human's answer with 'cancelled'."""
    store, link, bridge = _bridge()
    link.answer("c", "completed", "approve")
    bridge.tick()
    bridge.tick()
    assert link.closed == []


def test_an_expired_permit_closes_its_task():
    class Clock:
        def __init__(self): self.t = 1000.0
        def __call__(self): return self.t
    clock = Clock()
    store = PermitStore(ttl_s=60, clock=clock)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    clock.t += 61
    bridge.tick()
    assert link.closed == [("task-c", "expired")]


def test_the_expiry_the_phone_is_told_is_a_wall_clock_timestamp():
    store, link, bridge = _bridge()
    expires_at = link.posted[0]["expires_at"]
    assert expires_at.endswith("Z") and expires_at[:4].isdigit()


def test_tick_never_raises_when_the_keep_misbehaves():
    """KeepLink is best-effort but the bridge must survive anything it does —
    a raising link cannot be allowed to stop the poll loop."""
    class BrokenLink:
        def post_permit_task(self, **kw): raise RuntimeError("keep is down")
        def permit_task(self, challenge, task_id): raise RuntimeError("keep is down")
        def close_permit_task(self, task_id, result): raise RuntimeError("keep is down")
    store = PermitStore(ttl_s=60)
    bridge = PhoneBridge(store, BrokenLink(), poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    bridge.tick()
    assert store.get("c").state == "pending"


def test_a_post_that_fails_is_retried_next_tick():
    class FlakyLink(FakeLink):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def post_permit_task(self, **kw):
            self.attempts += 1
            if self.attempts == 1:
                return None            # offline / relay refused
            return super().post_permit_task(**kw)
    store = PermitStore(ttl_s=60)
    link = FlakyLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    assert store.get("c").phone_task_id is None
    bridge.tick()
    assert store.get("c").phone_task_id == "task-c"


def test_the_bridge_never_approves_a_permit_on_its_own():
    """An open, unknown or missing task means nobody has answered — it must not decide."""
    store, link, bridge = _bridge()
    for status in ("pending", "in-progress", "working", "input-required", "", None):
        link.tasks["c"]["status"] = status
        link.tasks["c"]["completed_by"] = dict(HUMAN)
        bridge.tick()
    link.tasks.pop("c")
    bridge.tick()
    assert store.get("c").state == "pending"


def test_run_loop_stops():
    import time
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.start()
    deadline = time.monotonic() + 3
    while not link.posted and time.monotonic() < deadline:
        time.sleep(0.01)
    bridge.stop()
    bridge.join(timeout=3)
    assert not bridge.is_alive() and link.posted
