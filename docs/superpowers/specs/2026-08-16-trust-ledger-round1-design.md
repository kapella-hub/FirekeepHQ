# Trust Ledger — round 1 (design, pre-registered 2026-08-16)

**Status: approved design (four decisions taken on the decision board, one
amendment pass), not yet built.** The Institution Thesis's first domino
([`2026-08-14-institution-thesis.md`](2026-08-14-institution-thesis.md),
pillar 2, ROADMAP §4). Visibility only — the house pattern of Autopilot
round 1 and Living Instructions round 1: **it reports, it never gates.** No
autonomy enforcement until the measurement has lived in production; the
enforcement half is round 2 (the capability broker), and earned autonomy is
the two together and exists only where both do.

The formulas below are **frozen at birth** — pre-registered here before the
first published number so later rounds compare cleanly. That is the Living
Instructions lesson (its predicates were frozen to a founding measurement),
applied from day one.

## 1. What it measures

A per-agent employment record built from the declarations agents already
make: the gateway takes a declared action with optional stated prediction
(`action_before`), reconciles the outcome (`action_after`), and those become
`agent.action.predict` / `agent.action.reconcile` events in replay. The
ledger aggregates them per `agent_id` into a track record — "214 declared
actions, reconciled 96%, prediction-match calibration 0.11 over 180 scored
predictions and improving, 3 reversals, 28 sessions."

**No single trust score (decision 1).** The card shows the honest
components, never a headline number. A composite gets treated as a gate, and
round 1 is visibility-only — publishing the one number people would gate on,
before the broker can enforce anything, is exactly the contradiction the
2026-08-14 review caught. Components only.

## 2. Data source — the event stream, and its honest coverage

The aggregation keys on `agent_id`, and the load-bearing fact is where that
key lives: **every replay event carries `agent_id` as a required field**
(`replay/models.py` — `agent_id: str = Field(..., min_length=1)`; the emit
path stamps it), whereas **`EvalResult` carries only a session-level
`agents: list[str]`** (`evals/models.py`) — it has no *event-level*
attribution, so a session's metrics cannot be partitioned among the agents
that produced them. That, precisely, is why the per-agent ledger cannot come
from `rp:eval:*` the way the compliance table does; it comes from the gateway
events in the global `rp:events` stream, where each event names its agent.

**What it therefore measures: replay-captured gateway actions, not every
gateway action.** Emission is best-effort — the gateway wraps `replay_emitter`
in try/except and logs a warning on failure (`agent_gateway/service.py`), and
the stream is trimmed with an approximate `maxlen` (`emitter.py` `xadd
approximate=True`). Either half of a predict/reconcile pair can be lost or
trimmed. The ledger reports what replay captured, and the card says so — it
is a floor on *declared* activity, itself already a floor on *actual*
activity (§4).

**One bounded scan with honest truncation.** Read the **latest
`TRUST_SCAN_CAP + 1`** stream entries within the window (`TRUST_WINDOW_DAYS`,
default 30, matching the eval TTL and compliance window), newest first,
filtered to `agent.action.predict` / `agent.action.reconcile`. If `cap + 1`
entries come back, the window holds more than the cap — **`truncated: true`**,
and the read is only the freshest slice. Truncation's effect is asymmetric
and stated exactly (correcting an earlier "everything is a floor" claim,
which was wrong):

- **Lower bounds under truncation** (still meaningful): `declared`,
  `reconciled`, `reversals`, `scored_predictions`, `sessions` — we saw at
  least this many.
- **Biased under truncation → returned `null`**: `reconciliation_rate`,
  `calibration`, `calibration_trend`, `first_seen_in_window` — each depends
  on having the *whole* window (an unread older slice moves the rate, the
  Brier, the trend split, and the earliest timestamp). `last_seen_in_window`
  survives (we read the newest). A per-type or per-agent index that would let
  the full window be read cheaply is a round-2 change if volume demands it —
  not a claim made now.

## 3. Frozen aggregation (per `agent_id`, deployment-global)

**The declaration cohort is the unit.** A `declared` action is an
`agent.action.predict` whose **predict timestamp falls inside the window**.
Every derived metric — reconciliations, reversals, calibration, trend — is
computed only over reconciles **paired by `action_id` to a declaration in
that cohort**, attributed to the *declaring* agent. A reconcile whose
`action_id` has no in-window declared predict is outside the cohort and
counts toward no agent. This is what keeps `reconciliation_rate` ≤ 100%: a
reconcile inside the window whose declaration fell just outside it is not
counted. `action_id` is globally unique, so pairing is unambiguous; a
reconcile is attributed to the agent that *declared*, never to its own
`agent_id` — **which is therefore IRRELEVANT to a reconcile**, and often
empty in practice: the gateway resolves a reconcile's agent from the
short-lived `ag:predict:{action_id}` record (~300s TTL), and once that has
expired it emits the reconcile with `agent_id=""` and `session_id=""` while
the predict *event* still lives in the 30-day stream. Round 1's first live
read proved this concrete: **99% of blank-agent reconciles (3941/3942)
paired to an in-window attributed predict**, so rejecting them at the scan —
which the first build did — discarded almost every real reconciliation and
undercounted every agent's rate to near-zero. The scan therefore keeps a
reconcile regardless of its own `agent_id`/`session_id`; only a **predict**
requires a non-empty `agent_id`, because the predict IS the declaration
being attributed (§ "Invalid input" below).

For each `agent_id` with a declaration in the cohort:

| Field | Frozen definition |
|---|---|
| `declared` | count of in-window `predict` events (INCLUDING declarations whose `prediction` is null — `action_before` emits a predict for every declaration; `agent_gateway/service.py` writes `prediction: null` when none was stated) |
| `reconciled` | count of cohort reconciles (paired by `action_id` to this agent's in-window declarations) |
| `reconciliation_rate` | `reconciled / declared` — null when `declared == 0`, and null when `truncated` (§2) |
| `scored_predictions` | count of cohort reconciles whose declaration carried a NON-null `prediction` and that produced a `prediction_match_score` — the Brier's actual sample size (the `calibration_n` the card shows) |
| `calibration` | **prediction-match** Brier over `scored_predictions`, the exact `evals.scorers.brier_score` input. **It scores stated `success_criteria` / `expected_changes` matches (`reconciler.compute_prediction_match_score`), NOT `outcome.success`** — and an action with empty criteria AND empty expected-changes scores 1.0 even if it failed. So calibration and `reversals` are DIFFERENT dimensions on purpose. Null below `TRUST_MIN_CALIBRATION_POINTS` (default 5) scored predictions, and null when `truncated`. |
| `calibration_trend` | Brier(newer half) minus Brier(older half), split at the MEDIAN timestamp of the scored points (balanced halves, not a wall-clock cut that bursty activity could dump into one side) — the compliance table's older-half→newer-half by-count precedent. Null when either half is below the min, or when `truncated`. **Lower Brier is better**, so a NEGATIVE trend is improvement; the card renders the direction, never a bare signed number. |
| `reversals` | count of cohort reconciles with `outcome.success == false` (decision 2) — the agent declared it, reconciled it, it failed. Direct from the event, no threshold. Distinct from calibration (above). |
| `sessions` | distinct `session_id` across the agent's cohort events (lower bound when `truncated`) |
| `first_seen_in_window` / `last_seen_in_window` | min / max event timestamp WITHIN the window (explicitly window-relative, not lifetime). `first_seen_in_window` is null when `truncated`; `last_seen_in_window` survives. |

**Unknowns stay unknown.** An `agent_id` with no in-window declaration has
**no record**, not a bad one — it is simply absent. An agent with fewer than
`TRUST_MIN_CALIBRATION_POINTS` scored predictions shows counts and reversals
but a null `calibration`, labelled "not enough signal", never a default-bad
number.

**Invalid input is counted, not silently dropped.** An event the ledger
genuinely cannot use increments a visible top-level `invalid` breakdown
(`{unattributed_predict, missing_action_id, malformed, bad_timestamp}`)
rather than joining any agent's row. The four causes: malformed event JSON,
a payload that is valid JSON but not an object, a missing `action_id` (a
predict cannot be keyed, a reconcile cannot pair), an unparseable timestamp,
and — **predict only** — a blank `agent_id` (`unattributed_predict`: a
declaration with no agent to attribute it to). A blank `session_id` is NOT
invalid: a predict with one is a real declaration (kept; it just does not
contribute to the session count), and a reconcile with one is the common
expired-record case above (kept; paired by `action_id`). A number the reader
cannot see is the silent-cap failure this repo bans.

## 4. The honest limits (frozen notes, rendered on the card)

Each is a first-class note, the Living Instructions "adapter is a transport
class" discipline — a reader who misses these will misread the table:

- **Behavior, not competence.** Calibration measures whether *stated
  prediction criteria* matched observed criteria/changes, not whether the
  decisions were good — and empty criteria score a perfect 1.0. A
  well-formed, confidently mediocre agent scores well here.
- **Declared actions only.** The ledger sees what an agent *declared* via the
  gateway, and only what replay *captured* of that (§2). Undeclared or
  unemitted work is invisible — the record is a floor on a floor, and an
  agent that declares nothing is unmeasured, not trusted.
- **`agent_id` is self-reported (decision 3).** It is an observability label,
  not the tenancy boundary (`workspace_id` is that, verified and unforgeable
  — root `CLAUDE.md`). A single actor can split its work across identities or
  merge two under one, and the ledger cannot tell. The record is *per
  declared identity* — which is precisely why round 1 is visibility-only: you
  do not gate autonomy on a spoofable key.
- **Deployment-global, not workspace-scoped (round 1).** The rows are every
  agent in the deployment, not the caller's workspace only — see §5 and the
  tenancy invariant.
- **Reversal ≠ miscalibration ≠ wrong decision.** `reversals` counts
  `outcome.success == false`; `calibration` scores stated-criteria match. A
  correctly-abandoned plan and a crash both reconcile as `success=false`; a
  failed action with empty criteria still scores 1.0 on calibration. Two
  honest dimensions, neither of them "was it a good call."
- **Windowed, capped, truncation-aware.** Only the last `TRUST_WINDOW_DAYS`;
  under truncation the biased metrics are `null`, not a guessed floor (§2).

## 5. Tenancy, the invariant, and the surface

- **Deployment-global in round 1, exactly like the compliance table.**
  `build_compliance(replay_redis)` takes no principal and scans all
  `rp:eval:*` deployment-wide; the trust ledger matches it. The honest reason
  it is NOT workspace-scoped: **replay events carry no `workspace_id`** — the
  emit path stamps `agent_id` and `session_id`, not the tenancy key — so
  per-workspace scoping would require threading `workspace_id` onto every
  replay event, a write-path change out of scope for a read-only round 1. On
  the single-workspace deployment — which is every deployment today
  (`FIREKEEP_WORKSPACE_ID` is one env var; the Living Procedures H6 WARN-path
  gap is the same shape) — deployment-global IS the one workspace.
- **Tenancy invariant (must hold before multi-workspace ships):** the day a
  single deployment serves more than one workspace, EITHER replay events must
  gain a `workspace_id` and this endpoint must filter on the caller's, OR the
  endpoint must be restricted to a deployment-level superadmin and otherwise
  fail closed. Shipping multi-workspace with this endpoint unchanged would
  leak one workspace's agent activity to another workspace's admin. Safe today
  (single-workspace); recorded here so it cannot be forgotten when that
  changes.
- **`GET /autopilot/trust`** (admin, additive) — `{agents: [<row>...],
  window_days, scanned, truncated, invalid, generated_at}`, rows sorted by
  `(-declared, agent_id)`. Admin-scoped like the rest of the Autopilot
  operator surface (`/autopilot/inbox`, `/autopilot/compliance`); no workspace
  parameter, matching `build_compliance`.
- **Dashboard**: a Trust Ledger card on the Autopilot tab beside the
  compliance table, same honesty-note treatment. Pure render function behind
  the extraction sentinels (the `proceduresPanel` / compliance precedent) so
  it is testable under node without a browser. Truncation shows a banner; a
  null biased-metric renders as "—" with the reason, never 0. Absent/empty →
  "no agent has declared an action yet", never an invented row.

## 6. Config (frozen constants)

| Var | Default | Purpose |
|---|---|---|
| `TRUST_WINDOW_DAYS` | 30 | Aggregation window; matches the eval TTL and compliance window |
| `TRUST_SCAN_CAP` | 50000 | Read the latest cap+1 stream entries; `cap+1` returned ⇒ `truncated: true` and the biased metrics null |
| `TRUST_MIN_CALIBRATION_POINTS` | 5 | Scored-prediction count below which `calibration`/`calibration_trend` report null ("not enough signal") — applies to PAIRED SCORED predictions, not raw declarations |

No feature flag: the endpoint is admin-gated and read-only-additive, exactly
like `/autopilot/compliance`, which ships always-on. A deployment with no
declarations renders an empty table, not an error.

## 7. Module and testing

- **`cortex/app/autopilot/trust.py`** — the bounded scan + aggregation, the
  `compliance.py` mold: `scan_gateway_events(replay_redis, window_days, cap)`
  returning `(events, scanned, truncated, invalid)`; pure `build_rows(events)`
  over fetched events; `build_trust(replay_redis)` (no workspace param — §5).
  No writes, no new event types, no LLM.
- **Route** on the existing autopilot router (`cortex/app/autopilot/api.py`),
  admin dep, no workspace parameter.
- **Tests — the frozen formulas and every amendment boundary pinned by
  literal fixtures:**
  - cohort cutoff: a declaration just OUTSIDE the window with a reconcile
    inside → not counted; `reconciliation_rate` never exceeds 100%.
  - cap vs cap+1: exactly `cap` entries → not truncated; `cap+1` → truncated
    and the four biased metrics null while counts/sessions stay lower bounds.
  - asymmetric loss: predict-without-reconcile and reconcile-without-predict
    each handled (the orphan reconcile is outside the cohort; the unreconciled
    declaration lowers the rate, does not error).
  - `calibration` equals `scorers.brier_score` on the same paired input;
    `scored_predictions` counts only non-null-prediction pairs; empty-criteria
    action scores 1.0 and is visible as such.
  - trend sign (improvement = negative), midpoint split, null below the min.
  - `reversals` counts `success==false` only, independent of calibration.
  - distinct-session, window-relative first/last-seen, first null under
    truncation and last surviving.
  - invalid inputs (unattributed predict, missing action_id, malformed/
    non-dict JSON, bad timestamp) increment the visible `invalid` breakdown,
    join no row; a blank-session PREDICT and a blank-agent/session RECONCILE
    are KEPT (the latter recovered by action_id pairing).
  - cross-agent `action_id` pairing attributes to the declaring agent,
    including a reconcile whose own `agent_id`/`session_id` is empty.
  - unknown-stays-unknown (no record for zero declarations).
  - Dashboard card render tests under node behind the sentinels.
  - Docs-guard: the config table in the guide matches the code defaults (the
    `test_procedure_docs.py` pattern).

## 8. Out of scope for round 1 (stated)

No composite score; no gating, promotion, or autonomy tiers (round 3, and
only at the broker); **one deployment-wide operator view — no per-workspace
slice and no cross-deployment rollup** (the tenancy invariant, §5, is the
prerequisite for the workspace slice); no per-type or per-agent replay index
(round 2 if volume demands); no LLM judgement of decision quality; no write
path or stored snapshot (compute on demand — decision 4). The capability
broker (pillar 2b) that turns this from advisory reputation into earned
autonomy is round 2, and its effort is weeks, not days — the honest number
the thesis's first draft hid.

## 9. Decision trail

Four decisions, decision board 2026-08-16, all as recommended: (1)
components only, no composite score; (2) reversal = `outcome.success ==
false` on a reconciled declared action; (3) key on `agent_id` (deployment-
global — see §5; the board option's "workspace-scoped" phrasing was corrected
to match the compliance precedent and the replay events' actual fields);
(4) compute on demand, read-only.

**Amendment pass (same day, user review — six contract fixes, all folded
in):** (1) the declaration cohort makes `reconciliation_rate` ≤ 100% by
pairing on in-window predicts; (2) "replay-captured actions, not all actions"
stated, and truncation nulls the biased metrics (rate/calibration/trend/
first_seen) instead of calling them floors; (3) `scored_predictions`/
`calibration_n` exposed, `TRUST_MIN_DECLARATIONS` → `TRUST_MIN_CALIBRATION_
POINTS`, and calibration relabeled "prediction-match" with the empty-criteria-
scores-1.0 caveat stated; (4) three textual contradictions fixed —
`EvalResult` has `agents` but no event-level attribution, decision 3 no longer
says "workspace-scoped", §8 says "one deployment-wide view" not "no global
view"; (5) window-relative `first_seen`/`last_seen`, visible `invalid` counts,
declared-vs-scored distinction; (6) the tenancy invariant recorded for the
multi-workspace future.

**Second amendment (same day, from the FIRST live production read — the
frozen formula corrected before any number is published).** The first deploy
read `/autopilot/trust` against real replay data and exposed a correctness
bug: the scan rejected any gateway event with a blank `agent_id`, counting
3193 as invalid — but those were all `agent.action.reconcile` events whose
agent the gateway could not resolve (expired predict record), and a
reconcile's own `agent_id` is irrelevant because it is attributed to the
declaring agent by `action_id`. 99% (3941/3942) paired to a real in-window
predict, so the ledger had been discarding almost every reconciliation and
reporting near-zero rates (agents that "never reconciled" actually reconciled
fine). Fix: the scan keeps a reconcile regardless of its `agent_id`/
`session_id`; only a predict requires a non-empty `agent_id`; blank sessions
no longer count toward the session set. The `invalid` breakdown became
`{unattributed_predict, missing_action_id, malformed, bad_timestamp}` (blank
`agent_id`/`session_id` dropped as rejection reasons). Also fixed the same
pass: a production 500 when a real reconcile's payload `outcome` was a string
not a dict — guarded by `isinstance`, plus a non-dict-payload guard in the
scan. The lesson (second time this class bit, after the corpus store): a scan
over HISTORICAL production data must carry the legacy and malformed shapes in
its fixtures, not only the current schema.
