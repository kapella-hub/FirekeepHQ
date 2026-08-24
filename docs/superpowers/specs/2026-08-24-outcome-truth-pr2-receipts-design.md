# Outcome Truth PR2 — receipts & honest measurement — design

**Status:** reviewed (one self-adversarial pass, 2026-08-24 — verdicts folded in below). Ready to plan.
**Date:** 2026-08-24
**Depends on:** PR1 (outcome truth, shipped `72cfa03`) — session outcomes are now a real
`(task_result, task_result_source)` grade via `recognized_grade_pair`, and `session_success`
grades from it. PR2 completes the *exposure→application* half of the join so "which artifact drove
which outcome" is answerable now that the outcome is true.
**Scope:** one subsystem — the replay-receipt / metric-honesty layer. Every decision **completes an
existing join path** (no new hot-path emit sites, one new event type). Deferred to PR3: skill
`artifact_exposed` receipts, causal `trace_links` narrowing, skill content-hash versioning, the
experiment rebuild + PatternCard provenance. Deferred generally: client-side token/byte accounting.

## Problem

PR1 made the *outcome* true. The *exposure→application* half is still partial and, on long
sessions, truncated. Verified against `main @ 72cfa03`:

1. **Streaming recall is fully receipt-blind.** `POST /memory/recall` (`main.py:1270-1342`) does
   four things on the hot path: bumps `memory:access_counts` (staleness/popularity), bumps
   `memory:last_recalled` (the skill-staleness clock — comment at `main.py:1301-1304`), bumps the
   untagged-call counter, and emits a `memory_read` replay event stamping the returned ids
   (`main.py:1340`). `POST /memory/recall/stream` (`streaming.py:32-60`) does **none of them** — it
   reads no `X-Session-Id`/`X-Agent-Id`, emits nothing, bumps nothing. Every memory recalled only
   via SSE is invisible to OWM efficacy, `recall_used_rate`/`memory_read_count`, compliance, and
   audit — and its staleness clock never advances, so it can be reaped as "never recalled" while
   being actively used. The ids are already on the wire (each `source` frame's
   `data["metadata"]["id"]`).

2. **The strongest "applied" signal is discarded.** `memory_feedback` is the one place an agent
   says "I acted on this recalled artifact and it was right/wrong." The MCP tool sends
   `X-Session-Id`/`X-Agent-Id` "for replay tracing", but the REST handler (`main.py:1595-1621`)
   **never reads them and emits no replay event** — it only bumps the per-point Qdrant counter via
   `set_feedback`. The headers are dead; the session-scoped application signal is lost.
   *Verified join-soundness:* the ids the tool passes back (`FeedbackRequest.memory_ids`) are the
   recall-result `metadata["id"]`s (`main.py:1294`), which are exactly what `memory_read` stamps as
   `memory_ids` (`main.py:1340`) **and** what `set_feedback` uses as the Qdrant point id
   (`vector.py:1197`). One id space end-to-end — the receipt joins cleanly to both `memory_read`
   and the outcome grade.

3. **Long-session metrics are truncated.** PR1 made the *grade* truncation-proof
   (`find_terminal_grade` snapshots ids and scans backward). The *metrics* still read the
   oldest-1,000 window: `compute_session_eval` fetches `get_session_timeline(..., limit=1000)`
   (`compute.py:102-104`) and computes every Tier-1 metric, the Brier predict/reconcile join, and
   `failure_event_ids` off it; OWM's join reads `get_session_timeline(event_type="memory_read",
   limit=1000)` (`owm.py:116-117`) with the filter applied *after* pagination, so late-session
   memory_reads never count. Meanwhile `get_session_summary` already full-scans (uncapped zcard) —
   so `event_count` and the metrics disagree on any >1,000-event session, silently.

## Decisions, and why

**D1. Streaming recall reaches full parity with the non-streaming path.** In `recall_stream`, read
`X-Session-Id`/`X-Agent-Id`, accumulate each `source` frame's `data["metadata"].get("id")` across
the stream (same key and truthy-filter the non-streaming path uses to build `accessed_ids`, so
parity holds by construction across the vector/graph source branches), and at the `done` frame — in
a `finally`, fire-and-forget — do exactly what `main.py:1291-1342` does: the
`access_counts`/`last_recalled` pipeline bump, `_bump_untagged_counter`, and
`_replay_emit("memory_read", sid, aid, payload={memory_ids, result_count, trigger, namespace})`.
`top_score` is deliberately omitted: the non-streaming comment (`main.py:1323-1330`, live-measured)
proves it is a **constant 1.0** by construction and no consumer reads it (OWM joins on ids,
`owm.py:122`); pass `raw_top_score` only if it is cheaply on hand. `emit()`/the bumps never raise
and run after the last yield, so they cannot break or delay the stream.

**D2. `memory_feedback` emits a `memory_feedback` replay receipt — the dead headers are read.**
The handler reads the `X-Session-Id`/`X-Agent-Id` the tool already sends, and after the
`set_feedback` loop emits `_replay_emit("memory_feedback", sid, aid, payload={memory_ids, useful,
comment_present, updated})` — one event per feedback call, session-scoped. The Qdrant counter write
is unchanged (it still feeds the recall multiplier). This is the explicit **applied** stage:
`memory_read` (exposed) → `memory_feedback` (applied, carries the `useful` bit) → `session_success`
(outcome), all joinable on the one id space verified above. Named `memory_feedback` (not
`artifact_applied`) to match `memory_read`/`memory_write` and the endpoint, and to avoid
presupposing the general `artifact_*` taxonomy deferred to PR3. New `EventType` — see D4.

**D3. Metrics and the OWM join read the whole session, truncation-safe, via PR1's primitives.**
Replace `compute_session_eval`'s `get_session_timeline(limit=1000)` with
`ids = await get_session_event_ids(replay_redis, session_id, limit=_METRIC_SCAN_MAX)` +
`events = await get_event_batch(replay_redis, ids)` — the same snapshot-then-hydrate shape
`find_terminal_grade` uses (`get_event_batch` is built for a ~5k scan: MGET + pipelined XRANGE
windows). Tier-1 metrics, the Brier join, and `failure_event_ids` then see the complete list. Do
the same in `owm.py` (full-scan the session, filter `event_type == "memory_read"` in Python rather
than the after-pagination `event_type=` window). `_METRIC_SCAN_MAX = 5000` matches
`get_session_event_ids`'s own default and `find_terminal_grade`'s cap. **The cap is explicit, not
silent:** when `len(ids) >= _METRIC_SCAN_MAX`, log a warning and stamp `metrics_truncated: true` on
the EvalResult, so a truncated metric is visible downstream (the *grade* stays truthful regardless,
via PR1). Existing eval/OWM tests stay green; add a >1,000-event test for each proving a late
failure / reconcile / memory_read that the old window dropped now counts.

**D4. Add `memory_feedback` to the `EventType` Literal + its enumeration test, for contract
honesty.** `emit()` treats `event_type` as an opaque string — `compute.py` already reads
`agent.action.predict`/`reconcile`, which are **not** in the Literal — so `memory_feedback` works
end-to-end with zero model changes. But a first-class, handler-emitted type belongs in the declared
contract: add it to `replay/models.py`'s `EventType` and to `test_all_event_types`
(`replay/tests/test_models.py:108`). That is the entire `replay/` footprint — no reader/emitter
change, no migration (old events stay valid; stream fields are free strings). (The absent
`agent.action.*` types are a pre-existing latent gap; not widened here, noted for PR3.)

**D5. Receipts carry ids and enums, never content; personal mode emits nothing.** Both new/extended
payloads are `{memory_ids, result_count?, trigger?, namespace?, useful?, comment_present?}` — no
memory text, no query beyond the 200-char cap `memory_read` already applies, and `comment_present`
is a bool, never the comment body. The receipts inherit the existing replay privacy posture; verify
personal/bypass mode suppresses the new emit paths exactly as it does the existing `memory_read`.

## Non-goals / deferred (with rationale)

- **Skill `artifact_exposed` receipts (was a candidate D4).** Cut after the code showed the gap is
  narrower than it first appeared: skills surfaced through **general RAG recall already get a
  `memory_read` receipt and a `last_recalled` bump** (`main.py:1300-1304, 1340`) — only the
  dedicated `skill_recall` and briefing paths lack one. And the "typed enum kills OWM's per-id
  Qdrant round-trip" benefit was misattributed: that round-trip (`owm.py:139-152`) is the
  memory-outcome **exclusion** path, which a skill-exposure event would not flow through. It is the
  most design surface (a new type + an emit in the briefing hot path) for the least-verified
  benefit — **PR3**, where the cheap option is simply to have `skill_recall` emit a `memory_read`
  with the skill ids (symmetric with general RAG), no new type at all.
- **Causal `trace_links` population (narrowing).** 0-populated substrate; only `narrow()` consumes
  `trace_links` (not `parent_span_id`). A linear `preceded` back-chain would light narrowing up
  cheaply, but narrowing is a debugging tool orthogonal to the outcome-join — **PR3.**
- **Token / byte / latency accounting.** No accounting exists; the gateway serialize seam is
  client-side/stdio and needs a new client→server emit path — a separate feature. (Cheap optional
  byproduct if ever wanted: stamp `tokens_used` from `rag.py:398` + a per-recall `duration_ms` into
  the `memory_read` emit D1 already touches. Flagged, not built.)
- **Artifact version / content-hash provenance** and **skill content-hash versioning** — **PR3**,
  bundled with the experiment rebuild + PatternCard provenance.
- **A dedicated receipt→outcome nightly joiner.** PR2 *emits* the applied receipt and proves the
  join is possible (test-level: a session with a `memory_feedback` event and a recognized grade
  joins). A general "artifact efficacy" scorer is **PR3.**

## Disclosed residuals

- `_METRIC_SCAN_MAX` (D3): a >5,000-event session still truncates its *metrics* — but now
  **visibly** (`metrics_truncated: true` + log), and its *grade* stays truthful (PR1). Matched to
  `find_terminal_grade`'s cap and `get_session_event_ids`'s default.
- The streaming path applies no lifecycle/OWM score multipliers (pre-existing divergence). D1 emits
  the join-relevant fields (ids/count/trigger), not a comparable score — which is sufficient because
  `top_score` is a known constant and every consumer joins on ids.
- `memory_feedback` covers memory feedback ids; instruction/pattern application is not yet a
  first-class applied signal (PR3).

## Ship gates

- All six suites green (bridge, cortex, replay, client-unmodified, auth, root) + deploy shell tests.
- Pinned by test: SSE recall emits a `memory_read` with the returned ids and bumps
  access_counts/last_recalled (D1); a `memory_feedback` call emits a `memory_feedback` receipt
  carrying session/agent/ids/`useful` (D2); a >1,000-event session's metrics/Brier/failure_ids and
  the OWM join now include a late event the old window missed, and the cap stamps `metrics_truncated`
  (D3); `memory_feedback` is in the `EventType` Literal and its enumeration test (D4); receipts
  carry no content and personal mode emits nothing (D5).
- Replay `narrow()` / existing replay tests unchanged (PR2 adds an event *type*, not link
  semantics).
- **Docs updated** (per the change-consistency checklist, since PR2 adds an event type + endpoint
  behavior): `replay/models.py` contract, `docs/guides/replay-evals-patterns.md` (truncation fix +
  new event type), `docs/guides/memory-and-recall.md` (SSE parity + feedback receipt), and the
  root/`cortex` CLAUDE.md event-type inventory where the MCP tool surface is described.
