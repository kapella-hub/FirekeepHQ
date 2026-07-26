"""SP1b-server D1: crash-detection direction rule (review-corrected)."""
from __future__ import annotations

from datetime import datetime
import pytest
from unittest.mock import MagicMock

from app.briefing import sections as S


class _Resp:
    def __init__(self, json_data):
        self._json = json_data
    def raise_for_status(self):
        pass
    def json(self):
        return self._json


class _Client:
    def __init__(self, routes):
        self.routes = routes
    async def get(self, url, headers=None, params=None):
        # /sessions is disambiguated by the status query param
        if "/sessions" in url and params:
            return self.routes[f"status={params['status']}"]
        for frag, resp in self.routes.items():
            if frag in url:
                return resp
        return _Resp({})


_SETTINGS = MagicMock(RELAY_URL="http://relay:8050", BRIDGE_URL="http://bridge:8070",
                      SENTINEL_URL="http://sentinel:8060", FIREKEEP_INTERNAL_KEY="nxs")

# Orphaned active session. Derive the epoch from the ISO string itself so the
# presence started_at offsets stay exactly relative to what _to_epoch() parses.
_SESS_UPDATED = "2026-07-09T12:00:00+00:00"
_SESS_UPDATED_EPOCH = datetime.fromisoformat(_SESS_UPDATED).timestamp()


def _routes(presence):
    return {
        "status=paused": _Resp({"sessions": []}),
        "status=active": _Resp({"sessions": [
            {"session_id": "act1", "goal": "half-done refactor",
             "status": "active", "updated_at": _SESS_UPDATED, "agent_id": "moganes"}]}),
        "/presence/": presence,
    }


@pytest.mark.asyncio
async def test_orphaned_active_no_presence_is_crashed():
    # 404 -> our client returns {} -> treated as no presence
    client = _Client(_routes(_Resp({})))
    sec = await S.resumable_sessions_section(client, _SETTINGS, agent_id="moganes")
    reasons = {s["reason"] for s in sec["data"]["sessions"]}
    assert "crashed" in reasons
    assert sec["data"]["crash_check"]["presence_live"] is False


@pytest.mark.asyncio
async def test_matching_session_id_presence_not_crashed():
    presence = _Resp({"agent_id": "moganes", "session_id": "act1",
                      "started_at": str(_SESS_UPDATED_EPOCH + 999), "status": "active"})
    client = _Client(_routes(presence))
    sec = await S.resumable_sessions_section(client, _SETTINGS, agent_id="moganes")
    # Same session id -> alive -> NOT flagged crashed.
    assert all(s["reason"] != "crashed" for s in sec["data"]["sessions"])
    assert sec["data"]["crash_check"]["presence_live"] is True


@pytest.mark.asyncio
async def test_different_session_newer_presence_still_crashed():
    # A fresh sidecar for a DIFFERENT process registered AFTER the orphaned
    # session's last update. "newer-than" would wrongly suppress; the predate
    # rule keeps it crashed (audit defect #20 direction).
    presence = _Resp({"agent_id": "moganes", "session_id": "other-session",
                      "started_at": str(_SESS_UPDATED_EPOCH + 3600), "status": "active"})
    client = _Client(_routes(presence))
    sec = await S.resumable_sessions_section(client, _SETTINGS, agent_id="moganes")
    reasons = {s["reason"] for s in sec["data"]["sessions"]}
    assert "crashed" in reasons
    assert sec["data"]["crash_check"]["presence_live"] is False


@pytest.mark.asyncio
async def test_different_session_predating_presence_is_alive():
    # Presence registered BEFORE the session's last update -> evidence alive.
    presence = _Resp({"agent_id": "moganes", "session_id": "other-session",
                      "started_at": str(_SESS_UPDATED_EPOCH - 3600), "status": "active"})
    client = _Client(_routes(presence))
    sec = await S.resumable_sessions_section(client, _SETTINGS, agent_id="moganes")
    assert all(s["reason"] != "crashed" for s in sec["data"]["sessions"])
    assert sec["data"]["crash_check"]["presence_live"] is True
