"""Per-credential ceiling on memory writes (docs/THREAT-MODEL.md §5.19).

Threat 5 is a compromised agent holding a valid non-admin key writing memories
every teammate's agent then recalls. Writes have been attributed since
2026-10-04 (§5.12); this bounds how fast one credential can make them, and
tells the owner when one tries to go faster.

WHERE IT IS CHARGED. Every surface an agent writes memory through converges
on three REST routes: cortex-mcp's tools proxy onto them with the caller's own
key (mcp_server.py ``_CallerKeyAuth``), and the client kit, symdex, night shift
and the dashboard call them directly. So the charge sits in those routes'
shared bodies -- ``_store_memory_learning`` (both /memory/learn routes), the
/memory/stream route (one unit per event: the sleep cycle's graph writes carry
no identity, so intake is the only point a credential can be charged) and
POST /skills -- against ONE counter per credential. A decorator on one REST
route would have limited nothing that arrives over MCP under a different path.

THE COUNTER. A fixed window in Cortex's own Redis (DB 0, the ``redis_client``
every write route already holds), so it is shared by every cortex-api worker.
slowapi's ``Limiter`` is left as it is: it keys on the client address with
in-process storage, which is per worker and -- because every MCP write
reaches cortex-api from cortex-mcp's address -- one bucket for every agent.

WHO IS NOT LIMITED, and why:
- auth disabled (personal mode, the LongMemEval stack): there is one principal
  and nothing to tell apart; behaviour is exactly as before.
- ``admin`` / ``*`` keys: the owner and the dashboard. A compromised admin key
  can mint keys, so a write ceiling would not contain it.
- service keys (any scope in ``auth.keys.SERVICE_ONLY_SCOPES``): Bridge's
  distiller (``memory:write:delegated``) and FIREKEEP_INTERNAL_KEY. They are
  minted only by deploy/bootstrap-keys.sh; a distillate is still attributed to
  the member it is written for, so revert-by-credential covers it.

A Redis failure fails OPEN (logged): a counter outage must not stop every
member's writes. Missing identity on an authenticated principal fails CLOSED.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger(__name__)

LIMIT_KEY_PREFIX = "memory:write_limit:"
SIGNAL_KEY_PREFIX = "memory:write_limit_signalled:"
WEBHOOK_EVENT = "memory.write_limited"
REPLAY_EVENT = "memory_write_limited"
ERROR_CODE = "MEMORY_WRITE_LIMITED"


def limit_subject(principal: dict[str, Any]) -> str | None:
    """The counter a principal's writes are charged to; None = not limited.

    Keyed on the verified credential so two devices of one member are two
    budgets and one key used from many hosts is one. A principal with no
    credential id (a legacy record) falls back to its member.
    """
    if not principal.get("authenticated"):
        return None
    from auth.keys import SERVICE_ONLY_SCOPES, scopes_allow

    scopes = principal.get("scopes") or []
    if scopes_allow(scopes, "admin"):
        return None
    if set(scopes) & SERVICE_ONLY_SCOPES:
        return None
    credential_id = str(principal.get("credential_id") or "")
    if credential_id:
        return f"credential:{credential_id}"
    member_id = str(principal.get("member_id") or "")
    if member_id:
        return f"member:{member_id}"
    raise HTTPException(
        status_code=403,
        detail="Memory write refused: the caller's credential is unattributable",
    )


async def charge_memory_write(
    principal: dict[str, Any],
    redis_client: Any,
    *,
    surface: str,
    units: int = 1,
    namespace: str = "default",
) -> None:
    """Spend ``units`` of the principal's write budget, or raise 429.

    Call BEFORE anything is written. A refused request spends nothing (its
    units are given back), so a too-large stream batch does not exhaust the
    budget it was refused for.
    """
    settings = get_settings()
    limit = int(getattr(settings, "MEMORY_WRITE_LIMIT_PER_CREDENTIAL", 0) or 0)
    if limit <= 0 or units <= 0:
        return
    subject = limit_subject(principal)
    if subject is None:
        return
    if redis_client is None:
        logger.warning(
            "Memory write limit not enforced (no counter store) for %s on %s; "
            "failing open", subject, surface,
        )
        return

    window = max(1, int(getattr(settings, "MEMORY_WRITE_LIMIT_WINDOW_SECONDS", 3600)))
    now = time.time()
    bucket = int(now // window)
    key = f"{LIMIT_KEY_PREFIX}{subject}:{bucket}"
    try:
        pipe = redis_client.pipeline()
        pipe.incrby(key, units)
        pipe.expire(key, window + 60)
        results = await pipe.execute()
        count = int(results[0])
    except Exception as exc:  # noqa: BLE001 -- a counter outage fails open
        logger.warning(
            "Memory write limit check failed for %s on %s; failing open: %s",
            subject, surface, exc,
        )
        return
    if count <= limit:
        return

    try:
        await redis_client.decrby(key, units)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not return refused write units for %s: %s", subject, exc)

    retry_after = max(1, math.ceil((bucket + 1) * window - now))
    credential_id = str(principal.get("credential_id") or "")
    logger.warning(
        "Memory write limit reached: %s (member %s) refused on %s "
        "(%d units, limit %d per %ds)",
        subject, principal.get("member_id"), surface, units, limit, window,
    )
    await _signal_first_refusal(
        redis_client, principal, subject=subject, bucket=bucket, window=window,
        limit=limit, surface=surface, namespace=namespace,
    )
    raise HTTPException(
        status_code=429,
        detail={
            "error_code": ERROR_CODE,
            "detail": (
                f"Memory write limit reached for this credential: {limit} writes "
                f"per {window} seconds. Nothing was written."
            ),
            "credential_id": credential_id,
            "limit": limit,
            "window_seconds": window,
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


async def _signal_first_refusal(
    redis_client: Any,
    principal: dict[str, Any],
    *,
    subject: str,
    bucket: int,
    window: int,
    limit: int,
    surface: str,
    namespace: str,
) -> None:
    """Tell the owner -- once per credential per window, so a hammering key
    cannot flood the replay stream or a Slack channel with its refusals."""
    try:
        first = await redis_client.set(
            f"{SIGNAL_KEY_PREFIX}{subject}:{bucket}", "1", nx=True, ex=window + 60,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Write-limit signal dedup failed for %s: %s", subject, exc)
        first = True
    if not first:
        return

    payload = {
        "credential_id": str(principal.get("credential_id") or ""),
        "member_id": principal.get("member_id"),
        "workspace_id": principal.get("workspace_id"),
        "surface": surface,
        "limit": limit,
        "window_seconds": window,
    }
    # Lazy: app.main imports this module.
    from app import main as _main
    from app import webhooks as _webhooks

    await _main._replay_emit(
        REPLAY_EVENT,
        session_id="unknown",
        agent_id="unknown",
        workspace_id=principal.get("workspace_id"),
        member_id=principal.get("member_id"),
        payload=payload,
        outcome="refused",
    )
    try:
        asyncio.create_task(_webhooks.fire_webhooks(
            redis_client, WEBHOOK_EVENT, payload, namespace=namespace,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Write-limit webhook failed: %s", exc)
