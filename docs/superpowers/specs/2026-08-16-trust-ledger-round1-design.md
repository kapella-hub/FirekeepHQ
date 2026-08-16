# Trust Ledger — round 1 (design, pre-registered 2026-08-16)

**Status: approved design (four decisions taken on the decision board), not
yet built.** The Institution Thesis's first domino
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
make: the gateway takes a declared prediction with stated confidence
(`action_before`), reconciles the outcome (`action_after`), and those become
`agent.action.predict` / `agent.action.reconcile` events in replay. The
ledger aggregates them per `agent_id` into a track record — "214 declared
actions, reconciled 96%, calibration 0.11 and improving, 3 reversals, 28
sessions."

**No single trust score (decision 1).** The card shows the honest
components, never a headline number. A composite gets treated as a gate, and
round 1 is visibility-only — publishing the one number people would gate on,
before the broker can enforce anything, is exactly the contradiction the
2026-08-14 review caught. Components only.

## 2. Data source — the event stream, not the evals

The aggregation keys on `agent_id`, and the load-bearing fact is where that
key lives: **every replay event carries `agent_id` as a required field**
(`replay/models.py` — `agent_id: str = Field(..., min_length=1)`; the emit
path stamps it), whereas **`EvalResult` does NOT carry `agent_id`** (it has
`session_id`, `runtime`, `outcome`, `has_failures`, `brier_score` — but no
agent). So a per-agent ledger cannot come from `rp:eval:*` the way the
compliance table does; it comes from the gateway events in the global
`rp:events` stream.

**One bounded windowed scan**, the compliance.py discipline moved one layer
down: `XRANGE rp:events` over the last `TRUST_WINDOW_DAYS` (default 30, to
match the eval TTL and the compliance window), filtered to
`agent.action.predict` and `agent.action.reconcile`, grouped by the
`agent_id` field. The two gateway event types are a small fraction of the
stream (most events are memory reads/writes), so the scan reads far more than
it keeps — acceptable for round 1 at current volume, and the scan is **capped
and the cap disclosed** (`TRUST_SCAN_CAP`, the `scan_evals` `truncated` bool
precedent): a truncated scan reports a floor, never a census, and says so.
A per-type or per-agent index is a round-2 change if volume demands it — not
a claim made now.

## 3. Frozen aggregation (per `agent_id`, deployment-global)

For each `agent_id` seen in the window (deployment-wide — see §5 on why
round 1 is not workspace-scoped):

| Field | Frozen definition |
|---|---|
| `declared` | count of `agent.action.predict` events |
| `reconciled` | count of `agent.action.reconcile` events |
| `reconciliation_rate` | `reconciled / declared` (null when `declared == 0`) |
| `calibration` | Brier over reconcile events PAIRED to their predict by `action_id` — the exact `evals.scorers.brier_score` computation (`compute.py` does it per session; the ledger does it per agent across the window). Null below `TRUST_MIN_DECLARATIONS` (default 5) paired points — a Brier over one or two actions is noise, not calibration. |
| `calibration_trend` | Brier(newer half of the window) minus Brier(older half), split at the window midpoint — mirrors the compliance table's older-half→newer-half trend. Null when either half is below the min. **Lower Brier is better**, so a NEGATIVE trend is improvement; the card renders the direction, never a bare signed number. |
| `reversals` | count of reconcile events with `outcome.success == false` (decision 2) — the agent declared an action, reconciled it, and it failed. Direct from the event, no threshold to tune. |
| `sessions` | distinct `session_id` across the agent's events |
| `first_seen` / `last_seen` | min / max event timestamp |

**Unknowns stay unknown.** An `agent_id` with no `predict` events has **no
record**, not a bad one — it is simply absent from the table. An agent with
declarations but fewer than `TRUST_MIN_DECLARATIONS` paired points shows
counts and reversals but a null calibration, labelled "not enough signal",
never a default-bad number.

## 4. The honest limits (frozen notes, rendered on the card)

Each is a first-class note, the Living Instructions "adapter is a transport
class" discipline — a reader who misses these will misread the table:

- **Behavior, not competence.** Calibration measures whether stated
  confidence matched outcomes, not whether the decisions were good. A
  perfectly calibrated agent can be confidently, consistently mediocre.
- **Declared actions only.** The ledger sees what an agent *declared* via the
  gateway. Undeclared work is invisible — so the record is a floor on
  activity, and an agent that declares nothing is unmeasured, not trusted.
- **`agent_id` is self-reported (decision 3).** It is an observability label,
  not the tenancy boundary (`workspace_id` is that, verified and unforgeable
  — root `CLAUDE.md`). A single actor can split its work across identities or
  merge two under one, and the ledger cannot tell. The record is *per
  declared identity*, and the note says so — which is precisely why round 1
  is visibility-only: you do not gate autonomy on a spoofable key.
- **Deployment-global, not workspace-scoped (round 1).** The rows are every
  agent in the deployment, not the caller's workspace only — see §5 for why.
- **Reversal = a failed declared action**, not "a wrong decision." A
  correctly-abandoned plan and a crash both reconcile as `success=false`.
- **Windowed and capped.** Only the last `TRUST_WINDOW_DAYS`, and a truncated
  scan is a floor — stated on the response, never hidden.

## 5. Tenancy and surface

- **Deployment-global in round 1, exactly like the compliance table.**
  `build_compliance(replay_redis)` takes no principal and scans all
  `rp:eval:*` deployment-wide; the trust ledger matches it. The honest reason
  it is NOT workspace-scoped: **replay events carry no `workspace_id`** — the
  emit path stamps `agent_id` and `session_id`, not the tenancy key — so
  scoping per workspace would require threading `workspace_id` onto every
  replay event, a write-path change out of scope for a read-only round 1. On
  the single-workspace deployment — which is every deployment today
  (`FIREKEEP_WORKSPACE_ID` is one env var; the Living Procedures H6 WARN-path
  gap is the same shape) — deployment-global IS the one workspace, so nothing
  leaks in practice. Multi-workspace scoping is a named later-round item, not
  a silent gap. The admin gate is the access boundary meanwhile: only an admin
  key reads the table.
- **`GET /autopilot/trust`** (admin, additive) — `{agents: [<row>...],
  window_days, scanned, truncated, generated_at}`, rows sorted by
  `(-declared, agent_id)`. Admin-scoped like the rest of the Autopilot
  operator surface (`/autopilot/inbox`, `/autopilot/compliance`), and it takes
  no workspace parameter — matching `build_compliance`.
- **Dashboard**: a Trust Ledger card on the Autopilot tab beside the
  compliance table, same honesty-note treatment. Pure render function behind
  the extraction sentinels (the `proceduresPanel` / compliance precedent) so
  it is testable under node without a browser. Absent/empty → "no agent has
  declared an action yet", never an invented row.

## 6. Config (frozen constants)

| Var | Default | Purpose |
|---|---|---|
| `TRUST_WINDOW_DAYS` | 30 | Aggregation window; matches the eval TTL and compliance window |
| `TRUST_SCAN_CAP` | 50000 | Max stream entries read per request; on hit, `truncated: true` and the rows are a floor |
| `TRUST_MIN_DECLARATIONS` | 5 | Paired points below which calibration/trend report null ("not enough signal"), mirroring `PROCEDURE_MIN_EXECUTIONS`/`OWM_PRIOR_N` |

No feature flag: the endpoint is admin-gated and read-only-additive, exactly
like `/autopilot/compliance`, which ships always-on. A deployment with no
declarations renders an empty table, not an error.

## 7. Module and testing

- **`cortex/app/autopilot/trust.py`** — the bounded scan + aggregation, the
  `compliance.py` mold: pure functions over fetched events (`build_rows`),
  one async `scan_gateway_events(replay_redis, window, cap)` returning
  `(events, scanned, truncated)`, one `build_trust(replay_redis)` (no
  workspace param — §5). No writes, no new event types, no LLM.
- **Route** on the existing autopilot router (`cortex/app/autopilot/api.py`),
  admin dep, no workspace parameter (§5 — deployment-global like compliance).
- **Tests**: the frozen formulas pinned by literal fixtures — declared/
  reconciled counts, reconciliation rate with `declared==0` → null, Brier
  paired-by-action_id matches `scorers.brier_score` on the same input, trend
  sign (improvement = negative), reversal counts `success==false` only,
  distinct-session and first/last-seen, min-declarations null-out, truncation
  flag on cap, and unknown-stays-unknown (no record for zero declarations).
   Dashboard card
  render tests under node behind the sentinels. Docs-guard: config table in
  the guide matches the code defaults (the `test_procedure_docs.py` pattern).

## 8. Out of scope for round 1 (stated)

No composite score; no gating, promotion, or autonomy tiers (round 3, and
only at the broker); no cross-workspace/global view; no per-type or per-agent
replay index (round 2 if volume demands); no LLM judgement of decision
quality; no write path or stored snapshot (compute on demand — decision 4).
The capability broker (pillar 2b) that turns this from advisory reputation
into earned autonomy is round 2, and its effort is weeks, not days — the
honest number the thesis's first draft hid.

## 9. Decision trail

Four decisions, decision board 2026-08-16, all as recommended: (1)
components only, no composite score; (2) reversal = `outcome.success ==
false` on a reconciled declared action; (3) key on `agent_id`, workspace-
scoped, spoofability a first-class honesty note; (4) compute on demand,
read-only. Data-source correction found during design and folded in: the
aggregation keys on the event stream's `agent_id`, not `rp:eval:*`, because
`EvalResult` carries no `agent_id`.
