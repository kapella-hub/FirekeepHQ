"""PR5 D2/D3/D12: the treatment section. The text assertions are BYTE
comparisons against the pre-registered wording — a drifted character is a
new experiment, so the test refuses it."""
from __future__ import annotations

from types import SimpleNamespace

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.briefing.sections import (
    GRADING_NUDGE_TEXT, TREATMENT_ARM, grading_nudge_section,
)
from app.briefing import sections as S

EXPECTED_TEXT = (
    "## Grade this task when you finish\n"
    "When you call `ctx_complete_session`, pass `task_result` — `success`, "
    "`partial`, or `failure` — with `task_evidence` naming what you actually "
    "verified. An honest `failure` or `partial` is expected and safe to "
    "report; it is worth more to this team than an unexamined `success`. "
    "Ungraded sessions teach nothing."
)


@pytest_asyncio.fixture
async def replay_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
def settings_flag_on(monkeypatch):
    monkeypatch.setattr(S, "get_settings",
                        lambda: SimpleNamespace(GRADING_NUDGE_ENABLED=True))


@pytest.fixture
def settings_flag_off(monkeypatch):
    monkeypatch.setattr(S, "get_settings",
                        lambda: SimpleNamespace(GRADING_NUDGE_ENABLED=False))


def test_treatment_arm_is_the_coin_result():
    assert TREATMENT_ARM == "A"  # D9: commit 47d8e17, first hex digit even


def test_text_is_byte_exact():
    assert GRADING_NUDGE_TEXT == EXPECTED_TEXT


async def test_flag_off_renders_nothing_for_treatment(replay_redis, settings_flag_off):
    sec = await grading_nudge_section(replay_redis, "b-1", TREATMENT_ARM)
    assert sec["data"]["shown"] is False
    assert sec["data"]["text"] == ""
    assert await replay_redis.get("rp:nudge_shown:b-1") is None


async def test_flag_on_treatment_shows_and_records(replay_redis, settings_flag_on):
    sec = await grading_nudge_section(replay_redis, "b-2", TREATMENT_ARM)
    assert sec["data"]["shown"] is True
    assert sec["data"]["text"] == GRADING_NUDGE_TEXT
    assert await replay_redis.get("rp:nudge_shown:b-2") is not None
    assert await replay_redis.ttl("rp:nudge_shown:b-2") > 0


async def test_flag_on_control_renders_nothing_and_records_nothing(replay_redis, settings_flag_on):
    sec = await grading_nudge_section(replay_redis, "b-3", "B")
    assert sec["data"]["shown"] is False
    assert sec["data"]["text"] == ""
    assert await replay_redis.get("rp:nudge_shown:b-3") is None


async def test_none_arm_gets_control_behavior(replay_redis, settings_flag_on):
    sec = await grading_nudge_section(replay_redis, "b-4", None)
    assert sec["data"]["shown"] is False


async def test_record_failure_withholds(settings_flag_on):
    """D12: no receipt, no nudge — an unrecorded exposure corrupts the loop."""
    class Broken:
        async def set(self, *a, **k):
            raise RuntimeError("redis down")
    sec = await grading_nudge_section(Broken(), "b-5", TREATMENT_ARM)
    assert sec["data"]["shown"] is False
    assert sec["data"]["text"] == ""
    assert sec["error"]  # surfaced, not swallowed (strategy-tips precedent)


# --- render ------------------------------------------------------------

def test_render_emits_text_verbatim_when_shown():
    from app.briefing import render
    sections = {"grading_nudge": {"status": "ok", "error": None, "data": {
        "group": "A", "shown": True, "text": GRADING_NUDGE_TEXT}}}
    out = render.render_briefing(agent_id="a", goal="g", sections=sections,
                                 instructions="")
    assert GRADING_NUDGE_TEXT in out


def test_render_emits_nothing_when_withheld():
    from app.briefing import render
    sections = {"grading_nudge": {"status": "ok", "error": None, "data": {
        "group": "B", "shown": False, "text": ""}}}
    out = render.render_briefing(agent_id="a", goal="g", sections=sections,
                                 instructions="")
    assert "Grade this task" not in out
