"""Fail-loud failure logging (SP0 D6): a dropped hook call leaves a trace, never
vanishes silently. Best-effort and NEVER raises. NEVER logs api_key: callers
build clean messages (mirroring shim.py's never-log-key discipline); no redaction
here -- it writes exactly (hook, message)."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

LOG_PATH = Path.home() / ".firekeep" / "logs" / "hooks.log"


def _log_path() -> Path:
    override = os.environ.get("FIREKEEP_LOG_DIR")
    return Path(override) / "hooks.log" if override else LOG_PATH


def log_failure(hook: str, message: str, exc: Exception | None = None) -> None:
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        h = str(hook).replace("\n", " ").replace("\r", " ")[:200]
        m = str(message).replace("\n", " ").replace("\r", " ")[:500]
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{ts} | {h} | {m}\n")
    except Exception:
        pass
    # Field-failure seam (spec, capture point 3): the hook cores already route
    # every caught failure through here — the dispatcher's own handler sees
    # only UNCAUGHT crashes. Class only; the free-text message stays local.
    if exc is not None:
        try:
            from firekeep_client import report
            report.emit("runtime", str(hook).replace("_", "-"), exc=exc)
        except Exception:
            pass
