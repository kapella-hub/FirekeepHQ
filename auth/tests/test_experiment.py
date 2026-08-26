"""Parity + contract tests for the shared arm/token functions (PR5 D1/D13).

FROZEN_REFERENCE is a byte-frozen copy of bridge/app/session.py's pre-PR5
_experiment_group. If auth/experiment.py ever diverges from it, every
member's arm reassigns and the PR4/PR5 stamped labels stop meaning anything.
"""
import hashlib

from auth.experiment import experiment_group, member_token


def _frozen_reference(owner_member):
    if not owner_member:
        return None
    h = int(hashlib.sha256(owner_member.encode("utf-8")).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


MEMBERS = ["mogan", "member-owner", "alice@example.com", "x", "member-7f3a"]


def test_arm_parity_with_frozen_bridge_implementation():
    for m in MEMBERS:
        assert experiment_group(m) == _frozen_reference(m)


def test_arm_none_for_empty_and_none():
    assert experiment_group(None) is None
    assert experiment_group("") is None


def test_arm_is_stable_and_binary():
    for m in MEMBERS:
        arm = experiment_group(m)
        assert arm in ("A", "B")
        assert experiment_group(m) == arm  # deterministic across calls


def test_member_token_derivation():
    for m in MEMBERS:
        expected = hashlib.sha256(m.encode("utf-8")).hexdigest()[:12]
        assert member_token(m) == expected
        assert len(member_token(m)) == 12


def test_member_token_none_for_empty_and_none():
    assert member_token(None) is None
    assert member_token("") is None


def test_token_and_arm_derive_from_same_string():
    # D13: token and arm may never disagree about which member — both are
    # pure functions of the same input, so equality of input is the proof.
    m = "mogan"
    h = int(hashlib.sha256(m.encode("utf-8")).hexdigest(), 16)
    assert experiment_group(m) == ("A" if h % 2 == 0 else "B")
    assert member_token(m) == hashlib.sha256(m.encode("utf-8")).hexdigest()[:12]
