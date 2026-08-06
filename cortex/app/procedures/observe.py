"""The pre-edit stage: recognise, observe, warn.

Deliberately NOT a policy rule (spec §3). PolicyContext carries no action type,
so a rule cannot distinguish a file path from a run_command target before
globbing; ActionBeforeRequest can. It also keeps the blast radius local — a
failure here cannot remove the policy rule set.

TWO PHASES, and the split is the whole point. `decide()` is not finished
deciding when this stage runs: PathDenyRule can already have blocked, and the
rethink-limit escalation can still turn a rethink into a block AFTERWARDS. An
observation is a claim that an edit HAPPENED, so writing one here recorded edits
that never occurred — inflating Tier A frequency with work that did not exist
and, worse, suppressing the warn for that step for the rest of the execution,
because the next call reads `observed` as the earlier-step evidence. On a
rethink the agent resubmits the same edit, so it also double-counted.

`plan()` does the matching and the advisory work, in place, so the advisory
still reaches the human in its original position in the list. `commit()` writes,
and `decide()` calls it only once the decision has settled on `allow`.
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


class PendingObservation:
    """Recognised work whose write has not happened yet.

    Empty by default, so every caller has something to commit or abort and no
    branch has to test for None.
    """

    __slots__ = ("advisories", "_redis", "_settings", "_req", "_action_id",
                 "_writes", "_claims", "_unjoinable")

    def __init__(self, advisories=None, *, redis_client=None, settings=None,
                 req: ActionBeforeRequest | None = None, action_id: str = "",
                 writes=None, claims=None, unjoinable: bool = False):
        self.advisories: list[Advisory] = list(advisories or [])
        self._redis = redis_client
        self._settings = settings
        self._req = req
        self._action_id = action_id
        # (skill_id, step_id, exec_id) — one per matched index entry.
        self._writes = list(writes or [])
        # (skill_id, step_id) — warn latches claimed while building advisories.
        self._claims = list(claims or [])
        self._unjoinable = unjoinable

    async def commit(self) -> None:
        """The decision settled on `allow`: the edit happened, so record it.

        Never raises (I6) — a Redis failure must not cost a customer's edit.
        """
        try:
            for skill_id, step_id, exec_id in self._writes:
                await store.record_observation(
                    self._redis, self._settings,
                    session_id=self._req.session_id, skill_id=skill_id,
                    step_id=step_id, action_id=self._action_id,
                    target=self._req.action.target, agent_id=self._req.agent_id,
                    adapter=self._req.adapter, exec_id=exec_id,
                )
            if self._unjoinable:
                await store.bump_unjoinable(self._redis)
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("procedure observation not recorded: %s", exc)

    async def abort(self, decision: str = "") -> None:
        """The gateway refused the action: nothing happened, so record nothing.

        The warn claims taken while building the advisories are given back —
        holding them would spend this execution's one warn on an edit the agent
        is about to resubmit. The unjoinable counter is NOT bumped either: it
        counts recognised work that could not be joined to an outcome, and an
        edit that never happened is not work.
        """
        if not self._claims:
            return
        try:
            for skill_id, step_id in self._claims:
                await store.release_warn(
                    self._redis, session_id=self._req.session_id,
                    skill_id=skill_id, step_id=step_id,
                )
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("procedure warn claim not released: %s", exc)
        logger.debug(
            "procedure observation dropped for %s: decision=%s",
            getattr(getattr(self._req, "action", None), "target", ""), decision,
        )


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

    async def plan(self, req: ActionBeforeRequest, *,
                   action_id: str = "") -> PendingObservation:
        """Recognise the work and build the advisories. Writes NOTHING.

        Never raises. Returns an empty pending whenever anything is missing or
        off, so the caller's commit/abort is unconditional.

        `action_id` is the id `decide()` minted for THIS action. The spec's
        receipt is `{action_id, target, ts}`; storing an empty one makes the
        observation unjoinable to the `agent.action.predict` event it describes,
        which is the whole point of recording it.
        """
        try:
            settings = self._settings_fn()
            if not getattr(settings, "PROCEDURE_ENABLED", False):
                return PendingObservation()
            if req.action.type != "edit_file":
                return PendingObservation()

            redis_client = self._get_redis()
            if redis_client is None:
                return PendingObservation()
            index = await self._load_index(redis_client, settings)
            if not index:
                return PendingObservation()
            matched = match.match_target(index, req.action.target)
            if not matched:
                return PendingObservation()

            # Checked AFTER the match, deliberately. An execution that cannot be
            # joined to an outcome is not evidence, but the drop is only worth
            # counting once work was RECOGNISED — and checking earlier would put
            # a Redis write on every edit of a deployment with no specs at all.
            if (req.session_id or "").strip().lower() in _UNUSABLE_SESSIONS:
                logger.debug(
                    "procedure match on %s dropped: session_id %r is unjoinable",
                    req.action.target, req.session_id,
                )
                return PendingObservation(
                    redis_client=redis_client, settings=settings, req=req,
                    action_id=action_id, unjoinable=True,
                )

            warn_on = bool(getattr(settings, "PROCEDURE_WARN_ENABLED", True))
            advisories: list[Advisory] = []
            writes: list[tuple[str, str, str]] = []
            claims: list[tuple[str, str]] = []
            # Per skill, the observed step ids as they stand INCLUDING the ones
            # this same edit is about to record. One target can match two globs
            # of one procedure, and while the write happened inline the later
            # entry read the earlier one's step as already observed; deferring
            # the write without carrying this would make an edit warn that it
            # skipped a step it performed in the very same call.
            observed_by_skill: dict[str, set[str]] = {}
            exec_ids: dict[str, str] = {}
            for entry in matched:
                skill_id = entry["skill_id"]
                if skill_id not in observed_by_skill:
                    existing = await store.get_execution(
                        redis_client, req.session_id, skill_id
                    )
                    observed_by_skill[skill_id] = set(
                        (existing or {}).get("observed") or {}
                    )
                    # Minted here rather than by the write, because the advisory
                    # below quotes it and the advisory is built before the write
                    # is allowed to happen. record_observation prefers an id
                    # already on the record, so a second matched edit in the
                    # same execution still reports the first one's id.
                    exec_ids[skill_id] = (
                        (existing or {}).get("exec_id") or store.new_exec_id()
                    )
                exec_id = exec_ids[skill_id]
                writes.append((skill_id, entry["step_id"], exec_id))
                observed_ids = observed_by_skill[skill_id]
                observed_ids.add(entry["step_id"])

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
                    claims.append((skill_id, m["step_id"]))
                    advisories.append(Advisory(
                        code="procedure_step_missing",
                        message=match.advisory_text(entry, m, stats),
                        # The pre-built, previously unused receipt slot. Points
                        # at OUR durable record, not a replay event id — those
                        # resolve through a 30d index whose trim task is never
                        # scheduled.
                        evidence_event_id=exec_id,
                    ))
            return PendingObservation(
                advisories, redis_client=redis_client, settings=settings,
                req=req, action_id=action_id, writes=writes, claims=claims,
            )
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("procedure stage skipped: %s", exc)
            return PendingObservation()

    async def observe(self, req: ActionBeforeRequest, *,
                      action_id: str = "") -> list[Advisory]:
        """Plan and commit in one call — the settled-on-`allow` shape.

        Kept because it IS the whole stage for any caller that already knows the
        action is going ahead; `decide()` does not, which is why it uses the two
        halves separately.
        """
        pending = await self.plan(req, action_id=action_id)
        await pending.commit()
        return pending.advisories
