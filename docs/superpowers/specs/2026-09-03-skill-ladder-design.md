# The skill ladder — earned trust for skills — design

**Status:** approved in brainstorming (founder, 2026-09-03: "create a skill, if it's
used successfully several times, we promote it" + "ability to update a skill that
failed"; remaining forks ruled here and recorded); not yet implemented.
**Date:** 2026-09-03
**Scope:** a `trial` tier between draft and active; a nightly ladder pass that
admits drafts to trial, promotes trial skills on **independent, graded, applied**
evidence, demotes skills that fail, and hands a failed skill to the fleet to be
rewritten; the receipts that make "used" observable; a shadow mode that runs the
whole thing ledger-only first. Every automatic transition is reversible, logged
with its evidence, and vetoable by a human PATCH.

## Problem

Firekeep's knowledge base has a strict entry gate — every machine-authored skill
is a *draft* invisible to recall until a person activates it — and nothing
after it. Measured on the production Keep on 2026-09-02:

| measure | value |
|---|---|
| active skills | 93 |
| with any efficacy evidence (`skill_efficacy_n > 0`) | 0 |
| draft backlog | 32, median age 14 days, oldest 31 |

So the garbage-in/garbage-out guard is real but it has the failure the founder
does not want: **good drafts age out unreviewed while nothing bad can get in**,
and the one signal that could tell good from bad — outcome-weighted skill
efficacy — is unfed, because agents reach skills through the briefing (which
emits no receipt) or through `memory_recall` (which counts as a memory read), and
most sessions complete ungraded. Promotion "after several successful uses" is
the right rule; the work is making *used*, *successfully* and *several*
observable and honest.

The pattern engine already has the ladder shape this needs
(`cortex/app/patterns/lifecycle.py`: candidate → observed → trial → validated,
evidence + confidence thresholds, quarantine, decay, retirement — behind
`PATTERN_VALIDATION_ENABLED=false`). The autopilot guide's standing rule is to
generalize that ladder, not invent a second one, to require independence across
principals, and to run any autopilot action in shadow before it touches state.

## Decisions, and why

**1. A `trial` status, recallable but labeled and ranked last.** A draft can never
be "used", so a rung between draft and active is not optional: it is the only
place evidence can accrue. Trial skills appear in `skill_recall` and in the
briefing's skills section (**at most one per briefing**, after the actives),
rendered with a `[TRIAL]` marker so an agent knows the weight to give it. They
are excluded nowhere else new: `GET /skills?status=trial` lists them, the
dashboard filter gains the value, and `PATCH /skills/{id}` can move them anywhere
a human wants — the human path stays authoritative.

**2. "Used" is one of three signals; only the strongest promotes.** *Shown* — the
skill was injected into a briefing or returned by `skill_recall` (a
`memory_read` receipt; the briefing gains one with `trigger="briefing"`, the
one receipt it deliberately did not emit until now). *Reached* — an agent
deliberately asked for it: a `skill_recall` receipt (`trigger="skill_recall"`).
*Applied* — an agent said so: `memory_feedback(memory_ids=[skill_id],
useful=…)` (exists), or Living Procedures observed the skill's `step_specs`
executed (exists for procedure skills). A **success observation** is *applied
with `useful=true`* — or, when no feedback exists for that skill in that
session, a *reached* receipt — **and** the session's verified-owner grade is
`success` (`recognized_grade_pair` on the stored eval, Bridge `abandoned`
overriding as failure). A **failure observation** is `useful=false` **paired
with** a failed or abandoned grade. Passive exposure never counts either way:
a skill sitting in a briefing while the session succeeded for other reasons
would promote itself, and one displayed in three failed sessions would demote
itself. Shown-without-reach is the denominator of a *reach rate* the digest
shows — evidence about the trigger text, not the steps. **Cross-cutting
consequence, stated here because no single task can see it:** the new
`trigger="briefing"` receipts must be **excluded from OWM's `skill_efficacy`
exposure tally** (it joins every skill `memory_read` to the session grade, so
briefing receipts would drive every skill's efficacy toward the session base
rate); the ladder's evidence reader is the only consumer of briefing receipts.

**3. "Successfully" is the human-attributable grade, never "no exception".**
Ungraded and `partial` sessions contribute nothing; `abandoned` is a failure.
`useful=false` **alone** is not a failure — "not useful" is indistinguishable
from "not relevant to what I was doing", and a good skill with a broad trigger
would be punished for being recalled eagerly. It counts against the reach rate;
it becomes a failure observation only alongside a failed or abandoned grade.
**Two honest caveats.** The applied signal is a behavioural ask: today agents
rarely call `memory_feedback` on skill ids, so a healthy first fortnight is
measured by *feedback events on skills > 0* and *reach rate > 0*, and zero
promotions in shadow means the pipe is dry, not that the rules are wrong — the
stop hook's new sentence and the reached-receipt fallback exist for exactly
this. And on a solo Keep (one human, several agent identities — this PC, a
laptop, night-shift), "two distinct agents" is satisfied trivially; the
independence rule protects a team Keep and is stated as such, not oversold.

**4. "Several" means independent.** Promotion requires
`SKILL_LADDER_PROMOTE_MIN_SUCCESSES = 3` applied-and-succeeded observations from
at least `SKILL_LADDER_PROMOTE_MIN_AGENTS = 2` distinct agent identities, at most
`SKILL_LADDER_PER_AGENT_CAP = 2` observations counted per identity, zero
`useful=false` in the window, and Beta-shrunk efficacy
`(s + P/2)/(n + P) ≥ 0.6` with `P = OWM_PRIOR_N` (5) — so a lucky 1-of-1 cannot
pass. Replay events carry `agent_id` and `session_id` but no member id, so agent
identity is the independence key (the same key OWM's per-agent cap uses); when a
member id is present on the event it is preferred. These are lower than the
pattern ladder's 10/15/25 because each observation here is a graded session,
not a heuristic match. Evidence is counted only **since the skill's last status
change** (`ladder_since`), so a human demotion is not undone by stale evidence.
For the skills that exist before this ships, `ladder_since` defaults to
`approved_at`, else `stale_reviewed_at`, else the skill's `timestamp` — the
first run must not read a skill's whole history as if it were one window; it
is stamped explicitly by the first pass so later runs never re-derive it.

**5. Admission to trial is deterministic and capped.** A draft enters trial when:
trigger, symptoms and steps are non-empty; it is not a near-duplicate of an
active or trial skill (cosine ≥ `SKILL_LADDER_DUP_THRESHOLD = 0.92` against the
skill embedding); it is not `needs_rereview`; and the per-domain trial count is
under `SKILL_LADDER_TRIAL_CAP_PER_DOMAIN = 10`. At most
`SKILL_LADDER_ADMIT_PER_RUN = 20` per night, oldest first. A near-duplicate draft
is not admitted and is marked `duplicate_of=<id>` for the inbox — the fleet's own
output is the most likely flood, and the cap is what keeps a bad night from
filling the tier. **Never admitted:** a draft carrying `demoted_at`,
`ladder_rewrite_requested_at`, `trial_expired_at`, `superseded_by` or
`duplicate_of` — those are parked for a human or for the rewrite loop, and
without this rule a demoted skill would re-enter trial the next night on the
strength of nothing. Source-backed document drafts (`source_type="document"`)
are admitted on the same rule; they are not auto-*activated* on provenance in
this round (the guide allows it; it is a later switch).

**6. Failure demotes a trial skill; it flags an active one for rewrite.** A
**trial** skill with ≥ 3 failure observations and efficacy < 0.4 at n ≥ 5 (since
`ladder_since`) is demoted to **draft** with `demoted_at`, `demotion_reason` and
the evidence summary on its payload, and parked from re-admission. An **active**
skill meeting the same bar is **not demoted in this round**: a human activated it,
and three thumbs may mean "not what I needed" as easily as "wrong"; demoting and
rewriting would also open a visibility gap. It stays active and receives
`ladder_rewrite_requested_at`. In both cases the skill is handed to the fleet:
the enqueue pass posts a **`reauthor_failed_skill`** task carrying the skill plus
its failure evidence (the failing sessions' ids, the last feedback comment, the
efficacy numbers); Night Shift rewrites it into a new draft with
`reauthor_of=<failed id>` and `origin_job="reauthor_failed_skill"`; that draft
enters the ladder at trial like any other; and **when a re-authored skill is
promoted to active, the skill it rewrote is deprecated with `superseded_by`** —
the rewrite earns its place before the original loses it, which closes the loop
the founder asked for ("update the skill that failed") without ever leaving a
hole. A human can request the same rewrite by hand: the Skills tab gains a
**Rewrite** button that sets `ladder_rewrite_requested_at` through `PATCH`.
Unused trial skills — no *shown* receipt for `SKILL_LADDER_TRIAL_TTL_DAYS = 60` —
return to draft with `trial_expired_at` (never deleted). The rewrite job, the
supersede-on-promotion rule and the button ship in **PR2** (see Phasing).

**7. Shadow first, then enforce, by one setting.** `SKILL_LADDER_MODE` is
`"shadow"` by default: the pass computes every admission, promotion, demotion
and expiry exactly as it would apply them, writes each decision to a ledger
(`skills:ladder:decisions`, capped list in cortex Redis) and a per-skill
`ladder_shadow` payload field, and changes **no** status **and enqueues no fleet
task** — a "would request rewrite of X" line in the ledger is the whole effect,
so Night Shift never rewrites a skill that was never actually demoted or
flagged. The autopilot inbox
gains a `ladder_proposals` section and the digest a `ladder` block, so a human
watches two weeks of what the rules *would* have done before flipping to
`"enforce"`. In enforce mode every transition writes a `ladder_history` entry
(`from`, `to`, `at`, `reason`, evidence summary) onto the skill — the undo
trail — and still lands in the digest.

**8. The ledger, again.** Human-authored and document-derived skills carry no
`origin_job`, so ladder counters cannot hang off the fleet's per-job keys. The
ledger gains one **ladder-wide** key, `fleet:ledger:ladder`, with counters
`admitted`, `promoted`, `demoted`, `expired`, `rewrite_requested` (all-time and
per UTC day, same shape as the job keys); a ladder promotion of a fleet-authored
draft *additionally* counts `approved` on its origin job, so the fleet's kill
metric extends from "did a human approve the draft" to "did the draft earn its
way to active". Every promotion records `approved_by` on the skill —
`"ladder"` or `"human"`.

**9. The pattern ladder is reused in shape, not in code — said plainly.**
`patterns/lifecycle.py::evaluate_promotion` operates on `PatternCard`s with
heuristic match counts and a tip-lift statistic; skills are scored on graded
sessions and explicit feedback, which is why the thresholds here (3 / 2 agents /
0.6) are lower than 10 / 15 / 25 and why the two are not literally one function.
What is reused is the contract the guide asked for: staged rungs, evidence
thresholds, independence, decay/expiry, a terminal parked state a human lifts,
and shadow before state. The pattern engine itself is untouched.

**10. What this deliberately does not do.** No LLM anywhere in the ladder — every
rule is deterministic over receipts and grades (the deleted recall-ranker
lesson). No automatic demotion of active skills (decision 6). No `dormant` tier
for aged actives in this round (the staleness sweep plus the fleet re-author job
cover it; a demote-to-dormant rule is a named follow-up once trial has data). No
provenance-based auto-activation of document skills (switch later). No change to
the pattern engine itself. No dashboard write actions on the Autopilot tab (its
read-only pin holds; humans act in the Skills tab as today).

## Phasing

**PR1 — see it before it acts.** `trial` status end to end (API, MCP, recall,
briefing with one-trial cap and the `[TRIAL]` label, dashboard filter/badges);
the briefing receipt **with OWM's exclusion of `trigger="briefing"`** from its
skill tally; the evidence reader; the ladder pass in **shadow mode only**
(expire / demote-trial / flag-active / promote / admit as decisions, no status
change, no enqueue); ledger key; `ladder_proposals` inbox section and `ladder`
digest block; the stop hook's feedback sentence; settings, compose, docs. This
is the smart cut: it ships visibility immediately and answers the only question
that matters first — whether feedback events on skills appear at all.

**PR2 — let it act.** `SKILL_LADDER_MODE=enforce` path (transitions,
`ladder_history`, `approved_by`, supersede-on-promotion), the
`reauthor_failed_skill` fleet job (enqueue selector + Night Shift handler with
the failure-first prompt + ledger job), and the Skills-tab **Rewrite** button.
Flipping to enforce on the production Keep is a human decision taken after the
shadow ledger has shown at least a fortnight of decisions.

## Components

### A. Cortex — status, receipts, evidence

- `skill_status` gains `"trial"`. `SkillRequest.status` stays
  `Literal["active","draft"]` (nobody creates a trial by hand); `PATCH` accepts
  `"trial"`. Payload gains `ladder_since` (set on every status change, by the
  PATCH route and by the ladder), `ladder_history` (list, capped 20),
  `ladder_shadow` (last shadow decision), `approved_by`, `demoted_at`,
  `demotion_reason`, `ladder_rewrite_requested_at` (set on demotion; cleared when
  the fleet's re-author draft is created), `trial_expired_at`, `duplicate_of`,
  `superseded_by`.
- `search_skill_points` is unchanged (verbatim `must`); its two callers change:
  `GET /skills?status=active` keeps meaning active only, but `skill_recall` and the
  briefing's skills section query `skill_status ∈ {active, trial}` (`MatchAny`),
  sort actives first, cap trials to one for the briefing, and label trials.
- The briefing's skills section emits a `memory_read` receipt for the ids it
  injected with `trigger="briefing"` (best-effort, after the section renders).
- A new evidence reader `cortex/app/skills/ladder_evidence.py` joins, per skill
  and since `ladder_since`: `memory_read` receipts (shown; trigger recorded),
  `memory_feedback` events (applied; `useful`), and the session grade
  (`recognized_grade_pair` on `rp:eval:<sid>`, Bridge status override) into
  `{shown, applied, successes, failures, agents: {agent_id: successes},
  last_failure_sessions[:5], last_feedback_comment}`. It reuses OWM's helpers
  (`_default_events_fn`, `session_success`, `_fetch_bridge_statuses`) rather than
  re-implementing the join.

### B. Cortex — the ladder pass

`cortex/app/skills/ladder.py::run_skill_ladder()` — a Celery beat task
(`SKILL_LADDER_SCHEDULE_HOURS = 24`, registered after OWM in
`sleep_cycle.py`'s schedule), self-gated on `SKILL_LADDER_ENABLED` (default
`True`), SETNX-locked like its siblings, never raising. Order per run:
**expire** (trial → draft on TTL), **demote** (trial → draft on failure),
**flag** (active meeting the failure bar → `ladder_rewrite_requested_at`, stays
active), **promote** (trial → active; in PR2 also deprecate the `reauthor_of`
target with `superseded_by`), **admit** (draft → trial, capped, exclusions per
decision 5). In shadow mode each step writes decisions (`skills:ladder:decisions`
+ `ladder_shadow`) and touches no status and enqueues nothing; in enforce mode
(PR2) each step applies the transition in one `set_payload` together with its
`ladder_history` entry and records the ledger counter. Returns `{mode, expired,
demoted, flagged, promoted, admitted, skipped_duplicate, skipped_capped,
skipped_parked, errors}`; the run record is kept in Redis
(`skills:ladder:last_run`) for the digest.

### C. Cortex — the failed-skill re-author job (PR2)

`fleet_enqueue_pass` gains a third selector: skills marked for rewrite
(`ladder_rewrite_requested_at` set — by the ladder in enforce mode or by the
Skills-tab button — with no pending `reauthor_of` draft and no rejection marker)
→ task `title="reauthor_failed_skill"`, same dedup markers and caps as the stale
job, `context` = the stale-job context plus `{"failure": {"failures",
"successes", "efficacy", "last_failure_sessions", "last_feedback_comment",
"demotion_reason"}}`. The ledger's `JOBS` gains the title; the live-marker TTL is
the re-author 30-day one. In shadow mode nothing is ever flagged, so nothing is
ever enqueued.

### D. Client — Night Shift and the stop hook

PR1: the stop hook's completion message gains one sentence: "If a recalled skill
guided this work, `memory_feedback` its id with useful=true/false — that is what
promotes or demotes it." PR2: `nightshift.py` lists a fourth title,
`reauthor_failed_skill`, handled by the existing re-author handler with a prompt
variant that puts the failure evidence first ("this skill was applied in these
sessions and they failed / users marked it not useful — rewrite so the failure
cannot recur, or retire it") and passes `origin_job="reauthor_failed_skill"`.

### E. Autopilot surfaces and dashboard

- Inbox: `ladder_proposals` section (shadow mode: what would change tonight;
  enforce mode: what changed last night) — rows `{id, title, from, to, reason,
  evidence: {successes, failures, agents, shown, applied}}`; also lists
  `duplicate_of` drafts.
- Digest: `ladder` block `{mode, last_run, counts…, trial_count,
  reach_rate_by_tier}`.
- Dashboard: Skills tab filter gains `trial` (badge `TRIAL`, an *Activate* and a
  *Back to draft* button); skill cards show `approved_by` and the last
  `ladder_history` line; Autopilot tab renders the new section and block
  through the existing two fetches (no new fetch, no write verbs).

### F. Settings (config.py, compose ×4 services, .env.example, guide)

Six operator-facing settings — the ones someone will actually turn:
`SKILL_LADDER_ENABLED=true`, `SKILL_LADDER_MODE=shadow` (`shadow|enforce`),
`SKILL_LADDER_SCHEDULE_HOURS=24`, `SKILL_LADDER_PROMOTE_MIN_SUCCESSES=3`,
`SKILL_LADDER_PROMOTE_MIN_AGENTS=2`, `SKILL_LADDER_TRIAL_TTL_DAYS=60`. The
evidence window reuses `OWM_WINDOW_DAYS` (30, matching the eval TTL). Everything
else is a named module constant in `skills/ladder.py`, documented in the guide
but not plumbed as an env var: `PER_AGENT_CAP=2`, `PROMOTE_MIN_EFFICACY=0.6`,
`DEMOTE_MIN_FAILURES=3`, `DEMOTE_MAX_EFFICACY=0.4`, `DEMOTE_MIN_N=5`,
`DUP_THRESHOLD=0.92`, `TRIAL_CAP_PER_DOMAIN=10`, `ADMIT_PER_RUN=20`. Sixteen
knobs nobody tunes are a support surface, not a feature.

## Data flow

Day: agent session → briefing injects ≤3 active + ≤1 trial skill (receipt,
`trigger=briefing`, ladder-only) / `skill_recall` (receipt, reached) → agent
applies one → `memory_feedback(skill_id, useful)` (event) →
`ctx_complete_session(task_result)` (grade). Night: OWM scores `skill_efficacy`
(briefing receipts excluded); **ladder** joins receipts + feedback + grade since
`ladder_since` → expire / demote-trial / flag-active / promote / admit. PR1
(shadow): every decision goes to the ledger, inbox and digest and nothing
moves. PR2 (enforce): transitions apply with `ladder_history`; a flagged or
demoted skill becomes a `reauthor_failed_skill` task; Night Shift drains it into
a new draft → admitted to trial → earns its way up → on promotion the original
is superseded. Humans see proposals (shadow) or history (enforce) and can veto
with one PATCH at any point.

## Error handling

The pass is fault-isolated per step and per skill (one unreadable eval never
stops the run); a Redis or Qdrant failure ends the run with `errors` populated
and no partial transition left half-applied (status and `ladder_history` are
written in one `set_payload`). The briefing receipt is best-effort and cannot
fail the briefing. Shadow mode has no state-changing path at all. Enforce mode
never deletes: the worst automatic outcome is a wrong demotion, which is one
PATCH to undo and is visible in the digest the same morning.

## Testing

- Evidence reader: shown/applied/success/failure classification across graded,
  ungraded, partial, abandoned sessions; `useful=false` overrides a success
  grade; per-agent cap; `ladder_since` cutoff; member id preferred when present.
- Ladder rules as pure functions over evidence dicts (promote / demote / expire /
  admit decisions) with the threshold table; independence rule (3 successes from
  one agent do not promote); duplicate detection; per-domain cap; per-run cap.
- Pass in shadow mode: decisions logged, **no** `set_payload` status change;
  enforce mode: transitions applied with `ladder_history`, `superseded_by` on
  promotion of a `reauthor_of` skill, `reauthor_failed_skill` enqueued on
  demotion, ledger counters incremented; SETNX lock; never raises.
- API/MCP: `PATCH` to trial sets `ladder_since`; `skill_recall` returns trials
  labeled and last; briefing caps one trial and emits the receipt; `GET
  /skills?status=trial`.
- Fleet: enqueue selector for rewrite-requested skills; Night Shift handler for
  the new title with the failure-first prompt; ledger job.
- Dashboard: filter value, badges, buttons, Autopilot section/block, read-only
  pin unchanged; the section-key cross-file guard picks up `ladder_proposals`.
- Docs guards: config defaults in compose/env/guide (`test_config_fleet.py`
  pattern), client-kit forbidden tokens.

## Documentation

`docs/guides/knowledge-and-skills.md` (the ladder, statuses, receipts),
`knowledge-autopilot.md` (§4 sections, new §9 "The skill ladder"; update "What
unlocks round 2" — this *is* round 2's first rung, with the shadow gate
honoured), `cortex-configuration.md`, `cortex-api-endpoints.md`,
`client-kit.md` (Night Shift fourth title, stop-hook sentence), root `CLAUDE.md`,
`README.md` (Knowledge Autopilot row), `.env.example`, compose.
