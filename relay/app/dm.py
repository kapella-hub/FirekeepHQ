"""Direct message system for agent-to-agent and dashboard-to-agent communication.

Messages are stored per-recipient in Redis lists with key pattern nr:dm:{agent_id}.
Each message is a JSON object with sender, content, timestamp, and read status.
Messages expire after a configurable TTL (default 24h).

Who may read an inbox (THREAT-MODEL §5.14, 2026-10-04). The inbox key is the
recipient LABEL, and labels are self-asserted, so the label is not the gate.
Each message records, at send time:

    by           — the verified sender {workspace_id, member_id, credential_id,
                   authenticated}; ``from`` stays the sender's display label
    to_workspace / to_member
                 — the member the recipient label was bound to (its presence
                   row's owner, app.presence) when the message was sent

A message is visible to its ``to_member``; to an admin key in its workspace
(the dashboard's DM drawer); and — when it carries no ``to_member`` (a label
nobody had bound, such as ``dashboard``, or a message written before this
change) — to the deployment owner member alone. Rebinding a label later never
exposes messages addressed to its previous owner. With auth disabled every
caller is the deployment owner and sees everything, as before.
"""

import json
import logging
import time
import uuid

from app.principal import Caller, OWNER_MEMBER, OWNER_WORKSPACE, administers

logger = logging.getLogger(__name__)

DM_PREFIX = "nr:dm:"
DM_TTL_SECONDS = 86400  # 24 hours


async def send_dm(
    redis,
    to_agent_id: str,
    content: str,
    from_id: str,
    *,
    by: dict | None = None,
    to_owner: tuple[str, str] | None = None,
) -> dict:
    """Send a direct message to an agent. Stored in recipient's inbox.

    ``by`` is the verified sender stamp; ``to_owner`` the (workspace, member)
    the recipient label is bound to, or None when it is unbound."""
    msg_id = f"dm-{uuid.uuid4().hex[:8]}"
    now = time.time()

    message = {
        "id": msg_id,
        "from": from_id,
        "to": to_agent_id,
        "content": content,
        "timestamp": now,
        "read": False,
    }
    if by is not None:
        message["by"] = by
    if to_owner is not None:
        message["to_workspace"], message["to_member"] = to_owner

    key = f"{DM_PREFIX}{to_agent_id}"
    await redis.lpush(key, json.dumps(message))
    # Set/refresh TTL on the inbox
    await redis.expire(key, DM_TTL_SECONDS)

    return message


async def get_dms(
    redis,
    agent_id: str,
    unread_only: bool = False,
    limit: int = 50,
    *,
    caller: Caller | None = None,
) -> list[dict]:
    """Get direct messages for an agent, newest first.

    If unread_only is True, only returns messages where read is False.
    With ``caller``, only the messages that caller may read (``visible_to``).
    """
    key = f"{DM_PREFIX}{agent_id}"
    raw_messages = await redis.lrange(key, 0, -1)

    messages = []
    for raw in raw_messages:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if caller is not None and not visible_to(msg, caller):
            continue
        if unread_only and msg.get("read", False):
            continue
        messages.append(msg)
        if len(messages) >= limit:
            break

    return messages


def visible_to(msg: dict, caller: Caller) -> bool:
    """May ``caller`` read ``msg``? See the module docstring."""
    return administers(
        {OWNER_WORKSPACE: msg.get("to_workspace"), OWNER_MEMBER: msg.get("to_member")},
        caller,
    )


async def mark_read(
    redis,
    agent_id: str,
    *,
    caller: Caller | None = None,
) -> int:
    """Mark all messages in an agent's inbox as read. Returns count marked.

    With ``caller``, only the messages that caller may read are marked."""
    key = f"{DM_PREFIX}{agent_id}"
    raw_messages = await redis.lrange(key, 0, -1)

    if not raw_messages:
        return 0

    count = 0
    updated = []
    for raw in raw_messages:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            updated.append(raw)
            continue
        if caller is not None and not visible_to(msg, caller):
            updated.append(raw)
            continue
        if not msg.get("read", False):
            msg["read"] = True
            count += 1
        updated.append(json.dumps(msg))

    if count > 0:
        # Atomically replace the list
        pipe = redis.pipeline()
        pipe.delete(key)
        for item in reversed(updated):  # reversed because lpush prepends
            pipe.lpush(key, item)
        pipe.expire(key, DM_TTL_SECONDS)
        await pipe.execute()

    return count
