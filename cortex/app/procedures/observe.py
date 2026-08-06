"""The pre-edit stage: recognise, observe, warn.

Deliberately NOT a policy rule (spec §3). PolicyContext carries no action type,
so a rule cannot distinguish a file path from a run_command target before
globbing; ActionBeforeRequest can. It also keeps the blast radius local — a
failure here cannot remove the policy rule set.
"""

from __future__ import annotations

import logging
import time

from app.agent_gateway.models import ActionBeforeRequest, Advisory
from app.procedures import match, store

logger = logging.getLogger(__name__)

# A session id we cannot join to an outcome is not evidence; recording under it
# would manufacture executions that can never be evaluated.
_UNUSABLE_SESSIONS = {"", "unknown", "none", "null"}


class ProcedureObserver:
    def __init__(self, get_redis, settings_fn):
        self._get_redis = get_redis
        self._settings_fn = settings_fn
        self._index: list[dict] = []
        self._index_at: float = 0.0

    async def _load_index(self, redis_client, settings) -> list[dict]:
        ttl = float(getattr(settings, "PROCEDURE_INDEX_CACHE_SECONDS", 30) or 0)
        now = time.monotonic()
        if ttl and self._index_at and (now - self._index_at) < ttl:
            return self._index
        self._index = await store.load_index(redis_client)
        self._index_at = now
        return self._index

    async def observe(self, req: ActionBeforeRequest, *,
                      action_id: str = "") -> list[Advisory]:
        """Never raises. Returns [] whenever anything is missing or off.

        `action_id` is the id `decide()` minted for THIS action. The spec's
        receipt is `{action_id, target, ts}`; storing an empty one makes the
        observation unjoinable to the `agent.action.predict` event it describes,
        which is the whole point of recording it.
        """
        try:
            settings = self._settings_fn()
            if not getattr(settings, "PROCEDURE_ENABLED", False):
                return []
            if req.action.type != "edit_file":
                return []

            redis_client = self._get_redis()
            if redis_client is None:
                return []
            index = await self._load_index(redis_client, settings)
            if not index:
                return []
            matched = match.match_target(index, req.action.target)
            if not matched:
                return []

            # Checked AFTER the match, deliberately. An execution that cannot be
            # joined to an outcome is not evidence, but the drop is only worth
            # counting once work was RECOGNISED — and checking earlier would put
            # a Redis write on every edit of a deployment with no specs at all.
            if (req.session_id or "").strip().lower() in _UNUSABLE_SESSIONS:
                logger.debug(
                    "procedure match on %s dropped: session_id %r is unjoinable",
                    req.action.target, req.session_id,
                )
                await store.bump_unjoinable(redis_client)
                return []

            warn_on = bool(getattr(settings, "PROCEDURE_WARN_ENABLED", True))
            advisories: list[Advisory] = []
            for entry in matched:
                skill_id = entry["skill_id"]
                existing = await store.get_execution(
                    redis_client, req.session_id, skill_id
                )
                observed_ids = set((existing or {}).get("observed") or {})

                exec_id = await store.record_observation(
                    redis_client, settings,
                    session_id=req.session_id, skill_id=skill_id,
                    step_id=entry["step_id"], action_id=action_id,
                    target=req.action.target, agent_id=req.agent_id,
                    adapter=req.adapter,
                )
                if not warn_on:
                    continue
                missing = match.missing_load_bearing(
                    index, skill_id, entry.get("order", 0), observed_ids
                )
                if not missing:
                    continue
                stats = await store.get_step_stats(redis_client, skill_id)
                for m in missing:
                    claimed = await store.claim_warn(
                        redis_client, settings, session_id=req.session_id,
                        skill_id=skill_id, step_id=m["step_id"],
                    )
                    if not claimed:
                        continue
                    advisories.append(Advisory(
                        code="procedure_step_missing",
                        message=match.advisory_text(entry, m, stats),
                        # The pre-built, previously unused receipt slot. Points
                        # at OUR durable record, not a replay event id — those
                        # resolve through a 30d index whose trim task is never
                        # scheduled.
                        evidence_event_id=exec_id,
                    ))
            return advisories
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("procedure stage skipped: %s", exc)
            return []
