"""Liveness probe for the Celery worker container. `python -m app.worker_health`.

WHY NOT `celery inspect ping`. The worker runs `--pool=solo`: ONE thread both
executes the task and services control broadcasts. While it is inside a task —
and a skill draft measured 251s, a classify 56s — it cannot answer a ping, so
`celery -A ... inspect ping --timeout 5` fails on every task longer than five
seconds. Observed on the live VPS: `firekeep-cortex-worker-1  Up 24 hours
(unhealthy)`, health log `ExitCode 69, "No nodes replied within time
constraint"`, FailingStreak 6 — while the worker log for the same window showed
it executing tasks normally.

A health signal that goes red precisely when the service is BUSY is worse than
no signal: it is the one an operator learns to ignore, and it makes
`depends_on: {condition: service_healthy}` unusable against this container.

WHAT THIS DOES INSTEAD. Ping first, because a reply is positive proof and an
idle worker answers in milliseconds. If nobody replies, fall back to asking
whether a worker PROCESS exists, by reading `/proc/*/cmdline` — stdlib only, no
`procps`/`pgrep` in the slim base image, no new package on the production image
for a healthcheck.

WHAT IT DELIBERATELY DOES NOT DETECT: a wedged worker (process alive, event
loop stuck). Neither did the ping — a wedged worker and a busy worker are
indistinguishable from outside, which is the whole reason the ping was
unreliable. Progress is measured by queue depth (`GET /ops/queues`), not by
liveness, and `GET /ops/workers` now reports `probe: "no reply"` rather than
`count: 0` so "busy" is never rendered as "absent".

Exit codes: 0 alive, 1 no worker found.
"""

from __future__ import annotations

import glob
import os
import sys

# This container's command is
#   celery -A app.workers.sleep_cycle worker --loglevel=info --pool=solo
# and the beat container's is the same with `beat` in place of `worker`.
#
# Matched as EXACT ARGV TOKENS, not substrings. A substring test cannot tell
# the two apart: the module path `app.workers.sleep_cycle` contains the
# characters "worker", so `"worker" in cmdline` is true for `celery beat` as
# well, and the probe would report a beat-only container as a healthy worker.
_APP_ARG = "app.workers.sleep_cycle"
_WORKER_ARG = "worker"


def worker_process_alive() -> bool:
    """True when a celery worker process for this app exists in this namespace.

    Reads `/proc` directly. Every per-PID failure is skipped rather than
    raising: a process that exits mid-scan is normal, and a probe that crashes
    on a race reports a healthy container as dead.
    """
    self_pid = str(os.getpid())
    for path in glob.glob("/proc/[0-9]*/cmdline"):
        # The PID is the parent DIRECTORY name. Deriving it by index into the
        # split path assumes the exact glob shape and breaks on anything else.
        if os.path.basename(os.path.dirname(path)) == self_pid:
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        argv = [a for a in raw.decode("utf-8", "replace").split("\0") if a]
        if _APP_ARG in argv and _WORKER_ARG in argv:
            return True
    return False


def ping_replied(timeout: float = 3.0) -> bool:
    """True when the worker answered a control ping.

    Positive proof of health, and cheap when the worker is idle. A failure here
    means nothing on its own — see the module docstring — so every exception is
    treated as "did not reply", never as "unhealthy".
    """
    try:
        from app.workers.sleep_cycle import celery_app

        return bool(celery_app.control.inspect(timeout=timeout).ping() or {})
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    if ping_replied():
        return 0
    if worker_process_alive():
        # Busy, not dead. Say which, so a `docker inspect` health log is
        # readable rather than just green.
        print("worker busy (no ping reply, process alive)")
        return 0
    print("no celery worker process found", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
