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

ROUND 2 additions (spec 2026-08-15): the VERIFIED workspace threads into every
lookup and write (`workspace=`, resolved server-side from the auth principal —
never from the client payload, never from `agent_id`); command-kind steps get
their own two-phase pair (`plan_command`, backed by enforce.py) whose evidence
is PENDING at decide and commits only on a successful reconcile; and executions
now close when their terminal step commits, so the next match opens
execution_no+1 with a fresh evidence scope.
"""

from __future__ import annotations

import logging
import time

from app.agent_gateway.models import ActionBeforeRequest, Advisory
from app.procedures import enforce, match, store

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
                 "_writes", "_claims", "_unjoinable", "_workspace")

    def __init__(self, advisories=None, *, redis_client=None, settings=None,
                 req: ActionBeforeRequest | None = None, action_id: str = "",
                 writes=None, claims=None, unjoinable: bool = False,
                 workspace: str = ""):
        self.advisories: list[Advisory] = list(advisories or [])
        self._redis = redis_client
        self._settings = settings
        self._req = req
        self._action_id = action_id
        # (skill_id, step_id, exec_id, expected_execution_no) — one per
        # matched index entry. File-step commits NEVER close an execution:
        # "file_glob semantics unchanged" is a round-1 keep, and closing on a
        # terminal file edit would re-arm the warn latch round 1 promises fires
        # once. Only a terminal COMMAND step's successful reconcile closes
        # (enforce.reconcile).
        self._writes = list(writes or [])
        # (skill_id, step_id, execution_no) — warn latches claimed while
        # building advisories.
        self._claims = list(claims or [])
        self._unjoinable = unjoinable
        self._workspace = workspace

    async def commit(self) -> None:
        """The decision settled on `allow`: the edit happened, so record it.

        Never raises (I6) — a Redis failure must not cost a customer's edit.
        """
        try:
            for skill_id, step_id, exec_id, exec_no in self._writes:
                await store.record_observation(
                    self._redis, self._settings,
                    session_id=self._req.session_id, skill_id=skill_id,
                    step_id=step_id, action_id=self._action_id,
                    target=self._req.action.target, agent_id=self._req.agent_id,
                    adapter=self._req.adapter, exec_id=exec_id,
                    workspace_id=self._workspace,
                    expected_execution_no=exec_no, closes_execution=False,
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
            for skill_id, step_id, exec_no in self._claims:
                await store.release_warn(
                    self._redis, session_id=self._req.session_id,
                    skill_id=skill_id, step_id=step_id,
                    workspace_id=self._workspace, execution_no=exec_no,
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
        self._index_ok: bool = True

    async def _load_index(self, redis_client, settings) -> list[dict]:
        ttl = float(getattr(settings, "PROCEDURE_INDEX_CACHE_SECONDS", 30) or 0)
        now = time.monotonic()
        if ttl and self._index_at and (now - self._index_at) < ttl:
            return self._index
        # ok=False (unreadable, not merely absent) is never cached: the next
        # call retries the read rather than pinning "not evaluated" for a TTL.
        self._index, self._index_ok = await store.load_index_result(redis_client)
        self._index_at = now if self._index_ok else 0.0
        return self._index

    async def plan(self, req: ActionBeforeRequest, *,
                   action_id: str = "", workspace: str = "") -> PendingObservation:
        """Recognise the work and build the advisories. Writes NOTHING.

        Never raises. Returns an empty pending whenever anything is missing or
        off, so the caller's commit/abort is unconditional.

        `action_id` is the id `decide()` minted for THIS action. The spec's
        receipt is `{action_id, target, ts}`; storing an empty one makes the
        observation unjoinable to the `agent.action.predict` event it describes,
        which is the whole point of recording it.

        `workspace` is the VERIFIED workspace threaded by decide() from the
        auth principal. Empty means "no principal on this call path" (direct
        service construction, round-1 tests): lookups stay machine-global and
        keys keep their round-1 shape — byte-identical legacy behaviour.
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
            if workspace:
                index = [e for e in index if isinstance(e, dict)
                         and store.workspace_visible(
                             e.get("workspace_id") or "", workspace)]
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
                    action_id=action_id, unjoinable=True, workspace=workspace,
                )

            warn_on = bool(getattr(settings, "PROCEDURE_WARN_ENABLED", True))
            advisories: list[Advisory] = []
            writes: list[tuple[str, str, str, int]] = []
            claims: list[tuple[str, str, int]] = []
            # Per skill, the observed step ids as they stand INCLUDING the ones
            # this same edit is about to record. One target can match two globs
            # of one procedure, and while the write happened inline the later
            # entry read the earlier one's step as already observed; deferring
            # the write without carrying this would make an edit warn that it
            # skipped a step it performed in the very same call.
            observed_by_skill: dict[str, set[str]] = {}
            exec_ids: dict[str, str] = {}
            exec_nos: dict[str, int] = {}
            for entry in matched:
                skill_id = entry["skill_id"]
                if skill_id not in observed_by_skill:
                    existing = await store.get_execution(
                        redis_client, req.session_id, skill_id, workspace,
                    )
                    exec_nos[skill_id] = store.effective_execution_no(existing)
                    if existing and existing.get("closed_at"):
                        # Round-2 boundary: the previous execution ended when
                        # its terminal step committed. This match belongs to a
                        # FRESH evidence scope.
                        observed_by_skill[skill_id] = set()
                        exec_ids[skill_id] = store.new_exec_id()
                    else:
                        observed_by_skill[skill_id] = set(
                            (existing or {}).get("observed") or {}
                        )
                        # Minted here rather than by the write, because the
                        # advisory below quotes it and the advisory is built
                        # before the write is allowed to happen.
                        # record_observation prefers an id already on the
                        # record, so a second matched edit in the same
                        # execution still reports the first one's id.
                        exec_ids[skill_id] = (
                            (existing or {}).get("exec_id") or store.new_exec_id()
                        )
                exec_id = exec_ids[skill_id]
                exec_no = exec_nos[skill_id]
                writes.append((skill_id, entry["step_id"], exec_id, exec_no))
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
                        workspace_id=workspace, execution_no=exec_no,
                    )
                    if not claimed:
                        continue
                    claims.append((skill_id, m["step_id"], exec_no))
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
                workspace=workspace,
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

    # ------------------------------------------------------------------
    # Round 2 — command enforcement (enforce.py, threaded through decide())
    # ------------------------------------------------------------------

    async def plan_command(self, req: ActionBeforeRequest, *, action_id: str,
                           workspace: str = "",
                           member: str = "") -> enforce.CommandEnforcement:
        """Verdict + pending evidence for one run_command action. Never raises.

        `workspace`/`member` come from the VERIFIED principal, resolved
        server-side on the action path — never from the client payload and
        never from `agent_id`, which stays an observability label.
        """
        try:
            settings = self._settings_fn()
            if not getattr(settings, "PROCEDURE_ENABLED", False):
                # Feature off IS an evaluation result: no runbook governs
                # anything. The marker lets a client holding a stale bundle
                # with block entries drain gracefully instead of failing
                # closed forever against a deploy that disabled the feature.
                return enforce.CommandEnforcement(
                    advisories=[enforce.evaluated_marker()])
            if req.action.type != "run_command":
                return enforce.CommandEnforcement()
            redis_client = self._get_redis()
            if redis_client is None:
                # Enforcement backend gone: NOT evaluated — no marker, and a
                # block-mode client fails closed (review 2026-08-15: a
                # reachable-but-broken server must not convert block into an
                # authenticated allow).
                return enforce.CommandEnforcement()
            index = await self._load_index(redis_client, settings)
            return await enforce.evaluate(
                redis_client, settings, req=req, workspace=workspace,
                member=member, action_id=action_id, index=index,
                index_ok=self._index_ok,
            )
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("command enforcement stage skipped: %s", exc)
            return enforce.CommandEnforcement()

    async def reconcile_command(self, *, action_id: str, success: bool,
                                exit_status: int | None,
                                caller_workspace: str = "") -> dict:
        """Settle pending command evidence on the reconcile path. Never raises."""
        try:
            settings = self._settings_fn()
            if not getattr(settings, "PROCEDURE_ENABLED", False):
                return {"status": "disabled"}
            redis_client = self._get_redis()
            if redis_client is None:
                return {"status": "no-redis"}
            return await enforce.reconcile(
                redis_client, settings, action_id=action_id, success=success,
                exit_status=exit_status, caller_workspace=caller_workspace,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("command reconcile stage skipped: %s", exc)
            return {"status": "error"}
