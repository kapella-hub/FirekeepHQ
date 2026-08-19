# Proactive Recall — pushed memory at every prompt (design, 2026-08-18)

**Status: approved direction (owner: "design it, iterate till done, validate
and deploy"), build follows immediately.**

## 1. Why

The live compliance table quantified the owner's felt problem: fleet-wide,
**"recall before you answer" runs at 46%** and **"recalled knowledge used" at
35%**, while writes run 65–75%. The Keep accumulates faster than it is
consulted. The session that surfaced this also supplied the archetype: an
agent spent ~30 minutes empirically re-deriving a fix whose exact skill had
been in team memory for two weeks — because recall is PULL-based, and the
one push (the session-start briefing) matches only the session's original
goal. The design lesson of night-shift and backups applies: the Firekeep
features that work are the ones that happen TO agents. So recall moves from
pulled to pushed.

## 2. What it does

On every user prompt (hook-bearing runtimes), the prompt hook embeds the
prompt text against team memory and injects the few genuinely relevant,
not-yet-seen memories into context — or, far more often, injects nothing.

The noise discipline is the design's core, inherited from this exact hook's
own history (the raw-JSON-every-prompt field complaint, 2026-07-14):

- **Skip** prompts that carry no signal: fewer than 24 characters after
  whitespace collapse, or starting with `/` (slash commands).
- **Threshold**: only sources with relevance score ≥
  `FIREKEEP_RECALL_PUSH_MIN_SCORE` (default 0.55) are eligible.
- **Session dedupe**: a memory id injected once is never injected again in
  that session (scratch key per agent, 12h TTL — the tasks-digest pattern).
- **Cap**: at most 3, each rendered as ONE line trimmed to ~200 chars.
- **Fail-open + bounded**: hard 2.5s client timeout
  (`FIREKEEP_RECALL_PUSH_TIMEOUT_SECONDS`); any failure injects nothing and
  logs to hooklog. The hook path budget is sacred.

Rendering states what it is — background evidence, not instruction:

    [firekeep recall] team memory that may be relevant (verify before relying on it):
    - <one line> (score 0.71)

Channel: the existing `systemMessage` merge in the prompt core (visible to
the user = honest; no second output channel invented).

Controls: ON by default. `FIREKEEP_NO_RECALL_PUSH=1` or `[recall] push =
false` in `~/.firekeep/config` turns it off; private-session mode already
suppresses the whole prompt core at the dispatcher.

## 3. Coverage, stated honestly

Fires only where the runtime delivers prompt text to the hook: **Claude Code
and Kiro**. Codex is MCP-only (no hooks); OpenCode's bridge maps
`session.idle` without prompt text. New `proactive_recall` row in
`contract/matrix.py` (claude/kiro = per-prompt; others = none — briefing at
session start remains their only push). The pinned CAPS test updates with it.

## 4. Measurement honesty (the round-2 contract precedent)

A pushed recall IS a recall — the compliance table's "recall before you
answer" will rise mechanically once this ships. That must be attributable,
not confounding: `ContextQuery` gains an optional `trigger: str | None`
(max 32 chars; the hook sends `"prompt-hook"`), carried into the recall's
replay/eval record so future analysis can slice deliberate vs pushed recall.
The FROZEN founding predicates do not change (their freeze is the point);
the guide and the compliance-study context note the exposure-change date,
exactly as 0.1.41 did for the second channel. "Recalled knowledge used"
remains the honest judge — it is the number this feature exists to move.

## 5. Implementation shape

- **Client**: new `firekeep_client/promptrecall.py` (pure logic: gate,
  threshold, dedupe, render — testable without hooks) + a short call from
  `hooks/prompt.py`'s core appended to its existing inbox/systemMessage
  merge. Stdlib only; POST via `resolver`/`transport` with
  `{task: prompt[:2000], top_k: 3, trigger: "prompt-hook"}`.
- **Server**: the `trigger` field on `ContextQuery`, threaded to wherever
  the recall route records its replay/eval event. Additive; absent field
  behaves exactly as today.
- **Out of scope, stated**: any predicate change; per-prompt push on
  MCP-only runtimes (would need the gateway to intercept prompts it never
  sees); tuning the relevance threshold beyond the env knob; feedback
  auto-submission (memory_feedback stays deliberate).

## 6. Tests

Client: short/slash prompts skipped; kill-switch env + config; threshold
filters; per-session dedupe (second injection suppressed, different memory
still injected); 3-cap; render trim; timeout/unreachable → `{}` merge and a
hooklog line; systemMessage merges with the relay block when both fire.
Server: `trigger` accepted, absent-field back-compat (existing recall suite
must pass with ZERO edits), value lands in the replay/eval record. Matrix:
CAPS updated; per-runtime values asserted. Live: a real prompt in a real
session shows an injection; the replay record carries `trigger=prompt-hook`.

## 7. Docs

`docs/guides/memory-and-recall.md` (new Proactive Recall section: behavior,
knobs, coverage table, measurement note), `docs/guides/client-kit.md` (one
paragraph in the hook inventory), CLAUDE.md untouched (guide-level detail).
Site: after release only — one line where recall behavior is described.
