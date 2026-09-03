# Skill Ladder PR1 (shadow) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the earned-trust skill ladder in **shadow mode**: a `trial` status end to end, the receipts that make "used" observable (without contaminating OWM), an evidence reader and pure decision rules, a nightly pass that records what it *would* do, and the inbox/digest/dashboard surfaces that show it — changing no skill status and enqueuing nothing.

**Architecture:** Cortex-only logic plus one client sentence. New modules `cortex/app/skills/ladder_evidence.py` (join receipts + feedback + grade per skill), `cortex/app/skills/ladder_rules.py` (pure decisions over evidence dicts), `cortex/app/skills/ladder.py` (Celery beat task, SETNX lock, shadow ledger). Existing surfaces extended: skills API/MCP recall (`recallable` = active+trial), briefing skills section (one trial, `[TRIAL]`, receipt), OWM (exclude briefing receipts), fleet ledger (ladder-wide key), autopilot inbox/digest, dashboard Skills/Autopilot tabs, stop hook message.

**Tech Stack:** Python 3.11 FastAPI/Pydantic v2, qdrant-client (`MatchAny`), redis.asyncio + fakeredis, Celery beat, stdlib client kit, vanilla-JS dashboard pinned by node-executed tests.

**Spec:** `docs/superpowers/specs/2026-09-03-skill-ladder-design.md` — decisions 1–10 and the Phasing section are binding; PR2 items (enforce transitions, `reauthor_failed_skill`, supersede, Rewrite button) are OUT of this plan.

## Global Constraints

- **Shadow only.** `SKILL_LADDER_MODE` defaults to `"shadow"`; in this PR the pass never changes `skill_status`, never enqueues a fleet task, never increments the ledger's transition counters. The only payload writes it may perform are bookkeeping: `ladder_since` (first stamp), `ladder_shadow`, `duplicate_of`. If `SKILL_LADDER_MODE="enforce"` is set, the pass logs a warning `enforce mode ships in PR2 — running shadow` and behaves as shadow.
- Skill statuses: `draft`, `trial`, `active`, `deprecated`. `POST /skills` still accepts only `active|draft`. `GET /skills?status=recallable` = `active` + `trial` (via `MatchAny`); plain `status=active` stays active-only.
- Evidence classification (spec decisions 2–3), verbatim: **success** = (`memory_feedback useful=true` for the skill in that session, OR a `skill_recall` receipt for it with no feedback in that session) AND session grade `success`; **failure** = `memory_feedback useful=false` AND session grade `failure`/`abandoned`; briefing receipts (`trigger="briefing"`) count only as *shown*; ungraded/`partial` sessions contribute nothing; Bridge `abandoned` overrides any grade as failure.
- Thresholds: env `SKILL_LADDER_PROMOTE_MIN_SUCCESSES=3`, `SKILL_LADDER_PROMOTE_MIN_AGENTS=2`, `SKILL_LADDER_TRIAL_TTL_DAYS=60`; module constants `PER_AGENT_CAP=2`, `PROMOTE_MIN_EFFICACY=0.6`, `DEMOTE_MIN_FAILURES=3`, `DEMOTE_MAX_EFFICACY=0.4`, `DEMOTE_MIN_N=5`, `DUP_THRESHOLD=0.92`, `TRIAL_CAP_PER_DOMAIN=10`, `ADMIT_PER_RUN=20`; efficacy `(s + P/2)/(n + P)` with `P = settings.OWM_PRIOR_N`; window `settings.OWM_WINDOW_DAYS`.
- Independence key: the event's `member_id` when present, else `agent_id`.
- `ladder_since` default for unstamped skills: `approved_at` → `stale_reviewed_at` → `timestamp`; stamped once by the first pass; evidence before it is ignored.
- Never admitted to trial: drafts carrying `demoted_at`, `ladder_rewrite_requested_at`, `trial_expired_at`, `superseded_by`, `duplicate_of`, or `needs_rereview=true`.
- **OWM must ignore `trigger="briefing"` `memory_read` receipts** in its tallies (spec decision 2).
- Ledger: new key `fleet:ledger:ladder` with counters `admitted, promoted, demoted, expired, rewrite_requested` (same daily/all-time shape). Shadow increments nothing.
- `tests/test_dashboard_autopilot.py::TestRoundOneIsReadOnly` unchanged: three fetch URLs, no write verbs, no new fetch inside the sentinels.
- Commit trailer on every commit: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_01CLPowv3rPbXxBsRXDkNFze`.
- Run tests from the service dir (`cd cortex && python -m pytest tests/<file> -q`), repo guards from the worktree root.

---

## File map

| Area | Create | Modify |
|---|---|---|
| Settings | `cortex/tests/test_config_ladder.py` | `cortex/app/config.py`, `docker-compose.yml` (4 cortex blocks), `.env.example`, `docs/guides/cortex-configuration.md` |
| Status + recall | — | `cortex/app/models.py` (`SkillResponse`), `cortex/app/skills/api.py` (`list_skills`, `patch_skill`, `_point_to_response`), `cortex/app/mcp_server.py` (`skill_recall`), `cortex/tests/test_skill_api.py`, `cortex/tests/test_mcp_skill_recall_trial.py` |
| Briefing + OWM | — | `cortex/app/briefing/sections.py` (`skills_section`), `cortex/app/briefing/api.py`? (only if the section needs the replay emitter passed), `cortex/app/owm.py`, `cortex/tests/test_briefing_sections_inprocess.py`, `cortex/tests/test_owm.py` |
| Ledger | — | `cortex/app/fleet/ledger.py`, `cortex/tests/test_fleet_ledger.py` |
| Evidence + rules | `cortex/app/skills/ladder_evidence.py`, `cortex/app/skills/ladder_rules.py`, `cortex/tests/test_ladder_evidence.py`, `cortex/tests/test_ladder_rules.py` | — |
| Pass | `cortex/app/skills/ladder.py`, `cortex/tests/test_ladder_pass.py` | `cortex/app/workers/sleep_cycle.py` (include + beat) |
| Autopilot | — | `cortex/app/autopilot/inbox.py`, `cortex/app/autopilot/digest.py`, `cortex/app/autopilot/api.py`, `cortex/tests/test_autopilot_api.py` |
| Dashboard | — | `dashboard/index.html`, `tests/test_dashboard_autopilot.py` |
| Client | — | `client/firekeep_client/hooks/stop.py`, `client/tests/hooks/test_stop.py` |
| Docs | — | `docs/guides/knowledge-and-skills.md`, `docs/guides/knowledge-autopilot.md`, `docs/guides/cortex-api-endpoints.md`, `docs/guides/client-kit.md`, `CLAUDE.md`, `README.md` |

---

### Task 1: Settings, compose, `.env.example`, config guide

**Files:**
- Modify: `cortex/app/config.py` (after the `FLEET_ENQUEUE_*` block), `docker-compose.yml` (each of the four cortex service blocks, directly under the `FLEET_ENQUEUE_MAX_PER_RUN` line), `.env.example` (after the Fleet-as-GPU block), `docs/guides/cortex-configuration.md` (bullet after the `FLEET_ENQUEUE_ENABLED` bullet)
- Test: `cortex/tests/test_config_ladder.py`

**Interfaces:**
- Produces: `Settings.SKILL_LADDER_ENABLED: bool = True`, `SKILL_LADDER_MODE: str = "shadow"`, `SKILL_LADDER_SCHEDULE_HOURS: int = 24`, `SKILL_LADDER_PROMOTE_MIN_SUCCESSES: int = 3`, `SKILL_LADDER_PROMOTE_MIN_AGENTS: int = 2`, `SKILL_LADDER_TRIAL_TTL_DAYS: int = 60`.

- [ ] **Step 1: Write the failing test** — copy `cortex/tests/test_config_fleet.py`'s structure (it imports `_service_block`/`_has_env_entry` from `tests/test_procedure_config.py`):

```python
# cortex/tests/test_config_ladder.py
"""Skill-ladder settings exist, default as documented, and are plumbed everywhere."""
import re
from pathlib import Path

import pytest

from app.config import Settings
from tests.test_procedure_config import _has_env_entry, _service_block

REPO = Path(__file__).resolve().parents[2]
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
FLAGS = [
    ("SKILL_LADDER_ENABLED", "true"),
    ("SKILL_LADDER_MODE", "shadow"),
    ("SKILL_LADDER_SCHEDULE_HOURS", "24"),
    ("SKILL_LADDER_PROMOTE_MIN_SUCCESSES", "3"),
    ("SKILL_LADDER_PROMOTE_MIN_AGENTS", "2"),
    ("SKILL_LADDER_TRIAL_TTL_DAYS", "60"),
]


def test_defaults():
    s = Settings(_env_file=None)
    assert s.SKILL_LADDER_ENABLED is True
    assert s.SKILL_LADDER_MODE == "shadow"
    assert s.SKILL_LADDER_SCHEDULE_HOURS == 24
    assert s.SKILL_LADDER_PROMOTE_MIN_SUCCESSES == 3
    assert s.SKILL_LADDER_PROMOTE_MIN_AGENTS == 2
    assert s.SKILL_LADDER_TRIAL_TTL_DAYS == 60


def test_env_override(monkeypatch):
    monkeypatch.setenv("SKILL_LADDER_MODE", "enforce")
    monkeypatch.setenv("SKILL_LADDER_PROMOTE_MIN_SUCCESSES", "5")
    s = Settings(_env_file=None)
    assert s.SKILL_LADDER_MODE == "enforce" and s.SKILL_LADDER_PROMOTE_MIN_SUCCESSES == 5


@pytest.mark.parametrize("service", ["cortex-api", "cortex-mcp", "cortex-worker", "cortex-beat"])
@pytest.mark.parametrize("name,default", FLAGS)
def test_every_cortex_service_carries_the_flag_with_the_code_default(service, name, default):
    assert _has_env_entry(_service_block(service), name), f"{name} missing from {service}"
    hits = re.findall(rf"{name}:\s*\$\{{{name}:-([^}}]*)\}}", COMPOSE)
    assert hits and all(h.strip().lower() == default for h in hits), (name, hits)


def test_env_example_and_guide_carry_the_flags():
    env = (REPO / ".env.example").read_text(encoding="utf-8")
    guide = (REPO / "docs/guides/cortex-configuration.md").read_text(encoding="utf-8")
    for name, default in FLAGS:
        assert name in env, name
        assert f"`{name}` (default `{default}`)" in guide, name
```

- [ ] **Step 2: Run to verify it fails** — `cd cortex && python -m pytest tests/test_config_ladder.py -q` → FAIL (`AttributeError: SKILL_LADDER_ENABLED`).

- [ ] **Step 3: Declare settings** (`cortex/app/config.py`, right after `FLEET_ENQUEUE_MAX_PER_RUN`):

```python
    # The skill ladder (spec 2026-09-03): drafts earn `trial`, trial earns `active`
    # on independent graded evidence, failing trials fall back to draft and failing
    # actives are flagged for a fleet rewrite. MODE is the safety: "shadow" records
    # every decision (inbox + digest) and changes nothing; "enforce" applies them
    # and ships in PR2 — a shadow fortnight comes first. Thresholds that nobody
    # tunes live as constants in app/skills/ladder_rules.py, not here.
    SKILL_LADDER_ENABLED: bool = True
    SKILL_LADDER_MODE: str = "shadow"
    SKILL_LADDER_SCHEDULE_HOURS: int = 24
    SKILL_LADDER_PROMOTE_MIN_SUCCESSES: int = 3
    SKILL_LADDER_PROMOTE_MIN_AGENTS: int = 2
    SKILL_LADDER_TRIAL_TTL_DAYS: int = 60
```

- [ ] **Step 4: Plumb** — in `docker-compose.yml`, under each cortex block's `FLEET_ENQUEUE_MAX_PER_RUN: ${FLEET_ENQUEUE_MAX_PER_RUN:-20}` line add:

```yaml
      # Skill ladder (spec 2026-09-03): trial tier + evidence-gated promotion;
      # shadow mode records decisions and changes nothing (enforce ships in PR2).
      SKILL_LADDER_ENABLED: ${SKILL_LADDER_ENABLED:-true}
      SKILL_LADDER_MODE: ${SKILL_LADDER_MODE:-shadow}
      SKILL_LADDER_SCHEDULE_HOURS: ${SKILL_LADDER_SCHEDULE_HOURS:-24}
      SKILL_LADDER_PROMOTE_MIN_SUCCESSES: ${SKILL_LADDER_PROMOTE_MIN_SUCCESSES:-3}
      SKILL_LADDER_PROMOTE_MIN_AGENTS: ${SKILL_LADDER_PROMOTE_MIN_AGENTS:-2}
      SKILL_LADDER_TRIAL_TTL_DAYS: ${SKILL_LADDER_TRIAL_TTL_DAYS:-60}
```

and in `.env.example` after the Fleet-as-GPU block:

```bash
# --- Skill ladder (cortex) ---
# Drafts earn trial, trial earns active on independent graded evidence. shadow =
# record decisions only (inbox/digest), change nothing; enforce ships in PR2.
SKILL_LADDER_ENABLED=true
SKILL_LADDER_MODE=shadow
SKILL_LADDER_SCHEDULE_HOURS=24
SKILL_LADDER_PROMOTE_MIN_SUCCESSES=3
SKILL_LADDER_PROMOTE_MIN_AGENTS=2
SKILL_LADDER_TRIAL_TTL_DAYS=60
```

- [ ] **Step 5: Document** — `docs/guides/cortex-configuration.md`, after the `FLEET_ENQUEUE_ENABLED` bullet:

```markdown
- `SKILL_LADDER_ENABLED` (default `true`) — the skill ladder (`app/skills/ladder.py`, nightly Celery task `run_skill_ladder`, `docs/guides/knowledge-autopilot.md` §9): drafts are admitted to `trial` (recallable, labeled `[TRIAL]`, ranked last, at most one per briefing), trial skills are promoted to `active` on independent graded evidence, failing trials fall back to draft, failing actives are flagged for a fleet rewrite. `SKILL_LADDER_MODE` (default `shadow`) — `shadow` records every decision in the ledger, inbox (`ladder_proposals`) and digest (`ladder`) and **changes no status and enqueues nothing**; `enforce` applies transitions and ships in PR2 — flip it only after a shadow fortnight. `SKILL_LADDER_SCHEDULE_HOURS` (default `24`). `SKILL_LADDER_PROMOTE_MIN_SUCCESSES` (default `3`) applied-and-succeeded observations since the skill's last status change, from at least `SKILL_LADDER_PROMOTE_MIN_AGENTS` (default `2`) distinct identities (member id when present, else agent id; on a solo Keep with several agent ids this is satisfied trivially), at most 2 counted per identity, Beta-shrunk efficacy ≥ 0.6 with `OWM_PRIOR_N`, zero paired failures. `SKILL_LADDER_TRIAL_TTL_DAYS` (default `60`) — a trial skill never shown for this long returns to draft. Everything else (per-agent cap 2, demote at ≥3 paired failures and efficacy < 0.4 at n ≥ 5, duplicate cosine ≥ 0.92, 10 trials per domain, 20 admissions per run) is a named constant in `app/skills/ladder_rules.py`. Evidence: `skill_recall` receipts (reached), `memory_feedback` on skill ids (applied), the session's `ctx_complete_session` grade; briefing impressions (`trigger="briefing"`) count only as *shown* and are excluded from OWM's `skill_efficacy` tally.
```

- [ ] **Step 6: Run** — `cd cortex && python -m pytest tests/test_config_ladder.py tests/test_config_fleet.py tests/test_procedure_config.py -q` and from the worktree root `python -m pytest tests/test_no_dead_config.py tests/test_compose_security_defaults.py -q` → PASS.

- [ ] **Step 7: Commit** — `feat(cortex): SKILL_LADDER_* settings (shadow by default)`.

---

### Task 2: `trial` status end to end — API, MCP recall, `ladder_since`

**Files:**
- Modify: `cortex/app/models.py` (`SkillResponse`), `cortex/app/skills/api.py` (`list_skills`, `patch_skill`, `_point_to_response`), `cortex/app/mcp_server.py` (`skill_recall`)
- Test: `cortex/tests/test_skill_api.py` (append), `cortex/tests/test_mcp_skill_recall_trial.py`

**Interfaces:**
- Produces: `GET /skills?status=recallable` → `skill_status ∈ {active, trial}` via `MatchAny`, results sorted active-first (stable within tier); `GET /skills?status=trial` lists trials; `PATCH /skills/{id}` with any `skill_status` change stamps `ladder_since=<now iso>` and, when the new status is `active`, `approved_by="human"` (unless the request carries `approved_by="ladder"` — a header-free field PR2's ladder will send; add `approved_by: str | None = None` to `SkillPatchRequest`, allowed values `human|ladder`, default `human` on activation); `SkillResponse` gains `ladder_since`, `approved_by`, `ladder_shadow`, `ladder_history`, `demoted_at`, `demotion_reason`, `ladder_rewrite_requested_at`, `trial_expired_at`, `duplicate_of`, `superseded_by` (all optional, None by default). MCP `skill_recall` requests `status=recallable` and renders trial skills after actives with the header `**[TRIAL] <trigger>** — trial skill: not yet proven, verify before relying on it`.

- [ ] **Step 1: Failing tests** (append to `cortex/tests/test_skill_api.py`, using its `mock_vector`, `mock_settings`, `_make_app`, `_make_mock_point(skill_id, trigger, status)`; the file's async ASGI helper exists for fakeredis cases — these need none):

```python
# --- Skill ladder: trial status, recallable alias, ladder_since ---------------
from qdrant_client.models import MatchAny as _MatchAny


def _scroll_filter_must(mock_vector):
    """The Filter.must list the last scroll/search received."""
    call = mock_vector._client.scroll.call_args or mock_vector._client.search.call_args
    kw = call.kwargs
    flt = kw.get("scroll_filter") or kw.get("query_filter")
    return list(flt.must)


def test_status_recallable_matches_active_and_trial(mock_vector, mock_settings):
    active = _make_mock_point("a1", "Active one", status="active")
    trial = _make_mock_point("t1", "Trial one", status="trial")
    mock_vector._client.scroll = AsyncMock(return_value=([trial, active], None))
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.get("/skills?status=recallable")
    assert resp.status_code == 200
    statuses = [c for c in _scroll_filter_must(mock_vector) if c.key == "skill_status"]
    assert len(statuses) == 1 and isinstance(statuses[0].match, _MatchAny)
    assert set(statuses[0].match.any) == {"active", "trial"}
    # actives first, trial last
    assert [s["skill_status"] for s in resp.json()] == ["active", "trial"]


def test_status_active_is_still_active_only(mock_vector, mock_settings):
    mock_vector._client.scroll = AsyncMock(return_value=([], None))
    TestClient(_make_app(mock_vector, mock_settings)).get("/skills?status=active")
    statuses = [c for c in _scroll_filter_must(mock_vector) if c.key == "skill_status"]
    assert statuses[0].match.value == "active"


def test_status_trial_lists_trials(mock_vector, mock_settings):
    mock_vector._client.scroll = AsyncMock(return_value=([_make_mock_point("t1", status="trial")], None))
    resp = TestClient(_make_app(mock_vector, mock_settings)).get("/skills?status=trial")
    assert resp.status_code == 200 and resp.json()[0]["skill_status"] == "trial"


def test_patch_status_change_stamps_ladder_since_and_approved_by(mock_vector, mock_settings):
    draft = _make_mock_point("d1", status="draft")
    mock_vector._client.retrieve = AsyncMock(return_value=[draft])
    client = TestClient(_make_app(mock_vector, mock_settings))
    resp = client.patch("/skills/d1", json={"skill_status": "trial"})
    assert resp.status_code == 200, resp.text
    written = mock_vector._client.set_payload.call_args.kwargs["payload"]
    assert written["skill_status"] == "trial" and written["ladder_since"]
    assert "approved_by" not in written
    draft.payload.update(written)
    client.patch("/skills/d1", json={"skill_status": "active"})
    written = mock_vector._client.set_payload.call_args.kwargs["payload"]
    assert written["approved_by"] == "human" and written["ladder_since"] and written["approved_at"]


def test_patch_same_status_does_not_restamp_ladder_since(mock_vector, mock_settings):
    active = _make_mock_point("a1", status="active")
    active.payload["ladder_since"] = "2026-01-01T00:00:00+00:00"
    mock_vector._client.retrieve = AsyncMock(return_value=[active])
    client = TestClient(_make_app(mock_vector, mock_settings))
    client.patch("/skills/a1", json={"skill_status": "active", "stale": False})
    written = mock_vector._client.set_payload.call_args.kwargs["payload"]
    assert "ladder_since" not in written


def test_response_exposes_ladder_fields(mock_vector, mock_settings):
    p = _make_mock_point("a1", status="active")
    p.payload.update({"ladder_since": "2026-09-01T00:00:00+00:00", "approved_by": "human",
                      "ladder_shadow": {"would": "promote"}, "duplicate_of": None})
    mock_vector._client.retrieve = AsyncMock(return_value=[p])
    body = TestClient(_make_app(mock_vector, mock_settings)).get("/skills/a1").json()
    assert body["ladder_since"] == "2026-09-01T00:00:00+00:00"
    assert body["approved_by"] == "human" and body["ladder_shadow"] == {"would": "promote"}
```

`cortex/tests/test_mcp_skill_recall_trial.py` — patch the HTTP client the way `test_mcp_skill_create_fleet.py` does (`monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _Client)` plus the module's client reset fixture) and assert:

```python
@pytest.mark.asyncio
async def test_skill_recall_requests_recallable_and_labels_trials(monkeypatch):
    # _Client.get(path, params=...) records params and returns two skills, trial first
    ...
    out = await mcp_server.skill_recall("rotate the neo4j password")
    assert _Client.params["status"] == "recallable"
    active_idx = out.index("**Rotate the Neo4j password**")
    trial_idx = out.index("**[TRIAL] Restore from backup**")
    assert active_idx < trial_idx
    assert "trial skill: not yet proven, verify before relying on it" in out
```

- [ ] **Step 2: Run to verify they fail** — `cd cortex && python -m pytest tests/test_skill_api.py -q -k "recallable or trial or ladder_since or ladder_fields" && python -m pytest tests/test_mcp_skill_recall_trial.py -q` → FAIL.

- [ ] **Step 3: Models** — `SkillPatchRequest` gains `approved_by: Literal["human", "ladder"] | None = None`; `SkillResponse` gains the ten optional fields listed in Interfaces (all `| None = None`; `ladder_shadow: dict | None`, `ladder_history: list[dict] | None`).

- [ ] **Step 4: `list_skills`** — replace the single `skill_status` condition:

```python
        from qdrant_client.models import MatchAny

        status = status or "active"
        # `recallable` is the one alias: what an agent may be shown — active plus
        # trial (spec 2026-09-03 decision 1). Plain `active` stays active-only so
        # dashboards and the staleness sweep keep their exact meaning.
        if status == "recallable":
            status_cond = FieldCondition(key="skill_status", match=MatchAny(any=["active", "trial"]))
        else:
            status_cond = FieldCondition(key="skill_status", match=MatchValue(value=status))
        must = [
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            status_cond,
        ]
```

and after `results = [...]`, before the substring narrowing:

```python
        if status == "recallable":
            # Actives first, trials last; stable within a tier so the semantic
            # ranking survives inside each group.
            results.sort(key=lambda r: 0 if r.skill_status == "active" else 1)
```

- [ ] **Step 5: `patch_skill`** — inside `if req.skill_status is not None:` after `updates["skill_status"] = req.skill_status` add:

```python
            if req.skill_status != current.get("skill_status"):
                # Every status change opens a fresh evidence window for the ladder
                # (spec decision 4): promotions never ride evidence from a previous life.
                updates["ladder_since"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                if req.skill_status == "active":
                    updates["approved_by"] = req.approved_by or "human"
```

(keep the existing `approved_at`/ledger logic; `current` is already captured before `updates`).

- [ ] **Step 6: `_point_to_response`** — add the ten fields (`ladder_since=p.get("ladder_since")`, etc.).

- [ ] **Step 7: `skill_recall`** — `params["status"] = "recallable"`; render:

```python
        lines = ["## Relevant Skills\n"]
        for s in skills[:top_k]:
            if s.get("skill_status") == "trial":
                lines.append(f"**[TRIAL] {s.get('trigger', 'Skill')}** — trial skill: not yet "
                             "proven, verify before relying on it")
            else:
                lines.append(f"**{s.get('trigger', 'Skill')}**")
            lines.append(s.get("content", ""))
            lines.append("")
```

- [ ] **Step 8: Run** — `cd cortex && python -m pytest tests/test_skill_api.py tests/test_mcp_skill_recall_trial.py tests/test_skill_search.py tests/test_mcp_skill_create_fleet.py -q` → PASS.

- [ ] **Step 9: Commit** — `feat(cortex): trial skill status — recallable alias, ladder_since/approved_by on PATCH, [TRIAL] in skill_recall`.

---

### Task 3: Briefing shows one trial, emits a receipt; OWM ignores briefing receipts

**Files:**
- Modify: `cortex/app/briefing/sections.py` (`skills_section`), `cortex/app/owm.py` (the `memory_read` branch of the per-session event loop, ~line 282)
- Test: `cortex/tests/test_briefing_sections_inprocess.py` (append), `cortex/tests/test_owm.py` (append)

**Interfaces:**
- Produces: the briefing's skills section queries `skill_status ∈ {active, trial}`, returns actives first then at most **one** trial (total still ≤ 3), each item gains `"tier": "active"|"trial"`, trial triggers are prefixed `[TRIAL] ` in the rendered text; after selecting, it emits `memory_read` with `payload={"memory_ids": [...ids], "result_count": n, "trigger": "briefing"}` via `app.main._replay_emit` (lazy import, best-effort). OWM's event loop skips `memory_read` events whose `payload.trigger == "briefing"` for BOTH tallies.

- [ ] **Step 1: Failing tests** — in `cortex/tests/test_briefing_sections_inprocess.py` find the existing `skills_section` test (it builds a fake vector/settings; reuse its fixtures) and add:

```python
@pytest.mark.asyncio
async def test_skills_section_adds_one_trial_after_actives_and_emits_receipt(monkeypatch, ...):
    # fake search returns: trial T1, active A1, trial T2, active A2 (in that order)
    ...
    emitted = []
    async def fake_emit(event_type, **kw): emitted.append((event_type, kw))
    monkeypatch.setattr("app.main._replay_emit", fake_emit, raising=False)
    section = await skills_section(vector, settings, goal="rotate password", project=None)
    tiers = [s["tier"] for s in section.data["skills"]]
    assert tiers == ["active", "active", "trial"]          # ≤3 total, one trial, last
    assert section.data["skills"][-1]["trigger"].startswith("[TRIAL] ")
    assert emitted and emitted[0][0] == "memory_read"
    assert emitted[0][1]["payload"]["trigger"] == "briefing"
    assert set(emitted[0][1]["payload"]["memory_ids"]) == {"A1", "A2", "T1"}


@pytest.mark.asyncio
async def test_skills_section_receipt_failure_never_breaks_the_section(monkeypatch, ...):
    async def boom(*a, **k): raise RuntimeError("replay down")
    monkeypatch.setattr("app.main._replay_emit", boom, raising=False)
    section = await skills_section(vector, settings, goal="x", project=None)
    assert section.status == "ok"
```

(Adapt the fake-vector construction and `Section` field names to what the file already uses; the assertions are the contract. Look at how the section obtains `must` — the test should assert the filter carries `MatchAny(["active","trial"])` if the fake exposes it.)

In `cortex/tests/test_owm.py` add, using the file's existing fake replay fixtures (find the test that feeds a `memory_read` event for a skill id and asserts `skill_efficacy_n`):

```python
@pytest.mark.asyncio
async def test_briefing_receipts_are_not_skill_exposure(...):
    # same setup as the skill-exposure test, but the memory_read event carries
    # payload.trigger == "briefing"
    ...
    assert "skill_efficacy_n" not in written_payload_for("skill-1")   # nothing tallied
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: `skills_section`** — replace the `must` status condition with `MatchAny(any=["active", "trial"])`, request `limit=6` from `search_skill_points` (headroom for the split), then after building `skills` (each dict now includes `"tier": payload.get("skill_status")`), select:

```python
    actives = [s for s in skills if s["tier"] == "active"][:3]
    trials = [s for s in skills if s["tier"] == "trial"][:1]
    skills = (actives + trials)[:3]
    for s in skills:
        if s["tier"] == "trial" and not s["trigger"].startswith("[TRIAL] "):
            s["trigger"] = "[TRIAL] " + s["trigger"]
    # Exposure receipt (spec 2026-09-03 decision 2): the ladder's *shown* signal.
    # trigger="briefing" so OWM can exclude it — an impression is not a reach.
    if skills:
        try:
            from app.main import _replay_emit
            await _replay_emit("memory_read", session_id=session_id or "unknown",
                               agent_id=agent_id or "unknown",
                               payload={"memory_ids": [s["id"] for s in skills],
                                        "result_count": len(skills), "trigger": "briefing"})
        except Exception as exc:  # noqa: BLE001 — a receipt never costs the briefing
            logger.debug("briefing skills receipt skipped: %s", exc)
```

`skills_section` needs the caller's `session_id`/`agent_id`: add keyword params `session_id: str | None = None, agent_id: str | None = None` and pass them from the briefing router where the section is invoked (`briefing/api.py` — it already has the request headers; look for where `skills_section(` is called and thread `request.headers.get("X-Session-Id")` / `X-Agent-Id`). If the existing render code prints `trigger`, the `[TRIAL] ` prefix flows through unchanged.

- [ ] **Step 4: OWM** — in the per-session event loop, before the `memory_read` branch handles ids:

```python
                elif et == "memory_read":
                    if (ev.get("payload") or {}).get("trigger") == "briefing":
                        continue  # impression, not reach (skill ladder spec decision 2)
```

- [ ] **Step 5: Run** — `cd cortex && python -m pytest tests/test_briefing_sections_inprocess.py tests/test_briefing_api.py tests/test_briefing_render.py tests/test_owm.py -q` → PASS.

- [ ] **Step 6: Commit** — `feat(cortex): briefing shows one trial skill and emits a briefing receipt; OWM ignores briefing receipts`.

---

### Task 4: Ledger — the ladder-wide key

**Files:**
- Modify: `cortex/app/fleet/ledger.py`
- Test: `cortex/tests/test_fleet_ledger.py` (append)

**Interfaces:**
- Produces: `JOB_LADDER = "ladder"`, `LADDER_COUNTERS = ("admitted", "promoted", "demoted", "expired", "rewrite_requested")`; `record(redis, JOB_LADDER, <counter>)` works; `summarize()` returns `out["ladder"] = {"window": {...counters}, "all_time": {...counters}}` (no rate, no pending); the three fleet jobs are unchanged.

- [ ] **Step 1: Failing tests** (append to `cortex/tests/test_fleet_ledger.py`):

```python
@pytest.mark.asyncio
async def test_ladder_key_records_and_summarizes(redis):
    assert await ledger.record(redis, ledger.JOB_LADDER, "admitted", now=NOW) is True
    assert await ledger.record(redis, ledger.JOB_LADDER, "promoted", now=NOW) is True
    assert await ledger.record(redis, ledger.JOB_LADDER, "bogus", now=NOW) is False
    out = await ledger.summarize(redis, days=7, now=NOW)
    assert out["ladder"]["window"] == {"admitted": 1, "promoted": 1, "demoted": 0,
                                       "expired": 0, "rewrite_requested": 0}
    assert out["ladder"]["all_time"]["promoted"] == 1
    assert "approval_rate" not in out["ladder"]["window"] and "pending" not in out["ladder"]["all_time"]
    # the fleet jobs are untouched
    assert set(out) == {ledger.JOB_DISTILL, ledger.JOB_REAUTHOR, ledger.JOB_VERDICT, ledger.JOB_LADDER}
```

- [ ] **Step 2: Run to verify it fails** — `cd cortex && python -m pytest tests/test_fleet_ledger.py -q -k ladder`.

- [ ] **Step 3: Implement** — in `ledger.py` add:

```python
JOB_LADDER = "ladder"   # not a fleet job: the skill ladder's own transitions (spec 2026-09-03 decision 8)
LADDER_COUNTERS = ("admitted", "promoted", "demoted", "expired", "rewrite_requested")
```

register `JOB_LADDER: LADDER_COUNTERS` in `_COUNTERS` and append `JOB_LADDER` to `JOBS`; in `_with_rate` return counts unchanged for `JOB_LADDER` (no rate); in `summarize` skip the `pending` computation for `JOB_LADDER` (only skill jobs get it — the existing `if job != JOB_VERDICT` becomes `if job in (JOB_DISTILL, JOB_REAUTHOR)`). Keep `record`'s validation (unknown counter → False).

- [ ] **Step 4: Run** — `cd cortex && python -m pytest tests/test_fleet_ledger.py tests/test_autopilot_api.py -q` (the digest's fleet block now carries a fourth job; the dashboard's `AP_FLEET_JOBS` renders only the three it knows — fine, verified in Task 8).

- [ ] **Step 5: Commit** — `feat(cortex): fleet ledger gains the ladder-wide key`.

---

### Task 5: Evidence reader — `cortex/app/skills/ladder_evidence.py`

**Files:**
- Create: `cortex/app/skills/ladder_evidence.py`
- Test: `cortex/tests/test_ladder_evidence.py`

**Interfaces:**
- Consumes: `app.owm._default_events_fn(replay_r, session_id)` (events with `event_type`, `agent_id`, `session_id`, `payload`), `app.owm.session_success(eval_data, bridge_status)`, `app.owm._fetch_bridge_statuses(settings)`, `app.evals.models.recognized_grade_pair` (through `session_success`), replay keys `rp:eval_index` (zset scored by epoch) and `rp:eval:<sid>` (JSON), `settings.OWM_WINDOW_DAYS`, `settings.OWM_PRIOR_N`.
- Produces:

```python
@dataclass
class Evidence:
    shown: int = 0            # briefing + skill_recall receipts (sessions)
    reached: int = 0          # skill_recall receipts (sessions)
    applied: int = 0          # feedback events (any useful value)
    successes: int = 0        # per spec decision 2, after per-identity cap
    failures: int = 0         # paired useful=false + failed/abandoned grade
    identities: dict[str, int]   # identity -> successes counted
    last_failure_sessions: list[str]   # newest first, ≤5
    last_shown_at: str | None
    last_feedback_comment: str | None  # never set here (payload holds it); left for callers

async def gather(replay_r, settings, *, since_by_skill: dict[str, datetime], events_fn=None,
                 bridge_statuses: dict[str, str] | None = None, now: datetime | None = None,
                 per_identity_cap: int = 2) -> dict[str, Evidence]
def efficacy(ev: Evidence, prior_n: int) -> float   # (s + P/2)/(n + P), n = successes + failures
```

- [ ] **Step 1: Failing tests** — build a fake replay redis with fakeredis (`rp:eval_index` zset of session ids with epoch scores; `rp:eval:<sid>` JSON `{"task_result": ..., "task_result_source": "agent_self_report"}` — read `cortex/tests/test_owm.py` for the exact eval shape `recognized_grade_pair` accepts and reuse its helper if it has one) and an injected `events_fn` returning per-session event lists. Cases (one test each):
  1. `skill_recall` receipt + `useful=true` feedback + graded success → `successes == 1`, `reached == 1`, `applied == 1`.
  2. `skill_recall` receipt, no feedback, graded success → `successes == 1` (reached fallback).
  3. `briefing` receipt only, graded success → `shown == 1`, `successes == 0`.
  4. `useful=false` + graded success → `failures == 0`, `applied == 1`.
  5. `useful=false` + graded failure → `failures == 1`, session id in `last_failure_sessions`.
  6. `useful=true` + bridge status `abandoned` → `failures == 0`, `successes == 0` (abandoned ≠ paired failure; success requires success grade) — assert exactly that.
  7. ungraded / `partial` sessions → nothing counted beyond `shown/reached/applied`.
  8. per-identity cap: 4 successful sessions from agent `a` → `successes == 2`, `identities == {"a": 2}`; add agent `b` once → `successes == 3`.
  9. `member_id` in the event payload is preferred as the identity key over `agent_id`.
  10. `since_by_skill`: a success timestamped before the skill's `ladder_since` is ignored.
  11. `efficacy(Evidence(successes=3, failures=0), prior_n=5) == pytest.approx((3+2.5)/8)`.

- [ ] **Step 2: Run to verify they fail** — `ModuleNotFoundError`.

- [ ] **Step 3: Implement** — module docstring states the three-signal rule and the OWM-helper reuse; `gather` walks `rp:eval_index` from `now - OWM_WINDOW_DAYS`, loads each eval JSON, computes `grade = session_success(eval_data, bridge_statuses.get(sid))` (True/False/None), pulls events via `events_fn or _default_events_fn`, and for each skill id seen in `memory_read` (trigger `briefing` → shown; `skill_recall` → shown + reached) or `memory_feedback` (applied; remember `useful` per skill per session) accumulates per-session flags, then classifies once per (skill, session): success if `grade is True and (feedback_useful is True or (feedback_useful is None and reached))`; failure if `feedback_useful is False and grade is False`. Successes are capped per identity (`payload.member_id` else event `agent_id`). Sessions whose event timestamp (or eval `created_at`) is older than `since_by_skill[skill]` are skipped for that skill. Skill ids are recognised by presence in `since_by_skill` (the caller passes every skill it cares about) — no Qdrant access here.

- [ ] **Step 4: Run** — `cd cortex && python -m pytest tests/test_ladder_evidence.py tests/test_owm.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(cortex): skill-ladder evidence reader (shown / reached / applied / graded)`.

---

### Task 6: Decision rules — `cortex/app/skills/ladder_rules.py` (pure)

**Files:**
- Create: `cortex/app/skills/ladder_rules.py`
- Test: `cortex/tests/test_ladder_rules.py`

**Interfaces:**
- Produces constants `PER_AGENT_CAP = 2`, `PROMOTE_MIN_EFFICACY = 0.6`, `DEMOTE_MIN_FAILURES = 3`, `DEMOTE_MAX_EFFICACY = 0.4`, `DEMOTE_MIN_N = 5`, `DUP_THRESHOLD = 0.92`, `TRIAL_CAP_PER_DOMAIN = 10`, `ADMIT_PER_RUN = 20`, `PARKED_FIELDS = ("demoted_at", "ladder_rewrite_requested_at", "trial_expired_at", "superseded_by", "duplicate_of")`; and pure functions returning a `Decision | None`:

```python
@dataclass(frozen=True)
class Decision:
    skill_id: str
    action: str        # "expire" | "demote" | "flag" | "promote" | "admit"
    from_status: str
    to_status: str | None    # None for "flag"
    reason: str
    evidence: dict

def decide_expire(skill_id, status, last_shown_at, ladder_since, now, ttl_days) -> Decision | None   # trial only
def decide_demote(skill_id, status, ev: Evidence, prior_n) -> Decision | None                        # trial only
def decide_flag(skill_id, status, ev: Evidence, prior_n, already_flagged: bool) -> Decision | None    # active only
def decide_promote(skill_id, status, ev: Evidence, prior_n, *, min_successes, min_agents) -> Decision | None  # trial only
def decide_admit(skill_id, payload: dict, *, dup_match: tuple[str, float] | None, domain_trial_count: int) -> Decision | None  # draft only
def default_ladder_since(payload: dict) -> str | None   # approved_at -> stale_reviewed_at -> timestamp
```

- [ ] **Step 1: Failing tests** — one per rule edge: expire only when `status == "trial"` and no shown since max(ladder_since, now−ttl); demote requires `failures ≥ 3 and efficacy < 0.4 and n ≥ 5` on a trial; the same evidence on an active yields a `flag` (and `None` when `already_flagged`); promote requires successes ≥ min, distinct identities ≥ min_agents, failures == 0, efficacy ≥ 0.6 — include the "3 successes from one agent do not promote" case and the "efficacy floor blocks 1-of-1" case (a hypothetical min_successes=1); admit refuses empty trigger/symptoms/steps, `needs_rereview`, any `PARKED_FIELDS` present, a dup match ≥ 0.92 (returns a decision with `action="skip_duplicate"`? — no: returns `None` and the caller marks `duplicate_of`; test that `decide_admit` returns `None` and exposes the reason via a second helper `admit_block_reason(payload, dup_match, domain_trial_count) -> str | None` = `"incomplete" | "rereview" | "parked:<field>" | "duplicate:<id>" | "domain_cap" | None`); domain cap at 10; `default_ladder_since` precedence.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — pure Python, no I/O, no settings import (thresholds passed in or constants). `Decision.evidence` carries `{successes, failures, identities, shown, reached, applied, efficacy}` for the inbox row.

- [ ] **Step 4: Run** — `cd cortex && python -m pytest tests/test_ladder_rules.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(cortex): skill-ladder decision rules (pure)`.

---

### Task 7: The ladder pass — `cortex/app/skills/ladder.py` (shadow) + beat

**Files:**
- Create: `cortex/app/skills/ladder.py`
- Modify: `cortex/app/workers/sleep_cycle.py` (`include` list + beat schedule entry after `owm-scoring`)
- Test: `cortex/tests/test_ladder_pass.py`, `cortex/tests/test_memory_agent.py` or a new `cortex/tests/test_beat_schedule_ladder.py` (schedule pin)

**Interfaces:**
- Consumes: `ladder_evidence.gather`, `ladder_rules.*`, `fleet.ledger` (read-only in shadow), `app.owm._fetch_bridge_statuses`, a sync/async Qdrant client the way `owm.py` obtains `vector` (`_run_owm_impl` builds `VectorClient`/replay redis — copy that construction), Redis keys `skills:ladder:lock` (SETNX, TTL = schedule hours), `skills:ladder:decisions` (LPUSH + LTRIM 500, JSON per decision with `at`), `skills:ladder:last_run` (JSON run record).
- Produces: `async def run_ladder_impl(vector, replay_r, redis_client, settings, *, now=None, events_fn=None) -> dict` and the Celery task `run_skill_ladder` (`@celery_app.task(name="app.skills.ladder.run_skill_ladder")`). Run record: `{"mode": "shadow", "at", "expired", "demoted", "flagged", "promoted", "admitted", "skipped_duplicate", "skipped_capped", "skipped_parked", "stamped_since", "trial_count", "errors": [...]}`. Decision JSON: `{"at", "skill_id", "title", "action", "from", "to", "reason", "evidence", "mode"}`.

- [ ] **Step 1: Failing tests** — a fake Qdrant like `test_autopilot_api.py`'s (filter-matching `scroll`, `set_payload` recording, plus `search` returning a configurable cosine hit for the duplicate check), fakeredis for both redis handles, injected `events_fn` and `now`. Cases:
  1. **Shadow changes nothing:** with a promotable trial skill, a demotable trial, a flaggable active, an expired trial and an admissible draft, after the run **no `set_payload` call contains `skill_status`**, the decisions list has 5 entries with the right actions, `ladder_shadow` was written on each affected skill, `last_run` counts match, and the ledger hash `fleet:ledger:ladder` does not exist.
  2. **`ladder_since` is stamped once** for skills lacking it (value per `default_ladder_since`), and not re-stamped on a second run.
  3. **Parked drafts are never admitted** (`demoted_at` set) → `skipped_parked` and no decision.
  4. **Duplicate draft** (search returns 0.95 against an active) → `duplicate_of` written, `skipped_duplicate` counted, no admit decision.
  5. **Domain cap and per-run cap** honoured (`skipped_capped`).
  6. **Lock:** a second concurrent run returns `{"status": "locked"}`.
  7. **`SKILL_LADDER_ENABLED=False`** → `{"status": "disabled"}` before any I/O.
  8. **`SKILL_LADDER_MODE="enforce"`** → runs as shadow, run record carries `"mode": "shadow"` and `"warning": "enforce mode ships in PR2 — ran shadow"`.
  9. **Fault isolation:** one skill whose evidence gathering raises is recorded in `errors` and the rest still produce decisions.
  10. Beat/include pin: `app.skills.ladder` in `celery_app.conf.include` and a `skill-ladder` entry in `beat_schedule` with `timedelta(hours=settings.SKILL_LADDER_SCHEDULE_HOURS)` (mirror how `test_memory_agent.py` or an existing beat test asserts the schedule).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — order per spec: gather trial/active/draft skills (three scrolls, `memory_type=skill`); stamp `ladder_since` where missing; evidence for trial+active; **expire** trials (`decide_expire` using the skill's own `last_shown_at` from evidence, else `ladder_since`); **demote** trials; **flag** actives; **promote** trials; **admit** drafts oldest-first with dup check (`vector` semantic search of the draft's embedding text against `skill_status ∈ {active, trial}` — reuse `_skill_embed_text` from `skills/api.py` and the client's `search` with a `MatchAny` filter; a hit ≥ `DUP_THRESHOLD` blocks and writes `duplicate_of`). Every decision → LPUSH JSON + `set_payload({"ladder_shadow": {...decision minus evidence bulk, "at"}})`. Nothing else is written. Wrap per-skill work in try/except appending to `errors`. Write `last_run`. The Celery task builds clients like `owm._run_owm_impl` and runs the impl with `asyncio.run` (copy the exact pattern, including the `--pool=solo` note if present).

- [ ] **Step 4: Register** — `sleep_cycle.py`: add `"app.skills.ladder"` to `include` and

```python
            "skill-ladder": {
                "task": "app.skills.ladder.run_skill_ladder",
                "schedule": timedelta(hours=s.SKILL_LADDER_SCHEDULE_HOURS),
            },
```

- [ ] **Step 5: Run** — `cd cortex && python -m pytest tests/test_ladder_pass.py tests/test_ladder_rules.py tests/test_ladder_evidence.py tests/test_memory_agent.py -q` → PASS; also `python -c "import app.skills.ladder"` from `cortex/`.

- [ ] **Step 6: Commit** — `feat(cortex): nightly skill-ladder pass — shadow decisions, ladder_since stamping, duplicate parking`.

---

### Task 8: Autopilot inbox `ladder_proposals` + digest `ladder` block

**Files:**
- Modify: `cortex/app/autopilot/inbox.py`, `cortex/app/autopilot/digest.py`, `cortex/app/autopilot/api.py`
- Test: `cortex/tests/test_autopilot_api.py` (append; update the two pinned `total_actionable` expectations ONLY if the new section contributes a count — see Step 3)

**Interfaces:**
- Produces: inbox section `ladder_proposals` → `{"count": n, "mode": "shadow", "items": [{"id", "title", "action", "from", "to", "reason", "at", "evidence": {"successes","failures","identities","shown","reached","applied","efficacy"}}], "duplicates": [{"id","title","duplicate_of"}]}` built from `skills:ladder:decisions` (latest run only: entries whose `at` ≥ `last_run.at`) plus drafts carrying `duplicate_of`; it **counts toward `total_actionable`** (a proposal is something a human should glance at). Digest gains `"ladder": {"mode", "last_run": {...run record...} | None, "trial_count": int, "reach": {"active": {"shown","reached","rate"}, "trial": {...}}}` where reach numbers come from the ledger-free evidence summary stored in `last_run` (add `reach_by_tier` to the run record in Task 7 if not already there — the pass has the numbers). Read-only, fault-isolated like every other section.

- [ ] **Step 1: Failing tests** — using the file's `mk`/`stores` fixtures: seed `skills:ladder:decisions` and `skills:ladder:last_run` in the main fakeredis, one draft with `duplicate_of`; assert the section shape, that `total_actionable` includes the proposals, that a missing `last_run` yields `count 0, mode "shadow", items []` (not an error), and the digest `ladder` block shape (and `errors["ladder"]` when the redis read raises, rest of digest intact).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** — `inbox.ladder_proposals(redis_client, vector, settings)`; register in `api.py`'s `items` dict after `contested_memories`; if the two existing `total_actionable` pins break because fixtures now include a counted section, adjust the fixture (seed no decisions there) rather than the pin. `digest.py`: after the fleet block, `ladder` block with its own try/except → `errors["ladder"]`.

- [ ] **Step 4: Run** — `cd cortex && python -m pytest tests/test_autopilot_api.py -q` → PASS.

- [ ] **Step 5: Commit** — `feat(cortex): autopilot inbox ladder_proposals + digest ladder block`.

---

### Task 9: Dashboard — trial in the Skills tab, ladder section and block in Autopilot

**Files:**
- Modify: `dashboard/index.html` (Skills filter `<select>` ~1433, `loadSkills` card renderer ~6410–6470, `AUTOPILOT_SECTIONS` + a new `apLadderRow`, `renderAutopilotDigest` gains `apLadderBlock`)
- Test: `tests/test_dashboard_autopilot.py` (append)

**Interfaces:**
- Consumes: Task 8 shapes. Produces: filter option `<option value="trial">Trial</option>`; trial cards show a `TRIAL` badge (`badge-blue`), an *Activate* button (existing `activateSkill`) and a *Back to draft* button (`demoteSkill(id)` → `PATCH {skill_status:'draft'}`, new, Skills tab only); cards show `approved_by` when present and `ladder_shadow.action` as "ladder would: <action>"; Autopilot: `{ key: 'ladder_proposals', title: 'Skill ladder — proposed transitions', cta: 'Open skills', action: "autopilotOpenSkills('trial')", pick: s => s.items || [], row: apLadderRow }` and `apLadderBlock(d.ladder)` under the digest (mode, last run time, counts, trial count, reach rates; `null` rates render `—`). No new fetch, no write verbs inside the sentinels (the new `demoteSkill` lives in the Skills-tab code, outside them).

- [ ] **Step 1: Failing tests** — `_render("apLadderRow", {...})` shows action/from→to/reason/evidence; `_render("renderAutopilotDigest", dict(DIGEST, ladder={...}))` shows mode and counts and `—` for a null rate; the cross-file guard passes once `ladder_proposals` is listed; `TestRoundOneIsReadOnly` unchanged; a source assertion that the Skills filter contains `value="trial"` and that `autopilotOpenSkills('trial')` is a valid filter value (the function sets the select; add `trial` handling if it validates values).

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** (all dynamic values through `apEsc` inside the sentinels, `esc` in the Skills tab).

- [ ] **Step 4: Run** — `python -m pytest tests/test_dashboard_autopilot.py -q` from the worktree root → PASS; grep the sentinel block for write verbs/`fetchJSON(`/`method:` — nothing new.

- [ ] **Step 5: Commit** — `feat(dashboard): trial skills in the Skills tab; ladder proposals and block in Autopilot`.

---

### Task 10: Client — the stop hook asks for skill feedback

**Files:**
- Modify: `client/firekeep_client/hooks/stop.py` (`_MSG`)
- Test: `client/tests/hooks/test_stop.py` (append one assertion; find the test that pins `_MSG` content, e.g. the one asserting "ctx_complete_session" appears)

**Interfaces:**
- Produces: `_MSG` gains, after item 3, the sentence: `"If a recalled skill guided this work, call memory_feedback with its id (useful=true/false) — that is the evidence that promotes or demotes it."`

- [ ] **Step 1: Failing test** — `assert "memory_feedback with its id" in stop._MSG` and that `stop.run({})` in the file's usual fixture returns a `systemMessage` containing it.
- [ ] **Step 2: Run to verify it fails.** — [ ] **Step 3: Edit `_MSG`.** — [ ] **Step 4: Run** `cd client && python -m pytest tests/hooks/test_stop.py tests/test_docs_reference_client_kit.py -q`. — [ ] **Step 5: Commit** — `feat(client): stop hook asks for skill feedback (the ladder's applied signal)`.

---

### Task 11: Documentation

**Files:**
- Modify: `docs/guides/knowledge-and-skills.md` (Skills section: statuses incl. trial, `recallable`, `ladder_since`/`approved_by`, the receipts), `docs/guides/knowledge-autopilot.md` (§4: new sections/blocks; new **§9 "The skill ladder (round 2, rung one — shadow)"** placed before "What unlocks round 2", and one paragraph in that closing section noting this is round 2's first rung with the shadow gate honoured and the OWM exclusion), `docs/guides/cortex-api-endpoints.md` (`GET /skills?status=recallable|trial`, `PATCH approved_by`, inbox/digest additions), `docs/guides/client-kit.md` (stop hook sentence, one line), `CLAUDE.md` (one sentence in the kit paragraph pointing at §9), `README.md` (Knowledge Autopilot row: "…and a shadow-mode skill ladder that records which drafts would earn `trial` and which trials would earn `active`")
- Guards: `client/tests/test_docs_reference_client_kit.py`, `tests/test_guide_size_budget.py`, `tests/test_procedure_docs.py`, `cortex/tests/test_config_ladder.py`

Write §9 from the spec's decisions 1–10 in the guides' voice (dense, decision + evidence). State plainly: shadow changes nothing; what a healthy first fortnight looks like (feedback events on skills > 0, reach rate > 0); solo-Keep caveat on independence; enforce and the rewrite job are PR2. Do not use the word "twin"; no client version number.

- [ ] Run the guards → PASS. Commit — `docs: the skill ladder (shadow) — statuses, receipts, rules, surfaces`.

---

### Task 12: Final verification, branch finish, deploy, first shadow run

- [ ] Full suites: `cd cortex && python -m pytest tests -q`; `cd client && python -m pytest tests -q`; `cd relay && python -m pytest tests -q`; repo guards; ruff on every new/changed Python file (zero findings).
- [ ] Whole-branch review (most capable model) → one fix wave → scoped re-review.
- [ ] Integration per the repo's rule: `origin/main` requires linear history — fast-forward push of the branch tip after CI is green on it (no merge commit). Then `ssh root@<vps-host> 'cd /opt/Firekeep && nohup bash update.sh > /tmp/fk-update.log 2>&1 &'`, verify `/version`, then run the pass once by hand inside the worker: `docker compose exec -T cortex-worker python -c "from app.skills.ladder import run_skill_ladder; print(run_skill_ladder())"` and read `GET /autopilot/inbox` → `ladder_proposals` and `GET /autopilot/digest` → `ladder` on the live Keep. Expected on day one: many `admit` proposals (the 32-draft backlog minus duplicates), zero promotions (no evidence yet) — which is the honest baseline.
