# Outcome Truth PR4 — grading adoption instrumentation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Instrument the grading-adoption experiment and ship the mild server-side nudge, so PR1–3's now-inert apparatus becomes measurable and lightly nudged — all server-side, no client-kit change.

**Architecture:** Additive. An orthogonal `experiment_group` label stamped per session through the existing attribution seam; a freeze-safe adoption row + an optimism-skew detector in the compliance layer; a strengthened `ctx_complete_session` tool description. No new service, no new store, no grade-CAS change, no `statistics.py`/`Experiment` machinery (that is PR5).

**Tech Stack:** Python 3.11, FastAPI/FastMCP, Redis replay (DB 6) + bridge sessions (DB 3), pytest (fakeredis).

**Spec:** `docs/superpowers/specs/2026-08-25-outcome-truth-pr4-adoption-design.md` (D1–D5). The plan argues from the spec; conflicts resolve against it. **This spec is a pre-registration — it must be committed before D4's nudge can move any number.**

## Global Constraints

- **Explicit-file commits only.** `git add <exact paths>` — never `git add -A`. Never stage the peer's uncommitted `client/*`, `studio/`, `README.md`, root `CLAUDE.md`, `docs/MCP-TOOLS.md`, `docs/SETUP-*.md`, `docs/guides/agent-gateway-and-policy.md`, `scripts/installlab/lab.py`, `docs/marketing/`, `scripts/demo/`, or any `*firekeep-studio*` doc.
- **PR4 is server-side only** — bridge, cortex, docs. Touch NO file under `client/` (peer is mid-release there).
- **The compliance freeze is inviolate.** Existing frozen predicate rows must produce byte-identical output. Add the grade row; enrich the predicate-input dict at the call site; never mutate an existing predicate or change the `Callable[[_Metrics], bool]` signature.
- **`experiment_group` is orthogonal to the grade** — assigned at session start from a STABLE hash of `owner_member` (NOT Python `hash()`, which is per-process-salted); empty `owner_member` → `None` (not a hashed arm). Never a function of the outcome; never read the grade.
- **D3 is visibility-only** — no gating, no mutation. Never treat `tool_success_rate`/`failure_rate` as independent of the self-report unless `outcome_event_count >= 2`.
- **No grade-CAS / recognized-grade-contract / receipt / skill-efficacy change.**

---

### Task 1: Stamp `experiment_group` per session onto the eval record (D1)

**Files:**
- Modify: `bridge/app/session.py` — `start_session` (~199-259): derive + store `experiment_group` in the session meta beside `owner_member` (~234-259).
- Modify: `bridge/app/mcp_server.py` — the `session_start` replay payload (~429-435): add `"experiment_group"`.
- Modify: `cortex/app/evals/models.py` — `EvalResult` (~82-91): add `experiment_group: str | None = None`.
- Modify: `cortex/app/evals/compute.py` — the session_start attribution loop (~196-224): read `experiment_group` like `runtime`/`briefing_delivered`, thread it onto the `EvalResult` constructor (~232-249).
- Test: `bridge/tests/` (assignment) + `cortex/tests/` (reaches EvalResult).

**Interfaces:**
- Produces: `EvalResult.experiment_group ∈ {"A","B",None}` per session — the pre-dated arm label PR5 consumes.

**Implementation notes:**
- Assignment helper (bridge, at session start):
```python
import hashlib
def _experiment_group(owner_member: str | None) -> str | None:
    if not owner_member:            # auth disabled / unauthenticated → excluded from arms
        return None
    h = int(hashlib.sha256(owner_member.encode("utf-8")).hexdigest(), 16)
    return "A" if h % 2 == 0 else "B"
```
- Compute it at `start_session` from the same verified `owner_member` written to meta (`session.py:242`), store it in the meta mapping (~234-259), and add it to the `session_start` replay payload (`mcp_server.py:429-435`) beside the other attribution fields.
- In `compute_session_eval`, read it from the `session_start` payload in the existing attribution loop (`compute.py:196-224`) with the same absent-vs-present guard the other attribution fields use, and pass `experiment_group=...` to `EvalResult(...)`. Default `None` on the model so old records parse.

- [ ] **Step 1: Write the failing tests** — (a) `_experiment_group` is deterministic and STABLE for a given `owner_member` (same arm across calls/process restarts — assert against a hardcoded expected arm for a fixed member string), splits members across A/B, and returns `None` for `""`/`None`; (b) a session started with a verified member carries `experiment_group` into its `session_start` payload; (c) `compute_session_eval` reads it onto `EvalResult.experiment_group`; an old session_start without the field → `None`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** across the four files.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Full bridge + cortex suites** → green (grade CAS + attribution unchanged).
- [ ] **Step 6: Commit** — `git add bridge/app/session.py bridge/app/mcp_server.py cortex/app/evals/models.py cortex/app/evals/compute.py bridge/tests/<t> cortex/tests/<t> && git commit -m "feat: stamp experiment_group per session onto the eval record (PR4 D1)"`

---

### Task 2: Adoption row in the compliance layer — freeze-safe (D2)

**Files:**
- Modify: `cortex/app/autopilot/compliance.py` — the predicate call site (~265), `INSTRUCTIONS` (~64), and the per-arm reporting.
- Test: `cortex/tests/` — the compliance test module.

**Interfaces:**
- Consumes: `EvalResult.task_result`/`task_result_source`/`experiment_group` (top-level fields); `recognized_grade_pair` from `evals.models`.
- Produces: a `grade_self_reported` compliance row reporting `graded/completed` per `experiment_group`.

**Implementation notes (freeze-safe — the load-bearing constraint):**
- At the predicate call site (`compliance.py:265`, currently `predicate(e.get("metrics", {}))`), build and pass a MERGED dict:
```python
_pm = {**(e.get("metrics") or {}),
       "task_result": e.get("task_result"),
       "task_result_source": e.get("task_result_source"),
       "experiment_group": e.get("experiment_group")}
scored = [(e, predicate(_pm_for(e))) for e in evals]   # _pm computed per e
```
  (`_Metrics` is already `dict[str, Any]`, so string values are fine. No metric key collides with the three promoted keys — existing predicates read their metric keys unchanged.)
- Add the frozen row to `INSTRUCTIONS` (append; a NEW key):
```python
    (
        "grade_self_reported",
        "Grade your task on completion (task_result)",
        "recognized (task_result, task_result_source) present",
        lambda m: recognized_grade_pair(m.get("task_result"), m.get("task_result_source")) is not None,
    ),
```
  Import `from app.evals.models import recognized_grade_pair` (verify no import cycle; use a function-local import if needed).
- Report the `grade_self_reported` row's rate split by `experiment_group` (A / B / None-excluded / overall). Keep every existing row's overall output unchanged.

- [ ] **Step 1: Write the failing tests** — (a) FREEZE GUARD: every existing frozen row's rate is byte-identical before/after the change (feed a fixture of evals, assert the existing rows' numbers didn't move); (b) `grade_self_reported` counts a session with `task_result="success"`/`source="self_reported"` as a hit and an ungraded one as a miss; (c) the row's rate is reported per `experiment_group`; (d) a non-numeric grade string never raises (goes through `recognized_grade_pair`, not `_num`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Full cortex suite** → green (ALL existing compliance tests unchanged = the freeze held).
- [ ] **Step 6: Commit** — `git add cortex/app/autopilot/compliance.py cortex/tests/<t> && git commit -m "feat(cortex): compliance adoption row grade_self_reported, per experiment_group (PR4 D2)"`

---

### Task 3: Optimism-skew honesty detector (D3)

**Files:**
- Modify: `cortex/app/autopilot/compliance.py` (or a sibling `honesty.py` in autopilot) — the deterministic skew detector; wire into `GET /autopilot/compliance` (or `/autopilot/inbox` digest) as a read-only metric.
- Test: `cortex/tests/`.

**Interfaces:**
- Consumes: per session — `task_result` (self-report), Bridge `abandoned` status (`owm._fetch_bridge_statuses` / `session_success` at `owm.py:66-85`), `has_failures`/`failure_event_ids` (`EvalResult`/`compute.py:163-166`), per-agent Brier (`trust.py:188-210`).
- Produces: an `optimism_skew` metric — the fraction of self-reported-`success` sessions carrying an independent failure contradiction, per `experiment_group`, with denominators + an `unknown`/insufficient-N bucket.

**Implementation notes:**
- A session is a **skew hit** iff `binary_outcome(task_result) == "success"` (`models.py:24-27`) AND at least one independent contradiction: Bridge status `abandoned` (hard), OR `has_failures`/non-empty `failure_event_ids` (soft), OR (only when `outcome_event_count >= 2`) `tool_success_rate < 1.0`. Do NOT use `tool_success_rate`/`failure_rate` at `outcome_event_count < 2` (they echo the self-report — `scorers.py:171-178`).
- Denominator = self-reported-`success` sessions. Report per `experiment_group`. If the denominator `< MIN_SELF_SUCCESS_N` (propose 30), report `unknown`/insufficient — never a spurious 0.0. Disclose `approximate` if the scan capped, the compliance-table way.
- Visibility-only: no writes, no gating. Reuse the eval-scan machinery compliance already has (`scan_evals`, the windowed read).

- [ ] **Step 1: Write the failing tests** — (a) a self-`success` session with Bridge `abandoned` is a skew hit; (b) a self-`success` session with `has_failures=True` is a hit; (c) a self-`success` session with `tool_success_rate=0.5` but `outcome_event_count=1` is NOT a hit (guardrail); with `outcome_event_count>=2` it IS; (d) a self-`failure` session is never a hit; (e) below `MIN_SELF_SUCCESS_N` the metric is `unknown`, not 0.0; (f) reported per `experiment_group`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Full cortex suite** → green.
- [ ] **Step 6: Commit** — `git add cortex/app/autopilot/<files> cortex/tests/<t> && git commit -m "feat(cortex): optimism-skew honesty detector, visibility-only (PR4 D3)"`

---

### Task 4: The mild server-side nudge — `ctx_complete_session` tool description (D4)

**Files:**
- Modify: `bridge/app/mcp_server.py` — the `ctx_complete_session` docstring (~661-685; FastMCP serves it as the tool description).
- Test: `bridge/tests/` — assert the description (via the tool listing) names `task_result` as the default and marks failure safe.

**Implementation notes:**
- Rework the docstring's opening so grading is the stated default: e.g. lead with "When you finish, pass `task_result` — `success`, `partial`, or `failure` — for the TASK you were doing (not whether this RPC worked). Reporting `failure`/`partial` is expected and safe; an honest failure is more useful than an optimistic success. Back it with `task_evidence` (what you actually verified)." Keep the existing param docs (`task_result`/`task_evidence` at ~665,679-684) — do not change the signature or the `TASK_RESULTS` validation.
- Do NOT touch Cortex `_INSTRUCTIONS` (`cortex/app/mcp_server.py`) — that channel is discarded by the gateway (spec §"The reality the code forces", fact 1). The tool description is the one live server→kit-agent channel.

- [ ] **Step 1: Write the failing test** — build/inspect the `ctx_complete_session` tool description (the docstring, or the FastMCP tool listing if reachable in tests) and assert it contains the `task_result` default framing and the "failure is safe" phrasing. If the tool listing isn't reachable in a unit test, assert on the function's `__doc__`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the docstring change.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Full bridge suite** → green (behavior unchanged — only the docstring/description moved).
- [ ] **Step 6: Commit** — `git add bridge/app/mcp_server.py bridge/tests/<t> && git commit -m "feat(bridge): ctx_complete_session description makes honest task grading the default (PR4 D4)"`

---

### Task 5: Docs + change-consistency + pre-registration linkage

**Files:**
- Modify: `docs/guides/knowledge-autopilot.md` — the `grade_self_reported` compliance row + the `optimism_skew` metric.
- Modify: `docs/guides/replay-evals-patterns.md` — `experiment_group` on `EvalResult`; the honesty detector; note the two-proportion/agreement stats are PR5.
- Modify: `cortex/CLAUDE.md` and `bridge` doc surface where the eval/compliance/session tools are described.
- Modify: `docs/ROADMAP.md` — link the PR4 pre-registration (the spec) as the committed adoption experiment; note PR5 is the controlled client-side arm.
- Test: none (docs).

- [ ] **Step 1:** Read the reports/ledger for the exact shipped detail. `git grep -n "experiment_group\|grade_self_reported\|optimism" docs/ cortex/CLAUDE.md` to find surfaces.
- [ ] **Step 2:** Write the updates (preserve the decision-history voice; append, don't reword unrelated prose). Be explicit that PR4 is observational instrumentation + a mild nudge and the controlled experiment is PR5.
- [ ] **Step 3:** Commit — `git add docs/guides/knowledge-autopilot.md docs/guides/replay-evals-patterns.md cortex/CLAUDE.md docs/ROADMAP.md && git commit -m "docs: PR4 adoption pre-registration — experiment_group, adoption row, honesty detector, nudge"`

---

## Self-Review

**Spec coverage:** D1→Task 1; D2→Task 2; D3→Task 3; D4→Task 4; D5 (pre-registration) = the spec itself, committed with the branch and linked in Task 5. All decisions mapped. The stats spine is explicitly PR5 (no task).

**Placeholder scan:** each code step carries real code or a named edit against a cited anchor; the two research-anchored spots (the exact compliance predicate call site shape; the exact bridge session-meta seam) are flagged "verify against the anchor," not silent TODOs.

**Type consistency:** `experiment_group: str | None` across Tasks 1/2/3; the merged predicate-input dict (Task 2) carries the three promoted keys Task 1 produces; `recognized_grade_pair`/`binary_outcome` are the `evals.models` normalizers used in Tasks 2/3; the freeze guard (existing compliance tests green) is the invariant Task 2 must not break.
