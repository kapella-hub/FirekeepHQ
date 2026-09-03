# Knowledge Autopilot — round 1: truth and visibility

The goal is a knowledge base nobody has to manage daily. The design conclusion
that shaped this round (recorded here because it will be proposed again): **the
only dangerous autopilot is one built before the outcome signal is real.** The
measured state of that signal — one outcome-bearing replay event per typical
session, `failure_rate` 0.0 everywhere, `owm_efficacy` converging toward
"everything worked" (see the OWM section of
[`memory-and-recall.md`](memory-and-recall.md)) — means a promotion/retirement
engine built today would act confidently on a signal that cannot distinguish
success from silence. The Living Procedures design doc drew the sharpest
consequence: *"If every execution succeeds, then skipping any step correlates
with success, every step looks dead, and the pass proposes deleting the entire
procedure — confidently, with statistics."*

**Update, 2026-08-23 (outcome truth PR1) — the signal itself changed, not
just its consumers.** Appended here rather than folded into the paragraph
above, because that paragraph is the measurement that justified round 1's
caution and stays true as a record of the state that shaped it.
`ctx_complete_session` now accepts an optional structured self-grade —
`task_result` (`"success"` / `"partial"` / `"failure"`) plus up to 10
`task_evidence` claims — and both terminal replay events (`session_end` from
the tool layer, `session.completed` from `SessionManager`, independent
channels that fail independently) carry the same atomic `(task_result,
task_result_source)` pair rather than either one alone. The grade is
principal-bound: it is accepted only from the session's verified owner
(`owner_member`, written once at `start_session` and never reassigned), and a
caller's `agent_id` label is explicitly NOT terminal authority on its own —
`ctx_complete_session`, the public `ctx_abandon_session`, and
`ctx_resume_session`'s `takeover=True` all refuse a cross-member attempt
before any state changes, even though a label match alone used to be
sufficient. The trusted reaper path is unchanged: it calls
`SessionManager.abandon_session` directly with the session's own label owner
as `agent_id`, bypassing the public tool's member check entirely, because it
is Bridge's own machinery reclaiming an idle session on the system's
authority, not a caller claiming ownership. Only Bridge can submit a grade
hint on the eval-compute route — `eval:grade` is a service-only scope, minted
onto exactly one credential (`FIREKEEP_BRIDGE_KEY`), rejected outright by
`create_key`, and excluded from every enrollable and anonymous scope union.
The store is first-graded-wins (a WATCH/MULTI CAS in both
`app.evals.store.store_eval` and `SessionManager.complete_session` — a
stalled writer's late commit can never overwrite an already-graded record),
and a legacy record, a sourceless grade, or a session that never graded at
all resolves as `unknown`, never as a guessed "success" — see the OWM bullet
in [`memory-and-recall.md`](memory-and-recall.md) and
[`replay-evals-patterns.md`](replay-evals-patterns.md) for the full
mechanism. This closes the signal gap that motivated round 1's caution, but
it does not by itself unlock round 2: most sessions still complete with no
grade at all (an honest absence beats a guessed one, by the tool's own
contract), so both outcome classes still need to actually accumulate before
promotion criteria can trust them — see "What unlocks round 2" below, now
readable against a real signal instead of a degenerate one.

So round 1 ships **attribution and visibility, no autonomous mutation**:

| Shipped | Deliberately NOT shipped (yet) |
|---|---|
| Feedback-weighted recall + `memory_feedback` MCP tool | Auto-promotion of skills/memories |
| Bridge session reaper (the missing failure signal) | Auto-retirement (beyond existing archive-first GC) |
| Contested-not-superseded for unconfirmed conflicts | Trial stages, lifecycle ladders for memories/skills |
| `/autopilot/inbox` + `/autopilot/digest` + dashboard tab | Autopilot "modes" — round 1 *is* Recommend mode |
| `/memory/{id}/evidence` ledger read | LLM-judged "did the agent follow this advice" |
| `/autopilot/compliance` — Living Instructions rounds 1–4 (compliance table + attribution/exposure + grading adoption + honesty skew) | Instruction rewriting/AB — Living Instructions later rounds |

## 1. Feedback-weighted recall

`POST /memory/feedback` existed with dashboard thumbs wired to it — and
nothing consumed the stored signal, and a second thumb overwrote the first
(three flat last-write-wins fields). Now:

- `set_feedback` accumulates `feedback_useful_count` / `feedback_not_useful_count`
  (+ `feedback_last_at`, `feedback_last_comment`, comment bounded 500 chars).
  Read-modify-write, same benign-undercount contract as the recall access
  counters.
- Recall scoring applies a Beta-shrunk multiplier through the same
  `owm.compute_efficacy` OWM uses: neutral at zero feedback (unrated memories
  rank bit-identically to pre-feedback), clamped to `[1−W, 1+W]`. One thumb
  nudges (~2%); it never yanks — `compute_efficacy(1, 1, prior=4) = 0.6`.
- The `memory_feedback` MCP tool gives agents the channel session outcomes
  cannot carry: a session can succeed while one recalled memory was misleading.
  Its docstring tells agents to report on knowledge they *acted on*, not merely
  saw. Works on skill ids too (stored; skill search does not consume it yet).

Settings (cortex): `FEEDBACK_ENABLED=true`, `FEEDBACK_WEIGHT=0.10`
(deliberately below `OWM_WEIGHT=0.15` — one reader's thumb is noisier evidence
than a session outcome), `FEEDBACK_PRIOR_N=4`.

## 2. The session reaper (Bridge)

The larger half of the outcome-degeneracy fix. A session that died without
`ctx_complete_session` sat `status='active'` forever: no TTL, no eval, and —
because OWM reads Bridge `abandoned` as failure — invisible to outcome scoring.
The sessions most likely to have gone *badly* were exactly the ones that never
counted.

The reaper (`bridge/app/reaper.py`, same worker-loop shape as the distiller)
abandons sessions idle beyond a threshold through the existing abandon path —
pointer cleanup, TTL, `session_end` with `outcome='partial'` (payload carries
`reaped: true`), eval trigger — so walked-away sessions finally register as
non-successes.

Settings (bridge, `NB_` prefix): `NB_REAPER_ENABLED=true`,
`NB_REAPER_IDLE_HOURS=72` (three days of silence on an "active" session is a
crash or a walk-away, not a lunch break), `NB_REAPER_INTERVAL_SECONDS=3600`,
`NB_REAPER_MAX_PER_PASS=500` (bounds the first pass on an old deployment —
zrangebyscore returns longest-idle first, so the backlog drains oldest-first
across hourly passes instead of firing one eval POST per session in a burst).

**Honest tradeoff:** abandonment does not distill — a reaped session's content
is discarded when its TTL lapses (the pre-existing abandon semantic).
Recovering knowledge from failed sessions is future work; the outcome signal is
the point of this round.

**Known interaction:** OWM's abandoned-session detection reads Bridge's
200-newest session window (documented best-effort in
[`memory-and-recall.md`](memory-and-recall.md)), and the reaper by construction
abandons the *oldest* index entries — on a deployment busy enough to push a
reaped session past 200 before the nightly OWM join, that session's failure
signal is under-counted (the status flip and its eval still land immediately
and correctly). Far below current scale; widen or paginate
`_fetch_bridge_statuses` before trusting reaper-driven efficacy on a deployment
with hundreds of sessions per month. Until then, a flat `owm_efficacy` after
enabling the reaper is not evidence the reaper is idle.

## 3. Contested, not silently superseded

The deep-contradiction pass (memory agent, 0.85–0.95 similarity band) used to
supersede the lower-confidence side — which, with both counts at 0/0, meant
"the newer timestamp wins" dressed as a decision, and the loser became
unrecallable (recall filters hard on `status=active`). Nobody ever saw the
conflict.

New split, one rule per evidence class:

- **Confirmed keeper vs unconfirmed rival** → supersedes exactly as before.
  Human evidence is a real signal; acting on it is not guessing.
- **Unconfirmed vs unconfirmed** → both stay active, both get
  `contested`/`contested_with`/`contested_at` flags. Recall annotates the
  dispute (`[CONTESTED by <id>]`) at full score — the agent should *see* the
  disagreement, not have one side silently down-ranked out of view. The pair
  waits in the inbox for a verdict; an already-contested pair is never
  re-contested.

Resolution is `POST /memory/contested/resolve` (`memory:write`):
`action='supersede'` (winner confirmed — the verdict IS human evidence; loser
superseded with the contradiction counted and the SUPERSEDES edge recorded,
same as every other supersede path) or `action='coexist'` (both true in their
own contexts; flags clear and each side keeps a durable `coexist_with` marker
naming the other). The marker is load-bearing: both texts still sit in the
similarity band after a coexist verdict, so without it the nightly pass would
re-contest the identical pair within 24 hours and the verdict would be
functionally meaningless. Ordering is verdict-first, flags-cleared-last — a
failed supersede write 500s with the dispute still recorded, so the pair stays
in the inbox and the human retries instead of the verdict silently
evaporating.

**Proposed, then resolved (Fleet-as-GPU, 2026-09-02).** A client Night Shift worker can now file a *proposal* on a contested pair — `POST /memory/contested/propose` (`memory:write`), same shape as the verdict plus a `rationale` — which sets `proposed_verdict {action, winner_id}`, `proposed_rationale`, `proposed_by`, `proposed_at` on both points and nothing else: the pair stays contested, recall keeps annotating it, and the inbox row shows the proposal beside the pair. Only `/memory/contested/resolve` (a human) supersedes or coexists; it clears the four `proposed_*` fields with the contested flags and scores the proposal in the fleet ledger (`resolved`, plus `matched` when the human's action and winner equal the proposal's). A second proposal overwrites the first and is not counted again. Member-private points never get proposals because they are never enqueued (§8).

## 4. Inbox and digest

`GET /autopilot/inbox` (admin) aggregates every place review work already
accumulates — draft skills, stale skills, source-changed (`needs_rereview`)
skills, low-efficacy skills, Living Procedures proposals, runbook deviations,
contested pairs, the eval DLQ — into one surface with per-section fault
isolation (one broken store never blanks the inbox). A runbook deviation is Enforced Runbooks' exception
trail — a block that fired, a challenge an agent acknowledged, or a matched
command that failed — kept newest-first per workspace under a disclosed
200-entry cap (`MAX_DEVIATIONS`), so the section's `approximate` flag means
the ledger itself has trimmed. `GET /autopilot/digest?days=7` answers "what changed this week"
(learned/archived/superseded/dreamed/drafted/feedback/GC actions) with capped
scans marked `approximate` when capped. The dashboard's **Autopilot** tab
renders both, read-only — round 1 proposes and reports, it never mutates, and
the dashboard guard test pins that absence.

**`low_efficacy_skills` (2026-08-24, outcome truth PR3, D4).** Flags active
skills the nightly OWM pass (see the OWM section of
[`memory-and-recall.md`](memory-and-recall.md)) scored below neutral with
enough evidence to trust the number — filtered server-side on
`skill_efficacy_n >= MIN_N` (`5`, matching `OWM_PRIOR_N`'s default: the point
past which the Beta-shrinkage prior stops dominating the score) **and**
`skill_efficacy < THRESHOLD` (`0.4`, a below-neutral cutoff — `skill_efficacy`
is a success rate centered on `0.5`). Each row carries `skill_efficacy` and
`skill_efficacy_n` together, deliberately, so a reader can never mistake a
low-`n` neutral-prior score for a real measurement. VISIBILITY ONLY, same as
every other section here: it does not change recall ranking and does not
mutate `skill_status`; a flagged skill is a cue for a human to go read it, and
the ranking-side response to a low score is explicitly deferred to a later
round.

**Fleet block in the digest (2026-09-02).** `GET /autopilot/digest` gains `fleet: {enabled, jobs}` — per job type (`distill_session`, `reauthor_stale_skill`, `propose_contested_verdict`) a `window` and an `all_time` block read from the fleet ledger (§8): `produced / approved / rejected / approval_rate` for skill jobs, `proposed / resolved / matched / match_rate` for verdicts, all-time `pending = produced − approved − rejected` (skill jobs only — a verdict job's all-time block carries no `pending` field). A rate is `null` when its denominator is zero — never a prior — and the dashboard renders `null` as `—`. The dashboard also now lists the `low_efficacy_skills` section it had been omitting (the headline total counted rows the panel never showed); `tests/test_dashboard_autopilot.py` pins that every section the API emits has a dashboard entry.

**`ladder_proposals` in the inbox, and a `ladder` block in the digest (2026-09-03, skill ladder PR1, shadow).** `GET /autopilot/inbox` gains a section listing the skill ladder's most recent shadow decisions — admit, promote, demote, flag, expire, each with its evidence — plus every draft currently parked as a probable duplicate; it counts toward `total_actionable` like every other section. `GET /autopilot/digest` gains a `ladder` block with the last run's mode, counts and per-tier (active/trial) shown/reached/rate numbers. Full mechanics, thresholds and the shadow contract: §9.

## 5. The evidence ledger read

`GET /memory/{id}/evidence` (admin) composes what already flows into one
response: provenance (source, member, project, dream lineage), usage (access
counts, last recall), judgments (confirm/contradict counts, feedback
counters), outcomes (OWM efficacy), disputes (contested state), lineage
(supersession chain), archive state. Nothing new is recorded — the point is
that before anything is ever promoted or retired automatically, a human can
see *why* a memory ranks as it does in one read. Admin-scoped like the inbox,
and for the same reason: it exposes free-text feedback comments and
member/agent provenance, and memory ids are handed out by recall.

## 6. The instruction-compliance table (Living Instructions round 1)

`GET /autopilot/compliance` (admin) scores each rendered-block instruction
against what sessions actually did — deterministic predicates over the stored
session evals (`rp:eval:*`), rendered as the **Living Instructions** table on
the dashboard's Autopilot tab. The predicates are frozen to the 2026-08-11
founding measurement
([`docs/superpowers/specs/2026-08-11-living-instructions-design.md`](../superpowers/specs/2026-08-11-living-instructions-design.md)
is the pre-registration); changes arrive as new rows, never as edits, or every
later comparison against the baseline is orphaned. The response carries its
own honesty contract: a `notes` entry stating that compliance measures
*behavior*, not whether the behavior helped (the outcome signal is still
degenerate — see `replay-evals-patterns.md`), an `unparsed` count so the
denominator cannot silently shrink, and a halves-by-eval-time trend that is
withheld entirely below 10 sessions rather than shown small.

**Round 2 (2026-08-12, the spec's "Round 2 — the measurement contract") is
additive attribution, not rewriting** — instruction rewriting and A/B
validation remain later rounds, deliberately not built. The headline
`hits/total/rate` keep the all-sessions denominator (baseline comparability);
each row gains `by_runtime` (the same frozen predicate sliced by the session's
`X-Firekeep-Runtime` label — an untrusted observability header, never a gate —
with an `unattributed` bucket disclosed for sessions whose client predates
0.1.41) and `exposure`: exposed / not-exposed / unknown session counts plus an
exposed-only rate (`exposed_hits`/`exposed`, `null` when nothing was exposed),
where `exposure` itself is `null` for the two derived rows
(`recall_visibly_used`, `outcome_bearing`) that have no instruction text to be
exposed to. A session counts as *exposed* to a key only when a verified
artifact carrying that key's text reached it — rendered block verified current
or handshake delivered, with per-key introduction versions; everything else is
`unknown`, including every pre-0.1.41 session, forever — nothing backfills.
The dashboard feature-detects both fields per response, so the table renders
the round-1 surface unchanged against a server that does not send them.

**Round 3 (2026-08-25, outcome truth PR4 D2) adds grading ADOPTION as a new
frozen row, `grade_self_reported`** — appended per the "changes arrive as new
rows" rule above, so the six 2026-08-11 founding predicates stay untouched
(pinned by a dedicated test that reproduces the founding fixture with and
without the new grade/arm fields present, plus the existing compliance suites
staying green as their own freeze guard). The predicate reads
`recognized_grade_pair(task_result, task_result_source)[0] is not None` — did
the agent self-report ANY recognized grade at all, success, partial, or
failure, not whether the grade was good. `task_result`/`task_result_source`
live on the top-level eval record, not inside `metrics`, so the dict handed to
every predicate is now built at the call site as `{**metrics, task_result,
task_result_source, experiment_group}`; every frozen predicate keeps reading
its metric key out of the enriched dict exactly as before (no metric key
collides with the three promoted keys), and only the new row reads the grade
keys. `grade_self_reported` alone additionally carries `by_experiment_group` —
`{"A": {hits, total}, "B": {hits, total}}` — a session with no (or an
unrecognized) `experiment_group` is excluded from the split but still counts
toward the row's overall `hits`/`total`/`rate`.

**Round 4 (2026-08-25, outcome truth PR4 D3) adds a top-level `optimism_skew`
block — not an INSTRUCTIONS row, because it measures the grading channel's
HONESTY, not compliance with an instruction.** Of self-reported-success
sessions (`recognized_grade_pair(...)[0] == "success"`), what fraction also
carry an INDEPENDENT failure contradiction: `has_failures` (`failure_event_ids`
non-empty — per-tool-call `outcome=="failure"` events) OR a guarded
`tool_success_rate < 1.0`, counted ONLY when `outcome_event_count >= 2` — the
same non-independence trap `replay-evals-patterns.md` already documents for
`_failure_rate`/`_tool_success_rate` on a near-empty outcome population,
reapplied here as a non-negotiable guardrail: bare `tool_success_rate` or
`failure_rate` are never used as independent evidence. Bridge `abandoned` (a
third, harder contradiction) is DEFERRED — wiring `owm._fetch_bridge_statuses`'s
REST call into what is today a synchronous, network-free, Redis-only endpoint
would add a hard dependency on every request, plus a population mismatch
(Bridge's 200-session cap vs. the eval scan's window) to reconcile; the two
free signals are independent and sufficient to ship. Reported `overall` and
per `experiment_group` (same "A"/"B"-only, `None`-excluded convention as
`grade_self_reported`'s split above), each gated at `MIN_SELF_SUCCESS_N = 30`
self-success sessions: below the threshold, `rate` is `null` and
`insufficient_n` is `true` — never a bare `0.0` that would read as a clean
measurement on almost no data (the `outcome_event_count` lesson, applied to
skew). Reuses the exact parsed-evals scan `build_rows` already consumes (no
second scan of `rp:eval:*`), and shares the response's top-level
`approximate`/`unparsed` disclosures rather than duplicating them.

**PR4 is observational instrumentation and a mild nudge, not the controlled
experiment.** `experiment_group` (PR4 D1) — a stable sha256 hash of the
verified `owner_member`, stamped once at `start_session`, sticky per member,
orthogonal to the grade, `None` for an empty `owner_member` rather than a
hashed arm — exists so both rows above can report per-arm, but PR4 ships its
one nudge (the strengthened `ctx_complete_session` description — see
`replay-evals-patterns.md`) to EVERY session alike, so there is no
differential-by-arm treatment within PR4 to test. Its evidence is two plain
before/after proportions against pre-registered thresholds (adoption ≥ 20% of
completed sessions within a 2-week window; skew ≤ 15% of self-success sessions
once `MIN_SELF_SUCCESS_N` is reached), not an inferential test — PR4 builds no
statistics machinery. The pre-registration — hypotheses and thresholds
committed before the nudge could move the numbers — is
[`docs/superpowers/specs/2026-08-25-outcome-truth-pr4-adoption-design.md`](../superpowers/specs/2026-08-25-outcome-truth-pr4-adoption-design.md).
The CONTROLLED per-session A/B (the client-rendered nudge actually varying by
`experiment_group`, plus the two-proportion and McNemar/Cohen's-κ inferential
stats) is PR5, gated on the client channel and coordinated with the client
release.

**Round 5 (2026-08-26, outcome truth PR5) adds `arm_comparison` — the controlled-experiment readout — as a second top-level block, alongside `optimism_skew`, on the same `GET /autopilot/compliance` response.** Pre-registered in [`docs/superpowers/specs/2026-08-26-outcome-truth-pr5-controlled-nudge-design.md`](../superpowers/specs/2026-08-26-outcome-truth-pr5-controlled-nudge-design.md) (`cortex/app/autopilot/arm_comparison.py::compare_arm_proportions`, classifying record-by-record from the same parsed evals `build_compliance` already holds — not the pre-aggregated `grade_self_reported.by_experiment_group` buckets, which carry none of the dimensions below). D6 classifies every A/B record into one of four populations: **per-protocol** (arm assigned, `created_at >= T0`, `briefing_delivered == True`, `runtime == "claude"`), **ITT** (intent-to-treat — every arm-assigned session with `created_at >= T0`, the secondary analysis), **not_exposed** (an explicit `briefing_delivered == False` or a wrong runtime — assigned but unreached), and **unknown** (an unparseable or missing receipt — disclosed, excluded from both populations; absence is never counted as `not_exposed`). A mandatory **balance check** demotes the per-protocol read to descriptive (`balance_violated`) whenever either absolute bound is crossed: the arms' per-protocol fractions of ITT must sit within **10 percentage points** of each other, and no arm's per-protocol sessions may be **more than 50%** from a single member. **H1′** (member-level primary, D8) computes each qualifying member's graded fraction over per-protocol sessions and compares the arms' unweighted means with an exact/Monte-Carlo permutation test; it holds only above two floors together — **≥5 qualifying members per arm** (each with **≥5 per-protocol sessions**) AND **≥99 per-protocol sessions per arm** — below either, the block reports `insufficient_n`, never a verdict. **H2′** (honesty, non-inferiority) requires the treatment arm's optimism-skew rate at or below 15% AND a **one-sided, α=0.05, +10-percentage-point-margin** non-inferiority test on (treatment − control) skew, gated at **N≥30 self-success sessions per arm**; an underpowered comparison reports `insufficient_n`, never a pass — "not significantly worse" at small N would be the wrong side of an honesty guardrail. **`nudge_shown` coverage** (D12) reports the fraction of treatment per-protocol sessions whose briefing carries a server-recorded `rp:nudge_shown:{briefing_id}` receipt, flagged below 90%; the section is withheld entirely when the receipt write itself fails, so an assigned-but-unrecorded exposure still classifies per-protocol under D6 and dilutes toward null rather than fabricating a lift. The whole block carries `confirmatory: false` on every response (D14): the registered verdict of record is one dated snapshot taken at **T0 + 28 days**, committed as an addendum to the spec; every live view — including this one — is operational monitoring, never the readout. Two config vars gate the whole experiment (`cortex/app/config.py`): `GRADING_NUDGE_ENABLED` (default `False`) and `GRADING_NUDGE_T0` (default `""`, unset) — both are set together, once, at the human-calendar flip (T0, no earlier than 2026-09-08 per spec D4), never independently. **D11 (a readout rule, not new code):** if PR4-H2's `MIN_SELF_SUCCESS_N=30` gate (Round 4 above) is still unmet at T0, the PR4-H2 readout is thereafter taken from the **control arm only** — the existing `optimism_skew.by_experiment_group` split already carries the per-arm numbers this rule reads from.

## 7. The trust ledger (round 1)

`GET /autopilot/trust` (admin) aggregates the gateway declarations agents
already make — `agent.action.predict` / `agent.action.reconcile` events in the
`rp:events` replay stream — into a per-`agent_id` track record: declared and
reconciled counts, reconciliation rate, prediction-match calibration and its
trend, reversals, sessions, and window-relative first/last seen. It renders as
the **Trust Ledger** card on the Autopilot tab. Visibility only — like the
compliance table, it reports and never gates; the enforcement half (a capability
broker) is round 2, and earned autonomy is the two together. The formulas are
frozen at birth
([`docs/superpowers/specs/2026-08-16-trust-ledger-round1-design.md`](../superpowers/specs/2026-08-16-trust-ledger-round1-design.md)
is the pre-registration) so later rounds compare cleanly; changes arrive as new
components, never as edits.

Three frozen constants (module constants in `cortex/app/autopilot/trust.py`):
`TRUST_WINDOW_DAYS=30` (the aggregation window, matching the eval TTL and the
compliance window), `TRUST_SCAN_CAP=50000` (read the latest `cap+1` stream
entries — `cap+1` returned means the window holds more than the cap, so
`truncated: true`), and `TRUST_MIN_CALIBRATION_POINTS=5` (below this many
*scored* predictions `calibration`/`calibration_trend` report null, labelled
"not enough signal", never a default-bad number).

The card carries its honesty contract at the surface, not just in the design:

- **Behavior, not competence.** Calibration scores whether an agent's *stated
  prediction criteria* matched what was observed, not whether the decisions were
  good — an action with empty criteria scores a perfect 1.0, so a confidently
  mediocre agent still scores well. `reversals` (a reconciled declaration whose
  `outcome.success == false`) is a different dimension on purpose; neither is
  "was it a good call."
- **A floor on a floor.** The ledger sees only what an agent *declared* through
  the gateway, and only what replay *captured* of that (best-effort emit,
  approximate stream trim) — undeclared or unemitted work is invisible.
- **`agent_id` is self-reported.** It is an observability label, not the tenancy
  boundary (`workspace_id` is that, verified and unforgeable), so the record is
  *per declared identity* — one actor can split its work across identities or
  merge two under one, which is precisely why round 1 is visibility-only.
- **Truncation nulls the biased metrics, not the counts.** Under `truncated`,
  `reconciliation_rate`, `calibration`, `calibration_trend` and
  `first_seen_in_window` return `null` (the card renders "—" with the reason,
  never 0) because each needs the whole window; `declared`, `reconciled`,
  `reversals`, `scored_predictions`, `sessions` stay lower bounds and
  `last_seen_in_window` survives. Invalid events (blank `agent_id`/`session_id`,
  missing `action_id`, malformed JSON, bad timestamp) are counted in a visible
  `invalid` breakdown, never silently dropped.

Deployment-global in round 1, exactly like `build_compliance` — replay events
carry no `workspace_id`, so per-workspace scoping is a write-path change out of
scope here; the tenancy invariant (filter on the caller's workspace, or restrict
to a deployment superadmin and otherwise fail closed) must hold before any
deployment serves more than one workspace.

## 8. The fleet: enqueue, drain, and the approval ledger (Fleet-as-GPU MVP, 2026-09-02)

The nightly passes already *find* the work a fleet could do — `skill_staleness_pass` flags skills nobody recalled in `SKILL_STALE_AFTER_DAYS`, `deep_contradiction_pass` contests unconfirmed pairs — and until now both sat in the inbox until a human got to them. `fleet_enqueue_pass` (`app/fleet/enqueue.py`, registered **after** `skill_staleness` in `run_memory_agent`, gated by `FLEET_ENQUEUE_ENABLED`, default on) posts one relay task per finding through relay's new `POST /tasks` using `RELAY_URL` + `FIREKEEP_INTERNAL_KEY` — the same seam the briefing reads `GET /tasks` through. (That route carries no per-route scope on purpose: the internal key has no `relay:*` scope and deployed keys are never re-scoped; see `docs/guides/relay-coordination.md`.) The tasks — `reauthor_stale_skill` with the skill's fields in `context`, `propose_contested_verdict` with both texts — are drained by client Night Shift workers against a **local** model (`docs/guides/client-kit.md`, Night Shift). Nothing generates on the server.

**Dedup is state-based, because relay tasks have no idempotency and expire in 7 days.** A stale skill is enqueued only if no skill with `reauthor_of == its id` exists in any status and no rejection marker names it (`fleet:rejected:reauthor_stale_skill:<id>`, set when a human deletes the fleet's draft, 90-day TTL); a contested pair only if neither side carries `proposed_verdict`. A live marker (`fleet:enqueued:<job>:<subject>`) stops double-posting while a task is in flight and expires with the task — `SET NX EX 7d` for `propose_contested_verdict` (a proposal IS a state the store remembers, so re-enqueueing at 7 days just re-proposes onto an already-proposed pair, which the pending-check above blocks anyway), but `SET NX EX 30d` for `reauthor_stale_skill`: a `still_valid`/`retire` verdict writes nothing to the store and `stale` clears only on recall, so at the same 7-day TTL the identical skill was re-posted every week forever, burning a cap slot and a local-model call on a rewrite nobody acted on. Drained work never re-enqueues; expired work does; the whole night is capped at `FLEET_ENQUEUE_MAX_PER_RUN` (default 20, remainder reported as `capped`).

**Member-private never leaves.** Relay tasks are Keep-global (readable by every registered key, no workspace scoping) and the worker needs the text in `context`, so points with `visibility == "member"` are excluded at the query and a pair whose partner is private is skipped (`skipped_unpaired`). Cross-workspace writes fail server-side: `reauthor_of` must resolve inside the caller's workspace (404) and `propose` validates the pair like `resolve`. Excluding member-private points is not the same as excluding *other workspaces'* points, though — a workspace-visible skill or memory still goes into a Keep-global task. So the pass gathers every candidate first and, if candidates from more than one distinct workspace show up in the same run, it posts *nothing* and reports `status="skipped_multi_workspace"` (with the count) rather than pick one workspace to favor — meaning this pass, as shipped, is a single-workspace-Keep feature. Workspace-scoped relay tasks (tagging tasks with `workspace_id` and filtering `relay_task_list`/drain by the caller's own workspace) are the tracked follow-up that lifts this.

**The ledger (`app/fleet/ledger.py`).** Rejection of a draft is *deletion* and no approval timestamp existed, so a rate read from Qdrant would forget every rejected draft. Monotonic Redis counters are written at the moments the store forgets: `produced` (a draft created with `origin_job`), `approved` (its draft→active PATCH — which now also stamps a real `approved_at` on every skill), `rejected` (DELETE of a fleet draft), `proposed` (first proposal on a pair), `resolved` / `matched` (a human verdict on a pair that carried one). `fleet:ledger:<job>` all-time plus `fleet:ledger:<job>:<YYYY-MM-DD>` per UTC day (400-day TTL) feed the digest's `fleet` block (§4) and the dashboard's Fleet table. **This is the kill metric**: a job type whose approval rate stays low after enough verdicts is a job type to switch off, on evidence.

Out of the MVP, named so nobody infers them: the Dreaming port onto the queue, capability tags, per-job token budgets in trace, the other catalog jobs (handoff brief, doc drift, evidence pack, calibration review, merge near-duplicates), a headless-agent tier, an OS scheduled task, and writing a `still_valid`/`retire` verdict onto the skill's inbox row.

## 9. The skill ladder (round 2, rung one — shadow)

Measured on the production Keep just before this shipped (2026-09-02): 93 active
skills, zero with any efficacy evidence (`skill_efficacy_n > 0`), and a 32-skill
draft backlog with a median age of 14 days. The entry gate — every
machine-authored skill starts as an invisible `draft` — was doing its job; nothing
after it was. The one signal that could tell a good draft from a bad one —
outcome-weighted skill efficacy — was unfed, because agents reach skills through
the briefing (which emitted no receipt before this) or `skill_recall` (which never
distinguished a session that succeeded from one that didn't).

**A `trial` status is the fourth rung.** `skill_status` is now `draft` → `trial` →
`active` → `deprecated`. A draft can never be "used" — nothing about it can accrue
evidence — so `trial` is the only place that can happen: it is recallable
(`GET /skills?status=recallable` = `active` ∪ `trial`, actives first), shown in the
briefing at most once per session after the actives, and labeled `[TRIAL]`
everywhere an agent sees it. See [`knowledge-and-skills.md`](knowledge-and-skills.md)
for the status/receipt reference; this section is the evidence and the pass.

**Three signals, three different weights.** *Shown* is a `memory_read` receipt —
the briefing's own (`trigger="briefing"`, new) or `skill_recall`'s (`trigger=
"skill_recall"`, existing). *Reached* is specifically the `skill_recall` receipt —
an agent asked for the skill, rather than merely having it listed. *Applied* is a
`memory_feedback` naming the skill id. A **success observation** is the session's
verified grade reading `True` **and** either `useful=true` or — when no feedback
was given for that skill in that session — a reached receipt; a **failure
observation** is `useful=false` **paired with** a failed or abandoned grade.
Passive exposure never counts either way — a skill sitting in a briefing while the
session succeeded for other reasons does not promote it, and one shown in three
failed sessions does not demote it — and `useful=false` alone, with no failed
grade, is not a failure: a broad-triggering good skill recalled into the wrong
situation would otherwise be punished for being findable. All of this lives in one
module, `cortex/app/skills/ladder_evidence.py`, which reuses OWM's own
session-grading and Bridge-status helpers rather than re-deriving them.

**Independence and the thresholds.** Promotion (`trial` → `active`) needs
`SKILL_LADDER_PROMOTE_MIN_SUCCESSES` (default `3`) success observations from at
least `SKILL_LADDER_PROMOTE_MIN_AGENTS` (default `2`) distinct identities
(`member_id` when the event carries one, else `agent_id`; at most 2 successes
counted per identity, so one agent's streak cannot promote alone), zero failures
in the window, and a Beta-shrunk efficacy `(successes + P/2)/(n + P) ≥ 0.6` with
`P = OWM_PRIOR_N`. Demotion (`trial` → `draft` only) fires at ≥3 failures,
efficacy < 0.4, at n ≥ 5 — the same condition on an **active** skill never demotes
it (a human activated it; a run of thumbs-down may mean "not what I needed" as
easily as "wrong"); it instead sets `ladder_rewrite_requested_at` and leaves the
skill active and recallable. Expiry (`trial` → `draft`) fires when a trial's last
activity — the later of `ladder_since` and its last-shown date — is older than
`SKILL_LADDER_TRIAL_TTL_DAYS` (default `60`). Evidence is counted only since
`ladder_since`, which every real status change re-stamps, so a human's earlier
demotion is never undone by evidence from before it; a pre-existing skill's first
window defaults to `approved_at` → `stale_reviewed_at` → `timestamp`, stamped once
by the pass's first run.

**Admission (`draft` → `trial`) is deterministic, not a model call.** A draft is
blocked, checked in order: an empty trigger/symptoms/steps; `needs_rereview`; any
parked field (`demoted_at`, `ladder_rewrite_requested_at`, `trial_expired_at`,
`superseded_by`, `duplicate_of` — each means the draft belongs to a human or the
rewrite loop, never to a fresh trial); the per-run cap (module constant
`ADMIT_PER_RUN`, `20`, checked *before* the embed so a draft past the cap never
pays for one); a semantic near-duplicate against every active+trial skill (cosine
≥ `DUP_THRESHOLD`, `0.92` — an embed failure blocks the whole night's admissions
rather than being read as "no duplicate", so a broken embedding backend can never
wave a duplicate through); and the per-domain trial cap (`TRIAL_CAP_PER_DOMAIN`,
`10`). Drafts are processed oldest-first (a missing timestamp sorts last, never
first); a blocked duplicate is stamped `duplicate_of=<id>` so the inbox can list
it. No LLM sits anywhere in this pass — every rule is a deterministic read over
receipts and grades.

**Shadow, this PR.** `SKILL_LADDER_MODE` defaults to `shadow`. The nightly pass
(`cortex/app/skills/ladder.py::run_skill_ladder`, Celery beat every
`SKILL_LADDER_SCHEDULE_HOURS` hours, SETNX-locked, per-skill fault-isolated, never
raising) walks trial skills for expiry, then demotion, then promotion — first
match wins, so one skill gets at most one decision a night — then active skills
for the flag condition, then drafts for admission, computing every decision
exactly as it would apply it. It writes only three things: a decision-log entry
(`skills:ladder:decisions`, capped at 500), a `ladder_shadow` stamp on the
affected skill, and `duplicate_of` on a blocked draft. **No `skill_status`
changes, no fleet task is enqueued, and the fleet ledger's `fleet:ledger:ladder`
key (counters `admitted`/`promoted`/`demoted`/`expired`/`rewrite_requested`) stays
at zero.** Setting `SKILL_LADDER_MODE=enforce` still runs shadow this PR — it logs
"enforce mode ships in PR2 — ran shadow" and changes nothing, so a wrong setting
cannot flip live behavior early.

**Where the decisions surface.** The inbox's `ladder_proposals` section (§4) shows
only the latest run's decisions plus every currently-parked duplicate. The
digest's `ladder` block (§4) reports `mode` from what the last run actually did —
never from the live setting, which can change before the next run fires — and
`rate: null`, not `0.0`, when nothing was shown, since a zero-denominator fraction
is not a measurement of zero. The dashboard's Skills tab gains a Trial filter, a
`TRIAL` badge, `Activate`/`Back to draft` buttons on a trial skill, and shows
`approved_by` plus "ladder would: `<action>`" from `ladder_shadow`; the Autopilot
tab renders a "Skill ladder — proposed transitions" section and the digest block,
through the same two fetches round 1 already makes — no new fetch, no write verb,
the read-only pin holds.

**What a healthy first fortnight looks like.** `useful=true`/`false` events on
skill ids are rare today — the stop hook's new reminder and the
reached-receipt-with-no-feedback fallback exist because of this — so the two
numbers worth watching are *feedback events on skills > 0* and *reach rate > 0*
(shown-without-reach is itself a finding about the trigger text, not the steps).
Zero promotions on day one is the expected, honest baseline given a
≥3-successes-from-≥2-identities bar, not evidence the rules are wrong. Expect the
admit stream to work through the draft backlog minus whatever it correctly
recognizes as duplicates.

**The solo-Keep caveat.** On a Keep with one human and several agent identities
(this machine, a laptop, night-shift), "two distinct identities" is satisfied
trivially, since each reports its own `agent_id`. The independence rule is real
and does exactly what it says — it protects a *team* Keep, where two agreeing
identities means two people actually agreed — but on a solo Keep it is satisfied
by default rather than earned, which is worth knowing before reading a
zero-identity promotion as a bug.

**Excluded from OWM, by name.** The briefing's new `memory_read` receipt
(`trigger="briefing"`) is excluded from Outcome-Weighted Memory's `skill_efficacy`
tally, for both the memory and the skill branch (see the OWM bullet in
[`memory-and-recall.md`](memory-and-recall.md)) — OWM joins every `memory_read`
receipt to its session's grade, so counting a briefing impression there would pull
every skill's efficacy toward the session base rate. The ladder's own evidence
reader is the only consumer of that receipt.

**PR2 (not built).** `SKILL_LADDER_MODE=enforce` applies the same decisions
instead of only logging them, stamps a `ladder_history` entry on every transition,
and records `approved_by="ladder"` on an automatic promotion. A demoted trial or a
flagged active is handed to the fleet as a `reauthor_failed_skill` task (failure
evidence first — the failing sessions, the last feedback comment, the efficacy
numbers); Night Shift rewrites it into a new draft (`reauthor_of=<failed id>`)
that re-enters the ladder like any other; when that rewrite is promoted, the skill
it rewrote is deprecated with `superseded_by`. A human can request the same
rewrite by hand from a Skills-tab **Rewrite** button. Flipping to `enforce` on a
production Keep is a decision taken only after the shadow ledger has shown a
fortnight of what the rules would have done.

## What unlocks round 2

Auto-promotion becomes defensible when: (a) the reaper + feedback + gateway
reconciles have produced outcome distributions where both classes actually
occur (check `outcome_event_count` in evals and the OWM section's health
notes); (b) promotion criteria require independence across *principals*
(members/credentials), not sessions — one agent looping is not evidence; and
(c) the pattern engine's frozen candidate→trial→validated ladder
(`patterns/lifecycle.py`, `PATTERN_VALIDATION_ENABLED`) is generalized rather
than a second ladder invented. Source-backed doc skills may promote on
provenance before statistics exist. Run any autopilot action in shadow
(ledger-only) before letting it touch state.

§9 is this round's first rung, and it honours exactly this gate: every ladder
decision above is ledger-only, no `skill_status` ever changes in PR1, and OWM was
taught to exclude the new briefing receipt from its own `skill_efficacy` tally
before that receipt could distort it — the independence-across-principals
requirement (b) is enforced today (`SKILL_LADDER_PROMOTE_MIN_AGENTS`), not
deferred to PR2. Flipping `SKILL_LADDER_MODE` to `enforce` is the second rung,
taken once the shadow ledger has shown enough decisions to trust.
