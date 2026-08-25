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
