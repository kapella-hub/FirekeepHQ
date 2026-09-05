from firekeep_hands.broker.permits import PermitStore


class Clock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


def _store():
    c = Clock(); return PermitStore(ttl_s=60, clock=c), c


def test_request_is_idempotent_and_expires():
    s, c = _store()
    p = s.request(challenge="c1", title="Send", classes=("send",), task_id="t", step_index=1)
    assert s.request(challenge="c1", title="Send", classes=("send",), task_id="t", step_index=1) is p
    c.t += 61
    assert s.get("c1").state == "expired"


def test_oldest_pending_is_the_one_a_chord_approves():
    s, c = _store()
    s.request(challenge="a", title="A", classes=("send",), task_id="t", step_index=1); c.t += 1
    s.request(challenge="b", title="B", classes=("money",), task_id="t", step_index=2)
    assert s.decide_oldest("approve", via="chord").challenge == "a"
    assert s.get("a").state == "approved" and s.get("a").via == "chord" and s.get("b").state == "pending"


def test_consume_is_one_use_and_only_after_approval():
    s, _ = _store()
    s.request(challenge="c", title="x", classes=("destroy",), task_id="t", step_index=0)
    assert s.consume("c") is False
    s.decide("c", "approve", via="phone")
    assert s.consume("c") is True and s.consume("c") is False and s.get("c").state == "consumed"


def test_denied_and_expired_cannot_be_consumed_or_reapproved():
    s, c = _store()
    s.request(challenge="d", title="x", classes=("send",), task_id="t", step_index=0)
    s.decide("d", "deny", via="chord")
    assert s.decide("d", "approve", via="chord") is None and s.consume("d") is False
    s.request(challenge="e", title="x", classes=("send",), task_id="t", step_index=1); c.t += 61
    assert s.decide("e", "approve", via="chord") is None


# --- additions -------------------------------------------------------------


def test_an_approved_permit_is_not_reissued_by_a_retried_request():
    """A session that repeats its request must not throw away an approval the
    human has already given — the retry gets the same, already-approved
    permit, not a fresh pending one."""
    s, _ = _store()
    p = s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    s.decide("c", "approve", via="chord")
    again = s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert again is p and again.state == "approved"
    assert s.consume("c") is True


def test_a_terminal_permit_is_reminted_by_a_new_request():
    """Once consumed/denied/expired the challenge is spent; a new request for
    the same challenge starts a fresh pending permit that a human must
    approve again."""
    s, c = _store()
    first = s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    s.decide("c", "deny", via="chord")
    second = s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert second is not first and second.state == "pending"
    c.t += 61
    assert s.get("c").state == "expired"
    third = s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert third is not second and third.state == "pending"


def test_classes_are_normalised_to_a_tuple():
    """HTTP hands us a JSON list; the rest of Hands treats classes as a tuple."""
    s, _ = _store()
    p = s.request(challenge="c", title="x", classes=["send", "money"], task_id="t", step_index=0)
    assert p.classes == ("send", "money")


def test_decide_oldest_ignores_expired_and_returns_none_when_nothing_pends():
    s, c = _store()
    assert s.decide_oldest("approve", via="chord") is None
    s.request(challenge="old", title="x", classes=("send",), task_id="t", step_index=0)
    c.t += 61
    s.request(challenge="new", title="x", classes=("send",), task_id="t", step_index=1)
    assert s.decide_oldest("approve", via="chord").challenge == "new"


def test_pending_lists_oldest_first_and_drops_resolved():
    s, c = _store()
    s.request(challenge="a", title="A", classes=("send",), task_id="t", step_index=0); c.t += 1
    s.request(challenge="b", title="B", classes=("send",), task_id="t", step_index=1)
    assert [p.challenge for p in s.pending()] == ["a", "b"]
    s.decide("a", "deny", via="chord")
    assert [p.challenge for p in s.pending()] == ["b"]


def test_get_of_an_unknown_challenge_is_none_and_decide_is_none():
    s, _ = _store()
    assert s.get("nope") is None
    assert s.decide("nope", "approve", via="chord") is None
    assert s.consume("nope") is False


def test_only_approve_and_deny_are_decisions():
    """No third verdict can sneak a permit into a consumable state."""
    s, _ = _store()
    s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    assert s.decide("c", "consumed", via="chord") is None
    assert s.decide("c", "approved", via="chord") is None
    assert s.get("c").state == "pending" and s.consume("c") is False


def test_an_approved_permit_expires_before_it_is_consumed():
    """The TTL bounds the approval, not just the request: a human who
    approves and then walks away does not leave a permit usable forever."""
    s, c = _store()
    s.request(challenge="c", title="x", classes=("send",), task_id="t", step_index=0)
    s.decide("c", "approve", via="chord")
    c.t += 61
    assert s.get("c").state == "expired" and s.consume("c") is False
