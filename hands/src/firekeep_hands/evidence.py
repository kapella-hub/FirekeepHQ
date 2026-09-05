"""A local, hash-chained per-task ledger under `paths.evidence_root()`.

Every automated step Hands takes — an action, the route it went through
(shortcut / UI-tree / vision), which protected classes it touched, whether it
needed a human permit, before/after screenshots, and how it turned out — is
appended to one task's `steps.jsonl` as a line that also carries a `chain`
field: `sha256(prev_chain + canonical_json(line_without_chain))`, the first
line's `prev_chain` being `""`. A dropped, reordered, or edited line breaks
every chain after it, so the ledger is tamper-evident without needing a
remote party to co-sign each step — a human (or the Keep) can verify the
whole run offline by recomputing the chain and comparing.

Screenshots are stored as separate `NNN-before.png` / `NNN-after.png` files
(not inlined into the JSON) with only their sha256 recorded in the line —
the hash still binds the image into the chain, but a viewer can page through
`task.json` and `steps.jsonl` without loading megabytes of PNGs.

`prune` is the retention side of the same directory: a human is expected to
call it periodically (see `HandsConfig.evidence_retention_days`) against
`paths.evidence_root()` to drop tasks whose `task.json` `started` timestamp
has aged out. It is deliberately conservative — a task directory whose
`task.json` is missing or unreadable is left alone rather than guessed at,
since deleting evidence is the one mistake here that can't be undone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

from firekeep_client import hooklog, state

from . import paths

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime(_TS_FORMAT)


def _parse_iso(value: object) -> dt.datetime | None:
    """None for anything that isn't a well-formed ISO-8601 UTC timestamp —
    `prune` treats that the same as "leave this task alone", never as
    "delete it"."""
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.strptime(value, _TS_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


class Ledger:
    """One task's evidence directory: `paths.evidence_root() / task_id`."""

    def __init__(self, task_id: str, *, goal: str, apps: list[str], machine_id: str, session_id: str):
        self.task_id = task_id
        self.dir = paths.evidence_root() / task_id
        self.dir.mkdir(parents=True, exist_ok=True)
        state._private(self.dir)
        self._prev_chain = self._last_chain()
        if not self._task_json_path.exists():
            self._write_task_json({
                "goal": goal,
                "apps": list(apps),
                "machine_id": machine_id,
                "session_id": session_id,
                "started": _now_iso(),
            })

    @property
    def _task_json_path(self) -> Path:
        return self.dir / "task.json"

    @property
    def _steps_path(self) -> Path:
        return self.dir / "steps.jsonl"

    def _last_chain(self) -> str:
        """The previous line's `chain`, so a `Ledger` re-opened against an
        existing directory (a resumed task) continues the same hash chain
        instead of silently restarting it at `""`."""
        path = self._steps_path
        if not path.exists():
            return ""
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            return ""
        try:
            return json.loads(lines[-1]).get("chain", "")
        except (ValueError, AttributeError):
            return ""

    def _read_task_json(self) -> dict:
        try:
            data = json.loads(self._task_json_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — a corrupt task.json degrades to "empty", not a crash
            hooklog.log_failure("hands", f"could not read {self._task_json_path}: {exc}", exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_task_json(self, data: dict) -> None:
        path = self._task_json_path
        tmp = path.parent / f"{path.name}.tmp-{os.getpid()}"
        try:
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            state._private(tmp)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        state._private(path)

    def record(
        self,
        *,
        step_index: int,
        action: dict,
        route: str,
        classes: tuple[str, ...],
        permit: dict | None,
        before_png: bytes | None,
        after_png: bytes | None,
        outcome: str,
        error: str | None,
    ) -> dict:
        """Write one step: the screenshots (if any) as `NNN-before.png` /
        `NNN-after.png`, then the chained JSON line recording their hashes.
        Returns the line as written (including its `chain`)."""
        before_hash = None
        if before_png is not None:
            img = self.dir / f"{step_index:03d}-before.png"
            img.write_bytes(before_png)
            state._private(img)
            before_hash = hashlib.sha256(before_png).hexdigest()
        after_hash = None
        if after_png is not None:
            img = self.dir / f"{step_index:03d}-after.png"
            img.write_bytes(after_png)
            state._private(img)
            after_hash = hashlib.sha256(after_png).hexdigest()

        line = {
            "step_index": step_index,
            "ts": _now_iso(),
            "action": action,
            "route": route,
            "classes": list(classes),
            "permit": permit,
            "before": before_hash,
            "after": after_hash,
            "outcome": outcome,
            "error": error,
        }
        body = json.dumps({k: v for k, v in line.items() if k != "chain"}, sort_keys=True, separators=(",", ":"))
        chain = hashlib.sha256((self._prev_chain + body).encode("utf-8")).hexdigest()
        line["chain"] = chain
        self._prev_chain = chain

        with self._steps_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n")
        state._private(self._steps_path)
        return line

    def close(self, outcome: str, summary: str) -> None:
        data = self._read_task_json()
        data["ended"] = _now_iso()
        data["outcome"] = outcome
        data["summary"] = summary
        data["steps"] = self.steps()
        self._write_task_json(data)

    def steps(self) -> list[dict]:
        path = self._steps_path
        if not path.exists():
            return []
        out = []
        for ln in path.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue  # a torn last line (e.g. a crash mid-write) never poisons the rest
        return out


def prune(root: Path, *, older_than_days: int, now: dt.datetime | None = None) -> int:
    """Delete task directories under `root` whose `task.json` `started` is
    older than `older_than_days`. Returns the count removed. A directory with
    a missing, unreadable, or unparsable `task.json` is left alone — pruning
    is best-effort cleanup, not a place to guess."""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if not root.exists():
        return 0
    cutoff = now - dt.timedelta(days=older_than_days)
    removed = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        task_json = child / "task.json"
        if not task_json.exists():
            continue
        try:
            data = json.loads(task_json.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — unreadable/corrupt task.json means "leave it alone"
            hooklog.log_failure("hands", f"prune: could not read {task_json}: {exc}", exc)
            continue
        started = _parse_iso(data.get("started") if isinstance(data, dict) else None)
        if started is None or started >= cutoff:
            continue
        try:
            shutil.rmtree(child)
        except OSError as exc:  # e.g. a file locked by a running task on Windows
            hooklog.log_failure("hands", f"prune: could not remove {child}: {exc}", exc)
            continue
        removed += 1
    return removed
