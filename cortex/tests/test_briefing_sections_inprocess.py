"""SP1b-server: the 7 in-process briefing sections."""
from __future__ import annotations

import fakeredis.aioredis
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from app.briefing import sections as S
from app.evals.models import EvalResult
from app.evals.store import store_eval
from app.patterns.models import PatternCard
from app.patterns.store import store_patterns, _load_tip_groups


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# --- quality ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_quality_empty_when_no_evals(rr):
    sec = await S.quality_section(rr)
    assert sec["status"] == "empty"
    assert sec["data"]["total_sessions"] == 0


@pytest.mark.asyncio
async def test_quality_ok_with_insight(rr):
    await store_eval(rr, EvalResult(
        session_id="s1", trigger="session_complete",
        metrics={"tool_success_rate": 0.5, "failure_rate": 0.2}, event_count=5,
    ))
    sec = await S.quality_section(rr)
    assert sec["status"] == "ok"
    assert sec["data"]["total_sessions"] == 1
    # thresholds mirror briefing.sh: tool_success<0.9 and failure_rate>0.1 flag.
    assert any("tool success" in i.lower() for i in sec["data"]["insights"])
    assert any("failure" in i.lower() for i in sec["data"]["insights"])


# --- strategy_tips ---------------------------------------------------------

def _pattern(pid: str, cat: str = "procedural", conf: float = 0.72) -> PatternCard:
    # get_relevant_patterns only surfaces procedural/risk patterns at trial+ stage
    # (store.py:178-183); source_agent drives exclude_agent filtering (store.py:191).
    return PatternCard(
        id=pid, category=cat, confidence=conf, stage="trial",
        description=f"do {pid} before Y", recommendation=f"do {pid} before Y",
        pattern_type="tool_sequence", evidence_count=20, source_agent="alice",
    )


def _enable_validation(monkeypatch):
    """A/B tip recording (strategy_tips) is frozen behind PATTERN_VALIDATION_ENABLED
    (N=1 Task 1). These tests verify the KEPT A/B math, so enable the flag."""
    from types import SimpleNamespace
    monkeypatch.setattr(S, "get_settings",
                        lambda: SimpleNamespace(PATTERN_VALIDATION_ENABLED=True))


@pytest.mark.asyncio
async def test_strategy_tips_treatment_records_and_shows(rr, monkeypatch):
    _enable_validation(monkeypatch)
    await store_patterns(rr, [_pattern("p1")])
    sec = await S.strategy_tips_section(rr, goal="do p1 before Y", briefing_id="bid1", ab_group="treatment")
    assert sec["status"] == "ok"
    assert sec["data"]["ab_group"] == "treatment"
    assert sec["data"]["shown"] is True
    assert sec["data"]["patterns"] and sec["data"]["patterns"][0]["id"] == "p1"
    assert sec["data"]["briefing_id"] == "bid1"
    # D2: tip-shown recorded against the minted briefing_id.
    groups = await _load_tip_groups(rr)
    assert "bid1" in groups


@pytest.mark.asyncio
async def test_strategy_tips_control_withholds_patterns(rr, monkeypatch):
    _enable_validation(monkeypatch)
    await store_patterns(rr, [_pattern("p1")])
    sec = await S.strategy_tips_section(rr, goal="do p1 before Y", briefing_id="bid2", ab_group="control")
    # D6: control keeps consistent shape but patterns == [] and shown == False.
    assert sec["data"]["ab_group"] == "control"
    assert sec["data"]["shown"] is False
    assert sec["data"]["patterns"] == []


@pytest.mark.asyncio
async def test_strategy_tips_frozen_skips_ab_write_and_withholds(rr):
    """N=1 Task 1: with PATTERN_VALIDATION_ENABLED off (default), the section still
    returns but records no A/B tip-shown and shows no patterns — even for treatment."""
    await store_patterns(rr, [_pattern("p1")])
    sec = await S.strategy_tips_section(rr, goal="do p1 before Y", briefing_id="bid3", ab_group="treatment")
    assert sec["data"]["shown"] is False
    assert sec["data"]["patterns"] == []
    # No A/B write happened while frozen.
    groups = await _load_tip_groups(rr)
    assert "bid3" not in groups


# --- cross_agent -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cross_agent_empty_for_default_agent(rr):
    await store_patterns(rr, [_pattern("p1")])
    sec = await S.cross_agent_section(rr, goal="do p1 before Y", agent_id="default")
    assert sec["status"] == "empty"


@pytest.mark.asyncio
async def test_cross_agent_excludes_own_patterns(rr):
    await store_patterns(rr, [_pattern("p1")])  # authored by "alice"
    sec = await S.cross_agent_section(rr, goal="do p1 before Y", agent_id="alice")
    # excluding alice removes the only pattern -> empty
    assert sec["status"] == "empty"
    sec2 = await S.cross_agent_section(rr, goal="do p1 before Y", agent_id="bob")
    assert sec2["status"] == "ok"
    assert sec2["data"]["patterns"][0]["id"] == "p1"


# --- observed patterns (N=1 surface, Task 3) -------------------------------

def _observed_pat(pid: str = "op1", agent: str = "me", conf: float = 0.6) -> PatternCard:
    """A caller-owned candidate/observed pattern — the N=1 surface's input.

    PatternCard has no source_session field (models.py), so provenance falls
    back to source_agent, which observed_patterns_section reads via getattr.
    Stage is candidate/observed (NOT trial+) — the ones get_relevant_patterns
    filters OUT — so this surface is descriptive, never a promoted strategy card.
    """
    return PatternCard(
        id=pid, category="risk", confidence=conf, stage="observed",
        description=f"{pid} area is a hotspot",
        recommendation=f"cap embed input near {pid}",
        pattern_type="failure_mode", evidence_count=3, source_agent=agent,
    )


@pytest.mark.asyncio
async def test_observed_section_surfaces_one_grounded_tip(rr, monkeypatch):
    """N=1 value: with one observed pattern for the agent, the section is
    non-empty, carries provenance, and is labelled observed (unvalidated) so it
    is never confused with a promoted (trial+) strategy card."""
    async def _fake_get(r, *, agent_id, goal="", limit=1):
        return [_observed_pat(agent=agent_id)]
    monkeypatch.setattr(S, "get_observed_patterns", _fake_get)
    sec = await S.observed_patterns_section(rr, agent_id="me", goal="fix embeds")
    assert sec["status"] == "ok"
    assert sec["data"]["note"] == "observed (unvalidated)"
    assert sec["data"]["items"][0]["provenance"]         # non-empty provenance
    assert sec["data"]["items"][0]["recommendation"] == "cap embed input near op1"


@pytest.mark.asyncio
async def test_observed_section_empty_when_no_own_patterns(rr, monkeypatch):
    async def _fake_get(r, *, agent_id, goal="", limit=1):
        return []
    monkeypatch.setattr(S, "get_observed_patterns", _fake_get)
    sec = await S.observed_patterns_section(rr, agent_id="me", goal="x")
    assert sec["status"] == "empty"
    assert sec["data"]["items"] == []
    # The label stays stable even when empty.
    assert sec["data"]["note"] == "observed (unvalidated)"


# --- skills ----------------------------------------------------------------

def _skill_point():
    p = MagicMock()
    p.id = "sk1"
    p.payload = {"memory_type": "skill", "skill_status": "active",
                 "trigger": "collector down", "symptoms": "no events"}
    return p


def _semantic_vector(points):
    """A vector fake wired for the SEMANTIC path.

    Without an awaitable `_embed`, `search_skill_points` catches the resulting
    TypeError and degrades to scroll — so the test would pass while exercising the OLD
    code. That is precisely how the substring bug stayed invisible, so these fixtures
    assert the semantic branch was actually reached.
    """
    v = MagicMock()
    v._client = AsyncMock()
    v._embed = AsyncMock(return_value=[1.0, 0.0, 0.0])
    res = MagicMock()
    res.points = points
    v._client.query_points = AsyncMock(return_value=res)
    v._client.scroll = AsyncMock(return_value=([], None))
    return v


@pytest.mark.asyncio
async def test_skills_ok():
    """The goal shares NO literal substring with the trigger.

    Previously this passed only because goal='collector' happens to sit inside
    trigger='collector down' — it would have passed against the broken substring
    filter too, and therefore proved nothing about matching.
    """
    vector = _semantic_vector([_skill_point()])
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    sec = await S.skills_section(
        vector, settings, goal="events stopped arriving from the wiki sync", project=None
    )
    assert sec["status"] == "ok"
    assert sec["data"]["skills"][0]["trigger"] == "collector down"
    vector._client.query_points.assert_awaited()
    vector._client.scroll.assert_not_awaited()


@pytest.mark.asyncio
async def test_skills_empty():
    vector = _semantic_vector([])
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    sec = await S.skills_section(vector, settings, goal="x", project=None)
    assert sec["status"] == "empty"
    # Nothing cleared the floor -> the empty-result fallback consults scroll, which is
    # also empty. Both paths ran; the section stays 'empty' rather than erroring.
    vector._client.query_points.assert_awaited()


def _document_draft_skill_point():
    """A draft synthesized from a doc-ingest source (SP2 Task 1's source_type
    field) — must never surface in the briefing before human approval."""
    p = MagicMock()
    p.id = "doc1"
    p.payload = {"memory_type": "skill", "skill_status": "draft",
                 "trigger": "restart the widget", "symptoms": "widget hangs",
                 "source_type": "document"}
    return p


def _match_set(match):
    """Normalize a Qdrant match condition (MatchValue or MatchAny) to the set
    of values it accepts, so a fake filter-evaluator can test membership
    regardless of which the caller used."""
    values = getattr(match, "any", None)
    return set(values) if values is not None else {match.value}


def _filtering_scroll(points):
    """Unlike the canned-list fakes above, this one actually evaluates the
    Qdrant `must` FieldConditions skills_section passes to scroll — needed to
    prove the hardcoded skill_status filter really excludes drafts rather
    than just asserting on a fixture we control. skill_status is now a
    MatchAny(["active", "trial"]) (Task 3), not a single MatchValue, so this
    tests set-membership rather than equality."""
    async def _scroll(*, scroll_filter, limit, **_kwargs):
        conditions = {c.key: _match_set(c.match) for c in (scroll_filter.must or [])}
        matched = [
            p for p in points
            if all((p.payload or {}).get(k) in v for k, v in conditions.items())
        ]
        return matched[:limit], None
    return _scroll


@pytest.mark.asyncio
async def test_skills_section_excludes_document_draft():
    """SP2 Task 2 (draft-leak safety property): a source_type=document draft
    must be absent from the briefing's skills section even when it coexists
    with an approved active skill in the same collection."""
    vector = MagicMock()
    vector._client = AsyncMock()
    vector._client.scroll = _filtering_scroll(
        [_document_draft_skill_point(), _skill_point()]
    )
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")
    sec = await S.skills_section(vector, settings, goal="", project=None)
    ids = {s["id"] for s in sec["data"]["skills"]}
    assert "doc1" not in ids
    assert "sk1" in ids


def _tiered_skill_point(pid: str, status: str, trigger: str) -> MagicMock:
    p = MagicMock()
    p.id = pid
    p.payload = {"memory_type": "skill", "skill_status": status,
                 "trigger": trigger, "symptoms": "sym"}
    return p


@pytest.mark.asyncio
async def test_skills_section_adds_one_trial_after_actives_and_emits_receipt(monkeypatch):
    """Ladder shown signal (Task 3): actives first, then at most one trial
    (still <=3 total), each trial's trigger prefixed [TRIAL], and a
    memory_read receipt fires with trigger='briefing' so OWM can tell an
    impression apart from a reach (spec 2026-09-03 decision 2)."""
    points = [
        _tiered_skill_point("T1", "trial", "rotate the password"),
        _tiered_skill_point("A1", "active", "rotate keys"),
        _tiered_skill_point("T2", "trial", "rotate secrets"),
        _tiered_skill_point("A2", "active", "rotate tokens"),
    ]
    vector = _semantic_vector(points)
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")

    emitted = []

    async def fake_emit(event_type, **kw):
        emitted.append((event_type, kw))
    monkeypatch.setattr("app.main._replay_emit", fake_emit, raising=False)

    sec = await S.skills_section(vector, settings, goal="rotate password", project=None)

    tiers = [s["tier"] for s in sec["data"]["skills"]]
    assert tiers == ["active", "active", "trial"]          # <=3 total, one trial, last
    assert sec["data"]["skills"][-1]["trigger"].startswith("[TRIAL] ")
    assert emitted and emitted[0][0] == "memory_read"
    assert emitted[0][1]["payload"]["trigger"] == "briefing"
    assert set(emitted[0][1]["payload"]["memory_ids"]) == {"A1", "A2", "T1"}

    # The must filter carries the recallable MatchAny (active + trial), and
    # the section asks for headroom (limit=6) to make the actives/trial split
    # possible.
    call = vector._client.query_points.call_args
    status_cond = next(c for c in call.kwargs["query_filter"].must
                       if c.key == "skill_status")
    assert set(status_cond.match.any) == {"active", "trial"}
    assert call.kwargs["limit"] == 6


@pytest.mark.asyncio
async def test_skills_section_receipt_failure_never_breaks_the_section(monkeypatch):
    vector = _semantic_vector([_skill_point()])
    settings = MagicMock(QDRANT_COLLECTION="firekeep_memory")

    async def boom(*a, **k):
        raise RuntimeError("replay down")
    monkeypatch.setattr("app.main._replay_emit", boom, raising=False)

    sec = await S.skills_section(vector, settings, goal="x", project=None)
    assert sec["status"] == "ok"


# --- vault (admin-gated, D4) ----------------------------------------------

@pytest.mark.asyncio
async def test_vault_omitted_for_non_admin():
    sec = await S.vault_section(["session:read"])
    assert sec["status"] == "empty"
    assert sec["data"]["omitted_reason"] == "insufficient scope"


@pytest.mark.asyncio
async def test_vault_visible_for_admin(monkeypatch):
    async def _fake_list(category=None, limit=50):
        return [{"key": "office-jira-token", "category": "api"}]
    monkeypatch.setattr(S, "list_secrets", _fake_list)
    sec = await S.vault_section(["admin"])
    assert sec["status"] == "ok"
    assert sec["data"]["count"] == 1
    assert sec["data"]["secrets"][0]["key"] == "office-jira-token"


# --- discipline ------------------------------------------------------------

@pytest.mark.asyncio
async def test_discipline_counts_untagged(rr):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    await rr.set(f"cortex:untagged_calls:{today}", "4")
    sec = await S.discipline_section(rr)
    assert sec["status"] == "ok"
    assert sec["data"]["untagged_total"] == 4


@pytest.mark.asyncio
async def test_discipline_empty_when_zero(rr):
    sec = await S.discipline_section(rr)
    assert sec["status"] == "empty"
    assert sec["data"]["untagged_total"] == 0


# --- fail-loud limitation (documented, accepted) ---------------------------

@pytest.mark.asyncio
async def test_swallowing_sources_degrade_to_empty_not_unavailable():
    """quality/strategy_tips/cross_agent inherit their source's swallow-and-return
    behavior: `get_eval_summary` / `get_relevant_patterns` catch all exceptions
    internally and return empty defaults, so a genuine backend outage surfaces as
    status "empty" (NOT "unavailable") and NO exception escapes the builder.
    This is the accepted read-only-briefing limitation documented in each of
    those builders' docstrings.
    """
    broken = MagicMock()
    # Both source fns funnel through r.zrevrange first; make it blow up like a
    # real Redis outage would. The source's own try/except swallows it.
    broken.zrevrange = AsyncMock(side_effect=RuntimeError("redis down"))

    q = await S.quality_section(broken)
    assert q["status"] == "empty"
    assert q["data"]["total_sessions"] == 0

    t = await S.strategy_tips_section(broken, goal="anything", briefing_id="b", ab_group="treatment")
    assert t["status"] == "empty"
    assert t["data"]["patterns"] == []

    c = await S.cross_agent_section(broken, goal="anything", agent_id="bob")
    assert c["status"] == "empty"
    assert c["data"]["patterns"] == []


# --- dlq -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dlq_warnings(monkeypatch):
    async def _fake_depths():
        return {"celery": 0, "event_stream": 0, "event_dlq": 0,
                "memory_backfill": 0, "memory_backfill_dlq": 3, "distill_dlq": 2}
    monkeypatch.setattr(S, "collect_queue_depths", _fake_depths)
    sec = await S.dlq_section()
    assert sec["status"] == "ok"
    assert sec["data"]["memory_backfill_dlq"] == 3
    assert any("backfill" in w.lower() for w in sec["data"]["warnings"])
    assert any("distill" in w.lower() for w in sec["data"]["warnings"])


@pytest.mark.asyncio
async def test_dlq_empty_when_clean(monkeypatch):
    async def _fake_depths():
        return {"celery": 0, "event_stream": 0, "event_dlq": 0,
                "memory_backfill": 0, "memory_backfill_dlq": 0, "distill_dlq": 0}
    monkeypatch.setattr(S, "collect_queue_depths", _fake_depths)
    sec = await S.dlq_section()
    assert sec["status"] == "empty"
    assert sec["data"]["warnings"] == []
