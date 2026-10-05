"""Agent Gateway REST router."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends

from auth.middleware import require_scope

from app.session_owner import attributable_session_id
from app.agent_gateway.models import (
    ActionAfterRequest,
    ActionAfterResponse,
    ActionBeforeRequest,
    ActionBeforeResponse,
)

logger = logging.getLogger(__name__)


def create_agent_gateway_router(get_service: Callable[[], Any]) -> APIRouter:
    """Create the agent gateway router.

    Args:
        get_service: callable returning a service object exposing
            async `decide(request) -> ActionBeforeResponse` and
            async `record(request) -> ActionAfterResponse` methods.
    """
    router = APIRouter(prefix="/agent/action", tags=["agent-gateway"])

    @router.post("/before", response_model=ActionBeforeResponse)
    async def action_before(
        body: ActionBeforeRequest,
        identity: dict = Depends(require_scope("eval:read")),
    ) -> ActionBeforeResponse:
        # Tenancy precedes enforcement: the VERIFIED workspace/member from the
        # auth principal, stamped AFTER validation onto PrivateAttrs no client
        # payload can reach. `agent_id` stays an observability label.
        body._verified_workspace = (identity or {}).get("workspace_id") or ""
        body._verified_member = (identity or {}).get("member_id") or ""
        # The session id is client telemetry. A caller naming a session it does
        # not own acts under "unknown" instead: decide() files the predict
        # event, the prediction record (which the reconcile AND the Celery
        # overdue sweep later emit under) and the per-session rethink counter
        # by it, so this one line keeps all three out of another member's
        # session (docs/THREAT-MODEL.md §5.16).
        body.session_id = await attributable_session_id(
            body.session_id,
            workspace_id=body._verified_workspace,
            member_id=body._verified_member,
        )
        service = get_service()
        return await service.decide(body)

    @router.post("/after", response_model=ActionAfterResponse)
    async def action_after(
        body: ActionAfterRequest,
        identity: dict = Depends(require_scope("eval:write")),
    ) -> ActionAfterResponse:
        body._verified_workspace = (identity or {}).get("workspace_id") or ""
        service = get_service()
        return await service.record(body)

    return router
