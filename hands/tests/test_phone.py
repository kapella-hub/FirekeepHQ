from firekeep_hands.broker.permits import PermitStore
from firekeep_hands.broker.phone import PhoneBridge


class FakeLink:
    def __init__(self):
        self.posted = []
        self.states = {}
        self.closed = []

    def post_permit_task(self, **kw):
        self.posted.append(kw)
        return "task-" + kw["challenge"]

    def permit_task_state(self, challenge):
        return self.states.get(challenge, "pending")

    def close_permit_task(self, task_id, result):
        self.closed.append((task_id, result))


def test_bridge_posts_polls_and_decides():
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="Send", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    assert link.posted[0]["challenge"] == "c" and store.get("c").phone_task_id == "task-c"
    link.states["c"] = "approve"
    bridge.tick()
    assert store.get("c").state == "approved" and store.get("c").via == "phone"


def test_bridge_closes_task_when_permit_resolves_elsewhere():
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    store.decide("c", "approve", via="chord")
    bridge.tick()
    assert link.closed == [("task-c", "approved")]


# --- additions -------------------------------------------------------------


def test_each_permit_is_posted_once_and_each_task_closed_once():
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    bridge.tick()
    bridge.tick()
    assert len(link.posted) == 1
    store.decide("c", "deny", via="chord")
    bridge.tick()
    bridge.tick()
    assert link.closed == [("task-c", "denied")]


def test_a_phone_deny_denies_the_permit():
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    link.states["c"] = "deny"
    bridge.tick()
    assert store.get("c").state == "denied" and store.get("c").via == "phone"


def test_a_permit_the_phone_resolved_is_not_also_cancelled():
    """The dashboard already closed that relay task by approving it; cancelling
    it afterwards would overwrite the human's answer with 'cancelled'."""
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    link.states["c"] = "approve"
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
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    expires_at = link.posted[0]["expires_at"]
    assert expires_at.endswith("Z") and expires_at[:4].isdigit()


def test_tick_never_raises_when_the_keep_misbehaves():
    """KeepLink is best-effort but the bridge must survive anything it does —
    a raising link cannot be allowed to stop the poll loop."""
    class BrokenLink:
        def post_permit_task(self, **kw): raise RuntimeError("keep is down")
        def permit_task_state(self, challenge): raise RuntimeError("keep is down")
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
    """`pending` from relay means nobody has answered — it must not decide."""
    store = PermitStore(ttl_s=60)
    link = FakeLink()
    bridge = PhoneBridge(store, link, poll_s=0.01)
    store.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=1)
    bridge.tick()
    for state in ("pending", None, "", "approve-ish", "in-progress"):
        link.states["c"] = state
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
