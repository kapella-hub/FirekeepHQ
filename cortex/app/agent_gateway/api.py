"""Agent Gateway REST router."""

from __future__ import annotations

import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends

from auth.middleware import require_scope

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
