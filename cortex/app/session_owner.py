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

A session neither source can name (Bridge 404, or a legacy session with no
recorded owner) belongs to the deployment owner — ``auth.principal.
owns_session``'s legacy rule, the one Bridge applies. A session that cannot be
resolved at all (Bridge unreachable, misconfigured key, an older Bridge) is
``None``: callers decide. Replay attribution fails OPEN on it (the claimed id
is kept and counted), ``POST /skill/evaluate`` does not use this resolver.
"""

from __future__ import annotations

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
_CACHE_MAX = 4096
# Bridge's ctx_complete_session waits 5s for POST /skill/evaluate, and the
# skill route's own Bridge read must fit inside that.
BRIDGE_TIMEOUT_SECONDS = 2.0

_cache: OrderedDict[str, tuple["SessionOwner", float]] = OrderedDict()
_stats: dict[str, int] = {
    "reattributed": 0,
    "unresolved": 0,
    "bridge_lookups": 0,
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
    """(owner, cache ttl). ``(None, 0)`` = unresolvable, never cached."""
    from app.skills import internal_key_headers

    _stats["bridge_lookups"] += 1
    try:
        async with bridge_client() as client:
            response = await client.get(
                bridge_session_url(settings.BRIDGE_URL, session_id),
                headers=internal_key_headers(settings.FIREKEEP_INTERNAL_KEY),
            )
    except Exception as exc:  # noqa: BLE001 — unreachable is "unresolved"
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


async def resolve_session_owner(session_id: str, *, settings: Any = None) -> SessionOwner | None:
    """The recorded owner of ``session_id``, or None when it cannot be resolved."""
    owner = _cached(session_id)
    if owner is not None:
        return owner
    owner = await _owner_from_replay(session_id)
    if owner is not None:
        return _remember(session_id, owner, _OWNER_TTL_SECONDS)
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    owner, ttl = await _owner_from_bridge(session_id, settings)
    if owner is None:
        return None
    return _remember(session_id, owner, ttl)


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
    when replay is off (the event would be dropped anyway). An UNRESOLVABLE
    owner fails open: the claimed id is kept and counted in ``unresolved``.
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
