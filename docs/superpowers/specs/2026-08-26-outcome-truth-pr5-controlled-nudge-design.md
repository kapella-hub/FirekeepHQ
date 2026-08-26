# Outcome truth PR5 — the controlled grading-nudge experiment (pre-registration)

**Status:** PRE-REGISTRATION, committed before the differential nudge can move
any number. The code this spec describes deploys DARK (flag off); the flip
that starts the experiment is gated below (D4). Not yet implemented.
**Date:** 2026-08-26
**Scope:** PR5 of the outcome-truth arc: the controlled per-member A/B test of
a proactive grading nudge, measured as the incremental lift over PR4's
uniform baseline. Built from a five-stream code-read (2026-08-26) against the
PR4 pre-registration
(`docs/superpowers/specs/2026-08-25-outcome-truth-pr4-adoption-design.md`,
immutable — every deviation below is a dated note here, never an edit there).

## Problem

PR1–PR4 are live: grades are real when they flow, every consumer excludes
what isn't graded, and PR4's mild tool-description nudge started the adoption
clock (H1: ≥20% within two weeks of 2026-08-25). What no observational layer
can answer is *causal*: does a proactive, session-visible grading nudge
actually lift adoption, and does honesty survive the stronger push? That
needs the controlled arm test PR4 reserved — primed hypotheses, the
D1-stamped `experiment_group`, the two-proportion spine.

The scoping read dissolved PR5's stated gate. The PR4 document placed the
nudge "in base.py MEMORY_INSTRUCTIONS … via the session-start hook,
coordinated with the peer's client release." That channel is structurally
incapable of carrying it: `MEMORY_INSTRUCTIONS` is rendered once at install
(`adapters/base.py:358-476`), shared across arms, and byte-pinned by the
exposure contract (`RENDERED_INSTRUCTIONS_HASH` feeds the compliance
`exposed` classification — a per-arm variant forks the hash and corrupts the
existing rows). What CAN vary per session is the briefing: `GET /briefing`
resolves the verified `member_id` at request time (`briefing/api.py:85-87` —
the member, not the session, is the randomization unit, so the pre-session
timing of the fetch is sufficient), composes `rendered` server-side, and the
session-start hook delivers `rendered` verbatim into model context on Claude
Code (`hooks/__main__.py` additionalContext, the measured channel since
52b9b77). **The differential nudge therefore ships fully server-side, with
zero client change and no client release in the loop.**

## Decisions, and why

**D1. The arm channel is `GET /briefing`; one shared arm function.** The
5-line `_experiment_group` (sha256(member)%2 → "A"/"B", empty → None) moves
from `bridge/app/session.py:33-49` into the shared `auth/` package
(`auth/experiment.py`), with Bridge re-importing it — cortex and bridge then
share one implementation *by construction*, pinned additionally by a
cross-service parity test. Cortex's briefing route computes
`experiment_group(identity.get("member_id"))` — the same DB-7-resolved
member string Bridge later stamps on the session, so delivered arm and
recorded arm cannot diverge. Two inherited doc-drift points are corrected
here, not silently: (a) the "auth disabled → None" claim in earlier docs is
wrong — `anonymous_principal()` returns the deployment owner member
(`auth/principal.py:30-40`), which is truthy and gets a real arm; None occurs
only outside an authenticated context. A single-member deployment therefore
has no within-deployment contrast — its sessions sit in one arm, and only
the fleet-level comparison across deployments carries information; disclosed,
not fixed. (b) the "in base.py" delivery wording — superseded by this dated
document for the structural reasons above; the analysis contract it
pre-registered is unchanged.

**D2. The treatment is a server-composed briefing section; control is
absence.** A new `grading_nudge_section` composes the nudge block into
`rendered` for treatment-arm requests only, following the strategy-tips
shape: control (and None-arm, D10) get *no section at all* — absence, never
alternative wording. Both arms keep PR4-D4's uniform tool-description floor;
what is measured is the incremental lift. The existing per-request
`ab_group = random.choice(...)` strategy-tips experiment
(`briefing/api.py:71`) is a different experiment with different semantics;
PR5's field is `experiment_group` with the "A"/"B" label set and its
exposure records use distinct keys.

**D3. The treatment text, verbatim (pre-registered — wording is part of the
intervention):**

> ## Grade this task when you finish
> When you call `ctx_complete_session`, pass `task_result` — `success`,
> `partial`, or `failure` — with `task_evidence` naming what you actually
> verified. An honest `failure` or `partial` is expected and safe to report;
> it is worth more to this team than an unexamined `success`. Ungraded
> sessions teach nothing.

Four sentences, honest-grading framing per D4's, failure/partial explicitly
safe, evidence as the verification anchor. Any wording change after the flip
is a new experiment, not an edit.

**D4. Dark deploy now; the flip is the experiment start, gated on PR4's
window.** Everything ships behind `GRADING_NUDGE_ENABLED: bool = False`
(cortex config). Deploying the differential nudge inside PR4's H1 window
would contaminate the registered readout (H1's denominator is ALL completed
sessions — a mid-window treatment arm inflates it, and a control-arm-only
rescue is a different, un-registered estimand). The flip may happen **no
earlier than 2026-09-08** (D4-deploy + 14 days), and only after a dated
PR4 readout snapshot is committed (procedure: compliance snapshot with a
`created_at >= PR4-deploy` filter applied at readout — the snapshot spans
the whole 30-day store and nothing else serves a post-D4-only rate). The
flip timestamp is recorded in a dated addendum to this document as **T0**.

**D5. The envelope also carries the arm.** `experiment_group` is returned as
a top-level briefing field beside `briefing_id` — not consumed by any client
in PR5, but it makes the arm testable end-to-end and gives a later client
release something to act on without re-deriving (the client cannot derive
it: it holds no member_id, and hashing anything else would not match the
stamped labels).

**D6. The exposure rule (pre-registered).** A session enters the
**per-protocol** population iff ALL of: `experiment_group ∈ {"A","B"}`;
`created_at >= T0`; `briefing_delivered == True` (the mechanical fetch
receipt — shim-injected, survives agent non-compliance, and is deliberately
the only compliance-independent receipt available); `runtime == "claude"`
(the one runtime with verified model-facing delivery of `rendered`; kiro's
delivery is unverified, opencode's is console-only, hookless runtimes are
structurally unexposable). There is **no client_version gate** — the nudge
is fully server-rendered, so the client release ceases to be a confound;
this is why D2's mechanism was chosen. Absent receipts (unparseable record,
`briefing_delivered` None) classify **unknown** — excluded from both
populations, counted and disclosed, per the frozen compliance convention
(absence is never "not_exposed"). `briefing_delivered == False` or wrong
runtime is **not_exposed** — assigned-but-unreached contamination, reported.
**Intent-to-treat** over all A/B sessions with `created_at >= T0` is the
secondary analysis; its dilution by unexposable runtimes only pulls toward
null, never fabricates an effect. A **balance check** is mandatory: exposure
rates must not differ significantly between arms (the rule conditions only
on pre-treatment, arm-independent receipts; a significant imbalance
invalidates the per-protocol read for that snapshot).

**D7. The readout is an additive `arm_comparison` block on the always-on
compliance surface — not the Experiment shell.** `compare_arm_proportions`
(new, in the autopilot module) imports the three pure helpers from
`patterns/statistics.py` (`_chi_square_2x2` — whose stdlib fallback makes it
scipy-free; `_cohens_h`; `_confidence_interval_diff`) and feeds directly
from the `grade_self_reported` row's `by_experiment_group` buckets
restricted per D6. It reports per-arm rates (computed — the split ships only
hits/total today), p-value, Cohen's h, the CI on the difference, sessions
AND distinct members per arm, and `insufficient_n` gating per D8. Tests run
un-gated (not under the `PATTERN_EXPERIMENTS_ENABLED` skip). **Dated
walk-back of the deferred-list vehicle:** the PR4 document said "Experiment.
pattern_id made optional"; that shell is registration-gated off by default
(invisible on every real deployment), its compute joins SessionFeatures —
which carry no arm — and a None pattern_id degenerates every session to
control. The pre-registered *analysis* (two-proportion chi-square, Cohen's
h, CI over arms) is honored exactly; only the plumbing differs, for the
reason the compliance conventions exist: a readout nobody can see is not a
readout. `minimum_sample_size` is not called (its unguarded scipy import
raises on the shipped image); the fixed-z computation is stated in D8.

**D8. Hypotheses (primed, per the PR4 document's reservation), thresholds
fixed now.**

- **H1′ (controlled adoption lift):** among per-protocol sessions, the
  treatment arm's graded fraction exceeds control's, χ² p < 0.05 AND
  |Cohen's h| > 0.1 (the spine's own significance semantics). Minimum
  detectable effect pre-registered at **20 percentage points absolute**;
  minimum N per arm from the fixed-z two-proportion formula
  n = (z_α/2 + z_β)² · (p₁(1−p₁) + p₂(1−p₂)) / (p₁−p₂)² with z 1.96/0.8416
  and worst-case variance (p = 0.5): **99 sessions per arm**, AND at least
  **3 distinct members per arm**. Below either bound the block reports
  `insufficient_n` with the counts — never a verdict. The clustering caveat
  is disclosed in the block itself: randomization is member-level, sessions
  within a member are correlated, so the session-level test is
  anticonservative; members-per-arm is reported beside sessions-per-arm so a
  reader can see when the "experiment" is really two people.
- **H2′ (honesty under the stronger nudge):** the treatment arm's
  optimism-skew stays ≤ 15% of self-success sessions at N ≥ 30 (H2's bound
  and gate, unchanged) AND is not significantly worse than control's (same
  spine, same α). A nudge that lifts adoption by teaching flattery fails
  here regardless of H1′.
- **H3′ is deliberately NOT registered.** The McNemar/Cohen's-κ
  paired-agreement helper the PR4 document named has zero input rows today:
  `_RECOGNIZED_SOURCES = ("self_reported",)`, `llm_judged_*` fields are
  written by nothing, and the Tier-2 judge that would produce independent
  grades is itself deferred (and needs a generation backend embed-only
  deploys lack). Building the helper now would be machinery with no data —
  it lands with the judge, under its own dated pre-registration, with
  uniform or disclosed-stratification sampling (judging only suspects
  biases κ by construction). This supersedes the deferred-list line by
  dated note.

**D9. Arm→treatment mapping: A = treatment, B = control.** Fixed here,
mechanically (alphabetical), chosen before the author viewed any per-arm
adoption number (the compliance splits were never inspected during this
spec's preparation; the hash is deterministic, so this mapping is the last
researcher degree of freedom and it closes now).

**D10. None-arm sessions receive control behavior.** No nudge section, no
inclusion in inference, disclosed counts — unauthenticated and
pre-authentication traffic stays a clean D4-only population.

**D11. H2-continuation rule, stated before treatment could move it:** if
PR4-H2's N ≥ 30 gate is not reached by T0, the PR4-H2 readout is thereafter
taken from the **control arm only**.

**D12. A served-nudge receipt, withhold-on-record-failure.** When the
treatment section is composed, the server records `nudge_shown` keyed by
`briefing_id` (distinct key space from the tips experiment's records; same
Redis, TTL = the eval retention window). If the record write fails, the
section is **withheld** — the strategy-tips precedent: an unrecorded
exposure corrupts the loop. The receipt enables a delivered-vs-recorded
consistency check in the readout; `briefing_delivered` remains the exposure
receipt (D6) because it is client-side-mechanical, while `nudge_shown` is
server-side proof of composition.

## Non-goals

- Any client change or client release. base.py stays byte-identical; the
  instruction hashes do not move.
- The Tier-2 LLM judge and the McNemar/κ helper (D8/H3′).
- Editing the PR4 pre-registration, the frozen compliance predicate keys, or
  the `grade_self_reported` row semantics — everything lands additively.
- The Experiment/Dataset shell (D7's walk-back).
- PR6's efficacy question. Gated on H1/H2 (and now H1′/H2′) holding.
- Dashboard rendering of the arm comparison (the JSON block is the readout;
  a UI card is a follow-up).

## Detection of the two drift points this spec corrects in prose

`docs/guides/replay-evals-patterns.md`'s PR4-vs-PR5 paragraph and the
`knowledge-autopilot.md` cross-references are updated to describe the
server-composed channel and the deferred H3′, each as a dated addition.

## Files

`auth/experiment.py` (new — the shared arm function; bridge re-imports);
`bridge/app/session.py` (import swap only); `cortex/app/briefing/api.py` +
`cortex/app/briefing/sections.py` (arm computation, envelope field,
`grading_nudge_section`, D12 receipt); `cortex/app/config.py`
(`GRADING_NUDGE_ENABLED=False`); `cortex/app/autopilot/compliance.py` or
sibling (`compare_arm_proportions`, the `arm_comparison` block, D6
population logic); tests (`auth/tests/test_experiment_parity.py`,
`cortex/tests/test_grading_nudge_section.py`,
`cortex/tests/test_arm_comparison.py`, bridge parity re-pin);
`docs/guides/replay-evals-patterns.md`, `docs/guides/knowledge-autopilot.md`,
`docs/guides/bridge-context-and-briefing.md` (dated additions);
`CLAUDE.md` untouched (guides carry it).

## Testing

- Parity: sha256-arm equality between the shared function and a
  byte-frozen copy of Bridge's pre-PR5 implementation, over representative
  member ids + empty/None.
- Section: treatment renders the D3 text verbatim; control/None-arm renders
  nothing; flag off renders nothing for both; record-failure withholds
  (D12); the envelope field matches the composed behavior.
- Arm comparison: known-bucket fixtures against hand-computed χ²/h/CI;
  `insufficient_n` below either D8 bound; unknown/not_exposed
  classification per D6 including the balance check; freeze guard — the
  existing compliance rows byte-identical.
- Discipline: briefing availability unaffected — section composition
  failure degrades to no section, never a failed briefing.

## Deploy

Cortex only (the four cortex containers via the standard pull + rebuild);
Bridge redeploys only for the import swap (same image build anyway). Dark:
the flag stays False at deploy. The flip (T0) is its own dated act per D4:
readout snapshot first, then `GRADING_NUDGE_ENABLED=true` in the VPS `.env`,
addendum committed with T0. Office compose/K8s inherit on their next update.

## Risks

- **The flip is a human-calendar gate.** Nothing enforces 2026-09-08
  mechanically; the addendum requirement (T0 + readout snapshot committed
  together) is the audit trail. Flipping early with a dated amendment that
  pre-registers the population change is the documented fallback — worse
  science, disclosed if chosen.
- **Small-fleet degeneracy.** With few members, one arm can be empty or
  near-empty; D8's member floor turns that into `insufficient_n`, not a
  verdict. The experiment may simply take longer than the eval TTL window —
  readouts are snapshots; the analysis must complete on snapshots, not
  assume the store accumulates forever.
- **Post-treatment collider guard.** Nothing agent-behavior-dependent enters
  the exposure definition (D6 uses only mechanical receipts); the balance
  check is the tripwire if that assumption ever breaks.
