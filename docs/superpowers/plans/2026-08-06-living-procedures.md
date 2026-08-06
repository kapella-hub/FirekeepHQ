# Living Procedures Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a skill an observed procedure — executions open when work matches a step, a load-bearing step left undone raises an advisory, and a nightly pass reports per-step frequency (and, when the outcome signal permits, efficacy) as proposals a human approves.

**Architecture:** Entirely server-side in Cortex. Step specs live on the Qdrant skill point (written only by the create/PATCH path). A denormalised matcher index in Redis is read once per pre-edit call by a new stage inside `AgentGatewayService.decide()`, which records observations live and appends an advisory. A nightly Celery pass aggregates execution records into per-step stats and proposals. No client release: `pre_tool` already prints advisories on an `allow` decision.

**Tech Stack:** Python 3.11, FastAPI, pydantic v2, Qdrant (`qdrant_client`), `redis.asyncio`, Celery, pytest + `fakeredis`.

**Spec:** `docs/superpowers/specs/2026-08-06-living-procedures-design.md` — read §5 (invariants) and §3 (what the audit changed) before starting. The invariants are not style preferences; each exists because the naive version is provably wrong.

## Global Constraints

- **I1** Executions open by a spec MATCHING observed work. Never by an agent declaring it follows a procedure.
- **I2** A step counts as `skipped` only if ≥1 sibling step of the same procedure was observed in the same execution. Absence of evidence is never evidence of absence.
- **I3** `step_specs` live on the Qdrant point (create/PATCH only). Every derived number lives in Redis. No key is written by both.
- **I4** Tier B (efficacy verdicts) requires a knowable outcome. Tier A (frequency) requires none.
- **I5** The pre-edit path performs no Qdrant call and no embed. One Redis GET, memoised in-process.
- **I6** The feature can never block, never raise out of `decide()`, and is inert when `PROCEDURE_ENABLED=false`.
- Redis DB: `proc:*` keys on the **cortex data DB** (`REDIS_URL`, `app.state.redis_client`). Evals are read from the **replay DB** (`RP_REDIS_URL`, `app.state.replay_redis`).
- Every new setting lands in `cortex/app/config.py`, `docker-compose.yml` (**including `cortex-beat`**), `.env.example`, and a drift guard.
- Tests must use filter-honouring doubles. Every pre-existing Qdrant fake in `cortex/tests/` ignores its `scroll_filter`; a filter-dependent test against one passes while doing nothing. Prove each guard by mutation: revert the fix, the test must go red.
- Run tests from `cortex/`: `cd cortex && python -m pytest tests/<file> -v`.

---

## File Structure

**Create:**
- `cortex/app/procedures/__init__.py` — empty package marker.
- `cortex/app/procedures/models.py` — `StepSpec`, `Proposal`, `StepStats`. Pydantic only, no I/O.
- `cortex/app/procedures/match.py` — pure functions: glob matching, missing-load-bearing detection, advisory text. No I/O, no imports from `app.*` outside `models`.
- `cortex/app/procedures/store.py` — every Redis read/write. The only module that knows a key format.
- `cortex/app/procedures/observe.py` — `ProcedureObserver`, the stage called from `decide()`.
- `cortex/app/procedures/harden.py` — the nightly pass (Tier A + Tier B) and its Celery task.
- `cortex/app/procedures/api.py` — `GET /procedures`, `GET /procedures/{skill_id}/executions`, `POST /procedures/proposals/{proposal_id}/dismiss`.

**Modify:** `cortex/app/models.py` (skill models), `cortex/app/skills/api.py` (persist specs, rebuild index), `cortex/app/agent_gateway/models.py` (`AdvisoryCode`), `cortex/app/agent_gateway/service.py` (P1, P2, the stage), `cortex/app/config.py`, `cortex/app/main.py` (wiring + router), `cortex/app/mcp_server.py` (MCP tools), `cortex/app/workers/sleep_cycle.py` (beat), `docker-compose.yml`, `.env.example`, `dashboard/index.html`, `CLAUDE.md`, `cortex/CLAUDE.md`.

**Tests:** `cortex/tests/test_gateway_reconcile_identity.py`, `test_procedure_config.py`, `test_skill_step_specs.py`, `test_procedures_store.py`, `test_procedures_match.py`, `test_procedures_observe.py`, `test_procedures_harden.py`, `test_procedures_api.py`.

---

## Task 1: Prerequisite — the gateway loses session identity, outcome, and every warn

Three defects in `agent_gateway/service.py`, all pre-existing, all inherited by this feature. Fix them first so later tasks build on a gateway that records what it sees.

**P1a** — `record()` emits `session_id=entry["session_id"] if entry else ""` (`service.py:257`), and `entry` (`ag:predict:{action_id}`) is written only when `req.prediction is not None` (`service.py:159`). The shell hook sends no prediction (`client/firekeep_client/hooks/pre_tool.py:142-147`), so every reconcile from Claude Code is filed under the empty string.

**P1b** — the reconcile emit passes no `outcome=`, so `_failure_rate` (`evals/scorers.py:133-139`) never sees it. `post_tool` already computes a real `success` (`post_tool.py:43-58`).

**P2** — `record_policy_decision` is gated `if decision != "allow"` (`service.py:142`), but `warn` was remapped to `allow` at `service.py:102-103` *before* that check, so no warn has ever been recorded.

**Files:**
- Modify: `cortex/app/agent_gateway/service.py`
- Test: `cortex/tests/test_gateway_reconcile_identity.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ag:predict:{action_id}` is now written for **every** action, with `"prediction": None` when absent. `agent.action.reconcile` events carry a real `session_id` and `outcome`. `policy:decisions` records `action="warn"` rows.

- [ ] **Step 1: Write the failing tests**

Create `cortex/tests/test_gateway_reconcile_identity.py`:

```python
"""Guards for three pre-existing agent-gateway defects (plan Task 1).

Each test must go RED if its fix is reverted — that is the only proof these
assert the behaviour rather than the shape.
"""
import pytest

from app.agent_gateway.models import (
    Action, ActionAfterRequest, ActionBeforeRequest, Outcome,
)
from app.agent_gateway.service import AgentGatewayService, RethinkCounter


class _Decision:
    action = "allow"
    risk_score = 0.0
    reasons: list = []
    signals: dict = {}


class _WarnDecision(_Decision):
    action = "warn"
    reasons = ["file_risk: hot file"]


class _Engine:
    def __init__(self, decision):
        self._d = decision

    async def evaluate(self, ctx):
        return self._d


def _service(fakeredis_client, engine, emitted, recorded):
    async def _emit(**kwargs):
        emitted.append(kwargs)

    async def _no(*a, **k):
        return False

    svc = AgentGatewayService(
        policy_engine=engine,
        recent_failure_check=_no,
        fastpath_check=_no,
        session_touched_check=_no,
        replay_emitter=_emit,
        rethink_counter=RethinkCounter(fakeredis_client),
        prediction_redis=fakeredis_client,
        fastpath_redis=fakeredis_client,
        policy_decision_redis=fakeredis_client,
    )
    return svc


@pytest.mark.asyncio
async def test_reconcile_carries_the_session_even_without_a_prediction(monkeypatch):
    """P1a: the shell hook sends no prediction; the reconcile must still be
    filed under the real session, not the empty string."""
    import fakeredis.aioredis as fr

    r = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(r, _Engine(_Decision()), emitted, [])

    before = await svc.decide(ActionBeforeRequest(
        session_id="sess-real", agent_id="ag-1", adapter="shell-hook",
        action=Action(type="edit_file", target="requirements.txt"),
    ))
    await svc.record(ActionAfterRequest(
        action_id=before.action_id,
        outcome=Outcome(success=True, actual_changes=["requirements.txt"]),
    ))

    reconcile = [e for e in emitted if e["event_type"] == "agent.action.reconcile"]
    assert len(reconcile) == 1
    assert reconcile[0]["session_id"] == "sess-real"
    assert reconcile[0]["agent_id"] == "ag-1"


@pytest.mark.asyncio
async def test_reconcile_emits_a_real_outcome():
    """P1b: without outcome=, _failure_rate never sees this event and every
    session evaluates as a success."""
    import fakeredis.aioredis as fr

    r = fr.FakeRedis(decode_responses=True)
    emitted: list = []
    svc = _service(r, _Engine(_Decision()), emitted, [])

    before = await svc.decide(ActionBeforeRequest(
        session_id="s", agent_id="a", adapter="shell-hook",
        action=Action(type="edit_file", target="x.py"),
    ))
    await svc.record(ActionAfterRequest(
        action_id=before.action_id, outcome=Outcome(success=False),
    ))

    reconcile = [e for e in emitted if e["event_type"] == "agent.action.reconcile"]
    assert reconcile[0]["outcome"] == "failure"


@pytest.mark.asyncio
async def test_warn_decisions_are_recorded():
    """P2: warn is remapped to allow before the audit gate, so no warn has ever
    reached policy:decisions."""
    import fakeredis.aioredis as fr

    from app.policy.store import list_policy_decisions

    r = fr.FakeRedis(decode_responses=True)
    svc = _service(r, _Engine(_WarnDecision()), [], [])

    resp = await svc.decide(ActionBeforeRequest(
        session_id="s", agent_id="a", adapter="shell-hook",
        action=Action(type="edit_file", target="hot.py"),
    ))
    assert resp.decision == "allow"  # the wire contract is unchanged

    rows = await list_policy_decisions(r, limit=10)
    assert any(d.get("action") == "warn" for d in rows), rows
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd cortex && python -m pytest tests/test_gateway_reconcile_identity.py -v`
Expected: all three FAIL — the first with `session_id == ""`, the second with `KeyError`/`None` on `outcome`, the third with an empty `rows`.

If `list_policy_decisions` is not the reader's name, open `cortex/app/policy/store.py` and use the actual list function; do not invent one.

- [ ] **Step 3: Fix P1a — always store the action record**

In `cortex/app/agent_gateway/service.py`, change the prediction-store block (around line 158-177) so the record is written for every action:

```python
        # Store the action record for later reconciliation. Written for EVERY
        # action, not only predicted ones: the shell hook sends no prediction,
        # so gating this on `req.prediction is not None` filed every reconcile
        # from Claude Code under session_id="" and made it invisible to
        # get_session_timeline — which also silently zeroed compute_session_eval's
        # predict->reconcile Brier calculation on that path.
        if self.prediction_redis is not None:
            entry = {
                "agent_id": req.agent_id,
                "session_id": req.session_id,
                "prediction": req.prediction.model_dump() if req.prediction else None,
                "adapter": req.adapter,
                "action_type": req.action.type,
                "target": req.action.target,
            }
```

Leave the rest of that block (the `ttl`, the `set`, the `except`) unchanged.

- [ ] **Step 4: Make `record()` tolerate a null prediction**

Still in `service.py`, find where `record()` computes `prediction_match_score` from `entry["prediction"]`. Guard it so a record with `"prediction": None` scores `None` instead of raising:

```python
        stored_prediction = (entry or {}).get("prediction")
        score = _match_score(stored_prediction, req.outcome) if stored_prediction else None
```

Use the existing scoring helper's real name — read the function before editing. The only change required is that a `None` prediction yields `None` rather than an exception.

- [ ] **Step 5: Fix P1b — emit the outcome**

In the reconcile emit (around `service.py:255-265`), add the `outcome` kwarg:

```python
            await self.replay_emitter(
                event_type="agent.action.reconcile",
                session_id=entry["session_id"] if entry else "",
                agent_id=entry["agent_id"] if entry else "",
                payload={
                    "action_id": req.action_id,
                    "outcome": req.outcome.model_dump(),
                    "source": "agent",
                    "prediction_match_score": score,
                },
                # Without this the event carries no top-level outcome, so
                # _failure_rate (evals/scorers.py) never counts it. Measured:
                # no production emitter passed outcome= except Bridge's session
                # lifecycle, so effectively every session evaluated as success.
                outcome="success" if req.outcome.success else "failure",
            )
```

- [ ] **Step 6: Fix P2 — record warns**

In `decide()`, the audit block currently reads `if decision != "allow" and self._policy_decision_redis is not None:`. `raw_action` is already in scope from line 102. Replace the gate and the recorded action:

```python
        # `warn` is remapped to `allow` above (the gateway Decision literal has
        # no warn), so gating on `decision` dropped every warn ever produced by
        # FileRiskRule/SessionHealthRule/RecentFailureRule. Gate on the RAW
        # action and record the warn under its own name; block/rethink keep
        # recording the FINAL decision so rethink->block escalation is visible.
        audit_action = decision if decision != "allow" else raw_action
        if audit_action != "allow" and self._policy_decision_redis is not None:
```

and pass `action=audit_action` to `record_policy_decision` instead of `action=decision`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd cortex && python -m pytest tests/test_gateway_reconcile_identity.py -v`
Expected: 3 passed.

- [ ] **Step 8: Run the existing gateway, policy and evals suites for regressions**

Run: `cd cortex && python -m pytest tests/ -k "gateway or policy or eval" -v`
Expected: all pass. The fastpath cache is gated on the same `entry` and now populates for unpredicted actions — if a fastpath test asserted it stays empty, read it carefully: it was asserting the bug.

- [ ] **Step 9: Commit**

```bash
git add cortex/app/agent_gateway/service.py cortex/tests/test_gateway_reconcile_identity.py
git commit -m "fix(gateway): reconcile lost the session, the outcome, and every warn

Three pre-existing defects, each of which silently zeroed a downstream signal:

- ag:predict:{action_id} was written only when a prediction was present, and
  the shell hook sends none — so every reconcile from Claude Code emitted
  session_id=\"\" and never entered its session's timeline. compute_session_eval's
  predict->reconcile Brier calculation has therefore been computing nothing on
  the only path that produces real traffic.
- The reconcile emit passed no outcome=, so _failure_rate never counted it.
  Grep confirms no production emitter passed outcome= except Bridge's session
  lifecycle, which means effectively every session evaluated as a success and
  OWM's efficacy signal is correspondingly degenerate.
- record_policy_decision was gated on the post-remap decision, and warn is
  remapped to allow one step earlier, so no warn has ever been audited."
```

---

## Task 2: Config surface

**Files:**
- Modify: `cortex/app/config.py`, `docker-compose.yml`, `.env.example`
- Test: `cortex/tests/test_procedure_config.py` (create)

**Interfaces:**
- Produces: `Settings.PROCEDURE_ENABLED`, `PROCEDURE_WARN_ENABLED`, `PROCEDURE_MIN_EXECUTIONS`, `PROCEDURE_PRIOR_N`, `PROCEDURE_EFFICACY_DELTA`, `PROCEDURE_WINDOW_DAYS`, `PROCEDURE_EXEC_TTL_DAYS`, `PROCEDURE_AGENT_CAP`, `PROCEDURE_INDEX_CACHE_SECONDS`, `PROCEDURE_MAX_SPECS`, `PROCEDURE_SCHEDULE_HOURS`.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedure_config.py`. This mirrors `tests/test_decision_config.py` — read that file first and match its helpers rather than inventing new ones.

```python
"""Config drift guards: compose's ${VAR:-default} wins over the code default
whenever .env is silent, so the two must agree — and the beat service must
receive any schedule var, because beat_schedule is built from get_settings()
at import time."""
import re
from pathlib import Path

import pytest

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

DEFAULTS = {
    "PROCEDURE_ENABLED": "false",
    "PROCEDURE_WARN_ENABLED": "true",
    "PROCEDURE_MIN_EXECUTIONS": "5",
    "PROCEDURE_PRIOR_N": "5",
    "PROCEDURE_EFFICACY_DELTA": "0.15",
    "PROCEDURE_WINDOW_DAYS": "30",
    "PROCEDURE_EXEC_TTL_DAYS": "90",
    "PROCEDURE_AGENT_CAP": "5",
    "PROCEDURE_INDEX_CACHE_SECONDS": "30",
    "PROCEDURE_MAX_SPECS": "50",
    "PROCEDURE_SCHEDULE_HOURS": "24",
}


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_setting_exists_with_the_documented_default(name, expected):
    s = Settings()
    actual = getattr(s, name)
    assert str(actual).lower() == expected.lower(), f"{name}={actual!r}"


@pytest.mark.parametrize("name,expected", sorted(DEFAULTS.items()))
def test_compose_default_matches_the_code_default(name, expected):
    hits = re.findall(rf"{name}:\s*\$\{{{name}:-([^}}]*)\}}", COMPOSE)
    assert hits, f"{name} is not plumbed in docker-compose.yml"
    for h in hits:
        assert h.strip().lower() == expected.lower(), f"{name} compose default {h!r}"


@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_documented_in_env_example(name):
    assert name in ENV_EXAMPLE, f"{name} missing from .env.example"


def test_schedule_var_reaches_the_beat_service():
    """beat_schedule is built from get_settings() at import time, so a schedule
    var absent from cortex-beat's environment silently uses the code default
    there while the API uses the .env value."""
    beat = COMPOSE.split("cortex-beat:")[1]
    assert "PROCEDURE_SCHEDULE_HOURS" in beat.split("\n  cortex-")[0]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedure_config.py -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'PROCEDURE_ENABLED'`.

- [ ] **Step 3: Add the settings**

In `cortex/app/config.py`, next to the `DREAM_*` block, matching the surrounding declaration style exactly:

```python
    # --- Living Procedures (docs/superpowers/specs/2026-08-06-living-procedures-design.md)
    # Opt-in. While false the gateway stage is a no-op, the pass self-gates, and
    # the /procedures router is not mounted (the /dreams + /collectors precedent:
    # a disabled deploy 404s rather than serving a disabled-shaped body).
    PROCEDURE_ENABLED: bool = False
    # Observe-without-warning, for a first deployment that wants evidence before
    # it wants opinions.
    PROCEDURE_WARN_ENABLED: bool = True
    # Executions required before an efficacy VERDICT is offered. Frequency
    # reporting (Tier A) needs none. Deliberately an explicit gate: OWM has no
    # such gate because a ranking nudge can be neutral-by-prior, but a proposal
    # shown to a human cannot.
    PROCEDURE_MIN_EXECUTIONS: int = 5
    # Beta prior handed to owm.compute_efficacy (mirrors OWM_PRIOR_N).
    PROCEDURE_PRIOR_N: int = 5
    # How far efficacy(skipped) must fall below efficacy(observed) to call a
    # step load-bearing — and, symmetrically, how close it must stay to call it dead.
    PROCEDURE_EFFICACY_DELTA: float = 0.15
    # Evidence window; matches the 30d eval TTL, beyond which there is nothing to join to.
    PROCEDURE_WINDOW_DAYS: int = 30
    # Execution-record TTL. Deliberately > the window so a window never reaches
    # past its own data.
    PROCEDURE_EXEC_TTL_DAYS: int = 90
    # Max observations one agent_id contributes per step (mirrors OWM_AGENT_CAP):
    # one CI identity in a loop must not decide a team's procedure.
    PROCEDURE_AGENT_CAP: int = 5
    # In-process matcher-index cache. Staleness of this long on a warn is harmless;
    # a Redis GET per customer Edit is not.
    PROCEDURE_INDEX_CACHE_SECONDS: int = 30
    # Hard cap on specs per skill — bounds the index and the pre-edit match loop.
    PROCEDURE_MAX_SPECS: int = 50
    PROCEDURE_SCHEDULE_HOURS: int = 24
```

- [ ] **Step 4: Plumb compose and .env.example**

In `docker-compose.yml`, add all eleven vars to the `environment:` of **every cortex service that reads them** — `cortex-api`, `cortex-mcp`, `cortex-worker`, `cortex-beat` — in the `NAME: ${NAME:-default}` form used by the neighbouring `DREAM_*` entries. Copy the defaults from the table in Step 1 exactly.

In `.env.example`, add a commented block in the same style as the `DREAM_*` block, one line per var with its default.

- [ ] **Step 5: Run to verify it passes**

Run: `cd cortex && python -m pytest tests/test_procedure_config.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/config.py docker-compose.yml .env.example cortex/tests/test_procedure_config.py
git commit -m "feat(procedures): config surface, with compose/.env drift guards"
```

---

## Task 3: Step specs — make steps addressable

Steps are folded into one Markdown string under `## Steps` (`skills/api.py:141-148`) and `parse_skill_content` returns `body` as a blob. Specs are therefore **self-contained** — each carries its own `text` — so nothing depends on parsing.

**Files:**
- Create: `cortex/app/procedures/__init__.py`, `cortex/app/procedures/models.py`
- Modify: `cortex/app/models.py:421-476`, `cortex/app/skills/api.py`
- Test: `cortex/tests/test_skill_step_specs.py` (create)

**Interfaces:**
- Produces: `app.procedures.models.StepSpec` with fields `id: str`, `text: str`, `kind: Literal["file_glob","unobservable"]`, `pattern: str`, `load_bearing: bool`; `SkillRequest.step_specs`, `SkillPatchRequest.step_specs`, `SkillResponse.step_specs` (all `list[StepSpec] | None`); Qdrant payload key `step_specs` holding a list of dicts.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_skill_step_specs.py`. Read `cortex/tests/test_skill_api.py::_make_app` first and reuse that fixture rather than building a new app.

```python
"""Step specs round-trip through create, PATCH and the response projection.

Load-bearing detail: SkillRequest has no model_config, so pydantic's default
extra='ignore' silently DROPS an unknown field. A spec sent to a server without
this task's change is accepted with a 201 and lost. These tests are what make
that impossible to ship twice.
"""
import pytest

from app.procedures.models import StepSpec


def test_a_file_glob_spec_requires_a_pattern():
    with pytest.raises(ValueError):
        StepSpec(text="regen the lock", kind="file_glob", pattern="")


def test_an_unobservable_spec_needs_no_pattern():
    s = StepSpec(text="ask the customer to confirm")
    assert s.kind == "unobservable"
    assert s.pattern == ""


def test_a_blank_id_is_minted_and_a_supplied_id_is_kept():
    minted = StepSpec(text="a")
    assert minted.id and len(minted.id) >= 8
    kept = StepSpec(id="fixed-id", text="a")
    assert kept.id == "fixed-id"


@pytest.mark.asyncio
async def test_create_persists_specs_and_the_response_echoes_them(client, mock_vector):
    body = {
        "trigger": "publishing a client release",
        "symptoms": "teammates get a stale wheel",
        "steps": "1. bump the version\n2. bump the bundled symdex wheel",
        "step_specs": [
            {"text": "bump the version", "kind": "file_glob",
             "pattern": "client/pyproject.toml", "load_bearing": False},
            {"text": "bump the bundled symdex wheel", "kind": "file_glob",
             "pattern": "client/bootstrap/*", "load_bearing": True},
        ],
    }
    resp = await client.post("/skills", json=body)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert len(data["step_specs"]) == 2
    assert all(s["id"] for s in data["step_specs"])

    written = mock_vector._client.upsert.call_args.kwargs["points"][0]
    assert len(written.payload["step_specs"]) == 2
    assert written.payload["step_specs"][1]["load_bearing"] is True


@pytest.mark.asyncio
async def test_patch_replaces_the_spec_list_wholesale(client, mock_vector):
    resp = await client.patch(
        "/skills/skill-1",
        json={"step_specs": [{"text": "only step", "kind": "unobservable"}]},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["step_specs"]) == 1


@pytest.mark.asyncio
async def test_specs_are_not_a_semantic_field(client, mock_vector):
    """Changing specs must NOT re-embed: specs are metadata about the steps,
    not the skill's meaning, and a needless embed on every spec edit puts an
    embedding-backend outage in the write path."""
    await client.patch(
        "/skills/skill-1",
        json={"step_specs": [{"text": "s", "kind": "unobservable"}]},
    )
    mock_vector._embed.assert_not_called()


@pytest.mark.asyncio
async def test_more_than_the_cap_is_rejected(client):
    specs = [{"text": f"step {i}", "kind": "unobservable"} for i in range(51)]
    resp = await client.post("/skills", json={
        "trigger": "t", "symptoms": "s", "steps": "x", "step_specs": specs,
    })
    assert resp.status_code == 422
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_skill_step_specs.py -v`
Expected: FAIL on `ModuleNotFoundError: app.procedures`.

- [ ] **Step 3: Create the package and the model**

Create `cortex/app/procedures/__init__.py` (empty file).

Create `cortex/app/procedures/models.py`:

```python
"""Living Procedures models. Pydantic only — no I/O, no app imports."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

StepKind = Literal["file_glob", "unobservable"]

# Bounds the pre-edit match loop and the denormalised index. A pattern longer
# than this is not a glob anyone wrote on purpose.
MAX_PATTERN_CHARS = 200
MAX_TEXT_CHARS = 500


class StepSpec(BaseModel):
    """One step of a procedure, made addressable.

    SELF-CONTAINED by design: it carries its own `text` rather than an index
    into the skill's `## Steps` markdown, because that markdown is one blob
    (skills/api.py folds `steps` into `content`; parse_skill_content returns
    `body` undivided). An index would desync the moment a human PATCHes
    `content`, silently and undetectably.
    """

    id: str = ""
    text: str = Field(max_length=MAX_TEXT_CHARS)
    kind: StepKind = "unobservable"
    pattern: str = Field(default="", max_length=MAX_PATTERN_CHARS)
    load_bearing: bool = False

    @model_validator(mode="after")
    def _check(self) -> "StepSpec":
        if not self.id:
            self.id = uuid.uuid4().hex[:12]
        if self.kind == "file_glob" and not self.pattern.strip():
            raise ValueError("kind='file_glob' requires a non-empty pattern")
        if self.kind != "file_glob" and self.pattern:
            self.pattern = ""
        return self
```

- [ ] **Step 4: Add the fields to the skill models**

In `cortex/app/models.py`, add to `SkillRequest` (after `status`), to `SkillPatchRequest`, and to `SkillResponse`:

```python
    # Living Procedures: optional per-step matchers, compiled by the AUTHORING
    # agent (the server runs no LLM for this). Absent => the skill is simply not
    # an observed procedure. See docs/superpowers/specs/2026-08-06-living-procedures-design.md
    step_specs: list[StepSpec] | None = None
```

Add `from app.procedures.models import StepSpec` to the imports, and to `SkillRequest` add the cap validator:

```python
    @field_validator("step_specs")
    @classmethod
    def _cap_specs(cls, v):
        if v is not None and len(v) > 50:
            raise ValueError("at most 50 step_specs per skill")
        return v
```

Repeat the same validator on `SkillPatchRequest`. (The literal 50 matches `PROCEDURE_MAX_SPECS`; a validator cannot read Settings, so the cap is stated in both places and Task 2's drift guard is what keeps them honest — add `PROCEDURE_MAX_SPECS` to that test's `DEFAULTS` if it is not already there.)

- [ ] **Step 5: Persist on create**

In `cortex/app/skills/api.py::create_skill`, after the `payload` dict is built and before the tenancy block:

```python
        if req.step_specs:
            payload["step_specs"] = [s.model_dump() for s in req.step_specs]
```

and add `step_specs=payload.get("step_specs")` to the returned `SkillResponse(...)`.

- [ ] **Step 6: Persist on PATCH, without re-embedding**

In `patch_skill`, alongside the other `if req.X is not None:` blocks:

```python
        if req.step_specs is not None:
            # NOT in SEMANTIC_PATCH_FIELDS: specs describe how to OBSERVE the
            # steps, not what the skill means, so they must not trigger a
            # re-embed — that would put an embedding-backend outage in the path
            # of every spec edit, and the re-embed path fails loud by design.
            updates["step_specs"] = [s.model_dump() for s in req.step_specs]
```

Do **not** add `step_specs` to `SEMANTIC_PATCH_FIELDS`.

- [ ] **Step 7: Project it in the response**

In `_point_to_response`, add `step_specs=payload.get("step_specs")` to the constructed `SkillResponse`.

- [ ] **Step 8: Run the tests**

Run: `cd cortex && python -m pytest tests/test_skill_step_specs.py tests/test_skill_api.py -v`
Expected: all pass, **including all pre-existing `test_skill_api.py` tests unedited** — that is the contract check that the existing surface was not broken.

- [ ] **Step 9: Commit**

```bash
git add cortex/app/procedures/ cortex/app/models.py cortex/app/skills/api.py cortex/tests/test_skill_step_specs.py
git commit -m "feat(procedures): step_specs make a skill's steps addressable

Self-contained by design — each spec carries its own text rather than an index
into the ## Steps markdown, which is one undivided blob a human PATCH can
renumber silently. Not a semantic field: editing specs must not re-embed."
```

---

## Task 4: The Redis store

The only module that knows a key format. Everything else calls it.

**Files:**
- Create: `cortex/app/procedures/store.py`
- Test: `cortex/tests/test_procedures_store.py` (create)

**Interfaces:**
- Consumes: `StepSpec` (Task 3).
- Produces:
  - `INDEX_KEY = "proc:index"`, `exec_key(session_id, skill_id) -> str`
  - `async def rebuild_index(vector, redis_client, settings) -> int`
  - `async def load_index(redis_client) -> list[dict]`
  - `async def record_observation(redis_client, settings, *, session_id, skill_id, step_id, action_id, target, agent_id, adapter) -> str`
  - `async def get_execution(redis_client, session_id, skill_id) -> dict | None`
  - `async def claim_warn(redis_client, settings, *, session_id, skill_id, step_id) -> bool`
  - `async def iter_executions(redis_client) -> list[dict]`
  - `async def write_step_stats(redis_client, settings, skill_id, stats: dict) -> None`
  - `async def get_step_stats(redis_client, skill_id) -> dict`
  - `async def write_proposals(redis_client, skill_id, proposals: list[dict]) -> None`
  - `async def list_proposals(redis_client, skill_id: str | None = None) -> list[dict]`
  - `async def dismiss_proposal(redis_client, proposal_id: str) -> bool`
  - Index entry shape: `{"skill_id", "skill_trigger", "step_id", "step_text", "pattern", "load_bearing", "order"}`

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_store.py`:

```python
"""Redis layer for Living Procedures. Uses a filter-HONOURING Qdrant double —
every pre-existing fake in this suite ignores scroll_filter, so a test against
one would pass while the index silently included drafts and non-skills."""
import json

import pytest
import fakeredis.aioredis as fr

from app.procedures import store


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FilterHonouringQdrant:
    """Applies must-FieldCondition MatchValue filters for real."""

    def __init__(self, points):
        self._points = points

    async def scroll(self, *, collection_name, scroll_filter=None, limit=1000, **kw):
        pts = self._points
        if scroll_filter is not None:
            for cond in scroll_filter.must or []:
                key = cond.key
                want = cond.match.value
                pts = [p for p in pts if (p.payload or {}).get(key) == want]
        return pts[:limit], None


class _Vector:
    def __init__(self, points):
        self._client = _FilterHonouringQdrant(points)


class _Settings:
    QDRANT_COLLECTION = "c"
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50


@pytest.fixture
def redis_client():
    return fr.FakeRedis(decode_responses=True)


def _skill(pid, trigger, specs, status="active", mtype="skill"):
    return _Point(pid, {
        "memory_type": mtype, "skill_status": status,
        "trigger": trigger, "step_specs": specs,
    })


@pytest.mark.asyncio
async def test_index_holds_only_active_skills_file_glob_specs(redis_client):
    vector = _Vector([
        _skill("s1", "release", [
            {"id": "a", "text": "bump", "kind": "file_glob", "pattern": "p.toml", "load_bearing": True},
            {"id": "b", "text": "ask", "kind": "unobservable", "pattern": "", "load_bearing": False},
        ]),
        _skill("s2", "draft one", [
            {"id": "c", "text": "x", "kind": "file_glob", "pattern": "*.py", "load_bearing": False},
        ], status="draft"),
    ])
    n = await store.rebuild_index(vector, redis_client, _Settings())
    assert n == 1
    idx = await store.load_index(redis_client)
    assert [e["step_id"] for e in idx] == ["a"]
    assert idx[0]["skill_id"] == "s1"
    assert idx[0]["order"] == 0
    assert idx[0]["load_bearing"] is True


@pytest.mark.asyncio
async def test_order_is_the_spec_list_position_not_the_filtered_position(redis_client):
    """'Earlier step' means earlier in step_specs. An unobservable step still
    occupies its position — dropping it from the index must not renumber the
    observable ones, or the earlier-step check compares the wrong steps."""
    vector = _Vector([_skill("s1", "t", [
        {"id": "a", "text": "0", "kind": "unobservable", "pattern": "", "load_bearing": False},
        {"id": "b", "text": "1", "kind": "file_glob", "pattern": "x", "load_bearing": False},
    ])])
    await store.rebuild_index(vector, redis_client, _Settings())
    idx = await store.load_index(redis_client)
    assert idx[0]["step_id"] == "b"
    assert idx[0]["order"] == 1


@pytest.mark.asyncio
async def test_load_index_on_a_cold_store_is_empty_not_an_error(redis_client):
    assert await store.load_index(redis_client) == []


@pytest.mark.asyncio
async def test_observation_opens_then_extends_one_execution(redis_client):
    s = _Settings()
    e1 = await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="a",
        action_id="act1", target="p.toml", agent_id="ag", adapter="shell-hook")
    e2 = await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="b",
        action_id="act2", target="q.txt", agent_id="ag", adapter="shell-hook")
    assert e1 == e2  # same execution
    ex = await store.get_execution(redis_client, "sess", "s1")
    assert set(ex["observed"]) == {"a", "b"}
    assert ex["observed"]["a"][0]["action_id"] == "act1"
    assert await redis_client.ttl(store.exec_key("sess", "s1")) > 0


@pytest.mark.asyncio
async def test_warn_is_claimed_once_per_execution_and_step(redis_client):
    s = _Settings()
    await store.record_observation(
        redis_client, s, session_id="sess", skill_id="s1", step_id="a",
        action_id="x", target="t", agent_id="ag", adapter="shell-hook")
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="z") is True
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="z") is False
    assert await store.claim_warn(redis_client, s, session_id="sess", skill_id="s1", step_id="y") is True


@pytest.mark.asyncio
async def test_proposals_round_trip_and_dismiss(redis_client):
    await store.write_proposals(redis_client, "s1", [
        {"id": "p1", "kind": "dead_step", "step_id": "a", "detail": "d"},
    ])
    got = await store.list_proposals(redis_client, "s1")
    assert len(got) == 1
    assert await store.dismiss_proposal(redis_client, "p1") is True
    assert await store.list_proposals(redis_client, "s1") == []
    assert await store.dismiss_proposal(redis_client, "nope") is False


@pytest.mark.asyncio
async def test_rebuild_replaces_rather_than_appends(redis_client):
    s = _Settings()
    v1 = _Vector([_skill("s1", "t", [
        {"id": "a", "text": "x", "kind": "file_glob", "pattern": "1", "load_bearing": False}])])
    await store.rebuild_index(v1, redis_client, s)
    v2 = _Vector([_skill("s1", "t", [
        {"id": "b", "text": "y", "kind": "file_glob", "pattern": "2", "load_bearing": False}])])
    await store.rebuild_index(v2, redis_client, s)
    idx = await store.load_index(redis_client)
    assert [e["step_id"] for e in idx] == ["b"]


@pytest.mark.asyncio
async def test_a_corrupt_index_reads_as_empty_never_raises(redis_client):
    await redis_client.set(store.INDEX_KEY, "{not json")
    assert await store.load_index(redis_client) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_store.py -v`
Expected: FAIL on `ImportError: cannot import name 'store'`.

- [ ] **Step 3: Implement the store**

Create `cortex/app/procedures/store.py`:

```python
"""Every Redis read and write for Living Procedures.

Keys live on the CORTEX DATA db (REDIS_URL), not the replay db: these are
feature state, not trace events, and they must not inherit replay's retention
conventions. Evals are read from the replay db by the harden pass instead.

I3: nothing here writes to Qdrant. Step specs are owned by the skills PATCH
path alone, because a semantic PATCH does retrieve->merge->re-embed->full
upsert (skills/api.py) and would silently discard a concurrent set_payload.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchValue

logger = logging.getLogger(__name__)

INDEX_KEY = "proc:index"
_EXEC_PREFIX = "proc:exec:"
_EXEC_INDEX = "proc:exec:__index"
_STATS_PREFIX = "proc:stats:"
_PROPOSALS_PREFIX = "proc:proposals:"
_PROPOSAL_OWNER = "proc:proposal_owner"


def exec_key(session_id: str, skill_id: str) -> str:
    return f"{_EXEC_PREFIX}{session_id}:{skill_id}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def rebuild_index(vector, redis_client, settings) -> int:
    """Denormalise every active skill's file_glob specs into one key.

    I5: the pre-edit path must not touch Qdrant, so the scan happens here — on
    write and on the nightly pass — never on the hot path.
    """
    points, _ = await vector._client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=[
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value="active")),
        ]),
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    entries: list[dict[str, Any]] = []
    for p in points:
        payload = p.payload or {}
        specs = payload.get("step_specs") or []
        if not isinstance(specs, list):
            continue
        max_specs = int(getattr(settings, "PROCEDURE_MAX_SPECS", 50))
        for order, spec in enumerate(specs[:max_specs]):
            if not isinstance(spec, dict):
                continue
            if spec.get("kind") != "file_glob":
                continue  # unobservable steps are not matchable — but see `order`
            pattern = (spec.get("pattern") or "").strip()
            step_id = spec.get("id")
            if not pattern or not step_id:
                continue
            entries.append({
                "skill_id": str(p.id),
                "skill_trigger": payload.get("trigger") or "",
                "step_id": step_id,
                "step_text": spec.get("text") or "",
                "pattern": pattern,
                "load_bearing": bool(spec.get("load_bearing")),
                # POSITION IN THE FULL SPEC LIST, not in the filtered list:
                # "earlier step" is defined over step_specs, and renumbering
                # here would make the earlier-step check compare wrong steps.
                "order": order,
            })
    await redis_client.set(INDEX_KEY, json.dumps(entries))
    return len(entries)


async def load_index(redis_client) -> list[dict[str, Any]]:
    """Never raises: a corrupt or absent index degrades to no matching."""
    try:
        raw = await redis_client.get(INDEX_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("procedure index unreadable: %s", exc)
        return []


async def record_observation(
    redis_client, settings, *, session_id: str, skill_id: str, step_id: str,
    action_id: str, target: str, agent_id: str, adapter: str,
) -> str:
    """Open-or-extend the execution for (session, skill). Returns its exec_id."""
    key = exec_key(session_id, skill_id)
    raw = await redis_client.hgetall(key)
    if raw:
        exec_id = raw.get("exec_id") or f"proc_{uuid.uuid4().hex[:12]}"
        observed = json.loads(raw.get("observed") or "{}")
    else:
        exec_id = f"proc_{uuid.uuid4().hex[:12]}"
        observed = {}
        await redis_client.hset(key, mapping={
            "exec_id": exec_id, "skill_id": skill_id, "session_id": session_id,
            "agent_id": agent_id,
            # NOTE: `adapter` is a TRANSPORT class (shell-hook|mcp|rest), not a
            # runtime — pre_tool hardcodes "shell-hook" on every runtime. It is
            # stored for diagnostics only and must never be used to infer
            # observability; I2 is what does that.
            "adapter": adapter,
            "opened_at": _now(), "warned": "{}",
        })
        await redis_client.sadd(_EXEC_INDEX, key)
    observed.setdefault(step_id, []).append(
        {"action_id": action_id, "target": target, "ts": _now()}
    )
    await redis_client.hset(key, mapping={
        "observed": json.dumps(observed), "last_seen_at": _now(),
    })
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    await redis_client.expire(key, ttl)
    return exec_id


async def get_execution(redis_client, session_id: str, skill_id: str) -> dict | None:
    raw = await redis_client.hgetall(exec_key(session_id, skill_id))
    if not raw:
        return None
    out = dict(raw)
    out["observed"] = json.loads(raw.get("observed") or "{}")
    out["warned"] = json.loads(raw.get("warned") or "{}")
    return out


async def claim_warn(redis_client, settings, *, session_id: str, skill_id: str,
                     step_id: str) -> bool:
    """True exactly once per (execution, step) — the RethinkCounter shape."""
    key = f"{exec_key(session_id, skill_id)}:warned:{step_id}"
    ttl = int(getattr(settings, "PROCEDURE_EXEC_TTL_DAYS", 90)) * 86400
    return bool(await redis_client.set(key, _now(), nx=True, ex=ttl))


async def iter_executions(redis_client) -> list[dict[str, Any]]:
    """All live executions. Members whose key has expired are pruned from the
    index as they are found — the set has no TTL of its own."""
    out: list[dict[str, Any]] = []
    members = await redis_client.smembers(_EXEC_INDEX)
    for key in members:
        raw = await redis_client.hgetall(key)
        if not raw:
            await redis_client.srem(_EXEC_INDEX, key)
            continue
        rec = dict(raw)
        rec["observed"] = json.loads(raw.get("observed") or "{}")
        out.append(rec)
    return out


async def write_step_stats(redis_client, settings, skill_id: str, stats: dict) -> None:
    await redis_client.set(f"{_STATS_PREFIX}{skill_id}", json.dumps(stats))


async def get_step_stats(redis_client, skill_id: str) -> dict:
    try:
        raw = await redis_client.get(f"{_STATS_PREFIX}{skill_id}")
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


async def write_proposals(redis_client, skill_id: str, proposals: list[dict]) -> None:
    """Replaces this skill's proposals wholesale. A proposal with no supporting
    evidence in the window must DISAPPEAR (OWM's stale-reset shape) rather than
    stand forever — verdicts decay to neutral, they do not ratchet."""
    old = await list_proposals(redis_client, skill_id)
    for p in old:
        await redis_client.hdel(_PROPOSAL_OWNER, p["id"])
    await redis_client.set(f"{_PROPOSALS_PREFIX}{skill_id}", json.dumps(proposals))
    for p in proposals:
        await redis_client.hset(_PROPOSAL_OWNER, p["id"], skill_id)


async def list_proposals(redis_client, skill_id: str | None = None) -> list[dict]:
    if skill_id is not None:
        try:
            raw = await redis_client.get(f"{_PROPOSALS_PREFIX}{skill_id}")
            return json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []
    out: list[dict] = []
    owners = await redis_client.hgetall(_PROPOSAL_OWNER)
    for sid in set(owners.values()):
        out.extend(await list_proposals(redis_client, sid))
    return out


async def dismiss_proposal(redis_client, proposal_id: str) -> bool:
    skill_id = await redis_client.hget(_PROPOSAL_OWNER, proposal_id)
    if not skill_id:
        return False
    remaining = [p for p in await list_proposals(redis_client, skill_id)
                 if p.get("id") != proposal_id]
    await redis_client.set(f"{_PROPOSALS_PREFIX}{skill_id}", json.dumps(remaining))
    await redis_client.hdel(_PROPOSAL_OWNER, proposal_id)
    return True
```

- [ ] **Step 4: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_store.py -v`
Expected: all pass.

- [ ] **Step 5: Prove the filter guard by mutation**

Temporarily delete the `skill_status` `FieldCondition` from `rebuild_index`. Run
`tests/test_procedures_store.py::test_index_holds_only_active_skills_file_glob_specs`.
Expected: **FAIL** (the draft skill's spec appears). Restore the condition and confirm it passes again. If it passed with the condition removed, the double is not honouring filters — fix the double before continuing.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/procedures/store.py cortex/tests/test_procedures_store.py
git commit -m "feat(procedures): Redis store — index, executions, stats, proposals"
```

---

## Task 5: Pure matching

**Files:**
- Create: `cortex/app/procedures/match.py`
- Test: `cortex/tests/test_procedures_match.py` (create)

**Interfaces:**
- Consumes: index entries from `store.load_index`.
- Produces:
  - `def match_target(index: list[dict], target: str) -> list[dict]`
  - `def missing_load_bearing(index: list[dict], skill_id: str, matched_order: int, observed_step_ids: set[str]) -> list[dict]`
  - `def advisory_text(entry: dict, missing: dict, stats: dict | None) -> str`

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_match.py`:

```python
"""Pure matching. No I/O — these are the functions that must never raise on the
blocking pre-edit path (I6)."""
import pytest

from app.procedures import match


def _e(skill="s1", step="a", pattern="*.py", order=0, load_bearing=False, text="t"):
    return {"skill_id": skill, "skill_trigger": "trig", "step_id": step,
            "step_text": text, "pattern": pattern, "load_bearing": load_bearing,
            "order": order}


def test_matches_a_glob():
    idx = [_e(pattern="requirements.txt"), _e(step="b", pattern="*.md")]
    got = match.match_target(idx, "requirements.txt")
    assert [g["step_id"] for g in got] == ["a"]


def test_matches_a_path_suffix_so_absolute_targets_work():
    """pre_tool sends whatever path the tool was given — often absolute. A
    pattern authored as a repo-relative glob must still match."""
    idx = [_e(pattern="client/pyproject.toml")]
    got = match.match_target(idx, "E:/Documents/Projects/Firekeep/client/pyproject.toml")
    assert len(got) == 1


def test_backslash_paths_match_forward_slash_patterns():
    idx = [_e(pattern="cortex/app/*.py")]
    assert match.match_target(idx, r"cortex\app\main.py")


def test_a_hostile_pattern_cannot_raise():
    for bad in ["[", "**[", "\\", "a" * 5000, "../../*", "(?i)x"]:
        assert match.match_target([_e(pattern=bad)], "anything.py") == []


def test_no_match_on_empty_target():
    assert match.match_target([_e()], "") == []


def test_missing_load_bearing_only_looks_earlier():
    idx = [
        _e(step="a", order=0, load_bearing=True),
        _e(step="b", order=1, load_bearing=False),
        _e(step="c", order=2, load_bearing=True),
    ]
    missing = match.missing_load_bearing(idx, "s1", matched_order=1, observed_step_ids=set())
    assert [m["step_id"] for m in missing] == ["a"]


def test_an_observed_step_is_not_missing():
    idx = [_e(step="a", order=0, load_bearing=True), _e(step="b", order=1)]
    assert match.missing_load_bearing(idx, "s1", 1, {"a"}) == []


def test_other_skills_steps_are_never_considered():
    idx = [_e(skill="other", step="x", order=0, load_bearing=True), _e(step="b", order=1)]
    assert match.missing_load_bearing(idx, "s1", 1, set()) == []


def test_advisory_text_without_stats_states_no_numbers():
    txt = match.advisory_text(_e(), {"step_id": "a", "step_text": "regen the lock"}, None)
    assert "regen the lock" in txt
    assert "%" not in txt and " of " not in txt


def test_advisory_text_with_stats_quotes_them():
    stats = {"a": {"observed": 11, "skipped": 4, "executions": 15}}
    txt = match.advisory_text(_e(), {"step_id": "a", "step_text": "regen the lock"}, stats)
    assert "11" in txt and "15" in txt
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_match.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement**

Create `cortex/app/procedures/match.py`:

```python
"""Pure step matching. No I/O, no exceptions — this runs on the blocking
pre-edit path, where a raise costs a customer's edit and a slow call costs the
whole gate (the client's own timeout turns it into a silent skip)."""

from __future__ import annotations

import fnmatch
from typing import Any


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _matches(pattern: str, target: str) -> bool:
    """Glob against the full path and against every path suffix.

    Suffix matching is what lets a repo-relative pattern authored by a human
    match the absolute path a tool actually reports.
    """
    try:
        p = _norm(pattern)
        t = _norm(target)
        if not p or not t:
            return False
        if fnmatch.fnmatch(t, p):
            return True
        parts = t.split("/")
        for i in range(1, len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), p):
                return True
        return False
    except Exception:  # noqa: BLE001 — a hostile pattern must never raise here
        return False


def match_target(index: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
    if not target:
        return []
    return [e for e in index if _matches(e.get("pattern", ""), target)]


def missing_load_bearing(
    index: list[dict[str, Any]], skill_id: str, matched_order: int,
    observed_step_ids: set[str],
) -> list[dict[str, Any]]:
    """Load-bearing steps of THIS skill, earlier in the spec list than the one
    just matched, with no observation in this execution."""
    return [
        e for e in index
        if e.get("skill_id") == skill_id
        and e.get("load_bearing")
        and e.get("order", 0) < matched_order
        and e.get("step_id") not in observed_step_ids
    ]


def advisory_text(entry: dict[str, Any], missing: dict[str, Any],
                  stats: dict[str, Any] | None) -> str:
    """One pre-formatted line. The client joins only `message` and flattens
    advisories with '; ' (pre_tool.py), so anything a human needs must be here.

    Numbers are quoted ONLY when the hardening pass has earned them. With no
    stats the message says what is missing and invents nothing.
    """
    trigger = (entry.get("skill_trigger") or "this procedure").strip()
    step_text = (missing.get("step_text") or missing.get("step_id") or "an earlier step")
    base = (f"Procedure \"{trigger}\" — step \"{step_text}\" has no evidence "
            f"in this session.")
    row = (stats or {}).get(missing.get("step_id")) or {}
    observed = row.get("observed")
    executions = row.get("executions")
    if isinstance(observed, int) and isinstance(executions, int) and executions:
        base += f" Present in {observed} of {executions} recorded executions."
    return base
```

- [ ] **Step 4: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_match.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add cortex/app/procedures/match.py cortex/tests/test_procedures_match.py
git commit -m "feat(procedures): pure glob matching and advisory text"
```

---

## Task 6: The gateway stage

**Files:**
- Create: `cortex/app/procedures/observe.py`
- Modify: `cortex/app/agent_gateway/models.py`, `cortex/app/agent_gateway/service.py`, `cortex/app/main.py`
- Test: `cortex/tests/test_procedures_observe.py` (create)

**Interfaces:**
- Consumes: `store.load_index`, `store.record_observation`, `store.claim_warn`, `store.get_execution`, `store.get_step_stats`, `match.*`.
- Produces: `ProcedureObserver(get_redis, settings_fn)` with `async def observe(self, req) -> list[Advisory]`; `AdvisoryCode` gains `"procedure_step_missing"`; `AgentGatewayService.__init__` gains `procedure_observer=None`.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_observe.py`:

```python
"""The gateway stage: recognise, observe, warn. I5 and I6 are asserted here."""
import pytest
import fakeredis.aioredis as fr

from app.agent_gateway.models import Action, ActionBeforeRequest
from app.procedures import store
from app.procedures.observe import ProcedureObserver


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_WARN_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_INDEX_CACHE_SECONDS = 0  # no memoisation in tests
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


class _ExplodingVector:
    """I5: the pre-edit path must never touch Qdrant."""

    def __getattr__(self, name):
        raise AssertionError(f"the pre-edit path touched Qdrant: {name}")


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


def _observer(r, settings=None):
    s = settings or _Settings()
    return ProcedureObserver(get_redis=lambda: r, settings_fn=lambda: s)


def _req(target="requirements.txt", type_="edit_file", session="sess"):
    return ActionBeforeRequest(
        session_id=session, agent_id="ag", adapter="shell-hook",
        action=Action(type=type_, target=target),
    )


async def _seed(r, load_bearing=True):
    import json
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "a",
         "step_text": "regenerate the lock", "pattern": "*.lock",
         "load_bearing": load_bearing, "order": 0},
        {"skill_id": "s1", "skill_trigger": "dependency change", "step_id": "b",
         "step_text": "edit requirements", "pattern": "requirements.txt",
         "load_bearing": False, "order": 1},
    ]))


@pytest.mark.asyncio
async def test_a_match_opens_an_execution_and_warns(r):
    await _seed(r)
    advisories = await _observer(r).observe(_req())
    assert len(advisories) == 1
    assert advisories[0].code == "procedure_step_missing"
    assert "regenerate the lock" in advisories[0].message
    assert advisories[0].evidence_event_id  # the exec_id receipt
    ex = await store.get_execution(r, "sess", "s1")
    assert "b" in ex["observed"]


@pytest.mark.asyncio
async def test_the_same_step_warns_only_once_per_execution(r):
    await _seed(r)
    obs = _observer(r)
    assert len(await obs.observe(_req())) == 1
    assert await obs.observe(_req()) == []


@pytest.mark.asyncio
async def test_an_observed_earlier_step_produces_no_warning(r):
    await _seed(r)
    obs = _observer(r)
    await obs.observe(_req(target="poetry.lock"))   # step a
    assert await obs.observe(_req()) == []          # step b: a is satisfied


@pytest.mark.asyncio
async def test_a_non_load_bearing_earlier_step_never_warns(r):
    await _seed(r, load_bearing=False)
    assert await _observer(r).observe(_req()) == []


@pytest.mark.asyncio
async def test_non_edit_actions_are_ignored(r):
    await _seed(r)
    assert await _observer(r).observe(_req(target="rm -rf *.lock", type_="run_command")) == []


@pytest.mark.asyncio
async def test_an_unknown_session_records_nothing(r):
    """An execution that cannot be joined to an outcome is not evidence."""
    await _seed(r)
    for sid in ("", "unknown"):
        assert await _observer(r).observe(_req(session=sid)) == []


@pytest.mark.asyncio
async def test_disabled_does_nothing_at_all(r):
    await _seed(r)

    class Off(_Settings):
        PROCEDURE_ENABLED = False

    assert await _observer(r, Off()).observe(_req()) == []
    assert await store.get_execution(r, "sess", "s1") is None


@pytest.mark.asyncio
async def test_warn_disabled_still_observes(r):
    await _seed(r)

    class NoWarn(_Settings):
        PROCEDURE_WARN_ENABLED = False

    assert await _observer(r, NoWarn()).observe(_req()) == []
    assert await store.get_execution(r, "sess", "s1") is not None


@pytest.mark.asyncio
async def test_a_dead_redis_never_raises(r):
    await _seed(r)

    class Dead:
        def __getattr__(self, name):
            async def boom(*a, **k):
                raise ConnectionError("redis is down")
            return boom

    obs = ProcedureObserver(get_redis=lambda: Dead(), settings_fn=lambda: _Settings())
    assert await obs.observe(_req()) == []


@pytest.mark.asyncio
async def test_the_stage_never_touches_qdrant(r):
    """I5. ProcedureObserver has no vector client at all — this asserts the
    constructor signature keeps it that way."""
    import inspect

    params = inspect.signature(ProcedureObserver.__init__).parameters
    assert "vector" not in params and "get_vector" not in params
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_observe.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Add the advisory code**

In `cortex/app/agent_gateway/models.py`, append to the `AdvisoryCode` Literal:

```python
    "file_risk",
    # Living Procedures. AdvisoryCode is a CLOSED Literal, so constructing an
    # Advisory with an unlisted code raises inside decide() — which is not
    # wrapped at that site, 500s the before-call, and makes the client fail open,
    # silently disabling the whole gate.
    "procedure_step_missing",
]
```

- [ ] **Step 4: Implement the observer**

Create `cortex/app/procedures/observe.py`:

```python
"""The pre-edit stage: recognise, observe, warn.

Deliberately NOT a policy rule (spec §3). PolicyContext carries no action type,
so a rule cannot distinguish a file path from a run_command target before
globbing; ActionBeforeRequest can. It also keeps the blast radius local — a
failure here cannot remove the policy rule set.
"""

from __future__ import annotations

import logging
import time

from app.agent_gateway.models import ActionBeforeRequest, Advisory
from app.procedures import match, store

logger = logging.getLogger(__name__)

# A session id we cannot join to an outcome is not evidence; recording under it
# would manufacture executions that can never be evaluated.
_UNUSABLE_SESSIONS = {"", "unknown", "none", "null"}


class ProcedureObserver:
    def __init__(self, get_redis, settings_fn):
        self._get_redis = get_redis
        self._settings_fn = settings_fn
        self._index: list[dict] = []
        self._index_at: float = 0.0

    async def _load_index(self, redis_client, settings) -> list[dict]:
        ttl = float(getattr(settings, "PROCEDURE_INDEX_CACHE_SECONDS", 30) or 0)
        now = time.monotonic()
        if ttl and self._index_at and (now - self._index_at) < ttl:
            return self._index
        self._index = await store.load_index(redis_client)
        self._index_at = now
        return self._index

    async def observe(self, req: ActionBeforeRequest) -> list[Advisory]:
        """Never raises. Returns [] whenever anything is missing or off."""
        try:
            settings = self._settings_fn()
            if not getattr(settings, "PROCEDURE_ENABLED", False):
                return []
            if req.action.type != "edit_file":
                return []
            if (req.session_id or "").strip().lower() in _UNUSABLE_SESSIONS:
                return []

            redis_client = self._get_redis()
            if redis_client is None:
                return []
            index = await self._load_index(redis_client, settings)
            if not index:
                return []
            matched = match.match_target(index, req.action.target)
            if not matched:
                return []

            warn_on = bool(getattr(settings, "PROCEDURE_WARN_ENABLED", True))
            advisories: list[Advisory] = []
            for entry in matched:
                skill_id = entry["skill_id"]
                existing = await store.get_execution(
                    redis_client, req.session_id, skill_id
                )
                observed_ids = set((existing or {}).get("observed") or {})

                exec_id = await store.record_observation(
                    redis_client, settings,
                    session_id=req.session_id, skill_id=skill_id,
                    step_id=entry["step_id"], action_id="",
                    target=req.action.target, agent_id=req.agent_id,
                    adapter=req.adapter,
                )
                if not warn_on:
                    continue
                missing = match.missing_load_bearing(
                    index, skill_id, entry.get("order", 0), observed_ids
                )
                if not missing:
                    continue
                stats = await store.get_step_stats(redis_client, skill_id)
                for m in missing:
                    claimed = await store.claim_warn(
                        redis_client, settings, session_id=req.session_id,
                        skill_id=skill_id, step_id=m["step_id"],
                    )
                    if not claimed:
                        continue
                    advisories.append(Advisory(
                        code="procedure_step_missing",
                        message=match.advisory_text(entry, m, stats),
                        # The pre-built, previously unused receipt slot. Points
                        # at OUR durable record, not a replay event id — those
                        # resolve through a 30d index whose trim task is never
                        # scheduled.
                        evidence_event_id=exec_id,
                    ))
            return advisories
        except Exception as exc:  # noqa: BLE001 — I6
            logger.debug("procedure stage skipped: %s", exc)
            return []
```

- [ ] **Step 5: Wire it into `decide()`**

In `cortex/app/agent_gateway/service.py`, add an optional constructor kwarg — **optional so an older construction cannot raise a TypeError and take out the whole gateway block**:

```python
        procedure_observer=None,
```
store it as `self._procedure_observer = procedure_observer`.

Then in `decide()`, immediately after the advisories list is built from policy reasons (after `service.py:106`):

```python
        # Living Procedures: recognise the work, record it, and advise on a
        # load-bearing step left undone. Advisory only, and its own try/except
        # inside observe() — it can never change the decision.
        if self._procedure_observer is not None:
            advisories.extend(await self._procedure_observer.observe(req))
```

- [ ] **Step 6: Construct it in main.py**

In `cortex/app/main.py`, inside the Agent Gateway `try` block before `AgentGatewayService(...)` (around line 527):

```python
        from app.procedures.observe import ProcedureObserver

        _procedure_observer = ProcedureObserver(
            get_redis=lambda: app.state.redis_client,
            settings_fn=get_settings,
        )
```

and pass `procedure_observer=_procedure_observer,` to the constructor.

- [ ] **Step 7: Rebuild the index when specs change**

In `cortex/app/skills/api.py`, at the end of both `create_skill` and `patch_skill` (before the return), add a best-effort rebuild:

```python
        # Keep the pre-edit matcher index fresh. Best-effort: a rebuild failure
        # must not fail the write, and the nightly pass rebuilds unconditionally.
        if req.step_specs is not None:
            try:
                from app.procedures import store as _proc_store

                _r = getattr(request.app.state, "redis_client", None)
                if _r is not None:
                    await _proc_store.rebuild_index(vector, _r, settings)
            except Exception as exc:  # noqa: BLE001
                logger.warning("procedure index rebuild skipped: %s", exc)
```

`patch_skill` has no `request: Request` parameter — add one (`request: Request,` after `req`). FastAPI injects it positionally-safely; the existing tests call the route by URL so they are unaffected.

- [ ] **Step 8: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_observe.py tests/test_skill_step_specs.py tests/test_skill_api.py -v`
Expected: all pass.

- [ ] **Step 9: Run the whole gateway suite**

Run: `cd cortex && python -m pytest tests/ -k "gateway or agent_action or policy" -v`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add cortex/app/procedures/observe.py cortex/app/agent_gateway/ cortex/app/main.py cortex/app/skills/api.py cortex/tests/test_procedures_observe.py
git commit -m "feat(procedures): recognise work, record executions, advise on a missing step

Lives in AgentGatewayService.decide(), not the policy chain: PolicyContext has
no action type, so a rule cannot tell a file path from a run_command target
before globbing. Advisory only, own try/except, inert when disabled. Reaches
the human through pre_tool's existing print-advisories-on-allow path — no
client release."
```

---

## Task 7: The hardening pass

**Files:**
- Create: `cortex/app/procedures/harden.py`
- Modify: `cortex/app/workers/sleep_cycle.py`
- Test: `cortex/tests/test_procedures_harden.py` (create)

**Interfaces:**
- Consumes: `store.iter_executions`, `store.rebuild_index`, `store.write_step_stats`, `store.write_proposals`; `app.owm.compute_efficacy`, `app.owm.session_success`; `app.evals.store.get_eval`; `app.evals.compute.compute_session_eval`; `replay.reader.get_session_timeline`.
- Produces: `async def run_pass(redis_client, replay_r, vector, settings) -> dict`; Celery task `app.procedures.harden.run_procedure_hardening`.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_harden.py`:

```python
"""Tier A (frequency, no outcome needed) and Tier B (efficacy, gated hard).

The gate matters more than the arithmetic: measured on this repo, no production
emitter passes outcome= to replay except Bridge's session lifecycle, so
_failure_rate is 0.0 and effectively every session reads as a success. A pass
that trusted that would find every step dead and propose deleting the procedure.
"""
import json

import pytest
import fakeredis.aioredis as fr

from app.procedures import harden, store


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_MIN_EXECUTIONS = 2
    PROCEDURE_PRIOR_N = 5
    PROCEDURE_EFFICACY_DELTA = 0.15
    PROCEDURE_WINDOW_DAYS = 30
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_AGENT_CAP = 5
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


class _Vector:
    class _C:
        async def scroll(self, **kw):
            return [], None
    def __init__(self):
        self._client = self._C()


@pytest.fixture
def r():
    return fr.FakeRedis(decode_responses=True)


async def _exec(r, session, skill, observed, agent="ag"):
    s = _Settings()
    for step_id in observed:
        await store.record_observation(
            r, s, session_id=session, skill_id=skill, step_id=step_id,
            action_id="x", target="t", agent_id=agent, adapter="shell-hook")
    if not observed:
        # an execution with no observation at all cannot exist by construction;
        # tests that need one build the hash directly
        pass


async def _index(r, steps):
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "t", "step_id": sid, "step_text": sid,
         "pattern": f"{sid}.py", "load_bearing": False, "order": i}
        for i, sid in enumerate(steps)
    ]))


@pytest.mark.asyncio
async def test_tier_a_counts_without_any_outcome_signal(r, monkeypatch):
    await _index(r, ["a", "b"])
    await _exec(r, "s-1", "s1", ["a", "b"])
    await _exec(r, "s-2", "s1", ["a"])          # b skipped, a observed => I2 satisfied

    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)

    result = await harden.run_pass(r, None, _Vector(), _Settings())
    stats = await store.get_step_stats(r, "s1")
    assert stats["a"]["observed"] == 2
    assert stats["b"]["observed"] == 1
    assert stats["b"]["skipped"] == 1
    assert stats["a"]["executions"] == 2
    assert result["tier_b"] == "insufficient outcome signal"


@pytest.mark.asyncio
async def test_i2_an_execution_with_no_sibling_evidence_counts_no_skips(r, monkeypatch):
    """The kiro / shell-only / personal-mode case: nothing was observed, so
    nothing was skipped. Without this, those sessions vote to delete every step."""
    await _index(r, ["a", "b"])
    key = store.exec_key("s-1", "s1")
    await r.hset(key, mapping={"exec_id": "e", "skill_id": "s1", "session_id": "s-1",
                               "agent_id": "ag", "adapter": "shell-hook",
                               "observed": "{}", "warned": "{}"})
    await r.sadd("proc:exec:__index", key)

    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await harden.run_pass(r, None, _Vector(), _Settings())

    stats = await store.get_step_stats(r, "s1")
    assert stats.get("a", {}).get("skipped", 0) == 0
    assert stats.get("b", {}).get("skipped", 0) == 0


@pytest.mark.asyncio
async def test_tier_b_stays_closed_without_enough_knowable_outcomes(r, monkeypatch):
    await _index(r, ["a", "b"])
    await _exec(r, "s-1", "s1", ["a"])
    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    result = await harden.run_pass(r, None, _Vector(), _Settings())
    assert await store.list_proposals(r, "s1") == []
    assert result["tier_b"] == "insufficient outcome signal"


@pytest.mark.asyncio
async def test_tier_b_proposes_load_bearing_when_skipping_predicts_failure(r, monkeypatch):
    await _index(r, ["a", "b"])
    # a observed + success, twice; a skipped + failure, twice
    await _exec(r, "ok-1", "s1", ["a", "b"], agent="ag1")
    await _exec(r, "ok-2", "s1", ["a", "b"], agent="ag2")
    await _exec(r, "bad-1", "s1", ["b"], agent="ag3")
    await _exec(r, "bad-2", "s1", ["b"], agent="ag4")

    async def _outcome(replay_r, sid):
        return True if sid.startswith("ok") else False

    monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
    await harden.run_pass(r, None, _Vector(), _Settings())

    props = await store.list_proposals(r, "s1")
    kinds = {(p["kind"], p["step_id"]) for p in props}
    assert ("load_bearing", "a") in kinds


@pytest.mark.asyncio
async def test_one_agent_cannot_decide_a_procedure(r, monkeypatch):
    """PROCEDURE_AGENT_CAP: a CI identity looping must not bury a step."""
    await _index(r, ["a", "b"])
    for i in range(20):
        await _exec(r, f"bot-{i}", "s1", ["b"], agent="ci-bot")

    async def _outcome(replay_r, sid):
        return False

    monkeypatch.setattr(harden, "_resolve_outcome", _outcome)
    await harden.run_pass(r, None, _Vector(), _Settings())

    stats = await store.get_step_stats(r, "s1")
    assert stats["a"]["skipped_scored"] <= _Settings.PROCEDURE_AGENT_CAP


@pytest.mark.asyncio
async def test_proposals_are_replaced_not_accumulated(r, monkeypatch):
    await store.write_proposals(r, "s1", [{"id": "old", "kind": "dead_step",
                                           "step_id": "z", "detail": "d"}])
    await _index(r, ["a"])
    await _exec(r, "s-1", "s1", ["a"])
    monkeypatch.setattr(harden, "_resolve_outcome", _no_outcome)
    await harden.run_pass(r, None, _Vector(), _Settings())
    assert all(p["id"] != "old" for p in await store.list_proposals(r, "s1"))


@pytest.mark.asyncio
async def test_disabled_returns_immediately(r):
    class Off(_Settings):
        PROCEDURE_ENABLED = False
    assert (await harden.run_pass(r, None, _Vector(), Off()))["status"] == "disabled"


async def _no_outcome(replay_r, sid):
    return None
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_harden.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement the pass**

Create `cortex/app/procedures/harden.py`:

```python
"""The nightly hardening pass.

TWO TIERS, and the split is the design's answer to a measured weakness rather
than a hedge:

  Tier A — frequency. Needs no outcome signal. "This procedure ran 41 times;
           step 3 was skipped in 24 of them" is true and useful on day one.
  Tier B — efficacy verdicts. Gated on executions whose sessions have a KNOWABLE
           outcome. Measured on this repo, no production emitter passes outcome=
           to replay except Bridge's session lifecycle, so _failure_rate is 0.0
           and effectively every session reads as a success. A pass that trusted
           that would find every step dead and propose deleting the procedure.
           Closed is the correct state; it reports that it is closed.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from app.procedures import store

logger = logging.getLogger(__name__)


async def _resolve_outcome(replay_r, session_id: str) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    Excludes on ANY doubt: no replay events, no outcome-bearing event (I4 —
    _failure_rate returns 0.0 in that case, which reads as success), an eval
    that cannot be computed, or session_success's ambiguous middle band.
    """
    if replay_r is None:
        return None
    try:
        from replay.reader import get_session_timeline

        timeline = await get_session_timeline(replay_r, session_id, limit=1000)
        events = (timeline or {}).get("events") or []
        if not any(e.get("outcome") for e in events):
            return None

        from app.evals.store import get_eval

        ev = await get_eval(replay_r, session_id)
        if ev is None:
            from app.evals.compute import compute_session_eval

            ev = await compute_session_eval(replay_r, session_id, trigger="manual")
        if ev is None:
            return None

        from app.owm import session_success

        data = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
        return session_success(data, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome unresolved for %s: %s", session_id, exc)
        return None


def _within_window(rec: dict, cutoff: datetime) -> bool:
    stamp = rec.get("last_seen_at") or rec.get("opened_at")
    if not stamp:
        return True  # undated: keep rather than silently drop evidence
    try:
        dt = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= cutoff


async def run_pass(redis_client, replay_r, vector, settings) -> dict:
    """One full hardening pass. Never raises for a single bad execution."""
    if not getattr(settings, "PROCEDURE_ENABLED", False):
        return {"status": "disabled"}

    min_n = int(getattr(settings, "PROCEDURE_MIN_EXECUTIONS", 5))
    prior_n = int(getattr(settings, "PROCEDURE_PRIOR_N", 5))
    delta = float(getattr(settings, "PROCEDURE_EFFICACY_DELTA", 0.15))
    cap = int(getattr(settings, "PROCEDURE_AGENT_CAP", 5))
    window = int(getattr(settings, "PROCEDURE_WINDOW_DAYS", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=window)

    # Self-healing: the index is also rebuilt on every spec write, but a pass
    # that rebuilds unconditionally means a missed write can never strand it.
    try:
        await store.rebuild_index(vector, redis_client, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warning("index rebuild failed during hardening: %s", exc)

    index = await store.load_index(redis_client)
    steps_by_skill: dict[str, list[dict]] = {}
    for entry in index:
        steps_by_skill.setdefault(entry["skill_id"], []).append(entry)

    executions = await store.iter_executions(redis_client)
    # tallies[skill][step] -> counters
    tallies: dict[str, dict[str, dict]] = {}
    # agent_seen[skill][step][agent] -> count, for the fairness cap
    agent_seen: dict[str, dict[str, dict[str, int]]] = {}
    outcome_backed = 0

    for rec in executions:
        skill_id = rec.get("skill_id")
        if skill_id not in steps_by_skill:
            continue
        if not _within_window(rec, cutoff):
            continue
        observed_ids = set(rec.get("observed") or {})
        # I2: no sibling evidence => this execution says nothing about any step.
        if not observed_ids:
            continue

        outcome = await _resolve_outcome(replay_r, rec.get("session_id") or "")
        if outcome is not None:
            outcome_backed += 1
        agent = rec.get("agent_id") or "unknown"

        for entry in steps_by_skill[skill_id]:
            step_id = entry["step_id"]
            t = tallies.setdefault(skill_id, {}).setdefault(step_id, {
                "observed": 0, "skipped": 0, "executions": 0,
                "observed_scored": 0, "observed_success": 0,
                "skipped_scored": 0, "skipped_success": 0,
            })
            t["executions"] += 1
            was_observed = step_id in observed_ids
            t["observed" if was_observed else "skipped"] += 1

            if outcome is None:
                continue
            seen = agent_seen.setdefault(skill_id, {}).setdefault(step_id, {})
            if seen.get(agent, 0) >= cap:
                continue  # one identity must not decide a team's procedure
            seen[agent] = seen.get(agent, 0) + 1
            bucket = "observed" if was_observed else "skipped"
            t[f"{bucket}_scored"] += 1
            if outcome:
                t[f"{bucket}_success"] += 1

    tier_b_open = outcome_backed >= min_n
    written = proposed = 0

    for skill_id, steps in tallies.items():
        await store.write_step_stats(redis_client, settings, skill_id, steps)
        written += 1
        proposals: list[dict] = []
        if tier_b_open:
            proposals = _tier_b_proposals(
                skill_id, steps, steps_by_skill[skill_id], min_n, prior_n, delta
            )
        await store.write_proposals(redis_client, skill_id, proposals)
        proposed += len(proposals)

    return {
        "status": "ok",
        "executions": len(executions),
        "skills": written,
        "proposals": proposed,
        "outcome_backed_executions": outcome_backed,
        "tier_b": "open" if tier_b_open else "insufficient outcome signal",
    }


def _tier_b_proposals(skill_id, steps, entries, min_n, prior_n, delta) -> list[dict]:
    from app.owm import compute_efficacy

    by_id = {e["step_id"]: e for e in entries}
    out: list[dict] = []
    for step_id, t in steps.items():
        if t["observed_scored"] < min_n or t["skipped_scored"] < min_n:
            continue
        eff_obs = compute_efficacy(t["observed_success"], t["observed_scored"], prior_n)
        eff_skip = compute_efficacy(t["skipped_success"], t["skipped_scored"], prior_n)
        entry = by_id.get(step_id, {})
        text = entry.get("step_text") or step_id
        if eff_skip < eff_obs - delta and not entry.get("load_bearing"):
            out.append({
                "id": uuid.uuid4().hex[:12], "kind": "load_bearing",
                "skill_id": skill_id, "step_id": step_id,
                "detail": (f"Skipping \"{text}\" tracks with worse outcomes "
                           f"({eff_skip:.2f} vs {eff_obs:.2f} over "
                           f"{t['skipped_scored']}/{t['observed_scored']} scored "
                           f"executions). Mark it load-bearing?"),
            })
        elif eff_skip >= eff_obs - delta and t["skipped"] >= min_n:
            out.append({
                "id": uuid.uuid4().hex[:12], "kind": "dead_step",
                "skill_id": skill_id, "step_id": step_id,
                "detail": (f"\"{text}\" was skipped in {t['skipped']} of "
                           f"{t['executions']} executions with no measurable cost "
                           f"({eff_skip:.2f} vs {eff_obs:.2f}). Remove it?"),
            })
    return out
```

- [ ] **Step 4: Register the Celery task**

Append to `cortex/app/procedures/harden.py`:

```python
# Import placement is load-bearing (the owm.py / confluence-collector
# precedent): celery_app is imported at the BOTTOM so this module's public
# surface exists before the worker imports it.
import asyncio  # noqa: E402

from app.workers.sleep_cycle import celery_app  # noqa: E402


@celery_app.task(name="app.procedures.harden.run_procedure_hardening")
def run_procedure_hardening() -> dict:
    """Beat fires unconditionally; the task self-gates and never raises."""
    from app.config import get_settings

    settings = get_settings()
    if not settings.PROCEDURE_ENABLED:
        return {"status": "disabled"}
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("procedure hardening crashed")
        return {"status": "error", "error": str(exc)}


async def _run() -> dict:
    import redis.asyncio as aioredis

    from app.config import get_settings
    from app.db.vector import VectorClient

    settings = get_settings()
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    replay_r = aioredis.from_url(settings.RP_REDIS_URL, decode_responses=True)
    vector = VectorClient(settings)
    try:
        return await run_pass(r, replay_r, vector, settings)
    finally:
        for c in (r, replay_r):
            try:
                await c.aclose()
            except Exception:  # noqa: BLE001
                pass
```

Before writing this, open `cortex/app/owm.py`'s `_run_owm_impl` and copy its exact client-construction idiom — `VectorClient`'s constructor signature and the close protocol must match what that file already does rather than what this plan guesses.

- [ ] **Step 5: Register the beat entry**

In `cortex/app/workers/sleep_cycle.py`, add `"app.procedures.harden"` to the `include=[...]` list and a beat entry beside `owm-scoring`:

```python
            "procedure-hardening": {
                "task": "app.procedures.harden.run_procedure_hardening",
                "schedule": timedelta(hours=s.PROCEDURE_SCHEDULE_HOURS),
            },
```

- [ ] **Step 6: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_harden.py -v`
Expected: all pass.

- [ ] **Step 7: Prove I2 by mutation**

In `run_pass`, temporarily remove the `if not observed_ids: continue` guard. Run
`tests/test_procedures_harden.py::test_i2_an_execution_with_no_sibling_evidence_counts_no_skips`.
Expected: **FAIL**. Restore the guard.

- [ ] **Step 8: Commit**

```bash
git add cortex/app/procedures/harden.py cortex/app/workers/sleep_cycle.py cortex/tests/test_procedures_harden.py
git commit -m "feat(procedures): nightly hardening — Tier A frequency, Tier B gated efficacy

Tier B stays closed until enough executions have a KNOWABLE outcome, and
reports that it is closed. Measured on this repo, no production emitter passes
outcome= to replay except Bridge's session lifecycle, so _failure_rate is 0.0
and effectively every session reads as a success; a pass that trusted that
would find every step dead and confidently propose deleting the procedure."
```

---

## Task 8: REST surface

**Files:**
- Create: `cortex/app/procedures/api.py`
- Modify: `cortex/app/main.py`
- Test: `cortex/tests/test_procedures_api.py` (create)

**Interfaces:**
- Produces: `create_procedures_router(get_redis, get_vector, settings_fn) -> APIRouter` serving `GET /procedures`, `GET /procedures/{skill_id}/executions`, `POST /procedures/proposals/{proposal_id}/dismiss`.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_api.py`:

```python
"""The /procedures surface. Mounted only when PROCEDURE_ENABLED — the
/dreams + /collectors precedent: a disabled deploy 404s rather than serving a
disabled-shaped body."""
import json

import pytest
import fakeredis.aioredis as fr
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.procedures import store
from app.procedures.api import create_procedures_router


class _Settings:
    PROCEDURE_ENABLED = True
    PROCEDURE_EXEC_TTL_DAYS = 90
    PROCEDURE_MAX_SPECS = 50
    QDRANT_COLLECTION = "c"


@pytest.fixture
async def app_and_redis():
    r = fr.FakeRedis(decode_responses=True)
    app = FastAPI()
    app.include_router(create_procedures_router(
        get_redis=lambda: r, get_vector=lambda: None, settings_fn=lambda: _Settings(),
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, r


@pytest.mark.asyncio
async def test_rollup_reports_coverage_honestly(app_and_redis):
    client, r = app_and_redis
    await r.set(store.INDEX_KEY, json.dumps([
        {"skill_id": "s1", "skill_trigger": "release", "step_id": "a",
         "step_text": "bump", "pattern": "*.toml", "load_bearing": True, "order": 0},
    ]))
    await store.write_step_stats(r, _Settings(), "s1", {
        "a": {"observed": 3, "skipped": 1, "executions": 4},
    })
    resp = await client.get("/procedures")
    assert resp.status_code == 200
    body = resp.json()
    row = body["procedures"][0]
    assert row["skill_id"] == "s1"
    assert row["observable_steps"] == 1
    assert row["steps"]["a"]["observed"] == 3


@pytest.mark.asyncio
async def test_executions_endpoint_returns_the_receipts(app_and_redis):
    client, r = app_and_redis
    await store.record_observation(
        r, _Settings(), session_id="sess", skill_id="s1", step_id="a",
        action_id="act", target="pyproject.toml", agent_id="ag", adapter="shell-hook")
    resp = await client.get("/procedures/s1/executions")
    assert resp.status_code == 200
    execs = resp.json()["executions"]
    assert execs[0]["session_id"] == "sess"
    assert execs[0]["observed"]["a"][0]["target"] == "pyproject.toml"


@pytest.mark.asyncio
async def test_dismiss_removes_a_proposal_and_404s_on_an_unknown_one(app_and_redis):
    client, r = app_and_redis
    await store.write_proposals(r, "s1", [
        {"id": "p1", "kind": "dead_step", "skill_id": "s1", "step_id": "a", "detail": "d"},
    ])
    assert (await client.post("/procedures/proposals/p1/dismiss")).status_code == 200
    assert await store.list_proposals(r, "s1") == []
    assert (await client.post("/procedures/proposals/nope/dismiss")).status_code == 404


@pytest.mark.asyncio
async def test_a_cold_deployment_reports_zero_not_an_error(app_and_redis):
    client, _ = app_and_redis
    resp = await client.get("/procedures")
    assert resp.status_code == 200
    assert resp.json()["procedures"] == []
    assert resp.json()["specs_total"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_api.py -v`
Expected: FAIL on import.

- [ ] **Step 3: Implement the router**

Create `cortex/app/procedures/api.py`:

```python
"""GET /procedures — what the dashboard reads.

Scope note: unlike the skills router (which declares no dependencies= and
contains no require_scope at all), these routes are gated. Accepting a proposal
is still a PATCH /skills/{id} and therefore still as ungated as it is today —
retrofitting a gate onto a shipped surface belongs in its own change.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.procedures import store

logger = logging.getLogger(__name__)


def create_procedures_router(get_redis, get_vector, settings_fn) -> APIRouter:
    router = APIRouter(tags=["procedures"])

    try:
        from auth.middleware import require_scope
        read_dep = [Depends(require_scope("memory:read"))]
        admin_dep = [Depends(require_scope("admin"))]
    except Exception:  # noqa: BLE001 — auth optional in unit tests
        read_dep, admin_dep = [], []

    @router.get("/procedures", dependencies=read_dep)
    async def list_procedures():
        r = get_redis()
        index = await store.load_index(r)
        by_skill: dict[str, list[dict]] = {}
        for e in index:
            by_skill.setdefault(e["skill_id"], []).append(e)

        rows = []
        for skill_id, entries in by_skill.items():
            stats = await store.get_step_stats(r, skill_id)
            proposals = await store.list_proposals(r, skill_id)
            executions = max(
                [s.get("executions", 0) for s in stats.values()] or [0]
            )
            rows.append({
                "skill_id": skill_id,
                "trigger": entries[0].get("skill_trigger", ""),
                # Coverage is REPORTED, never hidden: a step with no matcher is
                # unobservable, and a coverage number the user cannot see is the
                # same silent cap this repo bans elsewhere.
                "observable_steps": len(entries),
                "executions": executions,
                "steps": stats,
                "proposals": proposals,
            })
        rows.sort(key=lambda x: (-x["executions"], x["skill_id"]))
        return {"procedures": rows, "count": len(rows), "specs_total": len(index)}

    @router.get("/procedures/{skill_id}/executions", dependencies=read_dep)
    async def list_executions(skill_id: str, limit: int = 50):
        r = get_redis()
        all_execs = await store.iter_executions(r)
        mine = [e for e in all_execs if e.get("skill_id") == skill_id]
        mine.sort(key=lambda e: e.get("last_seen_at") or "", reverse=True)
        return {"executions": mine[:limit], "count": len(mine)}

    @router.post("/procedures/proposals/{proposal_id}/dismiss", dependencies=admin_dep)
    async def dismiss(proposal_id: str):
        r = get_redis()
        if not await store.dismiss_proposal(r, proposal_id):
            raise HTTPException(status_code=404, detail="Proposal not found")
        return {"dismissed": proposal_id}

    return router
```

- [ ] **Step 4: Mount it in main.py**

In `cortex/app/main.py`'s `_register_feature_routers`, beside the `/dreams` mount and following its exact conditional-mount style:

```python
    if get_settings().PROCEDURE_ENABLED:
        try:
            from app.procedures.api import create_procedures_router

            app.include_router(create_procedures_router(
                get_redis=lambda: app.state.redis_client,
                get_vector=lambda: app.state.vector_client,
                settings_fn=get_settings,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Procedures router not registered: %s", exc)
```

Use the real attribute name for the vector client on `app.state` — read the neighbouring registrations rather than assuming `vector_client`.

- [ ] **Step 5: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_api.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/procedures/api.py cortex/app/main.py cortex/tests/test_procedures_api.py
git commit -m "feat(procedures): GET /procedures rollup, executions, proposal dismiss"
```

---

## Task 9: MCP tools

Round 1's answer to the cold start: 25 active skills exist and none has specs. An agent needs a way to compile them without a PATCH tool.

**Files:**
- Modify: `cortex/app/mcp_server.py`
- Test: `cortex/tests/test_procedures_mcp.py` (create)

**Interfaces:**
- Produces: `skill_create(..., step_specs=None)`; new tool `skill_add_step_specs(skill_id, step_specs)`.

- [ ] **Step 1: Write the failing test**

Create `cortex/tests/test_procedures_mcp.py`. Read how the existing MCP tool tests in `cortex/tests/` stub the proxy client and mirror that exactly.

```python
"""The two MCP surfaces that let an agent compile specs."""
import pytest


def test_skill_create_accepts_step_specs():
    import inspect
    from app import mcp_server

    fn = getattr(mcp_server, "skill_create")
    params = inspect.signature(getattr(fn, "fn", fn)).parameters
    assert "step_specs" in params


def test_skill_add_step_specs_exists_and_takes_a_skill_id():
    import inspect
    from app import mcp_server

    fn = getattr(mcp_server, "skill_add_step_specs")
    params = inspect.signature(getattr(fn, "fn", fn)).parameters
    assert "skill_id" in params and "step_specs" in params
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_procedures_mcp.py -v`
Expected: FAIL — `AttributeError: skill_add_step_specs`.

- [ ] **Step 3: Add `step_specs` to `skill_create`**

In `cortex/app/mcp_server.py`, add the parameter and forward it in the JSON body. Extend the docstring — the tool schema is what prompts the authoring agent, so it must say what a spec is and what round 1 can observe:

```python
    step_specs: list[dict] | None = None,
```

```
        step_specs: Optional per-step matchers that turn this skill into an
            OBSERVED procedure. One entry per step, in order:
              {"text": "<the step, verbatim>",
               "kind": "file_glob" | "unobservable",
               "pattern": "<glob, e.g. 'client/pyproject.toml'>",
               "load_bearing": true|false}
            Use "file_glob" when the step edits a file whose path you can name.
            Use "unobservable" for anything else — asking a human, or running a
            shell command (round 1 does not observe shell commands, so a shell
            step must be "unobservable" even though the command is precise).
            Mark load_bearing=true only when skipping the step is what breaks
            things: that is what raises a warning to the next person.
            Prefer specific globs. "*.py" matches everything and is not a step.
```

- [ ] **Step 4: Add `skill_add_step_specs`**

Beside `skill_create`, following the same proxy pattern (`_get_client()`, identity headers, error shape):

```python
@mcp.tool()
async def skill_add_step_specs(skill_id: str, step_specs: list[dict]) -> str:
    """Compile step matchers onto an EXISTING skill (PATCH /skills/{id}).

    The cold-start path: a skill written before step specs existed has none, so
    the procedure machinery is inert for it. Read the skill's steps, write one
    spec per step in order, and send them here. Replaces the whole list.

    See skill_create's step_specs for the entry shape and the round-1 limits.
    """
```

It PATCHes `/skills/{skill_id}` with `{"step_specs": step_specs}` and returns a short confirmation naming how many specs were stored and how many are observable (`kind == "file_glob"`), so the agent can see the coverage it just created.

- [ ] **Step 5: Run the tests**

Run: `cd cortex && python -m pytest tests/test_procedures_mcp.py -v`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/mcp_server.py cortex/tests/test_procedures_mcp.py
git commit -m "feat(procedures): skill_create takes step_specs; skill_add_step_specs for existing skills"
```

---

## Task 10: Dashboard

**Files:**
- Modify: `dashboard/index.html`

- [ ] **Step 1: Read the existing Skills tab**

Open `dashboard/index.html` and read `loadSkills` (~line 4924) plus the PATCH handlers (~4987-5008) and the `window.` export block (~5010-5018). Note three hazards to avoid: the tab uses bare `fetch` with no timeout or status check; the PATCH handlers have no `.catch`, so a failed action fails silently and `loadSkills()` re-renders the unchanged row; and any handler used from an inline `onclick` must be `window.`-exported or the button does nothing at click time with no build- or test-time detection.

- [ ] **Step 2: Add a Procedures panel to the Skills tab**

Add a panel above the skills list that calls `GET /procedures` using `fetchJSON` (not bare `fetch`). Render one card per procedure:

- header: the trigger, and `N executions`
- a coverage line: `X of Y steps observable` — always shown, including when X is 0
- per-step rows: step text, `observed N / skipped M`
- any proposals as a row with its `detail` and a **Dismiss** button
- when `specs_total === 0`, render a single explanatory line instead of an empty panel: *"No procedure has step specs yet — ask an agent to compile them with `skill_add_step_specs`."*

Wire Dismiss to `POST /procedures/proposals/{id}/dismiss` with `.catch(err => showError(err))` — use whatever the file's existing error surface is — and `window.`-export every handler referenced from an inline `onclick`.

- [ ] **Step 3: Verify by hand**

Run the stack (`docker compose up -d`), set `PROCEDURE_ENABLED=true`, create a skill with two `step_specs` via the API, and confirm the card renders with `2 of 2 steps observable` and `0 executions`. With `PROCEDURE_ENABLED=false`, confirm the panel hides cleanly rather than showing a 404 error — the router is not mounted at all.

- [ ] **Step 4: Commit**

```bash
git add dashboard/index.html
git commit -m "feat(procedures): dashboard panel — coverage, per-step counts, proposals"
```

---

## Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md` (root), `cortex/CLAUDE.md`

- [ ] **Step 1: Root CLAUDE.md**

Add a `### Living Procedures (cortex/app/procedures/)` section after the Dreaming section, in the register of its neighbours: what it is, the five stages, each invariant with the code fact that forced it, the config table, and the honest limits (Claude file edits only; shell unobservable; cold start; Tier B expected closed). State explicitly that `adapter` is a transport class and cannot identify the runtime. Add the new env vars to the config listings.

- [ ] **Step 2: cortex/CLAUDE.md**

Add `procedures/` to the module map with a one-line purpose per file, and the three REST endpoints to the API list with their scope gates.

- [ ] **Step 3: Record the OWM finding where OWM's readers will see it**

In the root `CLAUDE.md` OWM paragraph, add the measured finding: no production emitter passes `outcome=` to replay except Bridge's session lifecycle, so `_failure_rate` is `0.0` and effectively every session reads as a success — which means `owm_efficacy` is far less discriminative than the design intends. Task 1 fixes the gateway half; the rest is a separate, unmade decision. This must not live only in the Living Procedures section: an OWM reader will never look there.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md cortex/CLAUDE.md
git commit -m "docs: Living Procedures, and the measured degeneracy in OWM's outcome signal"
```

---

## Self-Review

**Spec coverage.** §4 Stage 1 → Tasks 3, 9. Stage 2/3/4 → Task 6. Stage 5 → Task 7. §5 I1 → Task 6 (recognition, no delivery hook). I2 → Task 7 Step 7 (proved by mutation). I3 → Tasks 3 + 4 (specs on the point, numbers in Redis, no shared key). I4/F1 → Task 7 (`_resolve_outcome`, two tiers). I5 → Task 6 (`_ExplodingVector`, signature assertion). I6 → Tasks 5 + 6 (hostile patterns, dead Redis). §7 P1/P2 → Task 1. §8 → Task 2. §9 → Tasks 8, 9, 10. §10 → each task's tests.

**Deliberately not covered, matching §11:** kiro `fs_write` mapping, shell observation, Tier-2 progress matching, Night Shift backfill, briefing section.

**Type consistency.** `StepSpec` fields (`id/text/kind/pattern/load_bearing`) are identical in Tasks 3, 4, 6, 9. Index entry keys (`skill_id/skill_trigger/step_id/step_text/pattern/load_bearing/order`) are identical in Tasks 4, 5, 6, 7, 8. `store` function names in Task 4's Interfaces match every call site. `run_pass(redis_client, replay_r, vector, settings)` matches its test and its Celery caller.

**Known softness, stated rather than hidden.** Task 1 Step 4 and Task 7 Step 4 both instruct the implementer to read an existing function's real signature before editing (`_match_score`'s name; `VectorClient`'s construction idiom in `_run_owm_impl`). Those are the two places this plan describes a call it did not verify character-by-character; everything else quotes code that was read.
