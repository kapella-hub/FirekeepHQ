# Stale Presence Entry Cleanup — 2026-05-27

## Entries Removed

| agent_id | hostname | last_heartbeat | goal |
|----------|----------|----------------|------|
| agent-a2021a95b30ecc5fe | darwin | 2026-05-12 (approx) | Fix 6 bugs in Firekeep Skill Synthesis feature code review |
| agent-srv1574321-46d6 | srv1574321 | 2026-04-24 (approx) | Session started |
| polymarket-trader-agent | firekeep-stack | 2026-04-14 (approx) | Specialized agent for mechanical trading on Polymarket prediction markets |

All three showed `last_heartbeat == started_at` — no poll hook ever fired, indicating the sessions crashed before the first `UserPromptSubmit` event.

## Deregistration Logic Review

`debrief.sh` race-condition guard (lines 38–43): reads `/tmp/firekeep-presence-${AGENT_ID}-registered`
(written by `briefing.sh`) and skips deregistration if `NOW - REG_TIME < 5s`. This is correct — it
prevents a stopping debrief from deregistering a freshly-started new session.

**Root cause of stale entries:** crashed sessions where `debrief.sh` never ran. No code bug.

**Latent edge case:** A session that exits in under 5 seconds would also skip deregistration. None of the
stale entries matched this pattern, but it's worth noting. A future improvement could compare the stored
REG_TIME against a session-specific marker rather than a wall-clock delta.

## Remaining Presence

After cleanup: 2 entries (Alex: active, default: idle).
