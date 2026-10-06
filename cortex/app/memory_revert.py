"""Revert one credential's memory writes in a time window (THREAT-MODEL §5.19).

The second half of answering threat 5. The write ceiling (app/write_limit.py)
bounds how fast a compromised non-admin key can poison memory; this makes
what it already wrote reversible. Since 2026-10-04 every /memory/learn point
records the verified ``metadata.credential_id`` of its latest writer (§5.12).

``POST /admin/memory/revert`` -- admin only, scoped to the caller's workspace:

- selects points whose ``metadata.credential_id`` is the named credential and
  whose ``timestamp`` falls in ``[since, until)`` (``until`` defaults to now),
  skipping points already archived (a GC archive keeps its own provenance);
- dry run by default: the count, how many points had an unreadable timestamp,
  and a sample;
- ``apply=true`` archives each one through ``VectorClient.update_status`` --
  the same lifecycle a human archive uses, so recall excludes it (and the graph
  rows linked to it, via RAGEngine's vector-lifecycle gate), its pre-archive
  status is kept, it is never purge-eligible, and it carries a reason naming
  the credential, the window and the admin credential that applied it, plus a
  ``revert_id``.

``POST /admin/memory/revert/undo`` restores every point one revert archived
(dry run by default), each back to the status it held.

``timestamp`` is the point's LAST-SEEN time: a re-learn of identical text, a
confirm, a restore and the memory agent's merge all refresh it. So the window
selects points the credential was the latest writer of and that were last
touched in the window -- see docs/THREAT-MODEL.md §5.19 for what that misses.

It does NOT revoke the key. Runbook: revoke first
(``firekeep-admin keys revoke``), then dry-run, then apply -- a live key could
otherwise keep writing while the revert runs. Each archived point costs a
retrieve and a set_payload (``update_status`` reads the prior status per point).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, Field, model_validator
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.db.visibility import workspace_condition
from auth.middleware import require_scope

logger = logging.getLogger(__name__)

AUDIT_LOG_KEY = "gc:eviction:log"  # the maintenance trail the dashboard reads
_PAGE = 256
_SAMPLE = 10


class RevertRequest(BaseModel):
    credential_id: str = Field(pattern=r"^[0-9a-f]{1,64}$")
    since: AwareDatetime
    until: AwareDatetime | None = None
    apply: bool = False

    @model_validator(mode="after")
    def _window(self) -> "RevertRequest":
        if self.until is not None and self.until <= self.since:
            raise ValueError("until must be later than since")
        return self


class RevertUndoRequest(BaseModel):
    revert_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    apply: bool = False


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


async def _scroll(vector, scroll_filter: Filter) -> list[Any]:
    """Every point matching ``scroll_filter`` -- all pages, not the first."""
    out: list[Any] = []
    offset = None
    while True:
        records, offset = await vector._client.scroll(
            collection_name=vector._collection,
            scroll_filter=scroll_filter,
            limit=_PAGE,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        out.extend(records)
        if offset is None:
            return out


async def select_credential_writes(
    vector,
    *,
    workspace_id: str,
    credential_id: str,
    since: datetime,
    until: datetime,
) -> tuple[list[Any], int]:
    """(points newest first, count with an unreadable timestamp).

    Points in ``workspace_id`` whose latest writer was ``credential_id`` and
    whose last-seen ``timestamp`` is in ``[since, until)``; archived points
    are skipped. A naive stored timestamp is read as UTC.
    """
    candidates = await _scroll(vector, Filter(
        must=[
            workspace_condition(workspace_id),
            FieldCondition(key="metadata.credential_id",
                           match=MatchValue(value=credential_id)),
        ],
        must_not=[FieldCondition(key="status", match=MatchValue(value="archived"))],
    ))
    matched: list[tuple[datetime, Any]] = []
    unparsable = 0
    for point in candidates:
        ts = _parse_ts((point.payload or {}).get("timestamp"))
        if ts is None:
            unparsable += 1
        elif since <= ts < until:
            matched.append((ts, point))
    matched.sort(key=lambda pair: pair[0], reverse=True)
    return [point for _, point in matched], unparsable


def _sample_row(point: Any) -> dict[str, Any]:
    payload = point.payload or {}
    return {
        "id": str(point.id),
        "timestamp": payload.get("timestamp"),
        "member_id": payload.get("member_id"),
        "namespace": payload.get("namespace"),
        "status": payload.get("status", "active"),
        "text": str(payload.get("text", ""))[:200],
    }


async def _audit(redis_client: Any, entry: dict[str, Any]) -> None:
    if redis_client is None:
        return
    try:
        pipe = redis_client.pipeline()
        pipe.lpush(AUDIT_LOG_KEY, json.dumps(entry))
        pipe.ltrim(AUDIT_LOG_KEY, 0, 999)
        await pipe.execute()
    except Exception:
        logger.exception("Revert %s done but its audit entry was NOT written",
                         entry.get("revert_id"))


def create_memory_revert_router(
    get_vector: Callable[..., Any],
    get_redis: Callable[..., Any],
) -> APIRouter:
    router = APIRouter(prefix="/admin/memory", tags=["admin"])
    _admin = require_scope("admin")

    @router.post("/revert")
    async def revert_by_credential(
        body: RevertRequest,
        identity: dict = Depends(_admin),
        vector: Any = Depends(get_vector),
        redis_client: Any = Depends(get_redis),
    ) -> dict[str, Any]:
        """Archive (or, by default, only count) one credential's writes in a window."""
        workspace_id = identity["workspace_id"]
        since = body.since.astimezone(timezone.utc)
        until = (body.until or datetime.now(timezone.utc)).astimezone(timezone.utc)
        matched, unparsable = await select_credential_writes(
            vector, workspace_id=workspace_id, credential_id=body.credential_id,
            since=since, until=until,
        )

        window = (f"[{body.since.isoformat()}, "
                  f"{body.until.isoformat() if body.until else 'now'})")
        result: dict[str, Any] = {
            "applied": body.apply,
            "workspace_id": workspace_id,
            "credential_id": body.credential_id,
            "since": body.since.isoformat(),
            "until": body.until.isoformat() if body.until else None,
            "matched": len(matched),
            "unparsable_timestamps": unparsable,
            "archived": 0,
            "revert_id": None,
            "sample": [_sample_row(p) for p in matched[:_SAMPLE]],
        }
        if not body.apply or not matched:
            return result

        revert_id = uuid.uuid4().hex
        by = str(identity.get("credential_id") or "")
        reason = (f"revert {revert_id}: writes by credential {body.credential_id} "
                  f"in {window}, applied by credential {by or 'unknown'}")
        archived = 0
        for point in matched:
            try:
                await vector.update_status(
                    memory_id=str(point.id), status="archived",
                    reason=reason, revert_id=revert_id,
                )
                archived += 1
            except Exception:
                logger.exception("Revert %s: failed to archive %s", revert_id, point.id)
        logger.warning(
            "Revert %s: archived %d/%d memories written by credential %s in %s "
            "(workspace %s, by %s)",
            revert_id, archived, len(matched), body.credential_id, window,
            workspace_id, by,
        )
        await _audit(redis_client, {
            "action": "reverted",
            "revert_id": revert_id,
            "credential_id": body.credential_id,
            "since": result["since"],
            "until": result["until"],
            "count": archived,
            "workspace_id": workspace_id,
            "by_credential_id": by,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        })
        result.update({"archived": archived, "revert_id": revert_id})
        return result

    @router.post("/revert/undo")
    async def undo_revert(
        body: RevertUndoRequest,
        identity: dict = Depends(_admin),
        vector: Any = Depends(get_vector),
        redis_client: Any = Depends(get_redis),
    ) -> dict[str, Any]:
        """Restore every point one revert archived (or, by default, count them)."""
        workspace_id = identity["workspace_id"]
        points = await _scroll(vector, Filter(must=[
            workspace_condition(workspace_id),
            FieldCondition(key="revert_id", match=MatchValue(value=body.revert_id)),
            FieldCondition(key="status", match=MatchValue(value="archived")),
        ]))
        result: dict[str, Any] = {
            "applied": body.apply,
            "workspace_id": workspace_id,
            "revert_id": body.revert_id,
            "matched": len(points),
            "restored": 0,
            "sample": [_sample_row(p) for p in points[:_SAMPLE]],
        }
        if not body.apply or not points:
            return result
        restored = 0
        for point in points:
            try:
                if await vector.restore_memory(str(point.id)):
                    restored += 1
            except Exception:
                logger.exception("Undo of revert %s: failed to restore %s",
                                 body.revert_id, point.id)
        await _audit(redis_client, {
            "action": "revert_undone",
            "revert_id": body.revert_id,
            "count": restored,
            "workspace_id": workspace_id,
            "by_credential_id": str(identity.get("credential_id") or ""),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        })
        result["restored"] = restored
        return result

    return router
