"""A BUSY --pool=solo worker must not be reported as an ABSENT one.

WHY THESE EXIST. The worker runs `--pool=solo`: one thread executes the task
AND services control broadcasts, so while it is inside a task it cannot answer
a ping. Two surfaces got that wrong in the same way.

`GET /ops/workers` returned `{"workers": [], "count": 0}` on 11 consecutive
calls while the worker was productively executing a 73s LLM task and the celery
queue climbed 1 -> 7 -> 14; the dashboard rendered "No workers online."
Separately each call hung ~10.1s on a 2.0s timeout, because `_inspect_workers`
issues FIVE broadcasts and each waits the full timeout when nobody replies.

`docker-compose.yml`'s healthcheck ran `celery inspect ping --timeout 5`, which
fails on every task longer than five seconds — i.e. every LLM task. Observed:
`Up 24 hours (unhealthy)`, ExitCode 69, "No nodes replied within time
constraint", FailingStreak 6, against a worker whose own log showed it working.

Neither surface can distinguish busy from wedged — nothing outside the worker
can — so neither claims to. What they must not do is report busy as DEAD.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import worker_health
from app.ops import create_ops_router


class _SilentInspect:
    """A solo worker mid-task: answers nothing, on any broadcast."""

    def __init__(self):
        self.calls = []

    def ping(self):
        self.calls.append("ping")
        return None

    def _empty(self, name):
        self.calls.append(name)
        return {}

    stats = lambda self: self._empty("stats")            # noqa: E731
    active = lambda self: self._empty("active")          # noqa: E731
    reserved = lambda self: self._empty("reserved")      # noqa: E731
    scheduled = lambda self: self._empty("scheduled")    # noqa: E731
    registered = lambda self: self._empty("registered")  # noqa: E731


class _RespondingInspect(_SilentInspect):
    def ping(self):
        self.calls.append("ping")
        return {"worker@node": {"ok": "pong"}}

    def stats(self):
        self.calls.append("stats")
        return {"worker@node": {"pool": {"implementation": "solo", "processes": [1]},
                                "total": {"t": 1}}}


@pytest.fixture
def ops_client():
    app = FastAPI()
    app.include_router(create_ops_router())
    return TestClient(app)


class TestOpsWorkersProbe:
    def test_no_reply_is_reported_as_no_reply_not_as_no_workers(self, ops_client):
        """`count: 0` alone told an operator the worker was gone. It was busy."""
        inspect = _SilentInspect()
        with patch("app.ops.celery_app") as celery:
            celery.control.inspect.return_value = inspect
            body = ops_client.get("/ops/workers").json()

        assert body["count"] == 0
        assert body["probe"] == "no reply"
        assert "--pool=solo" in body["note"]

    def test_no_reply_costs_one_broadcast_not_five(self, ops_client):
        """~10.1s per call on a 2.0s timeout was five sequential timeouts.

        The ping short-circuits the other four, which are worthless anyway when
        nobody is answering.
        """
        inspect = _SilentInspect()
        with patch("app.ops.celery_app") as celery:
            celery.control.inspect.return_value = inspect
            ops_client.get("/ops/workers")

        assert inspect.calls == ["ping"]

    def test_a_replying_worker_is_still_enumerated(self, ops_client):
        """The fast path must be untouched — the dashboard reads these fields."""
        inspect = _RespondingInspect()
        with patch("app.ops.celery_app") as celery:
            celery.control.inspect.return_value = inspect
            body = ops_client.get("/ops/workers").json()

        assert body["probe"] == "replied"
        assert body["count"] == 1
        assert body["workers"][0]["name"] == "worker@node"

    def test_inspect_raising_is_distinguishable_from_silence(self, ops_client):
        """A broker outage and a busy worker are different problems."""
        with patch("app.ops.celery_app") as celery:
            celery.control.inspect.side_effect = RuntimeError("broker gone")
            body = ops_client.get("/ops/workers").json()

        assert body["probe"] == "error"
        assert "broker gone" in body["error"]


class TestWorkerHealthProbe:
    def test_busy_worker_with_a_live_process_is_healthy(self, monkeypatch):
        """The exact live failure: no ping reply, worker plainly running."""
        monkeypatch.setattr(worker_health, "ping_replied", lambda timeout=3.0: False)
        monkeypatch.setattr(worker_health, "worker_process_alive", lambda: True)
        assert worker_health.main() == 0

    def test_a_ping_reply_alone_is_enough(self, monkeypatch):
        """An idle worker answers instantly; don't go read /proc for nothing."""
        called = []
        monkeypatch.setattr(worker_health, "ping_replied", lambda timeout=3.0: True)
        monkeypatch.setattr(
            worker_health, "worker_process_alive",
            lambda: called.append(True) or True,
        )
        assert worker_health.main() == 0
        assert called == []

    def test_no_process_and_no_reply_is_unhealthy(self, monkeypatch):
        """The probe must still be able to say 'dead' — otherwise it is decoration."""
        monkeypatch.setattr(worker_health, "ping_replied", lambda timeout=3.0: False)
        monkeypatch.setattr(worker_health, "worker_process_alive", lambda: False)
        assert worker_health.main() == 1

    def test_ping_exceptions_are_not_unhealthy(self, monkeypatch):
        """A failed ping means nothing on its own — see the module docstring."""
        with patch(
            "app.workers.sleep_cycle.celery_app",
            MagicMock(control=MagicMock(inspect=MagicMock(side_effect=OSError("no broker")))),
        ):
            assert worker_health.ping_replied(timeout=0.01) is False

    def test_process_scan_ignores_unreadable_pids(self, monkeypatch, tmp_path):
        """A process exiting mid-scan is normal; crashing on it would report a
        healthy container as dead."""
        (tmp_path / "1").mkdir()
        (tmp_path / "1" / "cmdline").write_bytes(b"")
        monkeypatch.setattr(
            worker_health.glob, "glob",
            lambda pattern: [str(tmp_path / "1" / "cmdline"), "/proc/999999/cmdline"],
        )
        assert worker_health.worker_process_alive() is False

    def test_process_scan_matches_the_real_worker_command(self, monkeypatch, tmp_path):
        """`celery beat` must not be mistaken for a worker.

        This is the case a substring test cannot get right, and the first
        version of this probe got it wrong: the module path
        `app.workers.sleep_cycle` CONTAINS the characters "worker", so
        `"worker" in cmdline` is true for the beat command too — the beat
        container would have reported itself healthy as a worker. Matching
        exact argv tokens is what separates them.
        """
        cmd = tmp_path / "cmdline"
        cmd.write_bytes(
            b"celery\x00-A\x00app.workers.sleep_cycle\x00worker\x00--pool=solo\x00"
        )
        beat = tmp_path / "beat_cmdline"
        beat.write_bytes(b"celery\x00-A\x00app.workers.sleep_cycle\x00beat\x00")

        monkeypatch.setattr(worker_health.glob, "glob", lambda p: [str(beat)])
        assert worker_health.worker_process_alive() is False

        monkeypatch.setattr(worker_health.glob, "glob", lambda p: [str(cmd)])
        assert worker_health.worker_process_alive() is True
