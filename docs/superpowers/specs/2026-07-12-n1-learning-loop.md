<!-- Restored 2026-07-28 from the predecessor repository's archive branch
     (docs/commercialization-spec). STRATEGY.md's NOW item 1 cites this file;
     it did not survive the port. Predecessor product and client-package names
     were updated to Firekeep / firekeep_client. The cortex/ bridge/ relay/
     paths are unchanged and still accurate. -->

# N=1 Learning Loop — Design Spec

**Date:** 2026-07-12
**Status:** Design (not yet planned/built)
**Steering:** `docs/STRATEGY.md` — this is the NOW-horizon item that makes the defining bet pay off.

## Problem

The learning loop (replay → mine → briefing → measure → redeliver) is Firekeep's one defining bet, but today it only pays off at **team scale**:

- The Pattern Engine promotes strategy cards to briefing-eligibility only at `trial`/`validated` (evidence ≥15/≥25, positive tip lift). A solo maintainer or a 3-person team won't clear those thresholds for months.
- Tip-effectiveness A/B splits already-thin session volume into treatment/control.

So on session 1 — the moment a new user is deciding whether this is worth the logging discipline — the briefing has **nothing learned to show**. The substrate feels worthless exactly when it most needs to prove itself. This is the root of the discipline-dependence risk: voluntary logging survives only if it visibly pays the logger back *now*.

## Goal

Make the loop deliver **felt value on session 1**, so logging is self-reinforcing rather than mandated. Split **observation** (works at N=1, descriptive, unvalidated) from **validation** (needs scale, statistical) — surface the first immediately, freeze the second behind a flag until volume exists.

## Design

### 1. An "observed" surface in the briefing (N=1)
Add a briefing sub-section — **"From your recent sessions"** — that runs the pattern detectors over the caller's *own* recent replay + memories and emits, without any validation gate:
- a **descriptive quality read** ("your last 2 sessions: 1 with elevated tool-failure on `cortex/app/db/`"), and
- **one concrete grounded tip** with provenance ("last time you touched `vector.py` you hit an embedding-length 400 — the fix was truncation + shrink-to-fit").

Rules:
- Clearly labelled **`observed (unvalidated)`** so it's never confused with a validated pattern.
- Provenance is mandatory — every observation names the session/memory it came from. This is the payoff-attribution that turns logging into a felt reward.
- Max 1–2 items; this is a nudge, not a report.

Grounding: the detectors in `cortex/app/patterns/` already extract features from replay; today only `trial`+ cards reach the briefing (`cortex/app/briefing/`). This adds a *descriptive* path that surfaces at `candidate`/`observed` for the caller's own history — it does not touch the promotion ladder.

### 2. Freeze the validation statistics behind a flag
Gate the promotion thresholds (≥10/15/25), A/B treatment/control assignment, and the experiment framework (Cohen's h / chi-square / datasets — already behind `PATTERN_EXPERIMENTS_ENABLED=false`) under one clearly-named switch (e.g. `PATTERN_VALIDATION_ENABLED=false`). Detectors keep running and feeding the observed surface; only *validation/promotion* is frozen. Re-enable when a deployment crosses a real session-count threshold.

### 3. Structural capture on `stop` (discipline without decree)
The `stop` hook core (`firekeep_client.hooks.stop`) already snapshots the workspace. Extend it to **capture the session's durable facts deterministically** (branch, commits, diff summary, test results, files touched) and **enqueue a "distill this session" job** (the Fleet-as-GPU seam — a Relay task, drained later by a client agent that generates the memory/skill with its own model). This makes the *floor* — that a session happened and what it produced — captured by code, not by the model choosing to `memory_learn`. Model cooperation becomes the ceiling (rich judgment-call skills), never the floor.

_(The distill *worker* is the Fleet-as-GPU BET and out of scope here; this spec only lands the capture + enqueue so the floor stops depending on compliance.)_

### 4. North-star metric: recall hit-rate
Surface, in the briefing's existing discipline section, a single trend: **recall hit-rate** — of the memories/skills surfaced to a session, how many did it actually use/benefit from (measurable from replay: a surfaced memory followed by a related edit/recall). This replaces "feature count" as the number to steer by, and it's the honest read on whether the flywheel is spinning.

## Out of scope (later horizons)
- The Fleet-as-GPU distill *worker* (BET).
- Memory-as-guardrail Gateway enforcement (NEXT).
- Re-enabling validation stats at scale.

## Testing
- With exactly **one** prior session for a project, `GET /briefing` returns a non-empty "From your recent sessions" item with correct provenance.
- With `PATTERN_VALIDATION_ENABLED=false`, no promotion/A/B runs, but the observed surface still populates.
- The `stop` hook enqueues a distill task carrying the session's durable facts (assert the Relay task payload), with zero dependence on the agent having called `memory_learn`.
- Recall hit-rate computes from a replay fixture (surfaced memory → subsequent related action).

## Open questions
- Exact shape of a "distill job" payload + which Relay task type it uses (settle when the Fleet-as-GPU BET is specced).
- Whether the observed surface reuses the `strategy_tips` section or is its own section (leaning: its own, to keep `observed` vs `validated` visually distinct).
