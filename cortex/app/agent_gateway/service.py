"""Agent Gateway service — tier classification, policy evaluation, decision."""

from __future__ import annotations

import logging
import uuid
from typing import Awaitable, Callable

from app.agent_gateway.models import (
    ActionBeforeRequest,
    ActionBeforeResponse,
    Advisory,
    Decision,
)
from app.agent_gateway.tier import TierContext, classify_tier
from app.config import get_settings
from app.policy.engine import PolicyContext, PolicyEngine

logger = logging.getLogger(__name__)

# Adapters that cannot generate predictions from the tool-call envelope.
PREDICT_INCAPABLE_ADAPTERS = ("shell-hook",)

# Auto-reconcile defaults per (adapter, action_type)
AUTO_RECONCILE_MATRIX = {
    ("shell-hook", "edit_file"): True,
    ("shell-hook", "run_command"): True,
    ("shell-hook", "delete"): True,
    ("shell-hook", "call_api"): False,
    ("shell-hook", "other"): False,
}


class RethinkCounter:
    """Per-(session, target) counter for consecutive rethink verdicts."""

    def __init__(self, redis_client):
        self.redis = redis_client

    async def increment(self, key: str) -> int:
        full_key = f"ag:rethink:{key}"
        count = await self.redis.incr(full_key)
        await self.redis.expire(full_key, 3600)
        return count

    async def reset(self, key: str) -> None:
        await self.redis.delete(f"ag:rethink:{key}")


class AgentGatewayService:
    def __init__(
        self,
        policy_engine: PolicyEngine,
        recent_failure_check: Callable[[str], Awaitable[bool]],
        fastpath_check: Callable[[str, str, str], Awaitable[bool]],
        session_touched_check: Callable[[str, str], Awaitable[bool]],
        replay_emitter: Callable[..., Awaitable[None]],
        rethink_counter: RethinkCounter,
        prediction_redis=None,  # NEW — Redis for the prediction store (cross-process safe)
        fastpath_redis=None,  # optional: enables fastpath cache updates in record()
        policy_decision_redis=None,  # optional: enables audit recording of block/rethink decisions
        # Living Procedures stage. OPTIONAL by construction: main.py builds the
        # whole gateway inside one try/except, so a constructor that rejected an
        # older call site would take out /agent/action/* entirely rather than
        # just this feature.
        procedure_observer=None,
    ):
        self.policy_engine = policy_engine
        self.recent_failure_check = recent_failure_check
        self.fastpath_check = fastpath_check
        self.session_touched_check = session_touched_check
        self.replay_emitter = replay_emitter
        self.rethink_counter = rethink_counter
        self.prediction_redis = prediction_redis
        self._fastpath_redis = fastpath_redis
        self._policy_decision_redis = policy_decision_redis
        self._procedure_observer = procedure_observer

    async def decide(self, req: ActionBeforeRequest) -> ActionBeforeResponse:
        action_id = f"act_{uuid.uuid4().hex[:12]}"

        # Gather signals for tier classification
        recent_failure = await self.recent_failure_check(req.action.target)
        fastpath_hit = await self.fastpath_check(req.agent_id, req.action.type, req.action.target)
        session_clean = await self.session_touched_check(req.session_id, req.action.target)

        tier_ctx = TierContext(
            action=req.action,
            prediction=req.prediction,
            recent_failure_hit=recent_failure,
            fastpath_hit=fastpath_hit,
            session_clean_touch=session_clean,
        )
        tier = classify_tier(tier_ctx)

        # Evaluate policy with tier + prediction context
        policy_ctx = PolicyContext(
            file_path=req.action.target,
            agent_id=req.agent_id,
            session_id=req.session_id,
            tier=tier,
            prediction=req.prediction,
        )
        policy_decision = await self.policy_engine.evaluate(policy_ctx)

        # PolicyEngine can return "warn" (not in gateway Decision Literal).
        # Map warn → allow; advisories carry the reasons for telemetry.
        raw_action = policy_decision.action
        decision: Decision = raw_action if raw_action in ("allow", "rethink", "block") else "allow"  # type: ignore[assignment]
        advisories: list[Advisory] = []
        for reason in policy_decision.reasons:
            advisories.append(self._reason_to_advisory(reason))

        # Living Procedures: recognise the work, record it, and advise on a
        # load-bearing step left undone. Advisory only, and its own try/except
        # inside observe() — it can never change the decision.
        # action_id is threaded in because the observation's receipt is
        # {action_id, target, ts} — without it the record cannot be joined to
        # the agent.action.predict event that describes the same edit.
        if self._procedure_observer is not None:
            advisories.extend(
                await self._procedure_observer.observe(req, action_id=action_id)
            )

        # Predict-incapable adapter mitigation: never rethink on prediction_required alone.
        # Advisory is kept for telemetry; decision is softened to allow.
        if (
            decision == "rethink"
            and req.adapter in PREDICT_INCAPABLE_ADAPTERS
            and any(a.code == "prediction_required" for a in advisories)
            and not any(a.code == "low_confidence" for a in advisories)
        ):
            decision = "allow"

        # Rethink counter & limit escalation
        target_key = f"{req.session_id}:{req.action.target}"
        if decision == "rethink":
            count = await self.rethink_counter.increment(target_key)
            limit = get_settings().AGENT_RETHINK_MAX_LOOPS
            if count >= limit:
                decision = "block"
                advisories.append(Advisory(
                    code="rethink_limit",
                    message=f"Rethink limit ({limit}) reached for this target. Surface to user.",
                    suggested_questions=["What is preventing a confident prediction here?"],
                ))
        elif decision == "allow":
            await self.rethink_counter.reset(target_key)

        # Auto-reconcile defaults
        auto_reconcile = AUTO_RECONCILE_MATRIX.get(
            (req.adapter, req.action.type), False,
        )

        # Audit-record non-allow decisions (warn/block/rethink) for policy
        # visibility. Allows are the common case and would evict interesting
        # records under the list cap, so they are intentionally skipped.
        # Best-effort: never break the decision path.
        #
        # `warn` is remapped to `allow` above (the gateway Decision literal has
        # no warn), so gating on `decision` dropped every warn ever produced by
        # FileRiskRule/SessionHealthRule/RecentFailureRule. Gate on the RAW
        # action and record the warn under its own name; block/rethink keep
        # recording the FINAL decision so rethink->block escalation is visible.
        audit_action = decision if decision != "allow" else raw_action
        if audit_action != "allow" and self._policy_decision_redis is not None:
            try:
                from app.policy.store import record_policy_decision
                await record_policy_decision(
                    self._policy_decision_redis,
                    file_path=req.action.target,
                    agent_id=req.agent_id,
                    session_id=req.session_id,
                    action=audit_action,
                    risk_score=policy_decision.risk_score,
                    reasons=policy_decision.reasons,
                    signals=policy_decision.signals,
                )
            except Exception as exc:
                logger.warning("policy decision record failed for %s: %s", action_id, exc)

        # Store the action record for later reconciliation. Written for EVERY
        # action, not only predicted ones: the shell hook sends no prediction,
        # so gating this on `req.prediction is not None` filed every reconcile
        # from Claude Code under session_id="" and made it invisible to
        # get_session_timeline — which also silently zeroed compute_session_eval's
        # predict->reconcile Brier calculation on that path.
        if self.prediction_redis is not None:
            import json as _json
            entry = {
                "agent_id": req.agent_id,
                "session_id": req.session_id,
                "prediction": req.prediction.model_dump() if req.prediction else None,
                "adapter": req.adapter,
                "action_type": req.action.type,
                "target": req.action.target,
            }
            ttl = get_settings().AGENT_RECONCILE_DEADLINE_SECONDS
            try:
                await self.prediction_redis.set(
                    f"ag:predict:{action_id}",
                    _json.dumps(entry),
                    ex=ttl,
                )
            except Exception as exc:
                logger.warning("prediction store write failed for %s: %s", action_id, exc)

        resp = ActionBeforeResponse(
            decision=decision,
            action_id=action_id,
            tier=tier,
            advisories=advisories,
            reconcile_deadline_seconds=get_settings().AGENT_RECONCILE_DEADLINE_SECONDS,
            auto_reconcile=auto_reconcile,
        )

        # Emit replay event (best-effort)
        try:
            await self.replay_emitter(
                event_type="agent.action.predict",
                session_id=req.session_id,
                agent_id=req.agent_id,
                payload={
                    "action_id": action_id,
                    "action": req.action.model_dump(),
                    "prediction": req.prediction.model_dump() if req.prediction else None,
                    "tier": tier,
                    "decision": decision,
                    "adapter": req.adapter,
                    "advisories": [a.model_dump() for a in advisories],
                },
            )
        except Exception as exc:
            logger.warning("replay emit failed for action %s: %s", action_id, exc)

        return resp

    async def record(self, req) -> "ActionAfterResponse":  # noqa: F821 — runtime import below
        from app.agent_gateway.models import ActionAfterResponse, Prediction
        from app.agent_gateway.reconciler import compute_prediction_match_score
        import json as _json

        entry = None
        if self.prediction_redis is not None:
            try:
                raw = await self.prediction_redis.get(f"ag:predict:{req.action_id}")
                if raw:
                    entry = _json.loads(raw)
                    # Best-effort delete after read (idempotent)
                    await self.prediction_redis.delete(f"ag:predict:{req.action_id}")
            except Exception as exc:
                logger.warning("prediction store read failed for %s: %s", req.action_id, exc)

        # An action record is now written for EVERY action, so `prediction` is
        # None whenever the caller sent none (the shell-hook path). A null
        # prediction scores None — there is nothing to compare — rather than
        # raising into the warning log on every reconcile.
        score = None
        stored_prediction = (entry or {}).get("prediction")
        if stored_prediction:
            try:
                pred = Prediction(**stored_prediction)
                score = compute_prediction_match_score(pred, req.outcome)
            except Exception as exc:
                logger.warning("score computation failed for %s: %s", req.action_id, exc)

        # Update fastpath cache (best-effort, only if redis client is wired)
        if entry is not None and self._fastpath_redis is not None:
            try:
                from app.agent_gateway.fastpath import record_outcome_for_fastpath
                ttl = 86400
                try:
                    ttl = get_settings().AGENT_FASTPATH_CACHE_TTL_SECONDS
                except AttributeError:
                    pass
                await record_outcome_for_fastpath(
                    self._fastpath_redis,
                    entry["agent_id"],
                    entry.get("action_type", "other"),
                    entry.get("target", ""),
                    success=req.outcome.success,
                    ttl_seconds=ttl,
                )
            except Exception as exc:
                logger.debug("fastpath update skipped: %s", exc)

        # Emit reconcile event (best-effort)
        try:
            await self.replay_emitter(
                event_type="agent.action.reconcile",
                session_id=entry["session_id"] if entry else "",
                agent_id=entry["agent_id"] if entry else "",
                payload={
                    "action_id": req.action_id,
                    "outcome": req.outcome.model_dump(),
                    "source": "agent",
                    "prediction_match_score": score,
                },
                # Without this the event carries no top-level outcome, so
                # _failure_rate (evals/scorers.py) never counts it. Measured:
                # no production emitter passed outcome= except Bridge's session
                # lifecycle, so effectively every session evaluated as success.
                outcome="success" if req.outcome.success else "failure",
            )
        except Exception as exc:
            logger.warning("reconcile emit failed for %s: %s", req.action_id, exc)

        return ActionAfterResponse(
            action_id=req.action_id,
            prediction_match_score=score,
            recorded=True,
        )

    @staticmethod
    def _reason_to_advisory(reason: str) -> Advisory:
        """Map free-text reason from policy rules to a structured advisory.

        Reasons from the policy engine may be bare strings (e.g. "prediction_required")
        or prefixed with "[rule_name] ..." — both forms are handled.
        """
        # Strip optional "[rule_name] " prefix before matching
        bare = reason.split("] ", 1)[-1] if "] " in reason else reason

        if bare == "prediction_required" or bare.startswith("prediction_required"):
            return Advisory(
                code="prediction_required",
                message="Action tiered as elevated risk; please submit a prediction.",
                suggested_questions=[
                    "What outcome do you expect from this action?",
                    "What success criteria would prove it worked?",
                ],
            )
        if bare == "low_confidence" or bare.startswith("low_confidence"):
            return Advisory(
                code="low_confidence",
                message="Confidence below threshold for a full-tier action.",
                suggested_questions=[
                    "What information would raise your confidence?",
                    "Should you ask the user a clarifying question first?",
                ],
            )
        if "deny pattern" in reason:
            return Advisory(code="path_deny", message=reason)
        if "Session" in reason or "session" in reason.lower()[:10]:
            # SessionHealthRule message starts with "Session has high failure rate..."
            # Must be checked before the generic "failure" branch below
            return Advisory(code="session_health", message=reason)
        if "hotspot" in reason:
            return Advisory(code="file_risk", message=reason)
        if "failure rate" in reason or "failure" in reason:
            return Advisory(code="recent_failure", message=reason)
        return Advisory(code="pattern_risk", message=reason)


def get_agent_gateway_service():
    """DI hook overridden by main.py after lifespan wiring."""
    raise RuntimeError("Agent gateway service not yet wired")
