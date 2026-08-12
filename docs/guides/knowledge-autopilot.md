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

So round 1 ships **attribution and visibility, no autonomous mutation**:

| Shipped | Deliberately NOT shipped (yet) |
|---|---|
| Feedback-weighted recall + `memory_feedback` MCP tool | Auto-promotion of skills/memories |
| Bridge session reaper (the missing failure signal) | Auto-retirement (beyond existing archive-first GC) |
| Contested-not-superseded for unconfirmed conflicts | Trial stages, lifecycle ladders for memories/skills |
| `/autopilot/inbox` + `/autopilot/digest` + dashboard tab | Autopilot "modes" — round 1 *is* Recommend mode |
| `/memory/{id}/evidence` ledger read | LLM-judged "did the agent follow this advice" |
| `/autopilot/compliance` — Living Instructions round 1 | Instruction rewriting/AB — Living Instructions rounds 2–3 |

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
skills, Living Procedures proposals, contested pairs, the eval DLQ — into one
surface with per-section fault isolation (one broken store never blanks the
inbox). `GET /autopilot/digest?days=7` answers "what changed this week"
(learned/archived/superseded/dreamed/drafted/feedback/GC actions) with capped
scans marked `approximate` when capped. The dashboard's **Autopilot** tab
renders both, read-only — round 1 proposes and reports, it never mutates, and
the dashboard guard test pins that absence.

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
withheld entirely below 10 sessions rather than shown small. Rewriting
instructions and A/B validation are rounds 2–3 and deliberately not built.

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
