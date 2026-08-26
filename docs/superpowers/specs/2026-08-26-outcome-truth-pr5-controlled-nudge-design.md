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
null, never fabricates an effect. A **balance check** is mandatory, and it
is absolute bounds, not a significance test — non-significance at small N is
the weakest evidence of balance exactly when imbalance is most damaging. The
readout reports a per-arm composition table (members, sessions per member,
runtime mix, per-protocol fraction of ITT), and the per-protocol read is
valid only if (a) the arms' per-protocol fractions of ITT are within 10
percentage points of each other, and (b) no arm's per-protocol sessions are
more than 50% from a single member. A violated bound demotes that snapshot's
per-protocol read to descriptive (`balance_violated`, table attached) —
never a verdict. The rule conditions only on pre-treatment, arm-independent
receipts.

**D7. The readout is an additive `arm_comparison` block on the always-on
compliance surface — not the Experiment shell.** `compare_arm_proportions`
(new, in the autopilot module) classifies **from the parsed per-eval records
`build_compliance` already holds** — each carrying `experiment_group`,
`created_at`, `briefing_delivered`, `runtime` and the D13 member token —
applying D6 record-by-record. (The `grade_self_reported` row's
`by_experiment_group` buckets are pre-aggregated hits/total carrying none of
D6's dimensions; they cannot be "restricted", and they stay byte-identical
under the frozen-row guard.) The block reports: the member-level primary
result (D8 — arm means of member proportions, permutation p), the
session-level χ²/Cohen's h/CI **labeled descriptive**, sessions and distinct
members per arm, the D6 balance table, unknown/not_exposed counts, the D12
`nudge_shown` coverage, and `insufficient_n` gating per D8. Stats helpers
come from `patterns/statistics.py` (`_chi_square_2x2` — whose stdlib
fallback makes it scipy-free; `_cohens_h`; `_confidence_interval_diff`); the
permutation test is new and pure stdlib. Tests run un-gated (not under the
`PATTERN_EXPERIMENTS_ENABLED` skip). **Dated
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

- **H1′ (controlled adoption lift) — member-level primary.** Randomization
  is member-level (D1), so the confirmatory unit is the member, not the
  session: grading is plausibly a per-member habit (intra-member correlation
  near 1), and a session-level test under that structure is not merely
  anticonservative — with ~33 sessions per member, the design effect
  1+(m̄−1)ρ inflates variance ~17× even at ρ = 0.5, and the session-level
  p-value stops meaning anything. Primary analysis: for each member with
  **≥ 5 per-protocol sessions**, compute the graded fraction; compare the
  arms' unweighted means of member fractions with an **exact permutation
  test** over arm reassignments (all C(m, m_A) reassignments, or 10,000
  Monte Carlo draws when the exact enumeration exceeds that), two-sided.
  H1′ holds iff permutation p < 0.05 AND the difference in arm means is
  ≥ 10 percentage points. Floors: **≥ 5 qualifying members per arm** (below
  4-vs-4 the permutation test cannot reach p < 0.05 at all — 3-vs-3 bottoms
  out at exactly 1/20) AND **≥ 99 per-protocol sessions per arm** (the
  fixed-z two-proportion bound, retained for the descriptive session-level
  readout: n = (z_α/2 + z_β)² · (p₁(1−p₁) + p₂(1−p₂)) / (p₁−p₂)², z
  1.96/0.8416, worst-case p = 0.5, MDE 20pp). Below either floor the block
  reports `insufficient_n` with the counts — never a verdict. The
  session-level χ² p < 0.05 ∧ |Cohen's h| > 0.1 readout is retained
  **descriptive-only** and can never substitute for the member-level
  primary. Stated honestly: the current fleet may never reach 5 members per
  arm; in that case H1′ stays `insufficient_n` indefinitely and PR6 stays
  gated — that is the designed outcome, not a defect to be patched by
  promoting the session-level number.
- **H2′ (honesty under the stronger nudge) — non-inferiority, not
  absence-of-significance.** Two conditions, both required: (a) the
  treatment arm's optimism-skew stays ≤ 15% of self-success sessions, at
  N ≥ 30 self-success sessions **per arm** (H2's bound and gate, unchanged);
  (b) a **one-sided non-inferiority test at α = 0.05 with a
  10-percentage-point margin** — the upper bound of the one-sided 95%
  confidence interval on (skew_treatment − skew_control) must lie below
  +10pp. Passing requires evidence of non-inferiority: an underpowered
  comparison reports `insufficient_n`, never a pass — "not significantly
  worse" at small N is the underpowered test passing by default, which is
  exactly backwards for an honesty guardrail. A nudge that lifts adoption
  by teaching flattery fails here regardless of H1′.
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

**D9. Arm→treatment mapping: decided by a public coin, not by the author.**
The original draft fixed A = treatment "chosen blind" — an unauditable
claim, since the per-arm adoption splits were live on
`GET /autopilot/compliance` from PR4's deploy (2026-08-25), a day before
this document existed. Replaced by a data-independent mechanism declared
before its input exists: **the treatment arm is "A" if the first hex digit
of this revision's git commit hash is even (0, 2, 4, 6, 8, a, c, e), "B" if
odd**. The hash cannot be known before the commit is made and cannot be
steered toward either outcome without discarding commits the reflog would
show. The resolved mapping is recorded in the implementation plan (the next
commit) and again in the T0 addendum.

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
server-side proof of composition. The accounting is pre-registered, because
withhold-on-failure creates assigned-but-untreated arm-A sessions that D6 —
which never conditions on `nudge_shown` — still classifies per-protocol:
the readout reports **nudge_shown coverage** (the fraction of arm-A
per-protocol sessions whose briefing has a `nudge_shown` record),
classification stays as-assigned (the dilution pulls the measured effect
toward null, never fabricates one), and coverage below 90% is flagged in
the block.

**D13. A hashed member token rides the eval pipeline (new data element).**
D7 and D8 need members-per-arm and per-member graded fractions, but the
parsed eval records carry no member identity — PR4-D1 stamped only
`experiment_group`, and without a member key the registered member floor
was uncomputable. The same field-riding path gains `member_token =
sha256(owner_member).hexdigest()[:12]`: stamped on the session beside
`experiment_group` at the same resolution point (the verified
`owner_member`, so token and arm cannot disagree about which member),
carried through the session_start replay payload into the parsed eval
snapshot, and grouped on by `compare_arm_proportions`. The token is one-way
(analytics surfaces never expose the raw member string) and deterministic
(the same member aggregates across sessions). Records without the token —
every eval predating the PR5 deploy — classify **unknown** for member-level
analysis; the deploy precedes T0, so no per-protocol record can lack it.

**D14. The verdict of record is one dated snapshot; every earlier view is
non-confirmatory.** The always-on `arm_comparison` block recomputes on
every compliance GET. Continuous recomputation plus a lift-when-crossed
`insufficient_n` gate invites first-crossing selection — unlimited looks at
nominal α — and the 30-day eval TTL makes successive snapshots overlapping
sliding windows an experimenter could shop between. Registered now: the
H1′/H2′ verdict of record is a **single dated snapshot committed as an
addendum to this document, taken at T0 + 28 days** (inside the 30-day TTL,
so the whole [T0, readout] window is intact in one snapshot). If the D8
floors are unmet at that date, `insufficient_n` IS the registered readout
of this experiment, and any continuation runs under a new dated
registration. Every intermediate view of the block is operational
monitoring, never confirmatory; the block carries `confirmatory: false`
until the registered snapshot exists.

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

## Interference and co-intervention (disclosed)

Dark deploy (D4) protects the *code* path; it cannot protect the *text*.
This document commits the verbatim treatment wording (D3) into the
repository whose guides and specs the measured fleet's own agents read —
CLAUDE.md links the guides into every session, and the Detection section
below updates two guides inside PR4's H1 window. The population under
measurement is the team dogfooding this repo. Two spillover channels are
therefore part of the registration, each with its bias direction:

- **In-repo text.** Any agent — either arm, or the PR4 uniform population —
  can read the strong-nudge wording in this spec or the guides, before T0
  and after. For PR4-H1, the in-window text is a uniform co-intervention:
  the registered PR4-H1 snapshot therefore measures the effect of the
  **mild-tool-description-nudge + in-repo-strong-nudge-text bundle**, not
  the D4 description alone, and is reframed as such by this dated note. For
  H1′, control-arm exposure to the text moves control toward treatment
  behavior — contamination that pulls the measured incremental lift
  **toward null**; it cannot fabricate a lift.
- **Shared team memory.** Members share one Keep: a treatment-arm session's
  graded outcomes and any learned grade-your-sessions knowledge are
  recallable by control-arm sessions. Same direction — control
  contamination, toward null. SUTVA does not hold across arms on a
  shared-memory fleet; what H1′ estimates is the incremental effect of
  *direct briefing delivery over ambient exposure*, which is the deployable
  quantity anyway (a real rollout would be fleet-wide with the same ambient
  effects).

A design that removed these channels — deployment-level randomization
across isolated Keeps, the text quarantined out of the repo — is out of
reach at this fleet size and would answer a less relevant question. The
disclosure is the mitigation, and the direction analysis is why the readout
stays interpretable: both channels shrink the measured effect, so a
positive H1′ survives them, while a null H1′ is ambiguous between "no
effect" and "ambient saturation" — a caveat the block carries in its own
text.

## Detection of the two drift points this spec corrects in prose

`docs/guides/replay-evals-patterns.md`'s PR4-vs-PR5 paragraph and the
`knowledge-autopilot.md` cross-references are updated to describe the
server-composed channel and the deferred H3′, each as a dated addition.

## Files

`auth/experiment.py` (new — the shared arm function; bridge re-imports);
`bridge/app/session.py` (import swap + the D13 member-token stamp beside
`experiment_group`); the session_start payload model and eval parser on the
PR4-D1 field-riding path (D13 — exact files pinned in the implementation
plan); `cortex/app/briefing/api.py` +
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
- Arm comparison: record-level fixtures (per-eval records with arm, member
  token, receipts, timestamps) against hand-computed permutation p and arm
  means — the permutation test pinned against a fully worked 4-vs-4
  example; the session-level χ²/h/CI present and labeled descriptive;
  `insufficient_n` below each D8 floor separately; unknown/not_exposed
  classification per D6; each absolute balance bound (10pp,
  50%-single-member) demonstrated both violated and clean; `nudge_shown`
  coverage computed; `confirmatory: false` present pre-addendum; freeze
  guard — the existing compliance rows byte-identical.
- Member token (D13): stamped beside `experiment_group` from the same
  member string (token/arm parity); carried into the parsed eval snapshot;
  an absent token classifies unknown for member-level analysis.
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
- **Post-treatment selection.** D6's receipts are mechanical, but
  population entry itself is not: a session exists in the eval store only
  because the agent called `ctx_start_session`, which is agent behavior the
  nudge could in principle influence — a treatment that changes
  session-starting or session-completing rates selects differently into the
  two arms. The D6 composition table is the named guard: sessions-per-member
  and per-protocol fractions per arm make a differential-entry effect
  visible rather than assumed away.

## Revision record

**2026-08-26, same day, pre-implementation.** Adversarial review (two
independent streams — claims verification against the codebase, hostile
methodology review — each followed by a skeptic verification pass) returned
twelve findings; this revision absorbs them before any code exists, the one
window in which a pre-registration may legitimately change. The substantive
changes: D7's data flow corrected — the pre-aggregated
`by_experiment_group` buckets carry none of D6's dimensions and could never
be "restricted", so classification now runs record-level; D13 added —
member identity did not exist on the eval records, so the registered member
floor was uncomputable as written; H1′'s primary analysis moved to the
member level (the randomization unit) with an exact permutation test, the
session-level χ² demoted to descriptive and its 3-members floor (which the
review correctly called theater) replaced by a 5-member floor at which the
permutation test can actually reach significance; H2′ recast as one-sided
non-inferiority with a stated α and margin (it was
acceptance-by-non-significance); the balance check given absolute bounds
and a composition table (it was an unspecified significance test with the
burden inverted); D14 added — one dated snapshot at T0 + 28 days is the
verdict of record, because an always-on block recomputed at will is
unlimited-looks α inflation; D9's unauditable "chosen blind" replaced by a
commit-hash coin; the interference section added with bias directions,
reframing the registered PR4-H1 snapshot as measuring the mild-nudge +
in-repo-text bundle; D12 given pre-registered `nudge_shown`-coverage
accounting; the post-treatment risk restated (population entry is itself
agent behavior). No threshold was weakened; every change corrects a
mechanical impossibility or strengthens the inferential standard.
