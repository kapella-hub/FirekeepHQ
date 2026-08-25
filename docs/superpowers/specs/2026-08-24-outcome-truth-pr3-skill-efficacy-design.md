# Outcome Truth PR3 — skill efficacy — design

**Status:** draft for review (grounded in a 5-stream code read of the post-PR2 tree, 2026-08-24).
**Date:** 2026-08-24
**Depends on:** PR1 (outcomes are a real `(task_result, task_result_source)` grade), PR2 (the
`memory_read` exposure receipt reaches every recall path incl. SSE, and `memory_feedback` is the
applied-signal receipt — both on one verified id space). PR3 makes the artifact class PR2 left
receipt-less — **skills** — a scored, outcome-aware citizen, consuming those receipts.
**Scope:** one thesis — *skills become scored by outcome, the way memories already are.* An
extension of OWM's existing receipt→outcome machinery (~80% reuse), not a new subsystem.

## Why this, and not the other deferred items (decisions the research forced)

The PR2 spec deferred five things to PR3. The code read settles which are coherent now:

- **Causal `trace_links` back-chain — CUT, on evidence.** The cheap linear `preceded` chain is ~10
  lines in `emit()`, but `narrow()` already injects same-session ±5s events as inferred links
  (`narrowing.py:155-193`), so a linear chain only reproduces a recency heuristic it has — and it
  flips `session_has_trace_links` permanently True, **erasing the honest "this deployment records no
  causal data" signal** the recent three-outcomes work added (`narrowing.py:54-66`). "Turning the
  light green without adding diagnostic power" is worse than the honest red. The *valuable* version
  is semantic (`declared`/`inferred`) links emitted at each call site — diffuse cross-service
  instrumentation, a project of its own. Neither belongs in PR3.
- **A "general artifact efficacy scorer" — REFRAMED, not built as an abstraction.** Three efficacy
  mechanisms already exist (OWM=memory, Procedures Tier B=step, pattern `tip_lift`=card) and share
  the outcome primitives (`session_success`, `compute_efficacy`) already. The honest next step is
  *generalizing OWM to also cover skills*, not a new "one scorer for all" module — Tier B (a
  within-execution counterfactual) and pattern experiments (A/B chi-square) have legitimately
  different semantics and flattening them loses guards. So PR3 extends OWM; it does not abstract.
- **Skill content-hash versioning + experiment version-attribution — DEFER.** PR3's join keys on the
  stable `skill_id`, not a version, so it needs no hash. The fingerprint is cheap but has no
  consumer until the experiment-rebuild work exists — adding it now is a field nothing reads.
- **PatternCard provenance / experiment rebuild — DEFER.** A lineage feature, not a scorer.
- **Instruction/pattern application receipts — DEFER** (no serve-time hook; larger surface).

## Problem (verified against the post-PR2 tree)

1. **The most intentional skill exposure emits no receipt.** `skill_recall` (`mcp_server.py:1284-
   1330`) → `GET /skills?record_recall=true` → `_record_skill_usage` (`skills/api.py:114-116,
   359-386`) bumps only `memory:access_counts` / `memory:last_recalled` — **no `_replay_emit`**. A
   skill surfaced through *general RAG* recall already gets a `memory_read` receipt (its id rides in
   `memory_read.memory_ids`, `main.py:1293-1295,1340`), but the dedicated `skill_recall` path — the
   deliberate, highest-signal reach — is invisible to any outcome join. (This is exactly the gap PR2
   cut to PR3.)
2. **Nothing scores a skill by outcome.** OWM tallies skill ids into its `stats` dict (they ride the
   same `memory_read` receipts) and then **drops them at the write stage** — `if
   payload.get("memory_type") == "skill" or payload.get("source") == "corpus": continue`
   (`owm.py:167-168`) — so `skill_efficacy` is never computed. Staleness is freshness-only
   (`staleness.py`), GC protects skills (`gc.py:65,346`), `skill_score` is a creation-time
   breakthrough score (`skills/scorer.py`), Procedures Tier B covers only step-spec Living
   Procedures. A plain skill's real-world efficacy is measured by no one.
3. **PR2's applied signal has no consumer yet.** `memory_feedback` receipts are emitted
   (`main.py:1628-1639`) but nothing reads them. An efficacy pass is the natural first consumer.

## Decisions

**D1. `skill_recall` emits a `memory_read` receipt — reuse the type, not a new one.** In
`list_skills`, inside the existing `if record_recall and results:` branch (`skills/api.py:114-115`),
add a best-effort `_replay_emit("memory_read", sid, aid, payload={memory_ids:[r.id for r in
results], result_count, trigger})` beside `_record_skill_usage`, using the deferred `from app.main
import _replay_emit` circular-import dodge `streaming.py` already uses, wrapped so telemetry never
fails the recall. Read `X-Session-Id`/`X-Agent-Id` off the request; omit `top_score` (pinned
constant, per PR2 D1). Reuse of `memory_read` is *more* consistent than a new type: a skill served
via general RAG vs. via `skill_recall` then produces the **same** event with the **same** joinable
id, and no existing consumer must learn a second type. **No OWM contamination:** OWM already drops
skill ids at `owm.py:167`, so this is a pure trace receipt. **The briefing stays silent** — it
serves the top-3 active skills on *every* SessionStart with no agent intent; a receipt there would
make every session "see" those skills, so any outcome join would attribute every outcome to skills
the agent never read (the same impression-vs-reach reason the briefing must not touch
`last_recalled`, `skills/api.py:109-113`). Impression telemetry for the briefing is a separate,
later concern.

**D2. Extend the OWM nightly pass to score skills into a distinct `skill_efficacy` field.** Where
the pass currently `continue`s on a skill point (`owm.py:167`), instead route it into a *parallel*
skill tally, Beta-shrink it with the **same** shared primitives (`compute_efficacy`,
`session_success`, the `OWM_AGENT_CAP` fairness cap, the `OWM_WINDOW_DAYS` window, the
decay-to-neutral stale-reset), and write **`skill_efficacy` / `skill_efficacy_n` /
`skill_efficacy_updated_at`** onto the skill's Qdrant payload — reusing the single Qdrant `retrieve`
scan already in the pass. **It must NOT write `owm_efficacy` onto a skill:** the RAG lifecycle
scorer (`rag.py:1188`) and GC factor (`gc.py:109-116`) read `owm_efficacy` with *no* `memory_type`
guard, so writing it onto a skill would silently activate memory-ranking machinery on skills — the
distinct field is what keeps that clean. A skill-specific stale-reset (delete `skill_efficacy` keys
for skills with no in-window evidence) mirrors OWM's, so penalties decay to neutral rather than
ratchet. Neutral-at-low-N by construction (`compute_efficacy` = 0.5 at n=0).
**Flag coupling, made independent:** the pass shares the single Qdrant scan, but the two write
branches gate separately — the pass entry runs when `OWM_ENABLED or SKILL_OWM_ENABLED`, the memory
write-loop stays gated by `OWM_ENABLED`, and the skill write-loop + skill stale-reset gate on the new
`SKILL_OWM_ENABLED`. So a deployment can score skills without turning on memory outcome-weighting (and
vice versa), and `SKILL_OWM_ENABLED=false` makes the skill path bit-neutral. The skill branch reuses
the existing per-agent tally already built during the scan (skill ids are in `stats` — they are only
*dropped* at `owm.py:167` today), so enabling it adds a write loop + a scroll sweep, not a second scan.

**D3. The applied signal (`memory_feedback`) augments the SKILL tally only — never memory scoring.**
For skill ids that appear in a `memory_feedback` receipt (`main.py:1628-1639`), count `useful=true` as
a positive and `useful=false` as a negative observation into that skill's tally, capped by the same
`OWM_AGENT_CAP` fairness cap. This is PR2's applied receipt finally paying off, for the artifact class
that has no other feedback channel. **Double-count guard (self-review):** memory feedback is already
consumed for *memories* via the `set_feedback` Qdrant counter, read by the RAG feedback multiplier
(`rag.py:1194+`), so `memory_feedback` observations must be routed EXCLUSIVELY into the skill tally
and must never reach the shared `stats` used for `owm_efficacy` — feeding them to memory scoring would
count the same thumb twice (once via the counter, once via the replay join). Concretely: accumulate
`feedback_stats[id][agent]` for all ids in the session scan, and merge it into a skill's tally only at
the point the retrieve step has confirmed `memory_type == "skill"`; feedback on a memory id is simply
never merged (its counter path already handles it). Exposure-outcome and applied-feedback both feed
one Beta tally at equal weight (a documented default knob, not a new statistic).

**D4. A reader, so the score is not dead weight — VISIBILITY, not automated ranking (yet).** Nothing
reads skill efficacy today; a score with no reader is the "capability nothing uses" anti-pattern the
guides name repeatedly. PR3's reader is **surfacing**, not **acting**: the Knowledge Autopilot inbox
(`autopilot/inbox.py`, admin/read-only) gains a "low-efficacy skills" section (skills whose
`skill_efficacy` sits below a threshold at sufficient `skill_efficacy_n`), analogous to the existing
stale-skill surface, and `SkillResponse` exposes the three fields for the dashboard. **PR3 does NOT
add an automated skill-recall ranking multiplier** — at the N most deployments have, skill efficacy
is mostly the neutral prior, so ranking on it would move little while risking what agents see;
visibility first, automated action once the signal proves out at scale (the cautious rollout the
pattern engine's `PATTERN_VALIDATION_ENABLED` freeze already models). The recall-ranking multiplier
is a named, deferred follow-on.

## Non-goals / deferred (with rationale above)

Causal `trace_links` (cut, harmful cheap version); a "general artifact scorer" abstraction
(premature — Tier B & pattern experiments stay separate); skill content-hash versioning +
experiment version-attribution (no consumer yet; join keys on stable id); PatternCard provenance /
experiment rebuild (lineage, not scoring); instruction/pattern application receipts; and the
automated skill-recall **ranking** multiplier (D4 does visibility only — ranking waits for scale).

## Disclosed residuals

- Skill efficacy is only as populated as skill exposures + grades allow; at low N it is the neutral
  prior, and the inbox section discloses `skill_efficacy_n` so a reader never mistakes prior for
  signal (the `outcome_event_count` lesson, applied to skills).
- `memory_feedback` weighting (D3) shares the agent cap but is a coarser signal than a controlled
  experiment; it informs, it does not adjudicate.
- Corpus chunks remain excluded from all efficacy scoring (unchanged from OWM); only skills are
  un-excluded here.

## Ship gates

- All six suites green + deploy shell tests. New `SKILL_OWM_ENABLED` documented in the config guide
  and compose env, defaulted; `skill_efficacy*` fields documented.
- Pinned by test: `skill_recall` emits a `memory_read` carrying the served skill ids, and the
  briefing does NOT (D1); the OWM pass writes `skill_efficacy` onto a skill point that was recalled
  in a graded session, and writes NO `owm_efficacy` onto any skill (D2); a `memory_feedback`
  `useful` bit moves a skill's tally (D3); the inbox surfaces a low-efficacy skill only at
  sufficient `n`, and `SKILL_OWM_ENABLED=false` makes the pass bit-neutral (D4); receipts still carry
  ids/enums only.
- Docs updated (change-consistency): `docs/guides/memory-and-recall.md` (OWM now scores skills into a
  distinct field; skill_recall receipt), `docs/guides/knowledge-autopilot.md` (inbox section),
  `docs/guides/cortex-configuration.md` (`SKILL_OWM_ENABLED`), `cortex/CLAUDE.md`, `docker-compose.yml`.
