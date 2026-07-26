"""Best-effort git workspace snapshot (stdlib subprocess) for stop/prompt/
sidecar. SP1b Task 19 Part C: sidecar's _collect_snapshot duplicated this
logic with drift (cwd support, timeout 10 vs 5) -- this is now the ONE
implementation; sidecar delegates to workspace_snapshot(cwd=...)."""
from __future__ import annotations

import subprocess

_TIMEOUT = 10  # unified (was 5 here, 10 in sidecar's duplicate)


def _git(args: list[str], *, cwd: str | None = None) -> str:
    try:
        r = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=_TIMEOUT, cwd=cwd,
        )
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001 — not a git repo / git absent
        return ""


def workspace_snapshot(*, cwd: str | None = None) -> str:
    """cwd defaults to None (subprocess's own default: current process cwd),
    unchanged for the stop.py/prompt.py callers. The sidecar passes its own
    workdir explicitly since it may not share the hook process's cwd."""
    branch = _git(["branch", "--show-current"], cwd=cwd) or "unknown"
    log = _git(["log", "--oneline", "-3"], cwd=cwd) or "no commits"
    diff = _git(["diff", "--stat"], cwd=cwd) or "no changes"
    staged = _git(["diff", "--cached", "--stat"], cwd=cwd) or "nothing staged"
    return (
        f"branch: {branch}\n"
        f"recent_commits:\n{log}\n"
        f"changed_files:\n{diff}\n"
        f"staged_files:\n{staged}"
    )
