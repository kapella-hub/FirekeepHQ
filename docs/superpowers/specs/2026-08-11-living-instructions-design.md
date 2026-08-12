# Living Instructions — the instruction layer that measures itself

_Status: round 1 BUILT (2026-08-11) — `GET /autopilot/compliance` +
the Living Instructions table on the dashboard's Autopilot tab, predicates
frozen to the founding measurement below. Rounds 2–3 remain proposed. Not
publicly promised; the roadmap gate is met once round 1 is deployed._

## The observation this is built on

Four separate times, a Firekeep capability existed, worked, and was **never
used** until one paragraph landed in the rendered instruction block with a
concrete, observable trigger:

1. `decision_board` (client 0.1.11) — tool descriptions alone never fired it.
2. `memory_recall` triggers (0.1.34) — storage and retrieval were perfect;
   nothing triggered them until "not knowing IS the trigger" was rendered.
3. `memory_feedback` (0.1.37) — the ranking signal the whole feedback feature
   depends on lived only in a docstring until the block carried it.
4. `action_before` predictions (2026-08-10) — predictor calibration was
   structurally unpopulated (31/31 sessions, no Brier) because nothing
   instructed agents to declare confidence.

The binding constraint on the system is not the tools — it is the instruction
layer. And that layer is written by hand, on judgment, and never measured.
Living Instructions closes the loop on the instructions themselves: measure
compliance from replay, propose rewrites through the fleet, approve in the
inbox, deliver variants through the briefing, validate with the experiment
framework. Every stage except the compliance counters already exists.

## The founding measurement (2026-08-11)

Computed on the live deployment from the 32 stored session evals
(`rp:eval:*`, replay Redis DB 6) — deterministic predicates over recorded
metrics, no model in the loop:

| Instruction (as rendered today) | Predicate over stored eval metrics | Compliance |
|---|---|---|
| "Recall before you answer" | `memory_read_count > 0` | 18/32 — 56% |
| "Write as you go" (`memory_learn`) | `memory_write_count > 0` | 15/32 — 47% |
| Recall visibly used in the work | `recall_used_rate > 0` | 8/32 — 25% |
| "ctx_update as you go" | `context_snapshot_count > 0` | 20/32 — 62% |
| "Declare consequential actions" | `brier_score is not None` | 0/32 — 0% |
| Outcome-bearing events ≥ 2 | `outcome_event_count >= 2` | 10/32 — 31% |

Reading it: agents obey the session-state instruction (62%) more than the
recall instruction (56%), and only a quarter of sessions visibly *use* what
they recall. The 0% row is the `action_before` instruction shipped 2026-08-10,
which had reached no session at measurement time — see "the armed experiment".

This table is the product of round 1, not an appendix to it: no engineering
team has ever been shown which of its agent instructions are actually obeyed.

## Design — five stages, four of which exist

1. **Measure** (new, small): per-instruction compliance predicates computed
   over stored evals — exactly the founding-measurement queries, productized
   as an Autopilot inbox/tab section. Deterministic; extends
   `evals`/`autopilot` code, no new service.
2. **Propose** (exists: Relay task queue + client-side generation): a
   low-compliance instruction becomes a reasoning job ("this trigger fired in
   2 of 40 sessions; rewrite with a sharper observable edge"). The next agent
   session claims it and authors variants with its own model — the
   fleet-as-GPU pattern from `docs/STRATEGY.md`, given its first concrete
   workload. The server never generates.
3. **Approve** (exists: Autopilot inbox): instruction changes are a
   prompt-injection-adjacent surface. Every variant is a draft until a human
   verdict. No exceptions, ever — this is the same round-1 discipline as
   contested memories.
4. **Deliver** (exists: session-start briefing): approved variants ride the
   briefing, a per-session instruction surface that needs no client release.
   The rendered CLAUDE.md block stays the stable floor; the briefing carries
   the experimental delta.
5. **Validate** (exists, frozen: `PATTERN_EXPERIMENTS_ENABLED`): chi-square
   over compliance proportions between arms. The experiment machinery was
   frozen for lack of a use case with volume; instructions are the one
   experiment every session participates in.

## Rounds

- **Round 1 — visibility only.** The compliance table, live, per instruction,
  per runtime, with trend over time. No rewriting, no delivery, no stats.
  Ships the same way Autopilot round 1 shipped: it proposes and reports,
  never mutates.
- **Round 2 — proposals under verdict.** Fleet-authored rewrites queue in the
  inbox; an approved rewrite replaces the rendered text at the next client
  release / briefing refresh; measurement continues as sequential
  before/after. Time confounds are real and get named in the UI ("compliance
  moved after the 0.1.41 rewording; other things also changed that week").
- **Round 3 — true A/B.** Briefing-delivered variants split across sessions;
  the frozen stats validate. Gated on session volume (~90/month today ⇒ a
  20–30-point effect resolves in weeks; smaller effects need more fleet).

## The armed experiment

The `action_before` instruction shipped 2026-08-10 in both instruction layers
with a 0/32 baseline recorded here. The 0.1.40 client rollout is therefore a
natural experiment requiring zero new infrastructure: the `brier_score
is not None` row moving off zero — or not moving — is the first Living
Instructions measurement, with this document as the pre-registration.

## What the measurement can and cannot claim

**Can:** that behavior changed. Compliance predicates are event counts;
a proportion shift between arms is a real, testable behavioral effect.

**Cannot (yet):** that the behavior change improved session *quality*. The
outcome signal is still degenerate — `failure_rate` 0.0 everywhere, ~1
outcome-bearing event per typical session, Brier absent (all documented with
live measurements in `docs/guides/replay-evals-patterns.md`). Any claim that
a reworded prompt improved outcomes would currently be fiction, and round 1
must say so on the surface that shows the table.

**The recursion that resolves it:** the instructions most worth tuning first
are the ones that CREATE outcome signal — `memory_feedback` compliance
populates per-memory usefulness, `action_before` compliance populates
calibration, completion discipline plus the session reaper populates real
success/failure. Every compliance win makes the quality half more measurable.
Round N's behavior measurement bootstraps round N+1's outcome measurement.

## Risks, named

- **Goodhart.** Agents can satisfy a counter without the substance (a
  ritual `memory_learn` of nothing). Mitigations: predicates stay coarse
  (presence, not volume), the feedback/OWM layer scores whether written
  memories later helped, and rewrites that raise compliance while feedback
  quality falls are exactly what the inbox reviewer is shown.
- **Instruction thrash.** Rate-limit: one active experiment per instruction,
  minimum window before verdict, and the rendered block only changes at
  client releases.
- **Injection surface.** Fleet-authored variants are untrusted drafts;
  approval is mandatory and the diff shown is the exact rendered text. The
  briefing delta is capped in size and carries no tool output.
- **Runtime skew.** Kiro/Claude/Codex expose different hook surfaces, so some
  predicates are unobservable on some runtimes. Compliance is reported per
  runtime; cross-runtime aggregates would blame the instruction for the
  harness.

## What this deliberately is not

Not a new subsystem (`docs/ROADMAP.md` rule): stages 2–5 are existing
machinery. Not server-side generation, ever. Not autonomous — no instruction
changes without a human verdict, in any round. Not a claim about model
self-improvement: the models never change; the system's interface to them
does, under measurement.

## Roadmap placement

Third candidate behind the two published promises (linked instances, domain
profiles). **Gate for announcing it on firekeep.ai: the round-1 compliance
table exists on the Autopilot tab.** The site never promises what the
dashboard cannot yet show.
