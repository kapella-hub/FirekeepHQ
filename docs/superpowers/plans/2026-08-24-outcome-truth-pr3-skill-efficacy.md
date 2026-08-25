# Outcome Truth PR3 — skill efficacy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make skills a scored, outcome-aware artifact: the `memory_read` receipt `skill_recall` lacks, a `skill_efficacy` score extended from OWM's own machinery, PR2's `memory_feedback` applied signal consumed for skills, and a read-only inbox surface.

**Architecture:** Additive. One new emit (`skill_recall` → `memory_read`), an extension of the existing OWM nightly pass (route skills instead of dropping them; distinct `skill_efficacy` field), and a read-only autopilot inbox section. No new service, no new datastore, no migration. Reuses `compute_efficacy` / `session_success` / the agent-cap / window / stale-reset unchanged.

**Tech Stack:** Python 3.11, FastAPI, Qdrant (skill points), Redis replay stream, Celery (OWM beat), pytest (fakeredis).

**Spec:** `docs/superpowers/specs/2026-08-24-outcome-truth-pr3-skill-efficacy-design.md` (D1–D4). The plan argues from the spec; conflicts resolve against it.

## Global Constraints

- **Explicit-file commits only.** `git add <exact paths>` — never `git add -A`/`.`. Never stage pre-existing dirty/untracked files (root `CLAUDE.md`, `README.md`, `studio/`, `scripts/installlab/lab.py`, `docs/marketing/`, `scripts/demo/`, any `*firekeep-studio*` doc).
- **Never write `owm_efficacy` onto a skill point.** The RAG scorer (`rag.py:1187-1192`) and GC factor read `owm_efficacy` with NO `memory_type` guard, so a skill carrying it would have its recall silently re-ranked. Skills get the DISTINCT `skill_efficacy` / `skill_efficacy_n` / `skill_efficacy_updated_at` fields only.
- **`memory_feedback` observations feed the SKILL tally only.** Never merge them into the shared `stats` used for `owm_efficacy` — memory feedback is already counted via the `set_feedback` counter (`rag.py:1194+`); double-counting is the bug to avoid (spec D3).
- **The briefing never emits a skill receipt** and never touches `last_recalled` — impression ≠ deliberate reach (`skills/api.py:109-113`). Only `skill_recall`'s `record_recall=true` path emits.
- **Corpus chunks stay excluded** from all efficacy scoring. Only skills are un-excluded.
- **Grade path and memory OWM stay behavior-identical when the skill flag is off.** `SKILL_OWM_ENABLED=false` ⇒ the skill branch is bit-neutral; `OWM_ENABLED` unchanged for memories.
- Emits are best-effort and never break the request/pass.

---

### Task 1: `skill_recall` emits a `memory_read` receipt (D1)

**Files:**
- Modify: `cortex/app/skills/api.py` — the `list_skills` `record_recall` branch (~lines 114-115).
- Test: `cortex/tests/` — the skills api test module (grep for `record_recall` / `list_skills` tests) + confirm the briefing path emits nothing.

**Interfaces:**
- Consumes: `_replay_emit` from `app.main` (deferred import); `request` (already a param of `list_skills`); `results[].id` (skill ids, already used for `_record_skill_usage`).
- Produces: a `memory_read` replay event carrying served skill ids — the join key D2 consumes.

**Implementation notes:**
- `list_skills` already takes `request: Request` and the branch already computes `[r.id for r in results]`. Add the emit right beside `_record_skill_usage`, in the SAME `if record_recall and results:` branch.
- Deferred import to avoid the cycle (skills/api.py is imported by app.main): `from app.main import _replay_emit` INSIDE the branch, wrapped so it never fails the recall.
- Read `X-Session-Id`/`X-Agent-Id` off `request.headers` with `"unknown"` defaults. Omit `top_score`. `trigger`: mark it a deliberate skill recall (e.g. `"skill_recall"`).

```python
        if record_recall and results:
            await _record_skill_usage(request, [r.id for r in results])
            try:
                from app.main import _replay_emit
                sid = request.headers.get("X-Session-Id", "unknown")
                aid = request.headers.get("X-Agent-Id", "unknown")
                await _replay_emit(
                    "memory_read", session_id=sid, agent_id=aid,
                    payload={
                        "memory_ids": [r.id for r in results][:50],
                        "result_count": len(results),
                        "trigger": "skill_recall",
                    },
                )
            except Exception as exc:  # noqa: BLE001 — telemetry never fails the recall
                logger.warning("skill_recall replay receipt failed: %s", exc)
        return results
```
(Use the module's existing `logger`; add one if absent.)

- [ ] **Step 1: Write the failing test(s)** — (a) a `GET /skills?...&record_recall=true` (or the `skill_recall` path) with two active skills emits ONE `memory_read` whose `payload["memory_ids"]` are the two skill ids and `trigger == "skill_recall"`; (b) a `record_recall=false` / dashboard-browse call emits NO replay event; (c) the briefing `skills_section` path emits NO `memory_read` (assert zero events for a briefing render). Reuse the existing skills-test harness + the replay-read helper other cortex tests use (e.g. the `wired_replay_emitter` pattern from `test_streaming.py`).
- [ ] **Step 2: Run them** — `cd cortex && pytest tests/<skills test file> -k "skill_recall_receipt or briefing" -v` → expect FAIL (no emit).
- [ ] **Step 3: Implement** the emit above.
- [ ] **Step 4: Run** → expect PASS.
- [ ] **Step 5: Full cortex suite** `cd cortex && pytest tests/ -q` → green.
- [ ] **Step 6: Commit** — `git add cortex/app/skills/api.py cortex/tests/<test file> && git commit -m "feat(cortex): skill_recall emits a memory_read receipt for outcome joins"`

---

### Task 2: extend the OWM pass to score skills into `skill_efficacy` (D2)

**Files:**
- Modify: `cortex/app/config.py` — add `SKILL_OWM_ENABLED: bool = True` after the OWM block (~line 298).
- Modify: `cortex/app/owm.py` — `run_pass` (the retrieve/exclude loop ~152-173, the write loop ~177-193, the stale-reset ~195-224) and the pass entry gate in `run_owm_scoring` (~line 265, grep for the `OWM_ENABLED` gate).
- Test: `cortex/tests/test_owm.py`.

**Interfaces:**
- Consumes: the `stats` dict already built at `owm.py:112-148` (skill ids are ALREADY in it — only dropped at the retrieve step); `compute_efficacy`, `session_success`, `OWM_AGENT_CAP`, `OWM_WINDOW_DAYS`, `settings.OWM_PRIOR_N` — all unchanged.
- Produces: `skill_efficacy` / `skill_efficacy_n` / `skill_efficacy_updated_at` on skill Qdrant payloads; `out["skills_scored"]`.

**Implementation notes (mirror the memory path exactly, distinct field + independent gate):**
1. `config.py`: `SKILL_OWM_ENABLED: bool = True` beside `OWM_AGENT_CAP` (line 298).
2. `run_owm_scoring` entry gate: change the `if not OWM_ENABLED: return {...disabled}` guard to run when `OWM_ENABLED or SKILL_OWM_ENABLED` (grep the exact guard; keep the disabled-return shape when BOTH are off).
3. In the retrieve loop (`owm.py:165-173`), split the branch and build a second tally:
```python
        for pt in points:
            payload = pt.payload or {}
            if payload.get("source") == "corpus":
                continue  # corpus never scored (unchanged)
            per_agent = stats.get(str(pt.id)) or {}
            s_total = sum(v[0] for v in per_agent.values())
            n_total = sum(v[1] for v in per_agent.values())
            if payload.get("memory_type") == "skill":
                if n_total:
                    skill_scorable[str(pt.id)] = (s_total, n_total)
                continue
            if n_total:
                scorable[str(pt.id)] = (s_total, n_total)   # memory path, unchanged
```
   (`scorable` written only under `OWM_ENABLED`; `skill_scorable` only under `SKILL_OWM_ENABLED` — see step 5.)
4. Wrap the EXISTING memory write loop (`owm.py:177-193`) in `if settings.OWM_ENABLED:` and the memory stale-reset (`195-224`) in `if settings.OWM_ENABLED:`.
5. Add a SKILL write loop + SKILL stale-reset, both under `if getattr(settings, "SKILL_OWM_ENABLED", True):`, mirroring the memory ones but with the distinct field and its own `skill_written` set + `out["skills_scored"]`:
```python
        if getattr(settings, "SKILL_OWM_ENABLED", True):
            skill_written: set[str] = set()
            for sid_, (successes, n) in skill_scorable.items():
                sp = {"skill_efficacy": round(compute_efficacy(successes, n, settings.OWM_PRIOR_N), 4),
                      "skill_efficacy_n": n, "skill_efficacy_updated_at": now_iso}
                try:
                    await vector._client.set_payload(collection_name=settings.QDRANT_COLLECTION,
                                                     payload=sp, points=[sid_])
                    out["skills_scored"] += 1; skill_written.add(sid_)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OWM: skill payload write failed for %s: %s", sid_, exc)
                    out["write_errors"] += 1
            # stale reset: scroll skill_efficacy_n>=1 not in skill_written, delete the 3 skill keys
            # (mirror owm.py:198-224 with key="skill_efficacy_n" and keys=["skill_efficacy",
            #  "skill_efficacy_n","skill_efficacy_updated_at"])
```
   Add `skill_scorable: dict[str, tuple[int, int]] = {}` and `out["skills_scored"] = 0` up front.

- [ ] **Step 1: Write the failing tests** — (a) a skill id recalled (in a `memory_read`) in a graded-success session gets `skill_efficacy`≈`compute_efficacy(1,1,5)` written and `skills_scored==1`; (b) NO `owm_efficacy` key is ever written to a skill point; (c) a memory id in the same run still gets `owm_efficacy` (memory path intact); (d) `SKILL_OWM_ENABLED=false` ⇒ no skill_efficacy written, memory path unchanged; (e) a skill previously scored but absent this run has its `skill_efficacy*` keys deleted (stale reset). Use the existing `test_owm.py` fakeredis/vector-double harness + the `events_fn` seam.
- [ ] **Step 2: Run** → expect FAIL.
- [ ] **Step 3: Implement** config flag + the owm.py changes.
- [ ] **Step 4: Run** → expect PASS.
- [ ] **Step 5: Full cortex suite** → green, including all existing OWM tests (memory path must be behavior-identical).
- [ ] **Step 6: Commit** — `git add cortex/app/config.py cortex/app/owm.py cortex/tests/test_owm.py && git commit -m "feat(cortex): OWM scores skills into a distinct skill_efficacy field"`

---

### Task 3: consume the `memory_feedback` applied signal into the skill tally (D3)

**Files:**
- Modify: `cortex/app/owm.py` — the session scan loop (`run_pass`, ~112-148) + the skill merge point.
- Test: `cortex/tests/test_owm.py`.

**Interfaces:**
- Consumes: `memory_feedback` replay events (payload `{memory_ids, useful, ...}`, from PR2, `main.py:1628-1639`) available in `all_events` alongside `memory_read`.
- Produces: skill tallies that also reflect explicit `useful` feedback — WITHOUT touching memory scoring.

**Implementation notes (double-count-safe — spec D3):**
- In the same session loop, from `all_events` also read `event_type == "memory_feedback"`. Accumulate into a SEPARATE `feedback_stats: dict[id -> dict[agent -> [pos, n]]]`, capped by `agent_cap`: `useful=true` → `[+1, +1]`, `useful=false` → `[+0, +1]`.
- Merge `feedback_stats` into a skill's tally ONLY at the retrieve step where `memory_type == "skill"` is confirmed — add its `(pos_total, n_total)` to the skill's `(s_total, n_total)` before writing `skill_efficacy`. Feedback on a memory id is NEVER merged (its `set_feedback` counter already handles it — merging would double-count).
- Keep it inside the existing per-session `try` so one bad session is skipped, not fatal.

- [ ] **Step 1: Write the failing test** — a session recalls skill `sk1` (graded success → 1/1) AND emits a `memory_feedback` with `memory_ids=["sk1"], useful=false`; assert `sk1`'s `skill_efficacy` reflects the blended tally (e.g. 1 success out of 2 observations, shrunk) — strictly lower than exposure-only. Also assert a `memory_feedback` on a MEMORY id does NOT change that memory's `owm_efficacy` vs a no-feedback control (no double-count).
- [ ] **Step 2: Run** → expect FAIL.
- [ ] **Step 3: Implement** the `feedback_stats` accumulation + skill-only merge.
- [ ] **Step 4: Run** → expect PASS.
- [ ] **Step 5: Full cortex suite** → green.
- [ ] **Step 6: Commit** — `git add cortex/app/owm.py cortex/tests/test_owm.py && git commit -m "feat(cortex): skill efficacy consumes the memory_feedback applied signal (skills only)"`

---

### Task 4: the reader — inbox low-efficacy skills section + `SkillResponse` fields (D4)

**Files:**
- Modify: `cortex/app/models.py` — `SkillResponse` (~498-528): add `skill_efficacy: float | None = None`, `skill_efficacy_n: int | None = None`, `skill_efficacy_updated_at: str | None = None`.
- Modify: `cortex/app/skills/api.py` — `_point_to_response` (~389-414): map the three fields from the payload.
- Modify: `cortex/app/autopilot/inbox.py` — add a `low_efficacy_skills` section builder; wire it into the inbox response (and `autopilot/api.py` if sections are enumerated there).
- Test: `cortex/tests/` — the autopilot/inbox test module + a `_point_to_response` mapping assertion.

**Interfaces:**
- Consumes: `skill_efficacy` / `skill_efficacy_n` on skill Qdrant payloads (written by Task 2).
- Produces: a read-only inbox section listing skills with `skill_efficacy` below a threshold at sufficient `n`, and the three response fields for the dashboard.

**Implementation notes:**
- The section is SURFACING only — no ranking, no mutation, no status change (spec D4). It queries skill points with `skill_efficacy_n >= <min_n>` and `skill_efficacy < <threshold>` (Qdrant scroll with a range filter, like the OWM stale-reset scroll at `owm.py:203-208`), returns id/trigger/efficacy/n. Pick a conservative `min_n` (e.g. reuse a small floor; DISCLOSE it) so the prior isn't mistaken for signal — surface `skill_efficacy_n` in each row.
- Study `autopilot/inbox.py`'s existing section builders (draft/stale/rereview skills) and match their shape + the per-section fault isolation (`autopilot/api.py` builds sections defensively). Do NOT invent a new response envelope.
- `SkillResponse` fields are optional with `None` defaults → old points without the fields parse fine (three-state pattern, like `stale`).

- [ ] **Step 1: Write the failing tests** — (a) `_point_to_response` maps `skill_efficacy*` from payload; (b) the inbox `low_efficacy_skills` section includes a skill with `skill_efficacy=0.2, skill_efficacy_n=8` but EXCLUDES one with `skill_efficacy_n=1` (below the min-n floor) and one with `skill_efficacy=0.9`.
- [ ] **Step 2: Run** → expect FAIL.
- [ ] **Step 3: Implement** the model fields, the mapping, the section.
- [ ] **Step 4: Run** → expect PASS.
- [ ] **Step 5: Full cortex suite** → green.
- [ ] **Step 6: Commit** — `git add cortex/app/models.py cortex/app/skills/api.py cortex/app/autopilot/inbox.py cortex/app/autopilot/api.py cortex/tests/<tests> && git commit -m "feat(cortex): surface skill_efficacy — inbox low-efficacy section + response fields"`
  (Stage `autopilot/api.py` only if you touched it.)

---

### Task 5: docs + change-consistency

**Files:**
- Modify: `docs/guides/memory-and-recall.md` — OWM now scores skills into a distinct `skill_efficacy` field (never `owm_efficacy`); `skill_recall` now emits a `memory_read` receipt; the briefing still deliberately does not.
- Modify: `docs/guides/knowledge-autopilot.md` — the new inbox `low_efficacy_skills` section.
- Modify: `docs/guides/cortex-configuration.md` — `SKILL_OWM_ENABLED` (default true), its independent gating from `OWM_ENABLED`, and the `skill_efficacy*` payload fields.
- Modify: `cortex/CLAUDE.md` — note skill efficacy scoring + the skill_recall receipt where OWM / skills / the mcp tools are described.
- Modify: `docker-compose.yml` — add `SKILL_OWM_ENABLED` to the cortex service env (matching how `OWM_ENABLED` is passed, if it is).

**Interfaces:** none (docs/config surface).

- [ ] **Step 1:** Grep for the surfaces: `git grep -n "OWM_ENABLED\|owm_efficacy\|skill_recall" -- docs/guides/ cortex/CLAUDE.md docker-compose.yml`. Read each hit; update the OWM/skills descriptions to include the skill path. Confirm whether `OWM_ENABLED` is even in `docker-compose.yml` — if OWM settings ride defaults (not compose env), add `SKILL_OWM_ENABLED` only where `OWM_ENABLED` actually appears, else note in the config guide that it's a default-only setting.
- [ ] **Step 2:** Write the updates (preserve the decision-history voice of the guides; append, don't reword unrelated prose).
- [ ] **Step 3:** Re-grep; confirm no doc still says skills are unscored / excluded from all efficacy without the PR3 caveat.
- [ ] **Step 4: Commit** — `git add docs/guides/memory-and-recall.md docs/guides/knowledge-autopilot.md docs/guides/cortex-configuration.md cortex/CLAUDE.md docker-compose.yml && git commit -m "docs: PR3 skill efficacy — scorer, skill_recall receipt, inbox section, config"`
  (Drop `docker-compose.yml` from the add if Step 1 found `OWM_ENABLED` is not a compose env var.)

---

## Self-Review

**Spec coverage:** D1→Task 1; D2→Task 2; D3→Task 3; D4→Task 4; docs (spec ship-gate)→Task 5. All decisions mapped.

**Placeholder scan:** each code step carries real code or a named edit against a cited anchor; the two research-anchored spots (the `run_owm_scoring` gate line; the `autopilot/inbox.py` section shape) are flagged "grep/verify against the anchor," not left as silent TODOs.

**Type consistency:** `skill_efficacy` / `skill_efficacy_n` / `skill_efficacy_updated_at` are the field names across Tasks 2/4/5; `SKILL_OWM_ENABLED` is the one flag (Task 2 config, Task 5 docs); `feedback_stats` (Task 3) merges into `skill_scorable` (Task 2) only. The distinct-field and double-count invariants are Global Constraints, asserted by Task 2(b) and Task 3 tests.
