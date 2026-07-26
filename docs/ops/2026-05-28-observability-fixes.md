# Observability Hardening — 2026-05-28

Triggered by the 24h health review which surfaced three independent gaps. Plan:
`docs/superpowers/plans/2026-05-28-firekeep-observability-fixes.md`.

## What Was Broken

1. **session_id discipline failure.** Only 12 replay events in the prior 24h. Indexes were
   `relay` / `sentinel` / `smoke` / `unknown` — zero events tagged to any Bridge `session_id`.
   Root cause: `cortex/app/mcp_server.py:_resolve_identity` intentionally refuses env-var
   fallback (multi-tenant attribution guard); agents are expected to pass `session_id` /
   `agent_id` explicitly per call, and the convention wasn't being honored. The Cortex
   API silently defaulted both to `"unknown"` and the discipline failure was invisible.

2. **Bridge never emitted replay events.** `SessionManager.start_session` / `update` /
   `complete_session` / `abandon_session` updated Redis hashes but called zero replay code
   paths. Even when Cortex tagged events correctly, Bridge's own lifecycle markers were
   absent from `rp:events`.

3. **3,869 of 3,869 memories tagged `contributor=unknown`.** `ActionLog` had no `agent_id`
   field. `vector.upsert` metadata didn't carry one either. `/memory/contributors` reads
   `payload.agent_id` at top level — which never got written. Team continuity / handoffs
   were silently non-functional.

## What Was Changed

| Commit | Change |
|---|---|
| `608bb4d` | Cortex: Redis-backed counter on `/memory/recall` and `/memory/learn` when `X-Session-Id` is missing or `"unknown"`. New `GET /admin/untagged-calls?days=N` endpoint (clamped 1–30). `scripts/briefing.sh` surfaces total as a Discipline reminder. Multi-tenant guard in `_resolve_identity` preserved unchanged. |
| `0d763b3` | Code-review follow-up: clamp `days` param, reorganize helper placement, hoist `timedelta` import, add type annotation. |
| `fdf27dd` | Bridge: `_replay_emit` / `_ensure_replay` module-level helpers mirroring `cortex/app/main.py` pattern (lazy import, exception swallow). Emit `session.started` / `session.updated` / `session.completed` / `session.abandoned` from the four lifecycle methods. |
| `d499c8e` | Cortex: `/memory/learn` reads `X-Agent-Id` and `X-Session-Id` headers; `vector.upsert` extended whitelist so `agent_id` / `session_id` / `project` land at top-level Qdrant payload (where `/memory/contributors` reads). New `cortex/scripts/backfill_legacy_agent_id.py` — idempotent, tags pre-existing records with sentinel `"legacy-pre-team-continuity"`. |
| `c5fd725` | Test follow-up: assert payload promotion in `test_vector.py`, DRY `_PROMOTED_PAYLOAD_KEYS` / `_EXCLUDED_FROM_NESTED_METADATA` constants. |
| `52d7cb1` | Doc: `/admin/untagged-calls` added to `cortex/CLAUDE.md` API Endpoints. |
| `2b55059` | Dashboard: `firekeepHeaders()` helper, 4 memory call sites send `X-Session-Id` (sessionStorage UUID) + `X-Agent-Id` (from `firekeep_contributor` localStorage, fallback `"dashboard"`). New Discipline tile in Ops tab fetching `/admin/untagged-calls?days=1` with green/yellow/red threshold. |

## Verification

- **Container rebuild:** `docker compose up -d --build cortex-api cortex-mcp bridge` —
  `--force-recreate` alone is insufficient because the Dockerfile `COPY`s code (no bind
  mount). All three healthy post-rebuild.
- **Counter live:** `/admin/untagged-calls?days=1` increments on untagged recall (0 → 1),
  stays at 1 when same recall is sent with `X-Session-Id: smoketest-sid`.
- **Bridge emission live:** `rp:session_idx:smoketest-sid` appeared post-rebuild after a
  tagged recall. `ctx_abandon_session("a6cefc15-8db")` produced `session.abandoned` event in
  `rp:events` — first real-world event from the new wiring.
- **Backfill:** ran `python /tmp/backfill.py` inside `cortex-api`. Updated 3,869 records.
  `/memory/contributors` now returns one group `legacy-pre-team-continuity` with 3,869
  memories; `unknown` group gone.

## Side Cleanup

Stale "active" Bridge session `a6cefc15-8db` ("Dashboard redesign and alignment fixes",
created 2026-03-15, last updated same day — 73 days idle) abandoned via
`ctx_abandon_session`. Status breakdown is now `{completed: 4, paused: 11, abandoned: 1}`.

## Known Follow-ups Not Addressed

- `cortex/app/skills/synthesizer.py` and `cortex/app/skills/api.py` write to Qdrant
  directly via `PointStruct`, bypassing `VectorClient.upsert` — they won't benefit from
  the new whitelist promotion. Skills already set `agent_id` at top level so they're
  not broken, but the promotion logic doesn't cover them. Worth a follow-up if skill
  records start mattering in contributor reports.
- Pattern engine analyzer task is not registered in `cortex/app/workers/sleep_cycle.py`
  beat schedule. That's why `/patterns/` is empty — by design rather than breakage. A
  separate plan is required if patterns should actually run.
- Pre-existing test collection errors: `cortex/tests/test_ranker.py` (missing `joblib`)
  and `bridge/tests/test_mcp_tools.py` (`mcp` package version mismatch). Real CI gaps.

## Files Touched

```
cortex/app/main.py                              — counter helper, header extraction, /admin/untagged-calls
cortex/app/db/vector.py                         — payload whitelist constants, promotion logic
cortex/tests/test_api.py                        — TestSessionContextPropagation + TestAgentIdPersistence
cortex/tests/test_vector.py                     — TestUpsert payload promotion assertions
cortex/scripts/backfill_legacy_agent_id.py      — new, idempotent migration
bridge/app/session.py                           — _replay_emit + 4 lifecycle emit sites
bridge/tests/test_session.py                    — TestReplayEmission (6 tests)
scripts/briefing.sh                             — section 4b Discipline line
dashboard/index.html                            — firekeepHeaders helper, 4 call sites, Ops Discipline tile
CLAUDE.md                                       — Phase 3 Team Continuity bullets, briefing description, Replay emitters list
cortex/CLAUDE.md                                — /admin/untagged-calls endpoint documented
README.md                                       — dashboard tab table refreshed (Knowledge/Code/Twin/Embeddings removed, Skills/Ops/Policy/Vault added)
```
