"""Unit tests for `CdpTransport`'s JSON-RPC plumbing, driven through a fake
socket rather than a real browser — the real-browser path is `browser.py`'s
live check (see the task report). These exist specifically to pin down a
recv-loop bug found in review: `websocket.create_connection(..., timeout=10)`
sets a persistent socket timeout, and the receive loop used to treat an
idle `WebSocketTimeoutException` as a dead connection, killing the reader
thread for good after any ~10s gap with no CDP traffic — which is the
completely normal rhythm of a Hands session (an LLM thinking between
actions). Every later `send()` then timed out against a perfectly healthy
browser. `CdpTransport.__init__` takes a `ws` directly, so these tests
construct one against a fake without going through `launch()`.
"""
from __future__ import annotations

import json
import time

import pytest
import websocket

from firekeep_hands._cdp import CdpTransport


class FakeSocket:
    """Answers exactly one `recv()` with a `WebSocketTimeoutException` (an
    idle poll wakeup), then returns each of `frames` in order, then goes
    "idle" (repeating timeouts) until `closed` is set — at which point the
    next `recv()` raises `WebSocketConnectionClosedException`, the way a
    real socket does once torn down.

    `wait_for_send`, when true, withholds every frame until at least one
    request has actually been `send()`-t: `CdpTransport` starts its receive
    thread in `__init__`, before the test's first `send()` call, so a canned
    RESPONSE frame can otherwise race ahead of the request it responds to —
    delivered, found to match nothing yet registered in `_pending`, and
    silently dropped, which is a timing artifact of the fake, not a fact
    about the code under test. Unsolicited EVENT frames have no such
    causality to respect, hence this being opt-in rather than the default."""

    def __init__(self, frames: list[str], *, wait_for_send: bool = False) -> None:
        self._frames = list(frames)
        self._wait_for_send = wait_for_send
        self._raised_initial_timeout = False
        self.sent: list[str] = []
        self.closed = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str:
        if not self._raised_initial_timeout:
            self._raised_initial_timeout = True
            raise websocket.WebSocketTimeoutException("idle poll")
        if self._frames and (not self._wait_for_send or self.sent):
            return self._frames.pop(0)
        if self.closed:
            raise websocket.WebSocketConnectionClosedException("closed")
        raise websocket.WebSocketTimeoutException("idle poll")

    def settimeout(self, value: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_recv_loop_survives_an_idle_timeout_and_keeps_reading() -> None:
    response = json.dumps({"id": 1, "result": {"ok": True}})
    fake = FakeSocket([response], wait_for_send=True)
    transport = CdpTransport(fake)

    result = transport.send("Test.method", {}, timeout=5.0)

    # The loop must still be alive after eating the forced timeout above —
    # this is what would be False if the bug were still present.
    assert transport._recv_thread.is_alive()
    assert result == {"ok": True}

    fake.closed = True  # let the loop's next recv() end it cleanly
    transport._recv_thread.join(timeout=5.0)
    assert not transport._recv_thread.is_alive()


def test_send_still_times_out_against_a_genuinely_unresponsive_browser() -> None:
    """The fix must not swallow every timeout — only the idle-poll kind
    inside the receive loop. A request that never gets a reply still times
    out on its own budget."""
    fake = FakeSocket([])  # never delivers a response to anything
    transport = CdpTransport(fake)

    from firekeep_hands.backends.base import HandsError

    with pytest.raises(HandsError) as excinfo:
        transport.send("Test.method", {}, timeout=0.3)
    assert "timed out" in str(excinfo.value)

    fake.closed = True
    transport._recv_thread.join(timeout=5.0)


def test_events_are_buffered_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wait_event` sees an event that arrived before the wait started, and
    the per-(session, method) buffer does not grow without bound — oldest
    dropped first.

    Deliberately waits for the fake socket to finish emitting ALL frames
    (its internal queue empties) before touching `wait_event` at all: the
    recv loop and the test both run concurrently, and popping events while
    the producer might still be mid-delivery would make which exact events
    survive the cap a race rather than a fact about the trimming logic."""
    import firekeep_hands._cdp as cdp_module

    monkeypatch.setattr(cdp_module, "_MAX_BUFFERED_EVENTS_PER_KEY", 3)

    frames = [
        json.dumps({"method": "Page.loadEventFired", "sessionId": "S1", "params": {"n": i}})
        for i in range(5)
    ]
    fake = FakeSocket(frames)
    transport = CdpTransport(fake)
    try:
        deadline = time.monotonic() + 5.0
        while fake._frames and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not fake._frames, "recv loop did not drain all frames in time"

        key = ("S1", "Page.loadEventFired")
        with transport._cond:
            buffered = [item["n"] for item in transport._events.get(key, [])]
        assert buffered == [2, 3, 4]  # oldest two (0, 1) dropped by the cap

        seen = []
        while True:
            got = transport.wait_event("Page.loadEventFired", session="S1", timeout=0.2)
            if got is None:
                break
            seen.append(got["n"])
        assert seen == [2, 3, 4]
    finally:
        fake.closed = True
        transport._recv_thread.join(timeout=5.0)
