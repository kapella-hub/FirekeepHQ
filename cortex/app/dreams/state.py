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
# PERSISTENT — deliberately NOT namespaced under DONE_KEY, and deliberately
# never cleared by reset_progress(). See mark_consolidated() and
# reset_progress() below for the full reasoning.
CONSOLIDATED_KEY = "dreams:consolidated"


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

    def mark_consolidated(self, ids: list[str]) -> None:
        """Record source-memory ids that a successfully-stored dream now
        covers. This is the durable ledger behind the design spec's
        "not already consolidated" candidate criterion (final-review I2+I3),
        and it is what stops the pass re-synthesizing the same prefix forever.

        Before this existed, `select_clusters` returned the first
        DREAM_MAX_CLUSTERS_PER_RUN clusters in deterministic sorted-bucket
        order and `reset_progress()` wiped the per-run done-set at
        completion — so every run re-selected the IDENTICAL clusters. Proven
        on 30 clusters across two project buckets at cap 20: the second
        bucket was never reached, on any run. On the live store the
        project-less bucket (297 memories) would have consumed the cap and
        `firekeep`/`nexusstack`/`timegrapher` would never have been
        consolidated at all.

        Called only after a dream point is actually WRITTEN — a cluster the
        LLM could not synthesize is marked done for the run (so it isn't
        retried every tick) but its members stay candidates, because nothing
        was consolidated.

        Growth is unbounded by design: the ledger is one short id per
        consolidated memory and it must outlive every run, so there is no
        TTL and no trim. At the live store's scale (538 active memories) this
        is a few tens of KB; a store large enough for it to matter is one
        where round 2's archival would be retiring the sources anyway.
        """
        if not ids:
            return
        self._r.sadd(CONSOLIDATED_KEY, *ids)

    def consolidated_set(self) -> set[str]:
        """The whole consolidated ledger in one SMEMBERS round trip — the
        `done_set()` pattern, for the same reason: candidate selection tests
        membership once per scanned point, and a SISMEMBER per point would
        be one Redis round trip per candidate per tick."""
        raw = self._r.smembers(CONSOLIDATED_KEY) or set()
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
        """Clear per-run progress. The run record itself is history and stays.

        Two things are cleared, both PER-RUN by definition:
          - done-sets, kinds ("cluster", "profile") — which units this run has
            already spent a tick on.
          - counters ("new_memories", "clusters_done", "profiles_done",
            "errors") — this run's tallies. `clusters_done` doubles as the
            per-run budget against DREAM_MAX_CLUSTERS_PER_RUN, so clearing it
            is what starts the next run's budget.

        CONSOLIDATED_KEY is deliberately NOT in either list and must never be
        added to one. It is not progress — it is the durable answer to "has
        this memory already been consolidated?", which is a property of the
        STORE, not of a run. Clearing it would restore exactly the starvation
        bug it exists to fix: every run would rediscover the same first-N
        clusters in sorted-bucket order and re-synthesize them forever, while
        later buckets were never reached. See mark_consolidated() above.
        """
        for kind in ("cluster", "profile"):
            self._r.delete(DONE_KEY.format(kind=kind))
        for name in ("new_memories", "clusters_done", "profiles_done", "errors"):
            self._r.delete(COUNTER_KEY.format(name=name))
