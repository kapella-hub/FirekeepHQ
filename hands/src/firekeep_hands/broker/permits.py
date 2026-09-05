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

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

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

    def __init__(self, *, ttl_s: int = 60, clock=time.monotonic, on_change=None):
        self._ttl_s = int(ttl_s)
        self._clock = clock
        self._lock = threading.RLock()
        self._permits: dict[str, Permit] = {}
        # Called with this store whenever the pending set may have changed —
        # a new request, a decision, a consumption, an expiry noticed by the
        # sweep. The broker uses it to tell the human what is waiting.
        self._on_change = on_change
        self._dirty = False

    def now(self) -> float:
        """The store's own clock. The phone bridge needs it to turn a
        permit's monotonic deadline into a wall-clock time to show a human."""
        return self._clock()

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    # -- change notification ---------------------------------------------
    #
    # Every public method takes the lock, does its work through a `_locked`
    # helper, releases, and only then fires the callback. The callback writes
    # a file and can spawn a process; running it under the lock would hold
    # every other thread — the HTTP handlers, the chord listener — for the
    # length of a disk write, and would deadlock outright if it ever called
    # back into the store.

    def set_on_change(self, callback) -> None:
        """Install (or clear, with None) the change watcher. Settable after
        construction because the broker only knows which chord to name in the
        notification once the listener has had its say about whether the
        configured one is usable."""
        with self._lock:
            self._on_change = callback

    def _fire_change(self) -> None:
        with self._lock:
            dirty, self._dirty = self._dirty, False
            callback = self._on_change
        if not dirty or callback is None:
            return
        try:
            callback(self)
        except Exception as exc:  # noqa: BLE001 - a watcher must not break a permit
            log.debug("permit change callback failed: %s", exc)

    def _sweep(self) -> None:
        """Expire what has run out and forget what expired long ago. Called
        at the top of every public method so no caller can observe a permit
        that should already be dead — the alternative, a background timer,
        would leave a window where a stale permit is consumable."""
        now = self._clock()
        for permit in self._permits.values():
            if permit.state in _LIVE and now >= permit.expires_at:
                permit.state = "expired"
                self._dirty = True
        stale = [
            challenge
            for challenge, permit in self._permits.items()
            if permit.state not in _LIVE and now >= permit.expires_at + _RETAIN_S
        ]
        for challenge in stale:
            del self._permits[challenge]

    def _request_locked(self, *, challenge, title, classes, task_id, step_index) -> Permit:
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
        self._dirty = True
        return permit

    def _pending_locked(self) -> list[Permit]:
        self._sweep()
        return sorted(
            (p for p in self._permits.values() if p.state == "pending"),
            key=lambda p: p.created,
        )

    def _decide_locked(self, challenge, decision, via) -> Permit | None:
        self._sweep()
        permit = self._permits.get(challenge)
        if permit is None or permit.state != "pending":
            return None
        permit.state = "approved" if decision == "approve" else "denied"
        permit.via = via
        self._dirty = True
        return permit

    # -- public API -------------------------------------------------------

    def request(self, *, challenge, title, classes, task_id, step_index) -> Permit:
        """Idempotent while the permit is still live. A session that retries
        its request must get back the SAME permit — minting a fresh pending
        one would silently discard an approval the human had already given,
        and make them chord twice for one step."""
        with self._lock:
            permit = self._request_locked(
                challenge=challenge, title=title, classes=classes,
                task_id=task_id, step_index=step_index,
            )
        self._fire_change()
        return permit

    def get(self, challenge) -> Permit | None:
        with self._lock:
            self._sweep()
            permit = self._permits.get(challenge)
        self._fire_change()
        return permit

    def pending(self) -> list[Permit]:
        """Oldest first. `sorted` is stable, so two permits created in the
        same clock tick keep the order they were requested in."""
        with self._lock:
            waiting = self._pending_locked()
        self._fire_change()
        return waiting

    def decide(self, challenge, decision, via) -> Permit | None:
        """`"approve"` or `"deny"` on a pending permit; None for anything
        else. The decision vocabulary is closed on purpose — a caller cannot
        write an arbitrary string into `state` and land a permit in
        `consumed` or some state this module has never heard of."""
        if decision not in ("approve", "deny"):
            return None
        with self._lock:
            permit = self._decide_locked(challenge, decision, via)
        self._fire_change()
        return permit

    def decide_oldest(self, decision: str, via: str) -> Permit | None:
        """What a chord means: the human answered the question in front of
        them, which is the oldest one still waiting. There is no permit id in
        a keystroke, so `decide` needs this to have somewhere to land."""
        if decision not in ("approve", "deny"):
            return None
        with self._lock:
            waiting = self._pending_locked()
            permit = self._decide_locked(waiting[0].challenge, decision, via) if waiting else None
        self._fire_change()
        return permit

    def consume(self, challenge) -> bool:
        """True exactly once, and only for an approved, unexpired permit.
        Server-side by design: the requester cannot mark its own permit used
        (or unused), so a step can never run twice on one approval."""
        with self._lock:
            self._sweep()
            permit = self._permits.get(challenge)
            spent = permit is not None and permit.state == "approved"
            if spent:
                permit.state = "consumed"
                self._dirty = True
        self._fire_change()
        return spent
