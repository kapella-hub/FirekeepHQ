# Living Instructions — the instruction layer that measures itself

_Status: round 1 SHIPPED (server v0.4.1, deployed and verified live
2026-08-11) — `GET /autopilot/compliance` + the Living Instructions table on
the dashboard's Autopilot tab, predicates frozen to the founding measurement
below. Announced on firekeep.ai as ladder rung 05 the same day, after the
gate was verified. First live read: every instruction trended up between the
older and newer eval halves (recall 44%→69%, write 31%→63%, recall-used
13%→44%, ctx 56%→69%) — behavior, not quality. The round-2 MEASUREMENT
CONTRACT shipped 2026-08-12 (server v0.4.3 + client 0.1.41, deployed and
verified live: all rows serving `by_runtime` + `exposure`, 34 pre-0.1.41
sessions honestly unknown/unattributed) — see "Round 2 — the measurement
contract" below, including its two corrections. The rewrite loop (round 2
proper) and A/B (round 3) remain proposed._

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
  with trend over time. No rewriting, no delivery, no stats. Ships the same
  way Autopilot round 1 shipped: it proposes and reports, never mutates.
  (Per-runtime slicing was originally listed here and is NOT in round 1:
  stored evals carry no runtime/adapter attribution, so slicing needs an
  eval-schema addition first — moved to round 2 alongside exposure tracking;
  external review 2026-08-11.)
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

*(Exposure correction, 2026-08-12: "both instruction layers" was wrong. The
Cortex MCP handshake channel never reached kit users — the gateway discards
backend `instructions=` — so true exposure was the rendered block alone from
2026-08-10 until client 0.1.41 restored the gateway channel. The receipts
shipped in the same release make the two exposure epochs distinguishable.
Details under "Round 2 — the measurement contract".)*

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

## External review, 2026-08-11 — findings and dispositions

A same-day review of the shipped round 1 found six issues; all verified,
four fixed immediately, two folded into round-2 scope:

1. **Denominator conflates exposure with obedience** (valid, round-2 scope):
   rates cover all evaluated sessions, including sessions predating an
   instruction's rollout. One nuance held against the review: the 0/32
   predictions row was never claimed as disobedience — the spec frames it as
   the pre-rollout arm of the natural experiment, which is exactly a rollout
   measurement. The general point stands for cross-time comparisons; the
   response now says so in `notes`, and exposure tracking (instruction
   version per session) plus per-runtime slicing are round-2 requirements.
2. **`ctx_working_state` is satisfiable by the stop-hook's automatic per-turn
   snapshot** (confirmed): the row measures capture, not agent discipline.
   Predicate kept (frozen key; the capture rate is still worth watching),
   label and description now say what it actually measures; an agent-driven
   variant needs an event the hook does not emit — round 2, with exposure.
   *(CORRECTED 2026-08-12: this finding was itself wrong, and the relabel it
   produced overclaimed in the opposite direction — see "Round 2 — the
   measurement contract" below. The historical text is kept as a record of
   what was believed when the labels shipped.)*
3. **`recall_visibly_used` is a temporal proxy** (confirmed): relabeled
   "(temporal proxy)" with "proximity, not attribution" in the description.
   Predicate unchanged — comparability with the baseline preserved.
4. **A non-numeric metric value 500'd the endpoint** (confirmed, fixed):
   `_num()` coercion — non-numbers read as absent, bools excluded so a stray
   flag cannot masquerade as a count. Regression test reproduces the
   original TypeError input.
5. **Trend floor counted all evals, not dated ones** (confirmed, fixed):
   ten evals with two dates rendered a 1-vs-1 comparison as a trend. Floor
   moved to dated evals; `dated_sessions` added to the response.
6. **The renderer dropped `approximate`** (confirmed, fixed): a capped scan
   now discloses itself on the dashboard, same rule as the digest.

Label/description texts may sharpen (they describe predicates to humans and
were overclaiming); KEYS and PREDICATES stay frozen — no measured number
changed in any fix.

## Round 2 — the measurement contract (2026-08-12)

Scouting the round-2 requirements found that two loads the round-1 surfaces
were carrying are wrong — in opposite directions — and both corrections are
prerequisites to honest exposure tracking. Both were verified at the code
level, emitter to scorer, not re-reasoned from call sites.

**Correction 1 — review finding #2 was itself wrong; the ctx row already
measures agent discipline.** `_context_snapshot_count` counts events carrying
a `context_ref` (`cortex/app/evals/scorers.py:220`), and the only writer of
`context_ref` in the codebase attaches one exclusively when `ctx_update` is
called with `category` "decision" or "plan"
(`bridge/app/mcp_server.py:341-353`). Every hook write — stop, prompt,
precompact — is `category="scratch"`, so hook snapshots emit `ctx_update`
events with `context_ref=None` and are never counted. The founding
measurement's 62% was agent plan/decision discipline all along; the cb36570
relabel ("the per-turn stop-hook snapshot also satisfies this") asserts the
opposite of what the code does and is corrected in this round — a
description change, permitted by this file's own relabeling rule. The
round-2 item "an agent-driven variant needs an event the hook does not emit"
is retired: the distinction already exists structurally. The review
confirmed the finding by call-site reasoning (stop hook calls `ctx_update`;
`ctx_update` feeds the counter) without checking the category gate between
them; the lesson — verify the full emitter→scorer path before publishing a
claim on a measurement surface — is now itself recorded in team memory.

**Correction 2 — the armed experiment's second channel never existed.**
f23133a added the action_before paragraph to Cortex's FastMCP
`_INSTRUCTIONS` on the belief that "the Cortex MCP handshake" reaches
agents. It does not: every kit runtime connects only to the local gateway,
which discards backend `instructions=` during discovery
(`client/firekeep_client/gateway.py:99-108` — the initialize result is never
read) and serves its own `GATEWAY_INSTRUCTIONS`
(`client/firekeep_client/adapters/base.py:451-457`), which carried no
action_before paragraph. `client/tests/test_memory_instructions.py` asserts
the backends SEND instructions; nothing asserted anything receives them.
True exposure for the 0/32 experiment has therefore been the rendered
instruction block alone since 2026-08-10. Client 0.1.41 adds the paragraph
to `MCP_SERVER_INSTRUCTIONS` — deliberately in the same release as exposure
receipts, so the channel restoration lands as an attributable exposure
change rather than a silent confound in a pre-registered experiment.

### The contract

Two instruction artifacts exist per session, and the contract names both:
the **rendered block** (per-runtime file — may be stale, hand-edited, or
deleted; what is on disk is the truth) and the **gateway handshake text**
(served fresh from the running wheel every session).

- **Versioning.** `adapters/base.py` computes two module-level constants:
  `RENDERED_INSTRUCTIONS_HASH = sha256(FIREKEEP_INSTRUCTIONS)[:12]` and
  `GATEWAY_INSTRUCTIONS_HASH = sha256(GATEWAY_INSTRUCTIONS)[:12]`. The BEGIN
  marker is stamped — `<!-- firekeep:instructions:begin h=<hash> — … -->` —
  and block matching moves to LINE-ANCHORED PREFIX matching on
  `<!-- firekeep:instructions:begin` (the `find_legacy_block_bounds`
  precedent: the begin line was always allowed a variable tail), so stamped
  and legacy unstamped blocks upsert/strip identically. The stamp carries
  NO version field, by review (2026-08-12): a `v=` would rewrite the
  rendered files on every release even with unchanged instruction text,
  moving mtime on files in the customer's prompt prefix — the exact cost
  `write_text_if_changed` exists to avoid. The stamp is a pure function of
  the content: re-rendering from the same text is a byte-identical no-op,
  the hash covers only the text BETWEEN the markers (never itself), and
  which wheel rendered a block is recoverable from the hash while version
  attribution rides `X-Firekeep-Client`. Line anchoring and an
  orphaned-BEGIN heal path (replace exactly the damaged marker line, never
  append a second block) are load-bearing: the review demonstrated both
  unanchored matching and the legacy append shape destroying user content
  in `~/.claude/CLAUDE.md` under background auto-update. `firekeep doctor` gains a
  per-runtime staleness row (on-disk block hash vs wheel hash),
  generalizing the Codex-only containment check.
- **Attribution on the wire.** Five headers, attached wherever the caller
  knows its runtime — the gateway (each adapter now renders `firekeep
  gateway --runtime <name>`) on every proxied call, and the hook cores
  (dispatcher gains the same flag) on their server calls:
  `X-Firekeep-Runtime` (claude|codex|kiro|opencode), `X-Firekeep-Client`
  (wheel version), `X-Firekeep-Instr-Rendered` (re-hash of the on-disk
  block at process start, or `absent`), `X-Firekeep-Instr-Expected` (the
  wheel's rendered-block hash), `X-Firekeep-Instr-Gateway` (the wheel's
  handshake hash). The client re-hashes what is actually on disk rather
  than trusting its own stamp — a hand-edited block reports its true hash.
  Trust level is exactly `X-Agent-Id`'s: an untrusted observability label,
  never a gate (workspace-entitlements design record).
- **Persistence.** Bridge `ctx_start_session` reads the headers (the
  existing `get_http_headers` fallback pattern) and persists them on the
  session hash; they ride the `session_start` replay payload the same way
  `briefing_id`/`tags`/`project` already do. Sessions from clients that
  predate 0.1.41 carry no headers and read as unattributed — honestly.
- **Evals.** `EvalResult` gains optional top-level fields — `runtime`,
  `client_version`, `instructions` ({rendered, expected, gateway}),
  `briefing_delivered` (the session's `briefing_id` presence: the fetch
  receipt that already exists), `agents` (already computed per session by
  `get_session_summary` and previously discarded). `metrics` stays
  `dict[str, float]`; attribution is never a metric.
- **Compliance response — additive only.** Headline `hits/total/rate` keep
  the all-sessions denominator (baseline comparability). Each row gains
  `by_runtime` (same frozen predicate, sliced; `unattributed` bucket
  disclosed) and `exposure` — exposed / not-exposed / unknown session
  counts plus an exposed-only rate — `null` for the two derived rows
  (`recall_visibly_used`, `outcome_bearing`) that have no instruction text
  to be exposed to. A session counts as *exposed* to an instruction key
  when a verified artifact carrying that key's text reached it: rendered
  block verified current (`rendered == expected`), or handshake delivered,
  with per-key introduction versions (action_before: rendered ≥ 0.1.40,
  gateway ≥ 0.1.41; the other text-carrying keys predate attribution and
  need only artifact verification). Everything else is `unknown` —
  including every pre-0.1.41 session, forever, and including a receipt
  whose only surviving field is `expected` (the wheel's self-declared hash
  is a claim about the client, not evidence any artifact reached the
  session) — and nothing backfills: the
  30-day TTL plus non-overwriting eval writes mean the table tolerates
  null attribution for a full window after rollout, and the notes disclose
  it.

What does NOT change: predicate keys, predicate bodies, the headline
denominator, the 0/32 pre-registration — whose exposure story gets
*sharper*, not rewritten: single-channel until 0.1.41, dual-channel after,
receipts distinguishing the epochs.

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
