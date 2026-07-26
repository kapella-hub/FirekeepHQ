# Plan: Finish wiring `ops.py` and `policy/store.py`

**Date:** 2026-05-29
**Status:** Ready to implement (all integration points verified)
**Context:** Two roadmap items have testable cores written + passing (part of the green 760)
but zero wiring. `ops.py` additionally *collides* with already-shipped endpoints.

---

## Verified current state

### Feature A — `cortex/app/ops.py` (roadmap #9 / feeds #6)
- Defines `create_ops_router()` with `/ops/workers` + `/ops/queues`, gated `require_scope("eval:read")`.
- **`main.py:1335-1376` ALREADY has inline `/ops/workers` + `/ops/queues`** (committed, no auth).
- **Dashboard already consumes the inline shapes** (`index.html:1022-1023, 3264-3290`):
  - `/ops/workers` → `{workers:[{name,status,active_tasks,active_task_names}], count}`
  - `/ops/queues`  → `{queues:{celery, event_stream, event_dlq}}`
- `create_ops_router` is **never imported/registered** → dead code.
- The router's response shapes are **incompatible** with the dashboard:
  - queues returns `{queues:[{name,depth}]}` over `("celery","training")` — different keys, list vs dict.
  - workers omits `active_task_names`.
  - adds `eval:read` scope the dashboard doesn't send.

**Decision:** Do NOT wire `ops.py` as-is (would break the dashboard). Instead, *fold the working
inline implementations into `create_ops_router()` preserving EXACT dashboard response shapes*,
then register the router and delete the inline endpoints. Keep `eval:read` scope (consistent with
evals router; harmless while AUTH_ENABLED=false, which is the default). The richer worker inspection
(`_inspect_workers`) and `_get_queue_depths` helpers can stay as internal helpers IF their output is
remapped to the dashboard shape — otherwise keep the simpler proven inline logic. Simpler/proven wins.

### Feature B — `cortex/app/policy/store.py` (roadmap #3 "policy audit visibility")
- `record_policy_decision` / `get_policy_decisions` / `summarize_policy_decisions` — Redis-backed,
  capped list `policy:decisions` (MAXLEN 500). Only referenced by `tests/test_policy_store.py`.
- **No writer**, **no read endpoint**, **no dashboard view**.

**Chokepoint for recording:** `AgentGatewayService.decide()` in
`cortex/app/agent_gateway/service.py` — ALL policy evaluations flow through it now
(the deprecated `/policy/evaluate` proxies to the gateway; the precheck hook calls
`/agent/action/before`). Record AFTER the final decision is computed (post rethink-limit escalation,
~line 156, before building `resp`). Map gateway `decision` ∈ {allow,rethink,block} → stored `action`.
Note: gateway never emits "warn" (warn→allow at service.py:101), so stored actions are allow/rethink/block.

---

## Implementation steps

### 1. `service.py` — record decisions (writer)
- Add optional ctor param `policy_decision_redis=None` (reuse an existing Redis handle; the
  prediction_redis / a DB-0 client is fine — decisions are operational, not replay).
- After decision finalized, best-effort:
  ```python
  if self._policy_decision_redis is not None:
      try:
          from app.policy.store import record_policy_decision
          await record_policy_decision(
              self._policy_decision_redis,
              file_path=req.action.target,
              agent_id=req.agent_id,
              session_id=req.session_id,
              action=decision,                      # allow|rethink|block
              risk_score=policy_decision.risk_score,
              reasons=policy_decision.reasons,
              signals=policy_decision.signals,
          )
      except Exception as exc:
          logger.warning("policy decision record failed: %s", exc)
  ```
- Wrap in try/except — recording must never break the decision path.

### 2. `main.py` — wiring
- In the agent-gateway lifespan block (~line 299-389) pass the chosen Redis client as
  `policy_decision_redis=` into `AgentGatewayService(...)`.
- Pass a `get_policy_decision_redis` callable (or reuse existing redis DI) into
  `create_policy_router(...)` so the new read endpoint can reach Redis.
- Register the reconciled ops router: `app.include_router(create_ops_router())` and
  **delete inline `/ops/workers` + `/ops/queues`** (main.py:1335-1376).

### 3. `policy/api.py` — read endpoint
- Add `GET /policy/decisions?limit=50&action=&agent_id=` → `require_scope("eval:read")`:
  ```python
  decisions = await get_policy_decisions(redis, limit=limit, action=action, agent_id=agent_id)
  return {"decisions": decisions, "summary": summarize_policy_decisions(decisions)}
  ```
- `create_policy_router` needs a redis accessor — add param `get_decision_redis=None`; if None,
  endpoint returns `{"decisions": [], "summary": {...}, "error": "not wired"}` (graceful).

### 4. `dashboard/index.html` — audit view
- Add a "Policy Decisions" card near the Ops cards. Fetch `/policy/decisions?limit=50`.
- Render rows: timestamp, action (color: allow=green, rethink=amber, block=red), agent, file, reasons.
- Show summary counts (allow/warn/block — note warn will be 0) + unique agents/sessions.

### 5. Docs / checklist (CLAUDE.md change-consistency)
- `cortex/CLAUDE.md` + root `CLAUDE.md`: document `GET /policy/decisions`, the recording behavior,
  `policy:decisions` Redis key + 500 cap, and the ops router consolidation.
- No new env vars required (reuses Redis DB 0). If a dedicated key/db is chosen, add to
  docker-compose + local-setup.{sh,ps1}.

### 6. Verify (in-container — host lacks joblib)
```bash
docker compose exec -T cortex-api python -m pytest tests/ -q
# expect: existing 760 pass + new tests for /policy/decisions and ops router shape
docker compose restart cortex-api
curl -s localhost:8100/ops/workers | jq
curl -s localhost:8100/ops/queues  | jq         # must keep {queues:{celery,event_stream,event_dlq}}
curl -s localhost:8100/policy/decisions | jq
```
- Add `tests/test_policy_api.py` cases for the new endpoint (mocked redis).
- Add an ops-router shape test asserting dashboard-compatible keys.

---

## Risks / gotchas
- **Dashboard breakage** is the #1 risk — the queues shape MUST stay `{queues:{celery,...}}`.
  Test asserts this.
- Recording on EVERY edit decision adds 2 Redis ops (lpush+ltrim) per action — negligible, capped.
- `eval:read` on ops endpoints: fine while AUTH off; if AUTH is later enabled the dashboard needs a
  key for ALL eval-scoped endpoints (pre-existing condition, not introduced here).
- Keep `policy:decisions` on a Redis DB that persists with the rest of operational data (DB 0).
