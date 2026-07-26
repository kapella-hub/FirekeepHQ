"""Skill staleness sweep — the aging story active skills lacked.

Client-authored skills have no source document, so `needs_rereview` (which
fires only when a backing document changes) never catches a skill whose
subject rotted. This pass flags active skills that have gone unrecalled beyond
`SKILL_STALE_AFTER_DAYS` so a human can review them, and un-flags any that were
recalled again (self-healing both directions). It NEVER changes skill_status
and NEVER deletes — staleness is a review signal; deletion stays human-only
(mirrors reconcile.py's 'active skills are never deleted' rule and gc.py's
skill-eviction skip). Registered in run_memory_agent AFTER the access-count /
last-recalled flush, so freshness timestamps are current when it evaluates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

logger = logging.getLogger(__name__)


def _parse_iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    # Treat naive timestamps as UTC so the comparison never raises.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def skill_staleness_pass(client=None, settings=None, now: datetime | None = None) -> dict[str, Any]:
    """Flag/un-flag active skills by recall freshness. Injectable client /
    settings / now for tests; defaults to the real Qdrant client + settings."""
    close_after = client is None
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    if client is None:
        from app.workers.memory_agent import _get_qdrant_client
        client = _get_qdrant_client()
    if now is None:
        now = datetime.now(timezone.utc)

    stale_after_days = int(getattr(settings, "SKILL_STALE_AFTER_DAYS", 90))
    cutoff = now - timedelta(days=stale_after_days)

    flagged = unstaled = skipped = 0
    try:
        points, _ = client.scroll(
            collection_name=settings.QDRANT_COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="memory_type", match=MatchValue(value="skill")),
                FieldCondition(key="skill_status", match=MatchValue(value="active")),
            ]),
            limit=1000,
            with_payload=True,
            with_vectors=False,
        )
        for p in points:
            payload = p.payload or {}
            # Freshness = the most recent of: last recall, a human "still valid"
            # review, or creation time. Including stale_reviewed_at is what makes
            # the dashboard "Still valid" acknowledgment DURABLE — without it the
            # sweep re-derives stale purely from recall and re-flags a reviewed
            # skill on the next cycle (adversarial-review finding). A review buys
            # exactly one more SKILL_STALE_AFTER_DAYS window; it does NOT touch
            # last_recalled_at (which would lie about recall activity).
            candidates = [
                _parse_iso(payload.get("last_recalled_at")),
                _parse_iso(payload.get("stale_reviewed_at")),
                _parse_iso(payload.get("timestamp")),
            ]
            dated = [c for c in candidates if c is not None]
            freshness = max(dated) if dated else None
            if freshness is None:
                skipped += 1  # undated — can't judge; leave untouched
                continue
            is_stale = bool(payload.get("stale", False))
            should_be_stale = freshness < cutoff
            if should_be_stale and not is_stale:
                client.set_payload(
                    collection_name=settings.QDRANT_COLLECTION,
                    payload={"stale": True, "stale_detected_at": now.isoformat()},
                    points=[p.id],
                )
                flagged += 1
            elif not should_be_stale and is_stale:
                client.set_payload(
                    collection_name=settings.QDRANT_COLLECTION,
                    payload={"stale": False},
                    points=[p.id],
                )
                unstaled += 1
        return {"status": "ok", "flagged": flagged, "unstaled": unstaled, "skipped": skipped}
    except Exception:
        logger.exception("Error in skill_staleness_pass")
        return {"status": "error", "flagged": flagged, "unstaled": unstaled, "skipped": skipped}
    finally:
        if close_after:
            try:
                client.close()
            except Exception:
                pass
