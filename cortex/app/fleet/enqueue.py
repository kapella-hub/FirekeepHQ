"""Nightly fleet enqueue (spec 2026-09-02, decisions 3, 5, 6, 10).

Runs inside the sync Celery memory agent after the staleness sweep and turns
what the nightly passes already FOUND into relay tasks a client Night Shift can
drain: one `reauthor_stale_skill` per stale active skill, one
`propose_contested_verdict` per contested pair. Cortex reaches relay the way the
briefing does (RELAY_URL + FIREKEEP_INTERNAL_KEY), through the REST twin of
relay_task_post.

DEDUP IS STATE-BASED. Relay tasks have no idempotency and expire in 7 days, so
neither "post on transition" (loses the finding if the task expires undrained)
nor "post on a marker" (re-drafts work a human has not acted on) is right. The
store is asked what is TRUE: a stale skill with a re-author draft in any status
is done; a pair carrying a proposal is done; a rejection marker means a human
threw the last rewrite away. A short live marker only stops double-posting
while a task is in flight, and expires with the task.

MEMBER-PRIVATE NEVER LEAVES. Relay tasks are Keep-global and the worker needs
the text in `context`, so `visibility == "member"` points are excluded at the
query (both sides of a pair must be workspace-visible).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField

from app.fleet import ledger

logger = logging.getLogger(__name__)

ASSIGNER = "cortex-fleet"
SKILL_CONTENT_CAP = 6000
MEMORY_TEXT_CAP = 3000
POST_TIMEOUT = 10.0

_NOT_MEMBER_PRIVATE = FieldCondition(key="visibility", match=MatchValue(value="member"))


def post_relay_task(settings, task: dict) -> bool:
    """POST one task to relay. True only on 201; never raises."""
    from app.skills import internal_key_headers
    url = f"{str(settings.RELAY_URL).rstrip('/')}/tasks"
    try:
        resp = httpx.post(url, json=task, headers=internal_key_headers(settings.FIREKEEP_INTERNAL_KEY),
                          timeout=POST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — a relay outage means no fleet tasks tonight
        logger.warning("fleet enqueue: relay POST failed: %s", exc)
        return False
    if resp.status_code != 201:
        logger.warning("fleet enqueue: relay POST returned %s", resp.status_code)
        return False
    return True


def _scroll(client, settings, must: list, must_not: list | None = None) -> list:
    points, _ = client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=must, must_not=must_not or []),
        limit=1000, with_payload=True, with_vectors=False,
    )
    return list(points)


def _claim(redis_client, key: str) -> bool:
    """SET NX EX — the live marker. A Redis failure claims nothing (no post)."""
    try:
        return bool(redis_client.set(key, "1", nx=True, ex=ledger.LIVE_MARKER_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet enqueue: marker claim failed for %s: %s", key, exc)
        return False


def _release(redis_client, key: str) -> None:
    try:
        redis_client.delete(key)
    except Exception:  # noqa: BLE001
        pass


def _skill_context(pid: str, payload: dict) -> str:
    return json.dumps({
        "skill_id": pid,
        "trigger": str(payload.get("trigger") or "")[:1000],
        "symptoms": str(payload.get("symptoms") or "")[:2000],
        "content": str(payload.get("content") or "")[:SKILL_CONTENT_CAP],
        "domain": payload.get("domain") or "",
        "project": payload.get("project"),
        "timestamp": payload.get("timestamp"),
        "last_recalled_at": payload.get("last_recalled_at"),
        "stale_detected_at": payload.get("stale_detected_at"),
        "access_count": payload.get("access_count"),
        "skill_efficacy": payload.get("skill_efficacy"),
        "skill_efficacy_n": payload.get("skill_efficacy_n"),
    })


def _memory_side(pid: str, payload: dict) -> dict:
    return {
        "id": pid,
        "text": str(payload.get("text") or "")[:MEMORY_TEXT_CAP],
        "domain": payload.get("domain") or "",
        "timestamp": payload.get("timestamp"),
        "confirmed_count": payload.get("confirmed_count", 0),
        "contradicted_count": payload.get("contradicted_count", 0),
    }


def fleet_enqueue_pass(client=None, settings=None, redis_client=None,
                       post: Callable[[Any, dict], bool] | None = None, now=None) -> dict:
    """Sync; injectable client/settings/redis/post for tests. Never raises out
    of the per-pass try/except in run_memory_agent."""
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    if not getattr(settings, "FLEET_ENQUEUE_ENABLED", True):
        return {"status": "disabled"}

    out = {"status": "ok", "reauthor_enqueued": 0, "verdict_enqueued": 0,
           "skipped_private": 0, "skipped_pending": 0, "skipped_rejected": 0,
           "skipped_inflight": 0, "skipped_unpaired": 0, "capped": 0, "failed": 0}
    close_after = client is None
    if client is None:
        from app.workers.memory_agent import _get_qdrant_client
        client = _get_qdrant_client()
    if redis_client is None:
        from app.workers.sleep_cycle import _get_redis_client
        redis_client = _get_redis_client()
    post = post or post_relay_task
    budget = max(0, int(getattr(settings, "FLEET_ENQUEUE_MAX_PER_RUN", 20)))

    def _send(job: str, subject: str, task: dict) -> bool:
        nonlocal budget
        key = ledger.live_marker_key(job, subject)
        if budget <= 0:
            out["capped"] += 1
            return False
        if not _claim(redis_client, key):
            out["skipped_inflight"] += 1
            return False
        budget -= 1
        if post(settings, task):
            return True
        out["failed"] += 1
        _release(redis_client, key)
        return False

    try:
        # --- stale skills -> reauthor_stale_skill -----------------------------
        stale = _scroll(client, settings, [
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value="active")),
            FieldCondition(key="stale", match=MatchValue(value=True)),
        ], must_not=[_NOT_MEMBER_PRIVATE])
        # Private stale skills exist but were filtered — count them for honesty.
        out["skipped_private"] += sum(
            1 for p in _scroll(client, settings, [
                FieldCondition(key="memory_type", match=MatchValue(value="skill")),
                FieldCondition(key="skill_status", match=MatchValue(value="active")),
                FieldCondition(key="stale", match=MatchValue(value=True)),
                _NOT_MEMBER_PRIVATE,
            ])
        )
        already = {
            str((p.payload or {}).get("reauthor_of"))
            for p in _scroll(client, settings,
                             [FieldCondition(key="memory_type", match=MatchValue(value="skill"))],
                             must_not=[IsEmptyCondition(is_empty=PayloadField(key="reauthor_of"))])
        }
        for p in stale:
            pid, payload = str(p.id), (p.payload or {})
            if pid in already:
                out["skipped_pending"] += 1
                continue
            try:
                rejected = bool(redis_client.exists(ledger.rejected_reauthor_key(pid)))
            except Exception:  # noqa: BLE001
                rejected = False
            if rejected:
                out["skipped_rejected"] += 1
                continue
            task = {
                "title": ledger.JOB_REAUTHOR, "assigner": ASSIGNER, "priority": "normal",
                "description": f"skill_id={pid} workspace_id={payload.get('workspace_id') or '-'}",
                "context": _skill_context(pid, payload),
            }
            if _send(ledger.JOB_REAUTHOR, pid, task):
                out["reauthor_enqueued"] += 1

        # --- contested pairs -> propose_contested_verdict ---------------------
        contested = _scroll(client, settings, [
            FieldCondition(key="status", match=MatchValue(value="active")),
            FieldCondition(key="contested", match=MatchValue(value=True)),
        ], must_not=[_NOT_MEMBER_PRIVATE])
        by_id = {str(p.id): (p.payload or {}) for p in contested}
        for pid, payload in by_id.items():
            other = str(payload.get("contested_with") or "")
            if not other:
                continue
            other_payload = by_id.get(other)
            if other_payload is None:
                # The partner never made it into `by_id` — it's inactive, or
                # member-private and filtered at the query. Check this BEFORE
                # the lexical-ordering skip below: when the partner is the
                # lexically-smaller side, ordering alone would silently drop
                # this pair without ever counting it as unpaired.
                out["skipped_unpaired"] += 1
                continue
            if pid > other:
                continue  # each pair once, from its lexically-smaller side
            if payload.get("proposed_verdict") or other_payload.get("proposed_verdict"):
                out["skipped_pending"] += 1
                continue
            subject = f"{pid}:{other}"
            task = {
                "title": ledger.JOB_VERDICT, "assigner": ASSIGNER, "priority": "normal",
                "description": f"pair={pid},{other} workspace_id={payload.get('workspace_id') or '-'}",
                "context": json.dumps({
                    "a": _memory_side(pid, payload), "b": _memory_side(other, other_payload),
                    "contested_at": payload.get("contested_at") or other_payload.get("contested_at"),
                }),
            }
            if _send(ledger.JOB_VERDICT, subject, task):
                out["verdict_enqueued"] += 1
        return out
    except Exception:
        logger.exception("Error in fleet_enqueue_pass")
        out["status"] = "error"
        return out
    finally:
        if close_after:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
