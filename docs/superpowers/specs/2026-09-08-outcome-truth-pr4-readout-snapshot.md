# Outcome truth PR4 — readout snapshot (2026-09-08, pre-T0)

**Status:** dated readout snapshot of `GET /autopilot/compliance`, taken and
committed BEFORE the PR5 T0 flip, as PR5-D4 requires. Authority for the
hypotheses: `2026-08-25-outcome-truth-pr4-adoption-design.md` (H1, H2).
Authority for the reading rules applied here: PR5-D4 and PR5 "Interference
and co-intervention" in
`2026-08-26-outcome-truth-pr5-controlled-nudge-design.md`.

Fetched from the live Keep (cortex REST, `X-API-Key` auth) at
`generated_at = 2026-09-08T15:24:22.818652+00:00`; the arm_comparison block still reads
`not_started`, which proves the flip had not happened when this was taken.

## What this snapshot measures (registered reframing)

Per the PR5 "Interference and co-intervention" section, the in-repo
strong-nudge text (the PR5 spec and the two guides updated inside PR4's H1
window) is a uniform co-intervention on the whole measured fleet. This
snapshot therefore measures the effect of the **mild-tool-description-nudge
+ in-repo-strong-nudge-text bundle**, NOT the PR4-D4 tool description alone.
No claim about the description channel in isolation follows from it.

## Reading rule (PR5-D4)

PR5-D4 says the H1 read applies a `created_at >= PR4-deploy` (2026-08-25)
interpretation at readout, because the compliance table spans the whole
30-day eval store and nothing else serves a post-D4-only rate. The JSON
below carries NO per-session `created_at`, so that filter cannot be applied
to this payload exactly. Two views inside the payload approximate it, and
both are reported rather than a filtered rate this document cannot derive:

- `by_experiment_group` on the `grade_self_reported` row. `experiment_group`
  is stamped only on sessions started after PR4 deployed (2026-08-25), so the
  grouped subtotal is the closest thing to a post-D4 population the payload
  holds. Only arm B appears — the deployment owner hashes to B and no other
  member has produced attributed sessions.
- `recent_rate`, the later half of dated evals by eval time (a registered
  trend split, not a date filter).

## H1 (adoption) — ≥ 20% of completed sessions carry a recognized grade

| view | graded | total | rate |
|---|---|---|---|
| all evaluated sessions (unfiltered, whole 30-day store) | 217 | 472 | 45.97% |
| `by_experiment_group.B` (post-PR4-deploy proxy) | 211 | 235 | 89.79% |
| `recent_rate` (later half by eval time) | — | — | 89.41% |
| `earlier_rate` (earlier half by eval time) | — | — | 2.54% |

**H1 HELD** on every view, by the JSON's own numbers: even the unfiltered
whole-store rate clears 20%, and the post-deploy proxy is well above it. The
earlier/recent split (2.5% → 89.4%) is consistent with adoption
arriving with the bundle, not predating it. Reminder: the held hypothesis is
about the bundle (above), and it says nothing about grade quality.

## H2 (honesty) — optimism skew ≤ 15% at N ≥ 30 self-success sessions

| view | contradicted | self-success N | skew | insufficient_n |
|---|---|---|---|---|
| overall | 4 | 172 | 2.33% | False |
| arm B | 4 | 166 | 2.41% | False |

**H2 HELD**: N is far above the registered minimum of 30 and the skew is
well under the 15% bound, overall and in the only populated arm.

## PR4 decision rule, applied

H1 held and H2 held → proceed to PR5's controlled A/B (the T0 flip that
follows this commit). The `approximate: false` and `unparsed: 0` disclosures
mean no row here is degraded.

## Full JSON, verbatim

```json
{"generated_at":"2026-09-08T15:24:22.818652+00:00","sessions_evaluated":472,"dated_sessions":472,"unparsed":0,"approximate":false,"instructions":[{"key":"recall_before_work","instruction":"Recall before you answer","predicate":"memory_read_count > 0","hits":273,"total":472,"rate":0.5784,"earlier_rate":0.4703,"recent_rate":0.6864,"by_runtime":{"codex":{"hits":197,"total":284},"claude":{"hits":23,"total":65},"unattributed":{"hits":53,"total":123}},"exposure":{"exposed":349,"not_exposed":0,"unknown":123,"exposed_hits":220,"exposed_rate":0.6304}},{"key":"write_as_you_go","instruction":"Write as you go (memory_learn)","predicate":"memory_write_count > 0","hits":286,"total":472,"rate":0.6059,"earlier_rate":0.6314,"recent_rate":0.5805,"by_runtime":{"codex":{"hits":182,"total":284},"claude":{"hits":31,"total":65},"unattributed":{"hits":73,"total":123}},"exposure":{"exposed":349,"not_exposed":0,"unknown":123,"exposed_hits":213,"exposed_rate":0.6103}},{"key":"recall_visibly_used","instruction":"Recalled knowledge used (temporal proxy)","predicate":"recall_used_rate > 0 — a later write/predict follows a read; proximity, not attribution","hits":196,"total":472,"rate":0.4153,"earlier_rate":0.3475,"recent_rate":0.4831,"by_runtime":{"codex":{"hits":135,"total":284},"claude":{"hits":19,"total":65},"unattributed":{"hits":42,"total":123}},"exposure":null},{"key":"ctx_working_state","instruction":"Working state captured (agent plan/decision)","predicate":"context_snapshot_count > 0 — counts context_ref events, which only an agent ctx_update(category=plan|decision) produces; the stop-hook's scratch snapshots never carry a context_ref, so this measures agent discipline (Correction 1, 2026-08-12 — the cb36570 disclosure asserted the opposite of what the code does)","hits":325,"total":472,"rate":0.6886,"earlier_rate":0.6907,"recent_rate":0.6864,"by_runtime":{"codex":{"hits":227,"total":284},"claude":{"hits":20,"total":65},"unattributed":{"hits":78,"total":123}},"exposure":{"exposed":349,"not_exposed":0,"unknown":123,"exposed_hits":247,"exposed_rate":0.7077}},{"key":"declared_predictions","instruction":"Declare consequential actions","predicate":"brier_score is not None","hits":78,"total":472,"rate":0.1653,"earlier_rate":0.0763,"recent_rate":0.2542,"by_runtime":{"codex":{"hits":65,"total":284},"claude":{"hits":6,"total":65},"unattributed":{"hits":7,"total":123}},"exposure":{"exposed":349,"not_exposed":0,"unknown":123,"exposed_hits":71,"exposed_rate":0.2034}},{"key":"outcome_bearing","instruction":"Outcome-bearing events ≥ 2","predicate":"outcome_event_count >= 2","hits":293,"total":472,"rate":0.6208,"earlier_rate":0.6568,"recent_rate":0.5847,"by_runtime":{"codex":{"hits":180,"total":284},"claude":{"hits":28,"total":65},"unattributed":{"hits":85,"total":123}},"exposure":null},{"key":"grade_self_reported","instruction":"Grade your task on completion (task_result)","predicate":"recognized (task_result, task_result_source) present","hits":217,"total":472,"rate":0.4597,"earlier_rate":0.0254,"recent_rate":0.8941,"by_runtime":{"codex":{"hits":180,"total":284},"claude":{"hits":37,"total":65},"unattributed":{"hits":0,"total":123}},"by_experiment_group":{"B":{"hits":211,"total":235}},"exposure":{"exposed":349,"not_exposed":0,"unknown":123,"exposed_hits":217,"exposed_rate":0.6218}}],"optimism_skew":{"overall":{"hits":4,"self_success_total":172,"rate":0.0233,"insufficient_n":false},"by_experiment_group":{"B":{"hits":4,"self_success_total":166,"rate":0.0241,"insufficient_n":false}}},"arm_comparison":{"status":"not_started","confirmatory":false,"approximate":false,"note":"GRADING_NUDGE_T0 unset — the experiment has not begun (spec D4); no session is per-protocol."},"notes":["Compliance measures BEHAVIOR — whether sessions did the instructed thing. It does not measure whether doing it helped: the outcome signal is still degenerate (replay-evals-patterns.md), so no quality claim follows from these rates.","Rates are over ALL evaluated sessions, including sessions that predate an instruction's rollout — the table measures the fleet, not obedience among instructed sessions. Attribution (runtime, client version, instruction-artifact hashes) starts with client 0.1.41 sessions and nothing backfills: the 30-day eval TTL plus non-overwriting eval writes mean every earlier session stays unattributed until it ages out, so the per-runtime and exposure slices tolerate a mostly-unknown window after rollout.","A session counts as exposed to an instruction only when a verified artifact carrying its text reached it — rendered block hash matching the wheel's, or the gateway handshake hash present, with per-key introduction version gates — and every unattributed session is unknown, never not_exposed.","Predicates are frozen to the 2026-08-11 founding measurement (docs/superpowers/specs/2026-08-11-living-instructions-design.md); a changed predicate would orphan the baseline, so changes arrive as new rows.","Trend halves the DATED evals by eval time; it is withheld below 10 dated sessions rather than shown small.","optimism_skew measures grading HONESTY, not compliance: of self-reported-success sessions, how many also carry an independent failure contradiction (has_failures, or a tool_success_rate < 1.0 guarded to outcome_event_count >= 2). Visibility only — no gating, no mutation. It reads the SAME scan as the table above, so the top-level `approximate` and `unparsed` disclosures apply to it too. Below 30 self-success sessions (overall or per arm) its rate is null with insufficient_n true, never a bare 0.0."]}
```
