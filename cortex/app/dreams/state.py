"""Redis-backed run record and progress for the Dreaming pass.

Mirrors collectors/state.py: the app's redis client is NOT decode_responses,
so every read goes through _s().
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

RUN_KEY = "dreams:run"
DONE_KEY = "dreams:done:{kind}"
COUNTER_KEY = "dreams:counter:{name}"


def _s(v: Any) -> Any:
    return v.decode("utf-8") if isinstance(v, bytes) else v


class DreamState:
    def __init__(self, redis_client: Any):
        self._r = redis_client

    def get_run(self) -> dict:
        raw = self._r.hgetall(RUN_KEY) or {}
        return {_s(k): _s(v) for k, v in raw.items()}

    def record_run(self, **fields: Any) -> None:
        payload = {k: str(v) for k, v in fields.items() if v is not None}
        payload["last_run"] = datetime.now(timezone.utc).isoformat()
        self._r.hset(RUN_KEY, mapping=payload)

    def last_run_at(self) -> str | None:
        return self.get_run().get("last_run")

    def mark_unit_done(self, kind: str, key: str) -> None:
        self._r.sadd(DONE_KEY.format(kind=kind), key)

    def is_unit_done(self, kind: str, key: str) -> bool:
        return bool(self._r.sismember(DONE_KEY.format(kind=kind), key))

    def done_set(self, kind: str) -> set[str]:
        """The whole done-set for `kind` in one SMEMBERS round trip. Added in
        the dreams Task 6/7 fix round (M6): task.py's profile grouping used
        to call is_unit_done once per scanned candidate (up to the scan
        cap SISMEMBERs per tick) purely to filter out already-profiled
        members — one bulk read replaces that whole loop of round trips."""
        raw = self._r.smembers(DONE_KEY.format(kind=kind)) or set()
        return {_s(v) for v in raw}

    def bump_counter(self, name: str, n: int = 1) -> int:
        return int(self._r.incrby(COUNTER_KEY.format(name=name), n))

    def get_counter(self, name: str) -> int:
        raw = _s(self._r.get(COUNTER_KEY.format(name=name)))
        try:
            return int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def reset_progress(self) -> None:
        """Clear per-run progress. The run record itself is history and stays."""
        for kind in ("cluster", "profile"):
            self._r.delete(DONE_KEY.format(kind=kind))
        for name in ("new_memories", "clusters_done", "profiles_done", "errors"):
            self._r.delete(COUNTER_KEY.format(name=name))
