# Outcome Truth PR5 — Controlled Grading Nudge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the dark-deployed, server-composed A/B grading-nudge experiment: shared arm function, member-token data element, treatment briefing section behind a flag, and the member-level `arm_comparison` readout on the compliance surface.

**Architecture:** The arm function moves to the shared `auth/` package (bridge re-imports). A hashed member token rides the existing PR4-D1 field path (bridge session → session_start replay payload → EvalResult → parsed compliance records). The nudge is a new briefing section composed server-side for arm-A members only, gated by `GRADING_NUDGE_ENABLED=False`, with a withhold-on-record-failure receipt. The readout is a new `arm_comparison` block built record-level from the same parsed evals `build_compliance` already scans, with a member-level exact permutation test as the primary analysis.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, Redis (fakeredis in tests), pytest. Pure-stdlib statistics (no scipy on any prod path).

**Spec:** `docs/superpowers/specs/2026-08-26-outcome-truth-pr5-controlled-nudge-design.md` (revision 47d8e17 — read it before starting; decisions cited as D1–D14).

## Global Constraints

- **`GRADING_NUDGE_ENABLED: bool = False` and it is NEVER flipped by this plan** — dark deploy (spec D4). No test may require the live flag on outside its own settings override.
- **`TREATMENT_ARM = "A"`** — resolved by the D9 commit-hash coin (spec revision commit `47d8e17`, first hex digit 4, even → A). Control = "B". This literal appears once, as a named constant.
- **The D3 treatment text is byte-exact** (Task 4 defines `GRADING_NUDGE_TEXT`); any wording change is a new experiment, not an edit.
- **No scipy import on any production code path** (`minimum_sample_size` in `patterns/statistics.py` is known to raise on the shipped image — never call it).
- **Frozen compliance surface untouched:** the six frozen predicates, `grade_self_reported` row semantics, `by_experiment_group` buckets, and `optimism_skew` block stay byte-identical. Everything lands additively.
- **No changes under `client/`** — the nudge is fully server-side (spec D2).
- **`member_token = hashlib.sha256(owner_member.encode("utf-8")).hexdigest()[:12]`** — exactly this derivation, defined once in `auth/experiment.py` (spec D13).
- **Absent is never a measured value:** missing fields classify `unknown`, never `not_exposed`, never control (frozen compliance convention).
- Run each service's suite from its own directory (`cd cortex && python -m pytest tests/ -v`, same for `bridge`); shared-module tests from repo root (`python -m pytest auth/tests/ -v`).

---

### Task 1: Shared arm + member-token functions in `auth/experiment.py`

**Files:**
- Create: `auth/experiment.py`
- Create: `auth/tests/test_experiment.py`
- Modify: `bridge/app/session.py:33-49` (replace the local function with the import)
- Modify: `bridge/tests/test_experiment_group.py` (add the identity re-pin)

**Interfaces:**
- Produces: `experiment_group(owner_member: str | None) -> str | None` and `member_token(owner_member: str | None) -> str | None`, importable as `from auth.experiment import experiment_group, member_token`. Every later task uses exactly these.
- Bridge keeps the name `_experiment_group` alive via import alias so `bridge/app/mcp_server.py:32` (`from app.session import ... _experiment_group`) keeps working unchanged.

- [ ] **Step 1: Write the failing test**

`auth/tests/test_experiment.py`:

```python
"""Parity + contract tests for the shared arm/token functions (PR5 D1/D13).

FROZEN_REFERENCE is a byte-frozen copy of bridge/app/session.py's pre-PR5
_experiment_group. If auth/experiment.py ever diverges from it, every
member's arm reassigns and the PR4/PR5 stamped labels stop meaning anything.
"""
import hashlib

from auth.experiment import experiment_group, member_token


def _frozen_reference(owner_member):
    if not owner_member:
        return None
    h = int(hashlib.sha256(owner_member.encode("utf-8")).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


MEMBERS = ["mogan", "member-owner", "alice@example.com", "x", "member-7f3a"]


def test_arm_parity_with_frozen_bridge_implementation():
    for m in MEMBERS:
        assert experiment_group(m) == _frozen_reference(m)


def test_arm_none_for_empty_and_none():
    assert experiment_group(None) is None
    assert experiment_group("") is None


def test_arm_is_stable_and_binary():
    for m in MEMBERS:
        arm = experiment_group(m)
        assert arm in ("A", "B")
        assert experiment_group(m) == arm  # deterministic across calls


def test_member_token_derivation():
    for m in MEMBERS:
        expected = hashlib.sha256(m.encode("utf-8")).hexdigest()[:12]
        assert member_token(m) == expected
        assert len(member_token(m)) == 12


def test_member_token_none_for_empty_and_none():
    assert member_token(None) is None
    assert member_token("") is None


def test_token_and_arm_derive_from_same_string():
    # D13: token and arm may never disagree about which member — both are
    # pure functions of the same input, so equality of input is the proof.
    m = "mogan"
    h = int(hashlib.sha256(m.encode("utf-8")).hexdigest(), 16)
    assert experiment_group(m) == ("A" if h % 2 == 0 else "B")
    assert member_token(m) == hashlib.sha256(m.encode("utf-8")).hexdigest()[:12]
```

- [ ] **Step 2: Run it to verify it fails**

Run (repo root): `python -m pytest auth/tests/test_experiment.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'auth.experiment'`

- [ ] **Step 3: Create `auth/experiment.py`**

Move the docstring and body verbatim from `bridge/app/session.py:33-49`, adding the token beside it:

```python
"""Pre-registered experiment identity (outcome truth PR4 D1 / PR5 D1+D13).

One shared implementation for every service: bridge stamps these on the
session, cortex's briefing route computes the arm for treatment routing.
Sharing by construction (bridge re-imports this module) is what pins
delivered-arm == recorded-arm; the parity test freezes the derivation.
"""
import hashlib


def experiment_group(owner_member: str | None) -> str | None:
    """The pre-registered arm ("A"/"B") for *owner_member*.

    Deterministic and STABLE across process restarts: sha256, never
    Python's built-in hash(), which is salted per-process (PYTHONHASHSEED)
    and would reassign every member's arm on the next restart, destroying
    stickiness. Called at session start / briefing time from the verified
    owner_member only, never from task_result.

    An empty/unverified owner_member returns None (excluded from arms)
    rather than a hashed arm: hash("") is a single fixed value, so hashing
    it would dump every unauthenticated session into the same arm.
    """
    if not owner_member:
        return None
    h = int(hashlib.sha256(owner_member.encode("utf-8")).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"


def member_token(owner_member: str | None) -> str | None:
    """One-way member key for arm analytics (PR5 D13).

    sha256 prefix, 12 hex chars: enough to never collide inside one fleet,
    short enough that analytics surfaces never leak the member string. Same
    input as experiment_group, so token and arm cannot disagree about which
    member a session belongs to. None (not "") for absent members — absence
    must stay distinguishable from a measured value on every wire.
    """
    if not owner_member:
        return None
    return hashlib.sha256(owner_member.encode("utf-8")).hexdigest()[:12]
```

If `auth/tests/` has no `__init__.py` and other shared-module test dirs do, match the existing convention.

- [ ] **Step 4: Run the auth tests**

Run: `python -m pytest auth/tests/test_experiment.py -v`
Expected: PASS (all 6)

- [ ] **Step 5: Swap bridge to the shared import**

In `bridge/app/session.py`, delete the whole `def _experiment_group(...)` block (lines 33-49) and add beside the other imports at the top:

```python
from auth.experiment import experiment_group as _experiment_group
```

The alias keeps `bridge/app/mcp_server.py:32`'s `from app.session import ... _experiment_group` working unchanged. Do NOT touch the call sites (`session.py:257`, `mcp_server.py:438`).

- [ ] **Step 6: Add the identity re-pin to bridge's suite**

In `bridge/tests/test_experiment_group.py`, add:

```python
def test_arm_function_is_the_shared_auth_implementation():
    """PR5 D1: bridge must use auth.experiment's function, not a local copy —
    identity, not equality, so a silent re-fork fails loudly."""
    from auth.experiment import experiment_group
    from app.session import _experiment_group
    assert _experiment_group is experiment_group
```

- [ ] **Step 7: Run bridge's suite**

Run: `cd bridge && python -m pytest tests/ -v`
Expected: PASS — the existing `test_experiment_group.py` cases prove the swap changed no behavior. (If imports fail because `auth/` is not on bridge's test path, check how `bridge/tests` already imports `replay.*` — the same conftest/path mechanism covers `auth`; `bridge/Dockerfile:56` already ships `COPY auth/ ./auth/` for production.)

- [ ] **Step 8: Commit**

```bash
git add auth/experiment.py auth/tests/test_experiment.py bridge/app/session.py bridge/tests/test_experiment_group.py
git commit -m "feat(auth): shared experiment arm + member token, bridge re-imports (PR5 D1/D13)"
```

---

### Task 2: Bridge stamps `member_token` on session + session_start payload

**Files:**
- Modify: `bridge/app/session.py:253-271` (session meta mapping)
- Modify: `bridge/app/mcp_server.py:430-441` (session_start replay payload)
- Test: `bridge/tests/test_experiment_group.py` (extend)

**Interfaces:**
- Consumes: `member_token` from Task 1 (import into both files: `from auth.experiment import member_token` — in `session.py` add it to the Task 1 import line; `mcp_server.py` imports it from `app.session` alongside `_experiment_group` or directly from `auth.experiment`, matching the file's existing style at line 32).
- Produces: session hash field `member_token` (`""` when absent) and session_start payload key `"member_token"` (None when absent — the payload side keeps None, mirroring `experiment_group` at `mcp_server.py:438`). Task 3's compute reads the payload key.

- [ ] **Step 1: Write the failing test**

Extend `bridge/tests/test_experiment_group.py`, following the file's existing fixture style for creating a session (reuse whatever fakeredis/SessionManager setup the existing arm-stamp test uses):

```python
async def test_member_token_stamped_beside_experiment_group(manager):
    """PR5 D13: the token is written at the same point as the arm, from the
    same owner_member, '' when absent (Redis hashes cannot store None)."""
    import hashlib
    sid = await manager.start_session(goal="g", agent_id="a", owner_member="mogan")
    meta = await manager._r.hgetall(manager._session_key(sid))
    expected = hashlib.sha256(b"mogan").hexdigest()[:12]
    assert meta["member_token"] == expected

    sid2 = await manager.start_session(goal="g", agent_id="a", owner_member=None)
    meta2 = await manager._r.hgetall(manager._session_key(sid2))
    assert meta2["member_token"] == ""
```

Adapt the exact `start_session` signature/fixture names to what the existing tests in that file use (read them first); the assertions are the contract.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd bridge && python -m pytest tests/test_experiment_group.py -v`
Expected: FAIL — `KeyError: 'member_token'`

- [ ] **Step 3: Stamp the token in `session.py`**

At `session.py:257`, beside the arm:

```python
        experiment_group = _experiment_group(owner_member)
        # PR5 D13: one-way member key for arm analytics — same owner_member,
        # same moment, so token and arm cannot disagree about the member.
        token = member_token(owner_member)
```

In the `hset` mapping (after the `"experiment_group"` line at `session.py:271`):

```python
            "member_token": token or "",
```

- [ ] **Step 4: Add the payload key in `mcp_server.py`**

In the payload dict at `mcp_server.py:430-439`, after the `"experiment_group"` entry:

```python
            # PR5 D13: rides the same path experiment_group does, into the
            # parsed eval record, so members-per-arm is computable there.
            "member_token": member_token(owner_member),
```

- [ ] **Step 5: Run the test, then the bridge suite**

Run: `cd bridge && python -m pytest tests/test_experiment_group.py -v` → PASS
Run: `cd bridge && python -m pytest tests/ -v` → PASS (no existing payload-shape test may pin an exhaustive key set; if one does, extend its expectation — the payload is additive by design).

- [ ] **Step 6: Commit**

```bash
git add bridge/app/session.py bridge/app/mcp_server.py bridge/tests/test_experiment_group.py
git commit -m "feat(bridge): stamp member_token on session + session_start payload (PR5 D13)"
```

---

### Task 3: `EvalResult` gains `member_token` and `briefing_id`; compute reads both

**Files:**
- Modify: `cortex/app/evals/models.py:85-98` (two new optional fields)
- Modify: `cortex/app/evals/compute.py:179-256` (read from start_payload, pass to EvalResult)
- Test: `cortex/tests/test_eval_attribution.py` (extend)

**Interfaces:**
- Consumes: session_start payload keys `member_token` (Task 2) and `briefing_id` (already emitted — `mcp_server.py:433`).
- Produces: `EvalResult.member_token: str | None` and `EvalResult.briefing_id: str | None` — stored in the eval JSON, so Task 6's record-level classifier reads `record.get("member_token")` / `record.get("briefing_id")` with no further plumbing. `briefing_id` exists to make D12's nudge_shown coverage joinable (the spec registers the metric; the bool `briefing_delivered` cannot join to a receipt keyed by briefing_id).

- [ ] **Step 1: Write the failing test**

In `cortex/tests/test_eval_attribution.py`, find the existing test that builds a session_start event and asserts `experiment_group` lands on the EvalResult; add beside it (reusing its event/summary fixtures):

```python
def test_member_token_and_briefing_id_ride_the_session_start_payload(...):
    # payload {"member_token": "abc123def456", "briefing_id": "b-1", ...}
    # → EvalResult.member_token == "abc123def456"
    # → EvalResult.briefing_id == "b-1"
    ...

def test_member_token_absent_key_reads_none(...):
    # A pre-PR5 payload has no member_token key → None, never "".
    ...
```

Write these as real tests in the file's existing style — the two assertions above are the contract; the fixtures come from the neighboring `experiment_group` test.

- [ ] **Step 2: Run to verify failure**

Run: `cd cortex && python -m pytest tests/test_eval_attribution.py -v`
Expected: FAIL — EvalResult has no field `member_token`.

- [ ] **Step 3: Add the model fields**

In `cortex/app/evals/models.py`, after `experiment_group` (line 96):

```python
    # PR5 D13: one-way member key (sha256(owner_member)[:12]) riding the same
    # path as experiment_group — the member-level analysis groups on it. None
    # covers both an unattributed session and a pre-PR5 record.
    member_token: str | None = None
    # PR5 D12: the briefing this session received, so the nudge_shown receipt
    # (keyed by briefing_id) is joinable per session. briefing_delivered above
    # stays the exposure receipt; this is the join key, not a new receipt.
    briefing_id: str | None = None
```

- [ ] **Step 4: Read them in compute**

In `cortex/app/evals/compute.py`: beside the local vars at lines 180-184 add `member_token: str | None = None` and `briefing_id: str | None = None`; in the start_payload block (after the `experiment_group = _attr("experiment_group")` line at :231) add:

```python
            member_token = _attr("member_token")
            briefing_id = _str_or_none(start_payload.get("briefing_id")) \
                if "briefing_id" in start_payload else None
```

(Match `_attr`'s actual absent-guarded semantics — if `_attr` already returns None for missing keys and empty strings, use `briefing_id = _attr("briefing_id")` instead; read `_attr`'s definition first and use the same helper for both if it fits. The requirement: absent key → None; empty string → None.)

Pass both to the `EvalResult(...)` construction at lines 250-256: `member_token=member_token, briefing_id=briefing_id,`.

- [ ] **Step 5: Run the tests**

Run: `cd cortex && python -m pytest tests/test_eval_attribution.py -v` → PASS
Run: `cd cortex && python -m pytest tests/test_compliance_adoption.py tests/test_autopilot_api.py -v` → PASS (freeze guards: additive fields must not move any existing number).

- [ ] **Step 6: Commit**

```bash
git add cortex/app/evals/models.py cortex/app/evals/compute.py cortex/tests/test_eval_attribution.py
git commit -m "feat(evals): member_token + briefing_id ride the eval record (PR5 D13/D12)"
```

---

### Task 4: The nudge section — flag, arm routing, D12 receipt, envelope field, render

**Files:**
- Modify: `cortex/app/config.py` (one flag, beside `PATTERN_EXPERIMENTS_ENABLED` at :518)
- Modify: `cortex/app/briefing/sections.py` (new section fn + text constant + receipt)
- Modify: `cortex/app/briefing/api.py:60-116` (arm computation, builder entry, envelope field)
- Modify: `cortex/app/briefing/render.py` (emit the section last)
- Test: `cortex/tests/test_grading_nudge_section.py` (new)

**Interfaces:**
- Consumes: `experiment_group` from Task 1 (`from auth.experiment import experiment_group`).
- Produces: `grading_nudge_section(replay_redis, briefing_id: str, group: str | None) -> Section` with `data` shape `{"group": group, "shown": bool, "text": str}` (`text` == `GRADING_NUDGE_TEXT` when shown, `""` otherwise); envelope key `"experiment_group"` on the `GET /briefing` response; Redis receipt key `f"rp:nudge_shown:{briefing_id}"`. Constants `TREATMENT_ARM = "A"` and `GRADING_NUDGE_TEXT` live in `sections.py`; Task 6 imports `TREATMENT_ARM` from there (single definition).

- [ ] **Step 1: Write the failing tests**

`cortex/tests/test_grading_nudge_section.py` (use the same fakeredis + settings-override fixtures the neighboring briefing/section tests use — read one first):

```python
"""PR5 D2/D3/D12: the treatment section. The text assertions are BYTE
comparisons against the pre-registered wording — a drifted character is a
new experiment, so the test refuses it."""
import pytest

from app.briefing.sections import (
    GRADING_NUDGE_TEXT, TREATMENT_ARM, grading_nudge_section,
)

EXPECTED_TEXT = (
    "## Grade this task when you finish\n"
    "When you call `ctx_complete_session`, pass `task_result` — `success`, "
    "`partial`, or `failure` — with `task_evidence` naming what you actually "
    "verified. An honest `failure` or `partial` is expected and safe to "
    "report; it is worth more to this team than an unexamined `success`. "
    "Ungraded sessions teach nothing."
)


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
```

Also add a render test (same file):

```python
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
```

And an envelope/route test — extend the existing `GET /briefing` route test file (find it via `grep -l "briefing_id" cortex/tests/`) with: response contains top-level `"experiment_group"` equal to `experiment_group(member_id)` for the authenticated identity, and the route still returns 200 when the nudge section raises (section fault isolation — briefing availability is never hostage to the section, spec Testing bullet 4).

- [ ] **Step 2: Run to verify failure**

Run: `cd cortex && python -m pytest tests/test_grading_nudge_section.py -v`
Expected: FAIL — ImportError (`GRADING_NUDGE_TEXT` etc. undefined).

- [ ] **Step 3: Add the flag**

`cortex/app/config.py`, beside `PATTERN_EXPERIMENTS_ENABLED` (line 518):

```python
    # Outcome truth PR5 (D4): the controlled grading-nudge experiment. Ships
    # dark — the flip to True is T0, a dated act recorded in the spec's
    # addendum, never a default. GRADING_NUDGE_T0 is set at the same moment
    # (ISO-8601 UTC): the arm_comparison readout admits only sessions with
    # created_at >= T0, so an empty T0 means "not started" even if the flag
    # were flipped alone.
    GRADING_NUDGE_ENABLED: bool = False
    GRADING_NUDGE_T0: str = ""
```

- [ ] **Step 4: Implement the section**

In `cortex/app/briefing/sections.py` (after `strategy_tips_section`, ~line 211):

```python
# Outcome truth PR5. TREATMENT_ARM resolved by the D9 commit-hash coin
# (spec revision 47d8e17: first hex digit 4, even -> "A"). The text is the
# pre-registered intervention (D3) — byte-frozen by its test; changing a
# character is a NEW experiment requiring a new dated registration.
TREATMENT_ARM = "A"
GRADING_NUDGE_TEXT = (
    "## Grade this task when you finish\n"
    "When you call `ctx_complete_session`, pass `task_result` — `success`, "
    "`partial`, or `failure` — with `task_evidence` naming what you actually "
    "verified. An honest `failure` or `partial` is expected and safe to "
    "report; it is worth more to this team than an unexamined `success`. "
    "Ungraded sessions teach nothing."
)
# 30 days — the eval retention window (D12: receipt lives exactly as long
# as the eval it joins to). If evals export a TTL constant, import it
# instead of this literal.
_NUDGE_SHOWN_TTL = 30 * 86400


async def grading_nudge_section(replay_redis, briefing_id: str,
                                group: str | None) -> Section:
    """PR5 D2/D3/D12: server-composed treatment section; control is absence.

    Withhold-on-record-failure (strategy-tips precedent, D12): if the
    nudge_shown receipt cannot be written, the section is NOT shown — an
    unrecorded exposure corrupts the A/B loop. The receipt is server-side
    proof of composition; briefing_delivered stays the exposure receipt.
    """
    withheld = {"group": group, "shown": False, "text": ""}
    if not get_settings().GRADING_NUDGE_ENABLED:
        return {"status": "empty", "error": None, "data": withheld}
    if group != TREATMENT_ARM:
        return {"status": "ok", "error": None, "data": withheld}
    try:
        await replay_redis.set(
            f"rp:nudge_shown:{briefing_id}", group, ex=_NUDGE_SHOWN_TTL)
    except Exception as exc:
        return {"status": "ok",
                "error": f"nudge-shown record failed: {exc}",
                "data": withheld}
    return {"status": "ok", "error": None,
            "data": {"group": group, "shown": True,
                     "text": GRADING_NUDGE_TEXT}}
```

Add `from auth.experiment import experiment_group` where `api.py` needs it (next step); `sections.py` itself needs only `get_settings` (already imported — verify).

- [ ] **Step 5: Wire the route**

In `cortex/app/briefing/api.py`: import `from auth.experiment import experiment_group`. Inside `get_briefing` after `ab_group` (line 71):

```python
        # PR5 D1: member-level arm, from the SAME verified member string
        # bridge later stamps on the session — delivered arm == recorded arm.
        arm = experiment_group(identity.get("member_id"))
```

Add to `builders`:

```python
            "grading_nudge": S.grading_nudge_section(st.replay_redis, briefing_id, arm),
```

Add to the response dict (beside `"briefing_id"`, line 111): `"experiment_group": arm,` (D5 — the envelope carries the arm).

- [ ] **Step 6: Emit in render**

In `cortex/app/briefing/render.py`, inside `render_briefing`, AFTER the last existing `emit(...)` call (read the function tail to find it) and before the final return:

```python
    # PR5 D2/D3: the grading-nudge treatment section, emitted LAST — it is an
    # instruction about how the session should END, so it is the final thing
    # read. Verbatim: the text is the registered intervention.
    def _nudge(d):
        if d.get("shown") and d.get("text"):
            lines.append(d["text"])
    emit("grading_nudge", _nudge)
```

- [ ] **Step 7: Run the tests**

Run: `cd cortex && python -m pytest tests/test_grading_nudge_section.py -v` → PASS
Run: `cd cortex && python -m pytest tests/ -v -k "briefing"` → PASS (existing route tests may pin the envelope key set or the section-name list — extend those expectations additively where they do).

- [ ] **Step 8: Commit**

```bash
git add cortex/app/config.py cortex/app/briefing/sections.py cortex/app/briefing/api.py cortex/app/briefing/render.py cortex/tests/test_grading_nudge_section.py
git commit -m "feat(briefing): dark-deployed grading-nudge section + arm envelope (PR5 D2-D5, D12)"
```

---

### Task 5: The member-level permutation test (pure stdlib)

**Files:**
- Create: `cortex/app/autopilot/permutation.py`
- Test: `cortex/tests/test_permutation.py` (new)

**Interfaces:**
- Produces: `permutation_test_member_means(arm_a: list[float], arm_b: list[float], *, max_exact: int = 10000, mc_draws: int = 10000) -> dict` returning `{"p_value": float, "diff": float, "mean_a": float, "mean_b": float, "method": "exact" | "monte_carlo", "reassignments": int}`. `diff = mean_a - mean_b`. Task 6 calls exactly this.
- No scipy, no numpy — `itertools.combinations`, `math`, `random.Random(0)` (fixed seed: the pre-registered analysis must be reproducible run-to-run).

- [ ] **Step 1: Write the failing test**

`cortex/tests/test_permutation.py`:

```python
"""PR5 D8: the member-level primary analysis. The 4-vs-4 case is fully
hand-worked; the 3-vs-3 floor case proves why D8 requires >= 5 members/arm
(C(6,3)=20 -> the smallest attainable two-sided p is exactly 2/20 = 0.1,
which can never satisfy p < 0.05)."""
from itertools import combinations

from app.autopilot.permutation import permutation_test_member_means


def test_four_vs_four_hand_worked():
    # Members' graded fractions. Observed diff = 0.75 - 0.25 = 0.5.
    a = [1.0, 0.8, 0.6, 0.6]   # mean 0.75
    b = [0.4, 0.3, 0.2, 0.1]   # mean 0.25
    r = permutation_test_member_means(a, b)
    assert r["method"] == "exact"
    assert r["reassignments"] == 70          # C(8,4)
    assert abs(r["diff"] - 0.5) < 1e-12
    # Hand count: pooled = [1.0,.8,.6,.6,.4,.3,.2,.1]. |mean_A - mean_B|
    # >= 0.5 holds only for the observed split and its mirror -> p = 2/70.
    assert abs(r["p_value"] - 2 / 70) < 1e-12


def test_three_vs_three_floor_is_two_twentieths():
    a, b = [1.0, 1.0, 1.0], [0.0, 0.0, 0.0]
    r = permutation_test_member_means(a, b)
    assert r["reassignments"] == 20          # C(6,3)
    assert abs(r["p_value"] - 2 / 20) < 1e-12  # observed + mirror


def test_null_data_is_not_significant():
    a = [0.5, 0.5, 0.5, 0.5, 0.5]
    b = [0.5, 0.5, 0.5, 0.5, 0.5]
    r = permutation_test_member_means(a, b)
    assert r["p_value"] == 1.0
    assert r["diff"] == 0.0


def test_monte_carlo_kicks_in_and_is_deterministic():
    a = [i / 20 for i in range(10)]
    b = [(i + 5) / 20 for i in range(10)]    # C(20,10) = 184756 > 10000
    r1 = permutation_test_member_means(a, b)
    r2 = permutation_test_member_means(a, b)
    assert r1["method"] == "monte_carlo"
    assert r1["p_value"] == r2["p_value"]    # fixed seed -> reproducible
```

(Verify the 3-vs-3 hand count while implementing: with maximal separation, only the observed assignment and its mirror reach |diff| = 1.0, hence 2/20. If your implementation counts >= with floating tolerance differently, fix the implementation, not the test.)

- [ ] **Step 2: Run to verify failure**

Run: `cd cortex && python -m pytest tests/test_permutation.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement**

`cortex/app/autopilot/permutation.py`:

```python
"""Exact/Monte-Carlo permutation test on member-level proportions (PR5 D8).

The randomization unit is the member, so the confirmatory test permutes
MEMBERS across arms — never sessions. Pure stdlib, deterministic: the exact
path enumerates every reassignment; the Monte-Carlo path uses a fixed seed
(a pre-registered analysis must give the same p on every run over the same
snapshot).
"""
import math
import random
from itertools import combinations


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def permutation_test_member_means(
    arm_a: list[float], arm_b: list[float], *,
    max_exact: int = 10000, mc_draws: int = 10000,
) -> dict:
    if not arm_a or not arm_b:
        raise ValueError("both arms need at least one member")
    pooled = list(arm_a) + list(arm_b)
    n_a = len(arm_a)
    observed = _mean(arm_a) - _mean(arm_b)
    total = math.comb(len(pooled), n_a)
    threshold = abs(observed) - 1e-12  # float-tolerant >=

    def diff_of(indices: tuple[int, ...]) -> float:
        chosen = set(indices)
        a = [pooled[i] for i in chosen]
        b = [pooled[i] for i in range(len(pooled)) if i not in chosen]
        return _mean(a) - _mean(b)

    if total <= max_exact:
        hits = sum(
            1 for idx in combinations(range(len(pooled)), n_a)
            if abs(diff_of(idx)) >= threshold
        )
        return {"p_value": hits / total, "diff": observed,
                "mean_a": _mean(arm_a), "mean_b": _mean(arm_b),
                "method": "exact", "reassignments": total}

    rng = random.Random(0)
    indices = list(range(len(pooled)))
    hits = 0
    for _ in range(mc_draws):
        sample = tuple(rng.sample(indices, n_a))
        if abs(diff_of(sample)) >= threshold:
            hits += 1
    # +1/+1: the observed assignment is always a member of the null set —
    # keeps Monte-Carlo p strictly positive and slightly conservative.
    return {"p_value": (hits + 1) / (mc_draws + 1), "diff": observed,
            "mean_a": _mean(arm_a), "mean_b": _mean(arm_b),
            "method": "monte_carlo", "reassignments": mc_draws}
```

- [ ] **Step 4: Run the tests**

Run: `cd cortex && python -m pytest tests/test_permutation.py -v` → PASS

- [ ] **Step 5: Commit**

```bash
git add cortex/app/autopilot/permutation.py cortex/tests/test_permutation.py
git commit -m "feat(autopilot): member-level permutation test, pure stdlib (PR5 D8)"
```

---

### Task 6: `arm_comparison` — D6 classification, D8 gates, balance, coverage

**Files:**
- Create: `cortex/app/autopilot/arm_comparison.py`
- Modify: `cortex/app/autopilot/compliance.py:485-494` (`build_compliance` gains the block)
- Test: `cortex/tests/test_arm_comparison.py` (new)

**Interfaces:**
- Consumes: parsed eval dicts (fields: `experiment_group`, `member_token`, `briefing_id`, `briefing_delivered`, `runtime`, `created_at`, `task_result`, `task_result_source`, `metrics`, `failure_event_ids`); `permutation_test_member_means` (Task 5); `TREATMENT_ARM` from `app.briefing.sections`; from `app.patterns.statistics`: `_chi_square_2x2(a, b, c, d) -> (chi2, p)`, `_cohens_h(p1, p2) -> float`, `_confidence_interval_diff(p1, n1, p2, n2) -> (lo, hi)`; from `app.autopilot.compliance`: `_parse_created_at`, `_is_self_success`, `_is_skew_hit`, and the `grade_self_reported` predicate via the `INSTRUCTIONS` table + `_predicate_input` (never re-implement the grade predicate — look it up: `next(p for k, _, _, p in INSTRUCTIONS if k == "grade_self_reported")`).
- Produces: `async def build_arm_comparison(replay_redis, evals: list[dict]) -> dict` and the key `"arm_comparison"` on the `/autopilot/compliance` payload.

**Pre-registered constants (module level, verbatim from spec D6/D8):**

```python
EXPOSED_RUNTIME = "claude"          # D6: only verified model-facing channel
MIN_MEMBERS_PER_ARM = 5             # D8 floor (permutation resolution)
MIN_SESSIONS_PER_MEMBER = 5         # D8: qualifying-member floor
MIN_SESSIONS_PER_ARM = 99           # D8 fixed-z bound (descriptive test)
MIN_ARM_MEAN_DIFF = 0.10            # D8: practical-significance gate
BALANCE_MAX_PP_FRACTION_GAP = 0.10  # D6 absolute bound (a)
BALANCE_MAX_SINGLE_MEMBER_SHARE = 0.50  # D6 absolute bound (b)
NONINFERIORITY_MARGIN = 0.10        # H2' margin, +10pp
NONINFERIORITY_Z = 1.645            # one-sided 95%
MIN_SELF_SUCCESS_PER_ARM = 30       # H2' gate (H2's bound, per arm)
```

**Behavior (implement exactly):**

1. **T0 gate.** `t0 = GRADING_NUDGE_T0` from settings, parsed as ISO-8601. Empty/unparseable → the block is `{"status": "not_started", "confirmatory": False, "note": "GRADING_NUDGE_T0 unset — the experiment has not begun (spec D4); no session is per-protocol."}` and nothing else runs.
2. **Classification (D6), record-by-record over `evals`:** arm = `record.get("experiment_group")`; not in `("A", "B")` → excluded (counted `no_arm`). Arm sessions with `created_at` parseable and `>= t0` form **ITT** (undated arm records → `unknown`). Within ITT: `briefing_delivered is True and runtime == EXPOSED_RUNTIME` → **per_protocol**; `briefing_delivered is False or (runtime not in (None, "") and runtime != EXPOSED_RUNTIME)` → **not_exposed**; anything else (None receipts, missing runtime) → **unknown**. Absence is never not_exposed.
3. **Graded predicate:** the frozen `grade_self_reported` predicate over `_predicate_input(record)` — looked up from `INSTRUCTIONS`, never copied.
4. **Balance (D6):** per arm: ITT count, PP count, PP fraction (`pp/itt`), sessions-per-member table (member_token → PP count; records with `member_token` None grouped as `"unknown"` and excluded from member analysis), runtime mix over ITT, max single-member share of PP. Violations: `|pp_frac_A - pp_frac_B| > BALANCE_MAX_PP_FRACTION_GAP` or either arm's max share `> BALANCE_MAX_SINGLE_MEMBER_SHARE` → `"balance_violated": true` and H1′ reports `"status": "balance_violated"` (descriptive numbers still shown).
5. **H1′ primary (D8):** qualifying members = token != None and PP sessions ≥ `MIN_SESSIONS_PER_MEMBER`; per-member graded fraction; floors — qualifying members per arm ≥ `MIN_MEMBERS_PER_ARM` AND PP sessions per arm ≥ `MIN_SESSIONS_PER_ARM`, else `"status": "insufficient_n"` with all counts. Otherwise run `permutation_test_member_means(fractions_A, fractions_B)`; `"holds": p < 0.05 and (mean_A - mean_B) >= MIN_ARM_MEAN_DIFF` (treatment is A per `TREATMENT_ARM` — if `TREATMENT_ARM` were "B", the sign flips; write it as `mean_treatment - mean_control` computed via the constant, never hardcoded A-minus-B).
6. **Descriptive session-level:** pooled PP graded counts per arm → `_chi_square_2x2`, `_cohens_h`, `_confidence_interval_diff`, under key `"session_level_descriptive"` with `"note": "descriptive only — sessions cluster within members; the member-level permutation above is the primary analysis (spec D8)"`.
7. **H2′ (non-inferiority):** per arm over PP: self-success sessions (`_is_self_success`), skew hits (`_is_skew_hit`); both arms need ≥ `MIN_SELF_SUCCESS_PER_ARM` self-success sessions else `insufficient_n`. Then `d = skew_T - skew_C`, `se = sqrt(sT(1-sT)/nT + sC(1-sC)/nC)`, upper = `d + NONINFERIORITY_Z * se`; conditions: `skew_T <= 0.15` and `upper < NONINFERIORITY_MARGIN` → `"holds": true`.
8. **nudge_shown coverage (D12):** collect `briefing_id`s of treatment-arm PP records (skip None); `MGET` `rp:nudge_shown:{id}` for each; coverage = non-null fraction (None when no ids). `"flagged": coverage is not None and coverage < 0.9`.
9. **Always:** `"confirmatory": False` with note `"the verdict of record is the dated T0+28d snapshot committed as a spec addendum (D14); every live view is operational monitoring"`, plus the interference caveat note: `"both disclosed spillover channels (in-repo text, shared memory) bias toward null — a positive H1' survives them; a null H1' is ambiguous between no-effect and ambient saturation"`.

**Wiring:** `build_compliance` (compliance.py:487-494) gains `"arm_comparison": await build_arm_comparison(replay_redis, evals),` — inside the payload dict. `build_arm_comparison` must never raise (wrap the body; on exception return `{"status": "error", "confirmatory": False, "error": str(exc)}`) — the compliance surface's availability is not hostage to the new block.

- [ ] **Step 1: Write the failing tests**

`cortex/tests/test_arm_comparison.py` — fixtures are plain dicts shaped like parsed evals. Cover, at minimum (write each as a real test; a record-builder helper keeps them short):

```python
def _rec(arm="A", token="m1", delivered=True, runtime="claude",
         created="2026-09-10T00:00:00+00:00", graded=True, briefing="b1",
         self_success=False, skew=False):
    r = {"experiment_group": arm, "member_token": token,
         "briefing_delivered": delivered, "runtime": runtime,
         "created_at": created, "briefing_id": briefing,
         "metrics": {}, "failure_event_ids": []}
    if graded:
        r["task_result"] = "success" if self_success else "partial"
        r["task_result_source"] = "self_reported"
    if self_success and skew:
        r["failure_event_ids"] = ["ev1"]
    return r
```

- `not_started` when `GRADING_NUDGE_T0` is empty.
- D6 classification: pre-T0 record excluded from ITT; `delivered=False` → not_exposed; `delivered=None` → unknown; `runtime="codex"` → not_exposed; `runtime=None` → unknown; no arm → no_arm.
- `insufficient_n`: 4 qualifying members per arm (each with 5+ PP sessions, 99+ sessions per arm via repetition) → insufficient (members floor); 5 members but 98 sessions → insufficient (sessions floor). Both reported with counts.
- H1′ holds on a constructed clear win: 5 members/arm × 20 PP sessions each (100/arm), treatment members ~0.8 graded fraction, control ~0.2 → `holds` True, permutation p below 0.05, diff ≥ 0.10.
- Balance bound (a): make control ITT much larger with equal PP so pp-fractions differ > 10pp → `balance_violated`, H1′ status `balance_violated`.
- Balance bound (b): one member owning > 50% of an arm's PP → violated.
- H2′: both arms ≥ 30 self-success, treatment skew 0 vs control 0 → holds; treatment skew far above control → fails; 29 self-success → insufficient_n.
- Coverage: two treatment PP records, one with `rp:nudge_shown:` key set in fakeredis → coverage 0.5, flagged True.
- Never-raise: a record that is garbage (`{"experiment_group": "A"}` alone) does not crash the block.
- Freeze guard: `build_compliance` output with the new key present — existing `instructions` rows and `optimism_skew` byte-identical to a no-PR5 expectation (extend the existing freeze test in `test_compliance_adoption.py` if one pins the payload key set).

- [ ] **Step 2: Run to verify failure** — `cd cortex && python -m pytest tests/test_arm_comparison.py -v` → module not found.

- [ ] **Step 3: Implement `arm_comparison.py`** per the Behavior contract above (single `build_arm_comparison` plus small pure helpers `_classify`, `_member_fractions`, `_balance`, `_h2_noninferiority`, `_coverage` — keep each testable).

- [ ] **Step 4: Wire into `build_compliance`** (one line in the payload dict; `build_compliance` is already async and holds `replay_redis` and `evals`).

- [ ] **Step 5: Run** `cd cortex && python -m pytest tests/test_arm_comparison.py tests/test_compliance_adoption.py tests/test_autopilot_api.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/autopilot/arm_comparison.py cortex/app/autopilot/compliance.py cortex/tests/test_arm_comparison.py cortex/tests/test_compliance_adoption.py
git commit -m "feat(autopilot): arm_comparison block — member-level primary, balance, coverage (PR5 D6-D8, D14)"
```

---

### Task 7: Guides — dated additions

**Files:**
- Modify: `docs/guides/replay-evals-patterns.md`
- Modify: `docs/guides/knowledge-autopilot.md`
- Modify: `docs/guides/bridge-context-and-briefing.md`

**Interfaces:** Prose only; every addition is dated **2026-08-26** and references the spec by path. `CLAUDE.md` stays untouched (the guides carry it).

- [ ] **Step 1: `replay-evals-patterns.md`** — find the PR4-vs-PR5 paragraph (grep `PR5`); add a dated note: the nudge ships server-composed in the briefing `rendered` (not base.py — that channel is hash-pinned and arm-shared); H3′ (McNemar/κ) is NOT registered, deferred to the Tier-2 judge under its own future registration; the `arm_comparison` block on `/autopilot/compliance` is the readout, its primary analysis member-level (permutation), session-level χ² descriptive-only; the verdict of record is the dated T0+28d snapshot (D14) — live views are non-confirmatory.

- [ ] **Step 2: `knowledge-autopilot.md`** — in the compliance section (§6 area), add a dated paragraph describing the `arm_comparison` block: D6 populations (per-protocol / ITT / unknown / not_exposed), absolute balance bounds (10pp PP-fraction gap, 50% single-member share), D8 floors (5 members × 5 sessions, 99 sessions/arm), H2′ non-inferiority (one-sided α=0.05, +10pp margin), `nudge_shown` coverage, `confirmatory: false` semantics, and the two config vars `GRADING_NUDGE_ENABLED` / `GRADING_NUDGE_T0` (both set only at the T0 flip, spec D4). Include D11: if PR4-H2's N ≥ 30 gate is unmet at T0, the PR4-H2 readout is thereafter taken from the control arm only (the existing `optimism_skew.by_experiment_group` split already carries the per-arm numbers — a readout rule, not new code).

- [ ] **Step 3: `bridge-context-and-briefing.md`** — dated note: the briefing envelope gains `experiment_group`; a `grading_nudge` section exists (dark until T0), rendered last, withhold-on-record-failure; sessions now stamp `member_token` beside `experiment_group` (D13, one-way sha256 prefix) and the session_start payload carries it into the eval record along with `briefing_id`.

- [ ] **Step 4: Check the doc-default guard tests** — `cd cortex && python -m pytest tests/ -v -k "doc or guide"` (several tests assert documented defaults match code; if any pins these guides' config tables, keep them consistent).

- [ ] **Step 5: Commit**

```bash
git add docs/guides/replay-evals-patterns.md docs/guides/knowledge-autopilot.md docs/guides/bridge-context-and-briefing.md
git commit -m "docs(guides): PR5 grading-nudge experiment — dated additions"
```

---

## Post-plan (controller, not a task)

Full suites: `cortex`, `bridge`, root shared-module run (`python -m pytest auth/tests/ replay/tests/ -v`). Final whole-branch review per SDD. Then finishing-a-development-branch. Deploy is DARK per spec D4 (cortex containers + bridge rebuild; flag stays False; no flip). The T0 flip is a separate, later, human-calendar act: PR4 readout addendum first, then `GRADING_NUDGE_ENABLED=true` + `GRADING_NUDGE_T0=<ISO now>` in the VPS `.env`, addendum committed — no earlier than 2026-09-08.
