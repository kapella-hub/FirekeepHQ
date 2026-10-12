"""replay_timeline's machine-readable format (Studio 0.5 receipts, spec §7.3).

The Markdown view drops each event's id, agent_id and memory_ids — exactly
what a per-turn receipt needs — so format="json" returns the REST route's
TimelineResponse verbatim. The default must stay byte-identical."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

_EVENTS = {
    "session_id": "sess-1",
    "events": [
        {
            "id": "e1",
            "session_id": "sess-1",
            "agent_id": "agent-a",
            "timestamp": "2026-10-11T10:00:00.000000+00:00",
            "event_type": "memory_read",
            "payload": {"query": "deploy vps", "trigger": "prompt-hook",
                        "result_count": 2, "memory_ids": ["m1", "m2"]},
            "outcome": None,
        },
        {
            "id": "e2",
            "session_id": "sess-1",
            "agent_id": "agent-a-studio",
            "timestamp": "2026-10-11T10:00:01.000000+00:00",
            "event_type": "memory_write",
            "payload": {"action_summary": "fixed the tunnel", "memory_id": "m3"},
            "outcome": "success",
        },
    ],
    "total": 2,
    "has_more": False,
}


def _client_returning(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    return client


async def _call(client, *args, **kwargs):
    from app import mcp_server

    with patch.object(mcp_server, "_get_client", new=AsyncMock(return_value=client)):
        return await mcp_server.replay_timeline(*args, **kwargs)


@pytest.mark.asyncio
async def test_default_markdown_is_unchanged():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1")
    client.get.assert_awaited_once_with(
        "/replay/sessions/sess-1/events", params={"limit": 20}
    )
    assert out == (
        "## Trace Timeline — sess-1 (2 events)\n"
        "- `2026-10-11T10:00:00` **memory_read**\n"
        "  query: deploy vps\n"
        "- `2026-10-11T10:00:01` **memory_write** [success]\n"
        "  action_summary: fixed the tunnel"
    )


@pytest.mark.asyncio
async def test_json_returns_the_route_payload_verbatim():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1", format="json", limit=200, offset=5,
                      event_type="memory_read")
    client.get.assert_awaited_once_with(
        "/replay/sessions/sess-1/events",
        params={"limit": 200, "offset": 5, "event_type": "memory_read"},
    )
    data = json.loads(out)
    assert data == _EVENTS
    assert data["events"][0]["payload"]["memory_ids"] == ["m1", "m2"]
    assert data["events"][1]["agent_id"] == "agent-a-studio"


@pytest.mark.asyncio
async def test_json_empty_session_is_json():
    empty = {"session_id": "s", "events": [], "total": 0, "has_more": False}
    out = await _call(_client_returning(empty), "s", format="json")
    assert json.loads(out) == empty


@pytest.mark.asyncio
async def test_unknown_format_is_rejected_without_a_call():
    client = _client_returning(_EVENTS)
    out = await _call(client, "sess-1", format="JSON")
    assert out.startswith("Error: format must be 'markdown' or 'json'")
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_json_mode_http_error_is_json():
    request = httpx.Request("GET", "http://cortex/replay/sessions/s/events")
    response = httpx.Response(403, request=request, json={"detail": "nope"})
    resp = MagicMock()
    resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("403", request=request, response=response)
    )
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    data = json.loads(await _call(client, "s", format="json"))
    assert set(data) == {"error"}
    assert data["error"]


@pytest.mark.asyncio
async def test_json_mode_connection_error_is_json():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    data = json.loads(await _call(client, "s", format="json"))
    assert set(data) == {"error"}
