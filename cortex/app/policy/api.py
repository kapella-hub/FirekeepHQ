"""Policy Engine REST API — FastAPI router mounted on Cortex."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.middleware import require_scope

from app.policy.engine import PolicyContext, PolicyEngine

logger = logging.getLogger(__name__)


class EvaluateRequest(BaseModel):
    file_path: str
    agent_id: str = ""
    session_id: str = ""


def create_policy_router(get_engine, get_gateway_service=None, get_decision_redis=None) -> APIRouter:
    """Create the policy API router.

    Args:
        get_engine: callable returning the PolicyEngine instance.
        get_gateway_service: optional callable returning the AgentGatewayService
            instance. When provided, /evaluate proxies to the gateway for
            backward-compatible behavior. When absent, falls back to the original
            policy-engine-only path.
        get_decision_redis: optional callable returning a Redis client holding the
            policy decision audit log. When absent, /decisions returns an empty
            list with a "not wired" note rather than erroring.
    """
    router = APIRouter(prefix="/policy", tags=["policy"])

    @router.post("/evaluate", deprecated=True)
    async def evaluate_policy(
        body: EvaluateRequest,
        identity: dict = Depends(require_scope("eval:read")),
    ) -> dict[str, Any]:
        """DEPRECATED: use /agent/action/before instead.

        Proxies to the agent gateway with adapter='shell-hook' for backward
        compatibility. Will be removed in a future minor version.
        """
        if get_gateway_service is not None:
            try:
                from app.agent_gateway.models import Action, ActionBeforeRequest
                req = ActionBeforeRequest(
                    session_id=body.session_id,
                    agent_id=body.agent_id,
                    adapter="shell-hook",
                    action=Action(type="edit_file", target=body.file_path),
                )
                service = get_gateway_service()
                resp = await service.decide(req)
                # Map back to legacy response shape.
                # 'rethink' has no legacy equivalent — treat as 'warn'.
                return {
                    "action": resp.decision if resp.decision != "rethink" else "warn",
                    "reasons": [a.message for a in resp.advisories],
                    "risk_score": 1.0 if resp.decision == "block" else 0.0,
                }
            except Exception as exc:
                logger.warning("policy_evaluate proxy failed, falling back: %s", exc)
        # Fallback: original engine path (no gateway service or proxy failed).
        engine: PolicyEngine = get_engine()
        ctx = PolicyContext(
            file_path=body.file_path,
            agent_id=body.agent_id,
            session_id=body.session_id,
        )
        decision = await engine.evaluate(ctx)
        return decision.model_dump()

    @router.get("/rules")
    async def list_rules(
        identity: dict = Depends(require_scope("eval:read")),
    ) -> dict[str, Any]:
        """List all policy rules and their enabled status."""
        engine: PolicyEngine = get_engine()
        return {"rules": engine.list_rules()}

    @router.get("/decisions")
    async def list_decisions(
        limit: int = 50,
        action: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Recent policy decisions (audit log of block/rethink outcomes).

        Unauthenticated to match the /ops operational endpoints the dashboard
        consumes. Allows are not recorded, so this surfaces only blocks/rethinks.
        """
        from app.policy.store import get_policy_decisions, summarize_policy_decisions

        limit = max(1, min(limit, 500))
        if get_decision_redis is None:
            return {"decisions": [], "summary": summarize_policy_decisions([]), "error": "not wired"}

        redis_client = get_decision_redis()
        if redis_client is None:
            return {"decisions": [], "summary": summarize_policy_decisions([]), "error": "not wired"}

        decisions = await get_policy_decisions(
            redis_client, limit=limit, action=action, agent_id=agent_id,
        )
        return {"decisions": decisions, "summary": summarize_policy_decisions(decisions)}

    @router.post("/rules/{rule_name}/toggle")
    async def toggle_rule(
        rule_name: str,
        identity: dict = Depends(require_scope("admin")),
    ) -> dict[str, Any]:
        """Toggle a policy rule on or off."""
        engine: PolicyEngine = get_engine()
        new_state = engine.toggle_rule(rule_name)
        if new_state is None:
            raise HTTPException(status_code=404, detail=f"Rule '{rule_name}' not found")
        return {"rule": rule_name, "enabled": new_state}

    return router
