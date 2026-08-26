"""Pre-registered experiment identity (outcome truth PR4 D1 / PR5 D1+D13).

One shared implementation for every service: bridge stamps these on the
session, cortex's briefing route computes the arm for treatment routing.
Sharing by construction (bridge re-imports this module) is what pins
delivered-arm == recorded-arm; the parity test freezes the derivation.
"""
import hashlib


def experiment_group(owner_member: str | None) -> str | None:
    """The pre-registered arm ("A"/"B") for *owner_member*.

    Deterministic and STABLE across process restarts: sha256, never
    Python's built-in hash(), which is salted per-process (PYTHONHASHSEED)
    and would reassign every member's arm on the next restart, destroying
    stickiness. Called at session start / briefing time from the verified
    owner_member only, never from task_result.

    An empty/unverified owner_member returns None (excluded from arms)
    rather than a hashed arm: hash("") is a single fixed value, so hashing
    it would dump every unauthenticated session into the same arm.
    """
    if not owner_member:
        return None
    h = int(hashlib.sha256(owner_member.encode("utf-8")).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


def member_token(owner_member: str | None) -> str | None:
    """One-way member key for arm analytics (PR5 D13).

    sha256 prefix, 12 hex chars: enough to never collide inside one fleet,
    short enough that analytics surfaces never leak the member string. Same
    input as experiment_group, so token and arm cannot disagree about which
    member a session belongs to. None (not "") for absent members — absence
    must stay distinguishable from a measured value on every wire.
    """
    if not owner_member:
        return None
    return hashlib.sha256(owner_member.encode("utf-8")).hexdigest()[:12]
