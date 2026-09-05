"""One-use, TTL-bounded permits keyed by a deterministic challenge id.

The whole safety property of Hands reduces to four rules enforced here:

1. A permit starts `pending` and only a listener (real keystroke) or the
   phone bridge (a person tapping the dashboard) can move it to `approved`.
2. `consume()` succeeds exactly once, and only from `approved` — so a permit
   authorises the one step it was minted for and never a replay of it.
3. Expiry applies to `approved` as well as `pending`: a human who approves
   and then walks away does not leave a usable permit behind them.
4. `denied`, `expired` and `consumed` are terminal. Nothing moves out of
   them; a new attempt at the same step needs a whole new permit, which
   needs a whole new human.

State lives in memory only. A broker restart voids every outstanding
permit, which is the correct failure direction: the model has to ask again.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# States a permit can still move out of. Everything else is terminal.
_LIVE = frozenset({"pending", "approved"})

# How long a resolved permit is kept so the requester can read back what
# happened to it (`via`, `state`) after acting. Long enough to outlive any
# request/response, short enough that a long-running broker does not grow a
# permit dictionary forever.
_RETAIN_S = 300.0


@dataclass
class Permit:
    challenge: str
    title: str
    classes: tuple[str, ...]
    task_id: str
    step_index: int
    created: float
    expires_at: float
    state: str = "pending"
    via: str | None = None
    phone_task_id: str | None = None


class PermitStore:
    """Thread-safe: the HTTP handler threads, the chord listener thread and
    the phone bridge thread all mutate this concurrently. A re-entrant lock
    because the public methods compose (`decide_oldest` -> `pending` ->
    `decide`) and splitting them into locked/unlocked pairs would double the
    number of places the expiry sweep has to be remembered."""

    def __init__(self, *, ttl_s: int = 60, clock=time.monotonic):
        self._ttl_s = int(ttl_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._permits: dict[str, Permit] = {}

    def now(self) -> float:
        """The store's own clock. The phone bridge needs it to turn a
        permit's monotonic deadline into a wall-clock time to show a human."""
        return self._clock()

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    def _sweep(self) -> None:
        """Expire what has run out and forget what expired long ago. Called
        at the top of every public method so no caller can observe a permit
        that should already be dead — the alternative, a background timer,
        would leave a window where a stale permit is consumable."""
        now = self._clock()
        for permit in self._permits.values():
            if permit.state in _LIVE and now >= permit.expires_at:
                permit.state = "expired"
        stale = [
            challenge
            for challenge, permit in self._permits.items()
            if permit.state not in _LIVE and now >= permit.expires_at + _RETAIN_S
        ]
        for challenge in stale:
            del self._permits[challenge]

    def request(self, *, challenge, title, classes, task_id, step_index) -> Permit:
        """Idempotent while the permit is still live. A session that retries
        its request must get back the SAME permit — minting a fresh pending
        one would silently discard an approval the human had already given,
        and make them chord twice for one step."""
        with self._lock:
            self._sweep()
            existing = self._permits.get(challenge)
            if existing is not None and existing.state in _LIVE:
                return existing
            now = self._clock()
            permit = Permit(
                challenge=str(challenge),
                title=str(title),
                classes=tuple(str(c) for c in classes),
                task_id=str(task_id),
                step_index=int(step_index),
                created=now,
                expires_at=now + self._ttl_s,
            )
            self._permits[challenge] = permit
            return permit

    def get(self, challenge) -> Permit | None:
        with self._lock:
            self._sweep()
            return self._permits.get(challenge)

    def pending(self) -> list[Permit]:
        """Oldest first. `sorted` is stable, so two permits created in the
        same clock tick keep the order they were requested in."""
        with self._lock:
            self._sweep()
            return sorted(
                (p for p in self._permits.values() if p.state == "pending"),
                key=lambda p: p.created,
            )

    def decide(self, challenge, decision, via) -> Permit | None:
        """`"approve"` or `"deny"` on a pending permit; None for anything
        else. The decision vocabulary is closed on purpose — a caller cannot
        write an arbitrary string into `state` and land a permit in
        `consumed` or some state this module has never heard of."""
        if decision not in ("approve", "deny"):
            return None
        with self._lock:
            self._sweep()
            permit = self._permits.get(challenge)
            if permit is None or permit.state != "pending":
                return None
            permit.state = "approved" if decision == "approve" else "denied"
            permit.via = via
            return permit

    def decide_oldest(self, decision: str, via: str) -> Permit | None:
        """What a chord means: the human answered the question in front of
        them, which is the oldest one still waiting. There is no permit id in
        a keystroke, so `decide` needs this to have somewhere to land."""
        with self._lock:
            waiting = self.pending()
            if not waiting:
                return None
            return self.decide(waiting[0].challenge, decision, via)

    def consume(self, challenge) -> bool:
        """True exactly once, and only for an approved, unexpired permit.
        Server-side by design: the requester cannot mark its own permit used
        (or unused), so a step can never run twice on one approval."""
        with self._lock:
            self._sweep()
            permit = self._permits.get(challenge)
            if permit is None or permit.state != "approved":
                return False
            permit.state = "consumed"
            return True
