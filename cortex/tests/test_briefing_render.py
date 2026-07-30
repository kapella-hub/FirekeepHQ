"""SP1b-server: rendered pre-flight text + instruction priority + injection safety."""
from __future__ import annotations

import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock
import fakeredis.aioredis

from app.briefing.render import build_instructions, render_briefing
from app.briefing.api import create_briefing_router


def _sec(status="empty", data=None, error=None):
    return {"status": status, "error": error, "data": data or {}}


def _base_sections(**overrides):
    names = ["environment", "tasks", "bulletins", "quality", "strategy_tips",
             "cross_agent", "skills", "vault", "discipline", "dlq", "resumable_sessions"]
    secs = {n: _sec() for n in names}
    secs.update(overrides)
    return secs


# --- instruction priority --------------------------------------------------

def test_instructions_resume_nudge_wins():
    secs = _base_sections(resumable_sessions=_sec(
        "ok", {"recommended": {"session_id": "s1", "goal": "finish X", "age_hours": 3.0,
                               "strong_nudge": True}, "sessions": [], "crash_check": {}}))
    instr = build_instructions(secs, agent_id="moganes", briefing_id="bf_abc123")
    assert "ctx_resume_session" in instr
    assert "s1" in instr


def test_instructions_agent_aware_ctx_start():
    instr = build_instructions(_base_sections(), agent_id="moganes", briefing_id="bf_abc123")
    assert "ctx_start_session" in instr
    assert "moganes" in instr


def test_instructions_plain_for_default_agent():
    instr = build_instructions(_base_sections(), agent_id="default", briefing_id="bf_abc123")
    assert "ctx_start_session" in instr
    assert "default" not in instr


# --- T35/final-review: briefing_id must be rendered into EVERY branch so the
# A/B reconciliation map (compute_tip_effectiveness / _build_briefing_map) has
# something to join against once the agent follows the printed instruction. ---

def test_instructions_resume_nudge_includes_briefing_id():
    secs = _base_sections(resumable_sessions=_sec(
        "ok", {"recommended": {"session_id": "s1", "goal": "finish X", "age_hours": 3.0,
                               "strong_nudge": True}, "sessions": [], "crash_check": {}}))
    instr = build_instructions(secs, agent_id="moganes", briefing_id="bf_abc123")
    assert "bf_abc123" in instr
    assert "briefing_id='bf_abc123'" in instr


def test_instructions_agent_aware_includes_briefing_id():
    instr = build_instructions(_base_sections(), agent_id="moganes", briefing_id="bf_abc123")
    assert "briefing_id='bf_abc123'" in instr


def test_instructions_plain_includes_briefing_id():
    instr = build_instructions(_base_sections(), agent_id="default", briefing_id="bf_abc123")
    assert "briefing_id='bf_abc123'" in instr


# --- rendered text ---------------------------------------------------------

def test_rendered_shows_inline_unavailable_marker():
    secs = _base_sections(environment=_sec("unavailable", None, "environment: timeout after 2s"))
    text = render_briefing(agent_id="moganes", goal="g", sections=secs,
                           instructions="do the thing")
    assert "[ENVIRONMENT unavailable: environment: timeout after 2s]" in text


def test_rendered_truncates_long_goal():
    long_goal = "x" * 200
    text = render_briefing(agent_id="a", goal=long_goal, sections=_base_sections(),
                           instructions="i")
    # goal truncated to 80 chars in the header line
    header = [ln for ln in text.splitlines() if ln.startswith("You are")][0]
    assert "x" * 80 in header
    assert "x" * 81 not in header


def test_resumable_sessions_header_rendered_once():
    """The section label is a header, not a per-item prefix: N resumable
    sessions must produce ONE 'RESUMABLE SESSIONS:' line and N session lines.
    (Contrast the sibling bulletin label, which IS a per-item prefix.)"""
    secs = _base_sections(resumable_sessions=_sec("ok", {"sessions": [
        {"session_id": "s1", "goal": "finish X", "age_hours": 3.0, "reason": "paused"},
        {"session_id": "s2", "goal": "finish Y", "age_hours": 9.0, "reason": "crashed"},
    ]}))
    text = render_briefing(agent_id="a", goal="g", sections=secs, instructions="i")
    assert text.count("RESUMABLE SESSIONS:") == 1
    # No session is lost by hoisting the header.
    assert "s1" in text and "s2" in text
    assert "finish X" in text and "finish Y" in text


def test_rendered_error_summary_truncated_to_60():
    secs = _base_sections(environment=_sec("ok", {
        "summary": "S", "collectors": {}, "event_count": 1,
        "recent_errors": [{"summary": "E" * 120, "source": "docker",
                           "severity": "error", "timestamp": "t"}]}))
    text = render_briefing(agent_id="a", goal="g", sections=secs, instructions="i")
    assert "E" * 60 in text
    assert "E" * 61 not in text


# --- injection safety (S1/S2/S3 cannot recur) ------------------------------

def test_injection_goal_produces_valid_json_no_execution():
    """A goal packed with the exact bytes that broke briefing.sh (double quote,
    triple-quote, unbound-style refs) must yield valid JSON and execute nothing."""
    evil = 'fix "the" collector \'\'\' and $SESSION_ID `rm -rf /`'
    app = FastAPI()
    app.include_router(create_briefing_router(section_timeout=2.0))
    app.state.replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.vector_client = MagicMock()
    app.state.vector_client._client = MagicMock()

    async def _scroll(**_k):
        return ([], None)
    app.state.vector_client._client.scroll = _scroll
    app.state.redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)

    class _C:
        async def get(self, url, headers=None, params=None):
            raise RuntimeError("offline")  # all outbound sections degrade
    app.state.http_client = _C()

    resp = TestClient(app).get("/briefing", params={"agent_id": "moganes", "goal": evil})
    assert resp.status_code == 200
    # The whole body round-trips through JSON with the evil goal intact.
    body = resp.json()
    assert body["goal"] == evil
    assert evil in body["rendered"]
    # Re-serialising proves it is valid JSON end to end (S2 class killed).
    json.loads(json.dumps(body))
    # Outbound sections degraded but the briefing still rendered (S1 class killed).
    assert body["degraded"] is True
    assert body["sections"]["environment"]["status"] == "unavailable"
