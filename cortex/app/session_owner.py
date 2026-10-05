"""Who owns Bridge session S — for Cortex code that acts on a caller-NAMED
session id (docs/THREAT-MODEL.md §5.16).

``X-Session-Id`` is client telemetry: any key can send any value. #56 stamps
every replay event with its verified writer and hides foreign events from
readers, but the in-process consumers (eval compute, OWM, the pattern engine,
the autopilot) compute over EVERY event filed under a session. So an event a
member writes under a session it does not own is not filed under that session:
:func:`attributable_session_id` re-files it under ``"unknown"`` (the id Cortex
already uses when no header is sent), keeping the writer's stamp so the event
stays in the writer's own timeline.

Owner resolution, cheapest first:

1. **In-process cache.** Owners never change once recorded, so a resolved
   owner is kept for hours (bounded LRU). The steady state is a dict lookup.
2. **The session's stamped start event in replay** (``replay.reader.
   get_session_owner``): Bridge stamps ``session_start`` / ``session.started``
   with the session's recorded owner, and no other emitter can write those
   event types. Three Redis round trips on the replay store this process
   already writes to — no cross-service call.
3. **Bridge** (``GET /sessions/{id}`` with ``FIREKEEP_INTERNAL_KEY``, which
   holds ``session:read:workspace``), only when the start event carries no
   stamp: sessions started before Bridge stamped its events (2026-10-04, so
   every session in flight at that deploy), or whose start emit was dropped.
   Without this step those sessions would fall to the legacy rule and their
   own members' events would be re-filed — changing what eval compute grades.

   **Never on the request path.** Every emit is awaited inside the recall /
   learn / gateway request, so the Bridge read is SCHEDULED as a background
   task and the emit that triggered it gets ``None`` (unresolved: the claimed
   id is kept, counted). The task fills the cache for the session's later
   emits. Bounded three ways so Bridge can neither slow Cortex nor be fanned
   out into: a hard ``RESOLVER_TIMEOUT_SECONDS`` per read, at most
   ``MAX_BRIDGE_LOOKUPS`` in flight (more are dropped and counted), and an
   unresolvable answer is remembered for ``_UNRESOLVED_TTL_SECONDS`` so an
   outage costs one read per session per window, not one per request.

A session neither source can name (Bridge 404, or a legacy session with no
recorded owner) belongs to the deployment owner — ``auth.principal.
owns_session``'s legacy rule, the one Bridge applies. A session not resolved
YET, or not resolvable (Bridge unreachable, misconfigured key, an older
Bridge), is ``None``: replay attribution fails OPEN on it. ``POST
/skill/evaluate`` does not use this resolver (it asks Bridge synchronously with
the caller's key and fails closed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from auth import keys as _auth_keys
from auth.principal import owns_session

logger = logging.getLogger(__name__)

# What Cortex files an event under when it has no attributable session: the
# value the memory routes already use for a request with no X-Session-Id.
UNATTRIBUTED_SESSION_ID = "unknown"
_SENTINEL_SESSION_IDS = frozenset({"", UNATTRIBUTED_SESSION_ID})

# Bridge's own lifecycle events that OPEN a session. They are what records the
# owner, so they are never re-filed (replay/authz.py SESSION_START_EVENT_TYPES).
_SESSION_START_EVENT_TYPES = ("session_start", "session.started")

_OWNER_TTL_SECONDS = 6 * 3600
_UNKNOWN_TTL_SECONDS = 60
_UNRESOLVED_TTL_SECONDS = 30
_CACHE_MAX = 4096
# Bridge's ctx_complete_session waits 5s for POST /skill/evaluate, and the
# skill route's own (synchronous, fail-closed) Bridge read must fit inside it.
BRIDGE_TIMEOUT_SECONDS = 2.0
# The background owner read: Bridge is on the same host/network.
RESOLVER_TIMEOUT_SECONDS = 0.5
MAX_BRIDGE_LOOKUPS = 4

_cache: OrderedDict[str, tuple["SessionOwner", float]] = OrderedDict()
_unresolved: OrderedDict[str, float] = OrderedDict()
_pending: dict[str, asyncio.Task] = {}
_stats: dict[str, int] = {
    "reattributed": 0,
    "unresolved": 0,
    "bridge_lookups": 0,
    "bridge_saturated": 0,
}


@dataclass(frozen=True)
class SessionOwner:
    """A session's recorded owner. ``member_id == ""`` is "no recorded owner"
    (legacy, or a session Bridge does not know): the deployment owner's."""

    member_id: str
    workspace_id: str

    def owned_by(self, *, member_id: str | None, workspace_id: str | None) -> bool:
        return owns_session(
            self.member_id, self.workspace_id,
            member_id=member_id, workspace_id=workspace_id,
        )


def get_stats() -> dict[str, int]:
    return dict(_stats)


def reset_cache() -> None:
    _cache.clear()
    _unresolved.clear()
    _pending.clear()


async def drain_bridge_lookups() -> None:
    """Wait for every scheduled Bridge owner read (tests; orderly shutdown)."""
    while _pending:
        await asyncio.gather(*list(_pending.values()), return_exceptions=True)


def bridge_client(timeout: float = BRIDGE_TIMEOUT_SECONDS) -> httpx.AsyncClient:
    """The HTTP client for Cortex -> Bridge session reads (a test seam)."""
    return httpx.AsyncClient(timeout=timeout)


def bridge_session_url(bridge_url: str, session_id: str) -> str:
    return f"{bridge_url.rstrip('/')}/sessions/{quote(session_id, safe='')}"


def _cached(session_id: str) -> SessionOwner | None:
    hit = _cache.get(session_id)
    if hit is None:
        return None
    owner, expires_at = hit
    if expires_at < time.monotonic():
        _cache.pop(session_id, None)
        return None
    _cache.move_to_end(session_id)
    return owner


def _remember(session_id: str, owner: SessionOwner, ttl: float) -> SessionOwner:
    _cache[session_id] = (owner, time.monotonic() + ttl)
    _cache.move_to_end(session_id)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)
    return owner


async def _owner_from_replay(session_id: str) -> SessionOwner | None:
    from replay.emitter import get_redis
    from replay.reader import get_session_owner

    replay_redis = get_redis()
    if replay_redis is None:
        return None
    try:
        stamp = await get_session_owner(replay_redis, session_id)
    except Exception as exc:  # noqa: BLE001 — fall through to Bridge
        logger.debug("replay owner lookup failed for %s: %s", session_id, exc)
        return None
    if not stamp.get("member_id"):
        return None
    return SessionOwner(str(stamp["member_id"]), str(stamp.get("workspace_id") or ""))


async def _owner_from_bridge(
    session_id: str, settings: Any,
) -> tuple[SessionOwner | None, float]:
    """(owner, cache ttl). ``(None, 0)`` = unresolvable."""
    from app.skills import internal_key_headers

    _stats["bridge_lookups"] += 1
    try:
        async with bridge_client(RESOLVER_TIMEOUT_SECONDS) as client:
            # wait_for, not just the client timeout: the bound must hold
            # whatever the transport does.
            response = await asyncio.wait_for(
                client.get(
                    bridge_session_url(settings.BRIDGE_URL, session_id),
                    headers=internal_key_headers(settings.FIREKEEP_INTERNAL_KEY),
                ),
                RESOLVER_TIMEOUT_SECONDS,
            )
    except Exception as exc:  # noqa: BLE001 — unreachable/slow is "unresolved"
        logger.warning("Session owner lookup: Bridge unreachable for %s: %s", session_id, exc)
        return None, 0
    if response.status_code == 404:
        return SessionOwner("", ""), _UNKNOWN_TTL_SECONDS
    if response.status_code != 200:
        logger.warning(
            "Session owner lookup: Bridge answered HTTP %d for %s (is "
            "FIREKEEP_INTERNAL_KEY carrying session:read:workspace?)",
            response.status_code, session_id,
        )
        return None, 0
    try:
        body = response.json()
    except ValueError:
        return None, 0
    if not isinstance(body, dict) or "owner_member" not in body:
        # An older Bridge that does not name owners: unknown, NOT legacy.
        return None, 0
    return (
        SessionOwner(str(body.get("owner_member") or ""), str(body.get("owner_workspace") or "")),
        _OWNER_TTL_SECONDS,
    )


def _recently_unresolved(session_id: str) -> bool:
    until = _unresolved.get(session_id)
    if until is None:
        return False
    if until < time.monotonic():
        _unresolved.pop(session_id, None)
        return False
    return True


def _mark_unresolved(session_id: str) -> None:
    _unresolved[session_id] = time.monotonic() + _UNRESOLVED_TTL_SECONDS
    _unresolved.move_to_end(session_id)
    while len(_unresolved) > _CACHE_MAX:
        _unresolved.popitem(last=False)


async def _resolve_through_bridge(session_id: str, settings: Any) -> None:
    try:
        owner, ttl = await _owner_from_bridge(session_id, settings)
        if owner is None:
            _mark_unresolved(session_id)
        else:
            _remember(session_id, owner, ttl)
    except Exception as exc:  # noqa: BLE001 — a background read never raises
        logger.warning("Session owner lookup failed for %s: %s", session_id, exc)
        _mark_unresolved(session_id)
    finally:
        _pending.pop(session_id, None)


def _schedule_bridge_lookup(session_id: str, settings: Any) -> None:
    """Start a background Bridge read for ``session_id`` unless one is running,
    one failed within ``_UNRESOLVED_TTL_SECONDS``, or ``MAX_BRIDGE_LOOKUPS``
    are already in flight (dropped and counted: a flood of fresh session ids
    must not fan out into Bridge)."""
    if session_id in _pending or _recently_unresolved(session_id):
        return
    if len(_pending) >= MAX_BRIDGE_LOOKUPS:
        _stats["bridge_saturated"] += 1
        return
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    _pending[session_id] = asyncio.get_running_loop().create_task(
        _resolve_through_bridge(session_id, settings))


async def resolve_session_owner(session_id: str, *, settings: Any = None) -> SessionOwner | None:
    """The recorded owner of ``session_id``, or None when it is not known yet.

    Request-path safe: at most the replay-store lookup is awaited. When that
    does not name an owner, a background Bridge read is scheduled and this call
    returns None; the read fills the cache for later calls.
    """
    owner = _cached(session_id)
    if owner is not None:
        return owner
    owner = await _owner_from_replay(session_id)
    if owner is not None:
        return _remember(session_id, owner, _OWNER_TTL_SECONDS)
    _schedule_bridge_lookup(session_id, settings)
    return None


async def attributable_session_id(
    session_id: str,
    *,
    workspace_id: str | None,
    member_id: str | None,
    event_type: str | None = None,
) -> str:
    """The session id a write by this verified writer may be filed under.

    ``session_id`` when the writer owns it (or there is nothing to check);
    :data:`UNATTRIBUTED_SESSION_ID` when the session belongs to someone else.
    Never raises.

    Nothing is checked — the id passes through unchanged — when auth is
    disabled (every caller is the deployment owner, rule 4), when there is no
    verified writer, for the sentinel ids, for the session-opening events, and
    when replay is off (the event would be dropped anyway). An owner not
    resolved yet (its Bridge read runs in the background) or unresolvable fails
    open: the claimed id is kept and counted in ``unresolved``.
    """
    try:
        if not _auth_keys._AUTH_ENABLED or not member_id:
            return session_id
        if session_id in _SENTINEL_SESSION_IDS or event_type in _SESSION_START_EVENT_TYPES:
            return session_id
        from replay.emitter import is_enabled
        if not is_enabled():
            return session_id
        owner = await resolve_session_owner(session_id)
        if owner is None:
            _stats["unresolved"] += 1
            return session_id
        if owner.owned_by(member_id=member_id, workspace_id=workspace_id or ""):
            return session_id
        _stats["reattributed"] += 1
        logger.warning(
            "Replay %s from member %s named session %s, which it does not own; "
            "filed under %r instead",
            event_type or "write", member_id, session_id, UNATTRIBUTED_SESSION_ID,
        )
        return UNATTRIBUTED_SESSION_ID
    except Exception as exc:  # noqa: BLE001 — attribution never costs the request
        logger.warning("Session attribution check failed for %s: %s", session_id, exc)
        return session_id
