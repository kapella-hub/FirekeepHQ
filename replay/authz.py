"""Who may read a replay event — THE one visibility predicate.

Replay events carry recall query text, memory content snippets, file paths and
(through ``context_ref``) whole context snapshots, so a read is scoped to the
verified caller, never to whoever holds ``replay:read`` (every member key does).

Ownership is PER EVENT, from the provenance the emitter stamped:
``workspace_id`` / ``member_id`` are the verified principal of the request that
caused the event (``replay.emitter.emit``'s keyword arguments; never a header).
Per-event rather than per-session because the session id is client-generated
telemetry (``X-Session-Id``): a first-writer session-owner index could be
pre-claimed by whoever writes a session id first, and resolving a session's
owner from Bridge would make every replay read a cross-service lookup into
Bridge's database. A stamp is written by the auth layer at the moment the event
happens and cannot be claimed by anyone else. It also leaves the Redis key
layout (``rp:session_idx:{sid}``, ``rp:eid:{id}``) exactly as it was.

The rules (docs/THREAT-MODEL.md, replay section):

* auth disabled -> no scope: the single anonymous principal is the deployment
  owner and sees everything, exactly as before;
* ``admin`` / ``*`` (the owner's key, the dashboard) -> every event in the
  caller's workspace;
* any other key -> only events stamped with the caller's own member, in the
  caller's workspace;
* an event with no ``workspace_id`` stamp belongs to the deployment workspace;
* an event with no ``member_id`` stamp — emitted before attribution, or by a
  background emitter that has no principal (collectors, sentinel)
  — belongs to the deployment OWNER member, and to no other member. The same
  legacy rule Bridge's ``session_owned_by`` applies to unowned sessions.
* a caller whose workspace or member cannot be determined sees nothing.

Internal Python callers (evals, OWM, the pattern engine) pass no scope and read
the stream unfiltered, as before; they never reach replay over HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from auth import keys as _keys
from auth.principal import deployment_owner_member_id, deployment_workspace_id

# The two Bridge lifecycle events that open a session. Only Bridge emits them
# (every replay emit site passes a literal event_type; no route lets a caller
# choose one), and Bridge stamps them with the session's owner_member /
# owner_workspace — the verified principal recorded when the session started.
SESSION_START_EVENT_TYPES = ("session_start", "session.started")


@dataclass(frozen=True)
class ReplayScope:
    """The caller's read boundary. ``member_id is None`` means the whole
    workspace (an admin); an empty string means "undeterminable" and matches
    nothing."""

    workspace_id: str
    member_id: str | None


def scope_for(identity: Mapping[str, Any] | None) -> ReplayScope | None:
    """The read scope for a verified identity; None means unrestricted.

    Reads the enable flag through the module attribute (as require_scope
    does) so init_auth() is observed at request time.
    """
    if not _keys._AUTH_ENABLED:
        return None
    identity = identity or {}
    workspace_id = str(identity.get("workspace_id") or "")
    if _keys.scopes_allow(identity.get("scopes") or [], "admin"):
        return ReplayScope(workspace_id, None)
    return ReplayScope(workspace_id, str(identity.get("member_id") or ""))


def event_visible(fields: Mapping[str, Any], scope: ReplayScope | None) -> bool:
    """May ``scope`` read a record carrying these provenance fields?

    ``fields`` is a raw stream entry, a parsed event, or any mapping with
    ``workspace_id`` / ``member_id`` (an eval record uses the same predicate).
    """
    if scope is None:
        return True
    if not scope.workspace_id:
        return False
    event_workspace = fields.get("workspace_id") or deployment_workspace_id()
    if event_workspace != scope.workspace_id:
        return False
    if scope.member_id is None:
        return True
    if not scope.member_id:
        return False
    event_member = fields.get("member_id") or ""
    if event_member:
        return event_member == scope.member_id
    return (
        scope.member_id == deployment_owner_member_id()
        and scope.workspace_id == deployment_workspace_id()
    )


def session_owner(events: Iterable[Mapping[str, Any]]) -> dict[str, str | None]:
    """``{workspace_id, member_id}`` of the session these events belong to.

    Read from the FIRST session-start event (see SESSION_START_EVENT_TYPES).
    Deliberately not "whoever stamped the most events": any key can emit a
    memory event under any client-chosen ``X-Session-Id``, so a vote could be
    stuffed. No start event, or an unstamped one (a session started before
    attribution) -> unattributed, i.e. the deployment owner's.
    """
    for event in events:
        if event.get("event_type") in SESSION_START_EVENT_TYPES:
            member = event.get("member_id") or None
            if not member:
                break
            return {
                "workspace_id": event.get("workspace_id") or None,
                "member_id": member,
            }
    return {"workspace_id": None, "member_id": None}
