# Fleet-as-GPU MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the `distill_session` seam into a scheduled, multi-job, measured fleet: cortex enqueues stale-skill re-author and contested-verdict-proposal tasks nightly, the client drains them opportunistically at session start against a local model, and an approval-rate ledger per job type shows on the Autopilot tab.

**Architecture:** Relay gains `POST /tasks` (key-registered, no per-route scope — decision 4). Cortex gains a `fleet` package (ledger + enqueue pass), `origin_job`/`reauthor_of`/`approved_at` on skills, `POST /memory/contested/propose`, and a `fleet` block in the autopilot digest. The client's `nightshift.py` becomes a title-dispatched job catalog; a new `nightshiftdrain.py` spawns it from `session_start` when a local LLM port answers. The dashboard renders the ledger and proposals read-only.

**Tech Stack:** Python 3.11 (FastAPI, Pydantic v2, qdrant-client, redis.asyncio, fakeredis in tests, Celery sync passes, httpx sync for server→relay), stdlib-only client kit (`urllib` via `transport`, `subprocess`, `socket`), vanilla JS dashboard pinned by node-executed tests.

**Spec:** `docs/superpowers/specs/2026-09-02-fleet-as-gpu-mvp-design.md` — read it first; every task below cites a decision number from it.

## Global Constraints

- Client kit stays **stdlib-only**: `nightshift.py` / `nightshiftdrain.py` import only `firekeep_client.{background,hooklog,resolver,state,transport}` and `hooks._mcp` — never `httpx`, `mcp`, `requests`.
- Every fleet output is a **draft or a proposal**: `skill_create(status="draft")`, `POST /memory/contested/propose`. No code in this plan activates a skill, supersedes a memory, or edits an active skill.
- Member-private points (`visibility == "member"`) **never** enter a relay task (spec decision 6).
- `POST /tasks` carries **no** `require_scope_asgi` (spec decision 4) — documented, not accidental.
- Relay task titles are the job ids, exactly: `distill_session`, `reauthor_stale_skill`, `propose_contested_verdict`.
- Ledger keys: `fleet:ledger:<job>` (all-time hash) and `fleet:ledger:<job>:<YYYY-MM-DD>` (daily hash, TTL 400 days). Rejection marker `fleet:rejected:reauthor_stale_skill:<skill_id>` (TTL 90 days). Live marker `fleet:enqueued:<job>:<subject>` (TTL 7 days = relay `TASK_TTL_SECONDS`).
- New cortex settings: `FLEET_ENQUEUE_ENABLED: bool = True`, `FLEET_ENQUEUE_MAX_PER_RUN: int = 20` — declared in `config.py`, mirrored in both compose files, documented in `docs/guides/cortex-configuration.md`.
- New client env/config: `FIREKEEP_NO_AUTO_NIGHTSHIFT` (env off-switch), `[nightshift] auto_drain = false` (config off-switch), `FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS` (default `6`).
- Rates are `null` when the denominator is zero; the dashboard renders `null` as `—`, never `0%`.
- `tests/test_dashboard_autopilot.py::TestRoundOneIsReadOnly` must pass **unchanged**: the Autopilot panel fetches exactly `/autopilot/compliance`, `/autopilot/digest?days=7`, `/autopilot/inbox` and names no write verb.
- Commit after every task with the trailer:
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_01CLPowv3rPbXxBsRXDkNFze`.
- Run tests from the service directory: `cd relay && python -m pytest tests/... -q`, `cd cortex && ...`, `cd client && ...`, repo-level `python -m pytest tests/test_dashboard_autopilot.py -q` from the repo root.

---

## File map

| Area | Create | Modify |
|---|---|---|
| Relay | `relay/tests/test_task_post_route.py` | `relay/app/routes.py` (add `handle_post_task`, `route_post_task`), `relay/app/mcp_server.py` (tool delegates to helper; register POST route), `docs/guides/relay-coordination.md` |
| Cortex config | — | `cortex/app/config.py`, `docker-compose.yml`, `docker-compose.office.yml`, `docs/guides/cortex-configuration.md` |
| Cortex ledger | `cortex/app/fleet/__init__.py`, `cortex/app/fleet/ledger.py`, `cortex/tests/test_fleet_ledger.py` | — |
| Cortex skills | — | `cortex/app/models.py` (`SkillRequest`, `SkillResponse`), `cortex/app/skills/api.py`, `cortex/app/mcp_server.py` (`skill_create`), `cortex/tests/test_skill_api.py` |
| Cortex contested | — | `cortex/app/models.py` (`ContestedProposeRequest`), `cortex/app/lifecycle.py`, `cortex/app/autopilot/inbox.py`, `cortex/tests/test_feedback_and_contested.py`, `cortex/tests/test_autopilot_api.py` |
| Cortex enqueue | `cortex/app/fleet/enqueue.py`, `cortex/tests/test_fleet_enqueue.py` | `cortex/app/workers/memory_agent.py`, `cortex/tests/test_memory_agent.py` |
| Cortex digest | — | `cortex/app/autopilot/digest.py`, `cortex/tests/test_autopilot_api.py` |
| Client worker | — | `client/firekeep_client/nightshift.py`, `client/firekeep_client/cli.py`, `client/tests/test_nightshift.py`, `client/tests/test_cli.py` |
| Client drain | `client/firekeep_client/nightshiftdrain.py`, `client/tests/test_nightshiftdrain.py` | `client/firekeep_client/hooks/session_start.py`, `client/tests/hooks/test_session_start.py` |
| Dashboard | — | `dashboard/index.html`, `tests/test_dashboard_autopilot.py` |
| Docs | — | `docs/guides/client-kit.md`, `docs/guides/knowledge-autopilot.md`, `docs/guides/cortex-api-endpoints.md`, `CLAUDE.md`, `README.md` |
| Site | — | `E:\Documents\Projects\firekeep-site\docs.html` (separate repo, deployed by SSH recipe) |

---

### Task 1: Relay `POST /tasks` with MCP-tool parity

**Files:**
- Modify: `relay/app/routes.py` (after `handle_get_tasks`, ~line 145; and after `route_get_tasks`, ~line 328)
- Modify: `relay/app/mcp_server.py:483-528` (`relay_task_post`), `relay/app/mcp_server.py:1086-1089` (route registration)
- Modify: `docs/guides/relay-coordination.md:14-17`
- Test: `relay/tests/test_task_post_route.py`

**Interfaces:**
- Produces: `async def handle_post_task(redis, *, title: str, assignee: str | None = None, assigner: str = "unknown", description: str = "", priority: str = "normal", files: list[str] | None = None, context: str = "") -> dict` (returns the created task dict; performs create + `tasks`-channel broadcast + `coordination/task_created` replay emit). REST: `POST /tasks` JSON body → `201 {"status": "created", "task": {...}}`, `400 {"error": ...}`, `500 {"error": ...}`. Cortex Task 6 posts to it.

- [ ] **Step 1: Write the failing tests**

```python
# relay/tests/test_task_post_route.py
"""POST /tasks — the REST twin of relay_task_post.

Cortex's nightly fleet pass creates tasks server-side with FIREKEEP_INTERNAL_KEY,
which carries NO relay scope (deploy/bootstrap-keys.sh:197) and cannot be
migrated on deployed keys — so, like every other /tasks, /dm and /presence
route, this one relies on the blanket key middleware and carries no per-route
scope gate (spec decision 4). Parity with the MCP tool is three side effects,
not one: create, the tasks-channel broadcast, the replay emit.
"""
import json
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import app.routes as routes_mod
from app.routes import route_post_task
from app.tasks import list_tasks


def _make_request(body, *, raw: bytes | None = None) -> Request:
    body_bytes = raw if raw is not None else json.dumps(body).encode("utf-8")

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/tasks",
             "headers": [(b"content-type", b"application/json")],
             "path_params": {}, "query_string": b"", "state": {}}
    return Request(scope, receive)


@pytest.fixture
def patched_redis(monkeypatch, redis):
    async def _fake_get_redis():
        return redis
    monkeypatch.setattr(routes_mod, "_get_redis", _fake_get_redis)
    return redis


@pytest.fixture
def effects(monkeypatch):
    """Record the two non-store side effects instead of running them."""
    import app.pubsub as pubsub_mod
    import app.mcp_server as mcp_mod
    bcast, emit = AsyncMock(), AsyncMock()
    monkeypatch.setattr(pubsub_mod, "broadcast", bcast)
    monkeypatch.setattr(mcp_mod, "_replay_emit", emit)
    return bcast, emit


@pytest.mark.asyncio
async def test_creates_task_and_fires_both_side_effects(patched_redis, effects):
    bcast, emit = effects
    resp = await route_post_task(_make_request({
        "title": "reauthor_stale_skill", "assigner": "cortex-fleet",
        "description": "skill_id=s1 workspace_id=ws", "context": "{\"skill_id\": \"s1\"}",
    }))
    assert resp.status_code == 201
    body = json.loads(resp.body)
    assert body["status"] == "created"
    task = body["task"]
    assert task["title"] == "reauthor_stale_skill" and task["status"] == "pending"
    assert task["assigner"] == "cortex-fleet"
    stored = await list_tasks(patched_redis, title="reauthor_stale_skill")
    assert [t["id"] for t in stored] == [task["id"]]
    bcast.assert_awaited_once()
    assert bcast.call_args.args[1] == "tasks"
    assert "New task: reauthor_stale_skill" in bcast.call_args.args[2]
    emit.assert_awaited_once()
    assert emit.call_args.args[0] == "coordination"
    assert emit.call_args.args[1]["action"] == "task_created"
    assert emit.call_args.args[1]["task_id"] == task["id"]


@pytest.mark.asyncio
async def test_assigned_task_broadcasts_the_assignee(patched_redis, effects):
    bcast, _ = effects
    resp = await route_post_task(_make_request({"title": "t", "assignee": "agent-b"}))
    assert resp.status_code == 201
    assert bcast.call_args.args[2] == "Task for agent-b: t"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "   "}, {"title": "x" * 501},
                                  {"title": "ok", "files": "not-a-list"}])
async def test_bad_bodies_are_400_and_touch_no_store(body, monkeypatch, effects):
    spy = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_redis", spy)
    resp = await route_post_task(_make_request(body))
    assert resp.status_code == 400
    assert "error" in json.loads(resp.body)
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_json_is_400(monkeypatch, effects):
    spy = AsyncMock()
    monkeypatch.setattr(routes_mod, "_get_redis", spy)
    resp = await route_post_task(_make_request(None, raw=b"{not json"))
    assert resp.status_code == 400
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_tool_and_route_share_one_helper(monkeypatch, redis):
    """Parity is structural: both paths call handle_post_task, so the three
    side effects cannot drift apart."""
    import app.mcp_server as mcp_mod
    calls = []

    async def fake_helper(r, **kw):
        calls.append(kw)
        return {"id": "task-1", "title": kw["title"], "status": "pending"}

    monkeypatch.setattr(routes_mod, "handle_post_task", fake_helper)

    async def _r():
        return redis
    monkeypatch.setattr(mcp_mod, "get_redis", _r)
    out = await mcp_mod.relay_task_post(title="via-tool", assigner="a1")
    assert out["status"] == "created" and calls[-1]["title"] == "via-tool"
    assert calls[-1]["assigner"] == "a1"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd relay && python -m pytest tests/test_task_post_route.py -q`
Expected: FAIL — `ImportError: cannot import name 'route_post_task'`.

- [ ] **Step 3: Add the helper and the route in `relay/app/routes.py`**

After `handle_get_tasks` (keep the existing extracted-handler section):

```python
async def handle_post_task(
    redis, *, title: str, assignee: str | None = None, assigner: str = "unknown",
    description: str = "", priority: str = "normal", files: list[str] | None = None,
    context: str = "",
) -> dict:
    """Create a task WITH the two side effects the MCP tool has always had.

    One helper for both the tool and the REST route: parity is three effects
    (store, tasks-channel broadcast, coordination/task_created replay emit),
    and a route that did only the first would create tasks nobody is told
    about. Lazy imports keep routes.py free of the mcp_server import cycle
    (`_get_redis` below does the same).
    """
    from app.tasks import create_task
    from app.pubsub import broadcast
    from app.config import get_settings
    from app.mcp_server import _replay_emit

    task = await create_task(redis, title, assignee, assigner, description, priority, files, context)
    msg = f"Task for {assignee}: {title}" if assignee else f"New task: {title}"
    await broadcast(
        redis, "tasks", msg, assigner, ["task-assigned"],
        backlog_size=get_settings().CHANNEL_BACKLOG_SIZE,
        backlog_ttl_seconds=get_settings().BULLETIN_TTL_HOURS * 3600,
    )
    await _replay_emit(
        "coordination",
        {"action": "task_created", "task_id": task["id"], "assignee": assignee or ""},
        agent_id=assigner,
    )
    return task
```

After `route_get_tasks`:

```python
async def route_post_task(request: Request) -> JSONResponse:
    """POST /tasks — REST twin of relay_task_post, for server-side enqueue.

    Auth is the blanket key middleware and deliberately NO per-route scope
    (same as GET/DELETE /tasks, /dm/*, /presence/*): cortex's internal key
    carries no relay scope and deployed keys cannot be re-scoped in place
    (spec 2026-09-02 fleet-as-gpu, decision 4).
    """
    try:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — malformed body is the caller's fault
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        title = str(body.get("title") or "").strip()
        if not title:
            return JSONResponse({"error": "title is required"}, status_code=400)
        if len(title) > 500:
            return JSONResponse({"error": "Title too long (max 500 chars)"}, status_code=400)
        files = body.get("files")
        if files is not None and not (
            isinstance(files, list) and all(isinstance(f, str) for f in files)
        ):
            return JSONResponse({"error": "files must be a list of strings"}, status_code=400)
        r = await _get_redis()
        task = await handle_post_task(
            r, title=title, assignee=(body.get("assignee") or None),
            assigner=str(body.get("assigner") or "unknown"),
            description=str(body.get("description") or ""),
            priority=str(body.get("priority") or "normal"),
            files=files, context=str(body.get("context") or ""),
        )
        return JSONResponse({"status": "created", "task": task}, status_code=201)
    except Exception as e:
        logger.error("POST /tasks failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 4: Make the MCP tool delegate, and register the route (`relay/app/mcp_server.py`)**

Replace the body of `relay_task_post` (keep signature + docstring):

```python
    try:
        if len(title) > 500:
            return {"error": "Title too long (max 500 chars)"}
        r = await get_redis()
        from app.routes import handle_post_task
        task = await handle_post_task(
            r, title=title, assignee=assignee, assigner=assigner,
            description=description, priority=priority, files=files, context=context,
        )
        return {"status": "created", "task": task}
    except Exception as e:
        logger.error("relay_task_post failed: %s", e)
        return {"error": str(e), "status": "unavailable"}
```

Beside `_route_get_tasks` add:

```python
@mcp.custom_route("/tasks", methods=["POST"], name="post_task")
async def _route_post_task(request: StarletteRequest) -> StarletteJSONResponse:
    from app.routes import route_post_task
    return await route_post_task(request)
```

- [ ] **Step 5: Run the new tests and the whole relay suite**

Run: `cd relay && python -m pytest tests/test_task_post_route.py -q && python -m pytest tests -q`
Expected: new file PASS; suite 182 + 6 passed (the parity test patches `handle_post_task` on `app.routes`, which the tool imports lazily — if it fails, the tool is not importing from `app.routes` inside the function).

- [ ] **Step 6: Document the REST line**

In `docs/guides/relay-coordination.md`, after the Task Queue `**MCP Tools:**` line (line 17) add:

```markdown
**REST Endpoints (on Relay :8050):** `GET /tasks?assignee=&status=&title=&oldest_first=&limit=`, `POST /tasks` (JSON `{title, assignee?, assigner?, description?, priority?, files?, context?}` → `201 {status, task}`; creates the task AND fires the same `tasks`-channel broadcast + `coordination/task_created` replay emit as `relay_task_post` — one shared helper, `app/routes.py::handle_post_task`), `DELETE /tasks/{task_id}`. **Auth on these is the blanket key middleware, deliberately with no per-route `relay:*` scope** (unlike the Scope-session routes): the server-side caller is cortex's `fleet_enqueue_pass` using `FIREKEEP_INTERNAL_KEY`, which is minted with `memory:write, session:read, eval:read, eval:write` and no relay scope, and `deploy/bootstrap-keys.sh`'s `ensure_env_key` does not reconcile scopes on already-provisioned keys — a `relay:write` gate would 403 every existing deployment. Every agent key can already create tasks over MCP, so the route grants nothing new. Follow-up that would allow gating later: a scope-reconciling bootstrap step.
```

- [ ] **Step 7: Commit**

```bash
git add relay/app/routes.py relay/app/mcp_server.py relay/tests/test_task_post_route.py docs/guides/relay-coordination.md
git commit -m "feat(relay): POST /tasks — REST twin of relay_task_post for server-side enqueue"
```

---

### Task 2: Cortex settings + compose + config doc for the fleet pass

**Files:**
- Modify: `cortex/app/config.py` (after `SKILL_STALE_AFTER_DAYS`, ~line 581)
- Modify: `docker-compose.yml` (every cortex service block that carries `PROCEDURE_ENABLED`: lines ~429, ~677, ~781), `docker-compose.office.yml` (~532)
- Modify: `docs/guides/cortex-configuration.md` (append a bullet in the skills/autopilot area)
- Test: `cortex/tests/test_config_fleet.py`

**Interfaces:**
- Produces: `Settings.FLEET_ENQUEUE_ENABLED: bool = True`, `Settings.FLEET_ENQUEUE_MAX_PER_RUN: int = 20` (read by Task 6 and Task 7).

- [ ] **Step 1: Write the failing test**

```python
# cortex/tests/test_config_fleet.py
"""Fleet-as-GPU settings exist, default as documented, and are wired in compose."""
from pathlib import Path

from app.config import Settings

REPO = Path(__file__).resolve().parents[2]


def test_defaults():
    s = Settings(_env_file=None)
    assert s.FLEET_ENQUEUE_ENABLED is True
    assert s.FLEET_ENQUEUE_MAX_PER_RUN == 20


def test_env_override(monkeypatch):
    monkeypatch.setenv("FLEET_ENQUEUE_ENABLED", "false")
    monkeypatch.setenv("FLEET_ENQUEUE_MAX_PER_RUN", "5")
    s = Settings(_env_file=None)
    assert s.FLEET_ENQUEUE_ENABLED is False and s.FLEET_ENQUEUE_MAX_PER_RUN == 5


def test_compose_and_docs_carry_the_flags():
    for f in ("docker-compose.yml", "docker-compose.office.yml"):
        text = (REPO / f).read_text(encoding="utf-8")
        assert "FLEET_ENQUEUE_ENABLED: ${FLEET_ENQUEUE_ENABLED:-true}" in text, f
        assert "FLEET_ENQUEUE_MAX_PER_RUN: ${FLEET_ENQUEUE_MAX_PER_RUN:-20}" in text, f
    guide = (REPO / "docs/guides/cortex-configuration.md").read_text(encoding="utf-8")
    assert "`FLEET_ENQUEUE_ENABLED` (default `true`)" in guide
    assert "`FLEET_ENQUEUE_MAX_PER_RUN` (default `20`)" in guide
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd cortex && python -m pytest tests/test_config_fleet.py -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'FLEET_ENQUEUE_ENABLED'`.

- [ ] **Step 3: Declare the settings** (`cortex/app/config.py`, right after `SKILL_STALE_AFTER_DAYS`)

```python
    # Fleet-as-GPU (spec 2026-09-02): the nightly memory agent posts ONE relay task
    # per stale skill (`reauthor_stale_skill`) and per contested pair
    # (`propose_contested_verdict`) for client Night Shift workers to drain against
    # a LOCAL model. Default ON because every output is a draft or a proposal
    # behind human review; the cap bounds a night's postings so a large backlog
    # cannot flood the relay queue. Dedup is state-based (see app/fleet/enqueue.py).
    FLEET_ENQUEUE_ENABLED: bool = True
    FLEET_ENQUEUE_MAX_PER_RUN: int = 20
```

- [ ] **Step 4: Mirror in compose** — in each cortex service env block that already has `PROCEDURE_ENABLED: ${PROCEDURE_ENABLED:-false}` (docker-compose.yml ×3, docker-compose.office.yml ×1) add directly under it:

```yaml
      FLEET_ENQUEUE_ENABLED: ${FLEET_ENQUEUE_ENABLED:-true}
      FLEET_ENQUEUE_MAX_PER_RUN: ${FLEET_ENQUEUE_MAX_PER_RUN:-20}
```

- [ ] **Step 5: Document** — in `docs/guides/cortex-configuration.md`, add a bullet next to the `SKILL_STALE_AFTER_DAYS` / skills bullets:

```markdown
- `FLEET_ENQUEUE_ENABLED` (default `true`) — Fleet-as-GPU: the nightly memory agent's `fleet_enqueue_pass` (`app/fleet/enqueue.py`, runs after `skill_staleness`) posts one relay task per stale active skill (`reauthor_stale_skill`) and per contested pair (`propose_contested_verdict`) through relay's `POST /tasks`, using `RELAY_URL` + `FIREKEEP_INTERNAL_KEY`. Client Night Shift workers drain them against a local model; every result is a draft skill or a verdict *proposal* behind human review. Dedup is state-based — a skill with a pending re-author draft or a pair that already carries a proposal is never re-enqueued; member-private points (`visibility=member`) are never enqueued at all. `FLEET_ENQUEUE_MAX_PER_RUN` (default `20`) — hard cap on tasks posted per nightly run; the remainder waits for the next night (reported as `capped` in the pass result). See `docs/guides/knowledge-autopilot.md` §8.
```

- [ ] **Step 6: Run the test and the compose guards**

Run: `cd cortex && python -m pytest tests/test_config_fleet.py -q && cd .. && python -m pytest tests/test_no_dead_config.py tests/test_compose_security_defaults.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add cortex/app/config.py docker-compose.yml docker-compose.office.yml docs/guides/cortex-configuration.md cortex/tests/test_config_fleet.py
git commit -m "feat(cortex): FLEET_ENQUEUE_ENABLED / FLEET_ENQUEUE_MAX_PER_RUN settings"
```

---

### Task 3: The fleet ledger (`cortex/app/fleet/ledger.py`)

**Files:**
- Create: `cortex/app/fleet/__init__.py` (docstring-only module), `cortex/app/fleet/ledger.py`
- Test: `cortex/tests/test_fleet_ledger.py`

**Interfaces:**
- Produces (all async, take `redis_client` = the app's `redis.asyncio` client; writers are best-effort and never raise):
  - constants `JOB_DISTILL = "distill_session"`, `JOB_REAUTHOR = "reauthor_stale_skill"`, `JOB_VERDICT = "propose_contested_verdict"`, `JOBS`, `SKILL_COUNTERS = ("produced", "approved", "rejected")`, `VERDICT_COUNTERS = ("proposed", "resolved", "matched")`
  - `async def record(redis_client, job: str, counter: str, *, now: datetime | None = None) -> bool`
  - `async def mark_rejected_reauthor(redis_client, skill_id: str) -> None`, `def rejected_reauthor_key(skill_id) -> str`
  - `async def summarize(redis_client, *, days: int, now: datetime | None = None) -> dict[str, dict]` → `{job: {"window": {...counters, "approval_rate"|"match_rate"}, "all_time": {...counters, rate, "pending"?}}}` (raises on Redis failure — the digest guards it)
  - `def rate(numer: int, denom: int) -> float | None`
  - `def total_key(job) -> str`, `def day_key(job, day: str) -> str`, `LIVE_MARKER_TTL_SECONDS = 7 * 86400`, `def live_marker_key(job, subject) -> str` (used synchronously by Task 6)

- [ ] **Step 1: Write the failing tests**

```python
# cortex/tests/test_fleet_ledger.py
"""The fleet ledger: the store forgets (rejection is deletion, no approval
timestamp existed), so approval rates come from monotonic counters."""
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fr
import pytest
import pytest_asyncio

from app.fleet import ledger

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def redis():
    r = fr.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_record_increments_total_and_daily_with_ttl(redis):
    assert await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=NOW) is True
    assert await redis.hget(ledger.total_key(ledger.JOB_REAUTHOR), "produced") == "1"
    day = ledger.day_key(ledger.JOB_REAUTHOR, "2026-09-02")
    assert await redis.hget(day, "produced") == "1"
    assert 0 < await redis.ttl(day) <= 400 * 86400


@pytest.mark.asyncio
async def test_unknown_job_or_counter_is_ignored(redis):
    assert await ledger.record(redis, "not_a_job", "produced") is False
    assert await ledger.record(redis, ledger.JOB_REAUTHOR, "bogus") is False
    assert await redis.keys("fleet:*") == []


@pytest.mark.asyncio
async def test_record_never_raises_without_redis():
    assert await ledger.record(None, ledger.JOB_REAUTHOR, "produced") is False


def test_rate_is_null_on_zero_denominator():
    assert ledger.rate(0, 0) is None
    assert ledger.rate(3, 4) == 0.75


@pytest.mark.asyncio
async def test_summarize_windows_by_day_and_reports_all_time(redis):
    old = NOW - timedelta(days=10)
    for _ in range(4):
        await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=old)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "approved", now=old)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=NOW)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "rejected", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "proposed", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "resolved", now=NOW)
    await ledger.record(redis, ledger.JOB_VERDICT, "matched", now=NOW)

    out = await ledger.summarize(redis, days=7, now=NOW)
    re = out[ledger.JOB_REAUTHOR]
    assert re["window"] == {"produced": 1, "approved": 0, "rejected": 1, "approval_rate": 0.0}
    assert re["all_time"] == {"produced": 5, "approved": 1, "rejected": 1,
                              "approval_rate": 0.5, "pending": 3}
    v = out[ledger.JOB_VERDICT]
    assert v["window"] == {"proposed": 1, "resolved": 1, "matched": 1, "match_rate": 1.0}
    assert v["all_time"]["match_rate"] == 1.0
    # A job with no activity still appears, with null rates — the dashboard must
    # show "not enough evidence", never a missing row or 0%.
    d = out[ledger.JOB_DISTILL]
    assert d["window"]["approval_rate"] is None and d["all_time"]["pending"] == 0


@pytest.mark.asyncio
async def test_rejected_reauthor_marker(redis):
    await ledger.mark_rejected_reauthor(redis, "sk-1")
    key = ledger.rejected_reauthor_key("sk-1")
    assert await redis.exists(key) == 1
    assert 0 < await redis.ttl(key) <= 90 * 86400
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd cortex && python -m pytest tests/test_fleet_ledger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.fleet'`.

- [ ] **Step 3: Implement**

`cortex/app/fleet/__init__.py`:

```python
"""Fleet-as-GPU server side: the approval ledger and the nightly enqueue pass.

Spec: docs/superpowers/specs/2026-09-02-fleet-as-gpu-mvp-design.md.
"""
```

`cortex/app/fleet/ledger.py`:

```python
"""Per-job-type approval counters for fleet output (spec decision 7).

WHY A LEDGER AND NOT A QUERY. Rejection of a draft skill is DELETION, and no
approval timestamp existed before this feature, so an approval rate read from
Qdrant would lose every rejected draft and flatter the fleet. These are
monotonic Redis counters written at the moments the store forgets: create
(`produced`), draft->active (`approved`), delete-while-draft (`rejected`), a
first verdict proposal (`proposed`), a human verdict on a pair that carried a
proposal (`resolved`, plus `matched` when the human agreed). All-time hash plus
one hash per UTC day (400-day TTL) so the digest can window them.

Every writer is best-effort: a Redis hiccup must never fail the skill write or
the verdict that triggered it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

JOB_DISTILL = "distill_session"
JOB_REAUTHOR = "reauthor_stale_skill"
JOB_VERDICT = "propose_contested_verdict"
JOBS = (JOB_DISTILL, JOB_REAUTHOR, JOB_VERDICT)

SKILL_COUNTERS = ("produced", "approved", "rejected")
VERDICT_COUNTERS = ("proposed", "resolved", "matched")
_COUNTERS = {JOB_DISTILL: SKILL_COUNTERS, JOB_REAUTHOR: SKILL_COUNTERS,
             JOB_VERDICT: VERDICT_COUNTERS}

LEDGER_PREFIX = "fleet:ledger"
DAILY_TTL_SECONDS = 400 * 86400
REJECTED_TTL_SECONDS = 90 * 86400
# Equals relay's TASK_TTL_SECONDS: an in-flight marker must not outlive the task it guards.
LIVE_MARKER_TTL_SECONDS = 7 * 86400


def total_key(job: str) -> str:
    return f"{LEDGER_PREFIX}:{job}"


def day_key(job: str, day: str) -> str:
    return f"{LEDGER_PREFIX}:{job}:{day}"


def rejected_reauthor_key(skill_id: str) -> str:
    return f"fleet:rejected:{JOB_REAUTHOR}:{skill_id}"


def live_marker_key(job: str, subject: str) -> str:
    return f"fleet:enqueued:{job}:{subject}"


def rate(numer: int, denom: int) -> float | None:
    """None when there is no evidence — a rate is never invented from a prior."""
    return None if denom <= 0 else round(numer / denom, 3)


async def record(redis_client, job: str, counter: str, *, now: datetime | None = None) -> bool:
    if redis_client is None or job not in _COUNTERS or counter not in _COUNTERS[job]:
        return False
    try:
        now = now or datetime.now(timezone.utc)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        pipe = redis_client.pipeline()
        pipe.hincrby(total_key(job), counter, 1)
        pipe.hincrby(day_key(job, day), counter, 1)
        pipe.expire(day_key(job, day), DAILY_TTL_SECONDS)
        await pipe.execute()
        return True
    except Exception as exc:  # noqa: BLE001 — bookkeeping never fails the caller
        logger.warning("fleet ledger write skipped (%s/%s): %s", job, counter, exc)
        return False


async def mark_rejected_reauthor(redis_client, skill_id: str) -> None:
    if redis_client is None or not skill_id:
        return
    try:
        await redis_client.set(rejected_reauthor_key(skill_id), "1", ex=REJECTED_TTL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet rejection marker skipped for %s: %s", skill_id, exc)


def _ints(raw: dict | None, counters: tuple[str, ...]) -> dict[str, int]:
    raw = raw or {}
    out: dict[str, int] = {}
    for c in counters:
        v = raw.get(c)
        if isinstance(v, bytes):
            v = v.decode()
        try:
            out[c] = int(v or 0)
        except (TypeError, ValueError):
            out[c] = 0
    return out


def _with_rate(counts: dict[str, int], job: str) -> dict:
    if job == JOB_VERDICT:
        return {**counts, "match_rate": rate(counts["matched"], counts["resolved"])}
    return {**counts, "approval_rate": rate(counts["approved"],
                                            counts["approved"] + counts["rejected"])}


async def summarize(redis_client, *, days: int, now: datetime | None = None) -> dict[str, dict]:
    """Window (last `days` UTC days, today included) and all-time, per job.

    Raises on a Redis failure — the digest's per-source guard owns the catch,
    so a dead Redis degrades the fleet block in place like every other source.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    days = max(1, int(days))
    out: dict[str, dict] = {}
    for job in JOBS:
        counters = _COUNTERS[job]
        window = {c: 0 for c in counters}
        for i in range(days):
            day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            for c, v in _ints(await redis_client.hgetall(day_key(job, day)), counters).items():
                window[c] += v
        total = _ints(await redis_client.hgetall(total_key(job)), counters)
        all_time = _with_rate(total, job)
        if job != JOB_VERDICT:
            all_time["pending"] = max(0, total["produced"] - total["approved"] - total["rejected"])
        out[job] = {"window": _with_rate(window, job), "all_time": all_time}
    return out
```

- [ ] **Step 4: Run**

Run: `cd cortex && python -m pytest tests/test_fleet_ledger.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

Stage `cortex/app/fleet` and `cortex/tests/test_fleet_ledger.py`; message `feat(cortex): fleet ledger — per-job produced/approved/rejected and proposal counters`.

---

### Task 4: Skills carry `origin_job` / `reauthor_of`; activation stamps `approved_at`; ledger hooks

**Files:**
- Modify: `cortex/app/models.py` (`SkillRequest` ~472, `SkillResponse` ~500)
- Modify: `cortex/app/skills/api.py` (`create_skill` ~150-230, `patch_skill` ~232-343, `delete_skill` ~345-355, `_point_to_response` ~408)
- Modify: `cortex/app/mcp_server.py` (`skill_create` ~1349-1429)
- Test: `cortex/tests/test_skill_api.py` (append), `cortex/tests/test_mcp_skill_create_fleet.py`

**Interfaces:**
- Consumes: `app.fleet.ledger.record`, `mark_rejected_reauthor`, `JOB_REAUTHOR`.
- Produces: `POST /skills` accepts `origin_job` (`^[a-z][a-z0-9_]{0,63}$`) and `reauthor_of` (1-128 chars; **404** `reauthor_of skill not found` when no skill by that id exists or its `workspace_id` differs from the caller's); payload keys `origin_job`, `reauthor_of`, `approved_at`; `SkillResponse` exposes all three; MCP `skill_create(..., origin_job: str | None = None, reauthor_of: str | None = None)` forwards them when truthy (Task 8 relies on this exact spelling).

- [ ] **Step 1: Write the failing tests** — append to `cortex/tests/test_skill_api.py` (it already defines `mock_vector`, `mock_settings`, `_make_app(mock_vector, mock_settings, redis_client=None)`, `_make_mock_point(skill_id, trigger, status)`):

```python
# --- Fleet-as-GPU: origin_job / reauthor_of / approved_at / ledger ---------
import asyncio as _asyncio

import fakeredis.aioredis as _fr


def _fleet_client(mock_vector, mock_settings):
    r = _fr.FakeRedis(decode_responses=True)
    return TestClient(_make_app(mock_vector, mock_settings, redis_client=r)), r


def _await(coro):
    return _asyncio.get_event_loop().run_until_complete(coro)


def _skill_body(**extra):
    return {"trigger": "t", "symptoms": "s", "steps": "do it", "status": "draft", **extra}


def test_create_stores_origin_and_reauthor_and_counts_produced(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    mock_vector._client.retrieve = AsyncMock(return_value=[_make_mock_point("old-1")])
    resp = client.post("/skills", json=_skill_body(origin_job="reauthor_stale_skill",
                                                   reauthor_of="old-1"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["origin_job"] == "reauthor_stale_skill"
    assert resp.json()["reauthor_of"] == "old-1"
    payload = mock_vector._client.upsert.call_args.kwargs["points"][0].payload
    assert payload["origin_job"] == "reauthor_stale_skill" and payload["reauthor_of"] == "old-1"
    assert _await(r.hget("fleet:ledger:reauthor_stale_skill", "produced")) == "1"


def test_create_without_origin_writes_no_ledger_and_no_keys(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    assert client.post("/skills", json=_skill_body()).status_code == 201
    payload = mock_vector._client.upsert.call_args.kwargs["points"][0].payload
    assert "origin_job" not in payload and "reauthor_of" not in payload
    assert _await(r.keys("fleet:*")) == []


@pytest.mark.parametrize("bad", ["Reauthor", "1abc", "has-dash", "x" * 65])
def test_origin_job_pattern_is_enforced(mock_vector, mock_settings, bad):
    client, _ = _fleet_client(mock_vector, mock_settings)
    assert client.post("/skills", json=_skill_body(origin_job=bad)).status_code == 422


def test_reauthor_of_unknown_skill_is_404(mock_vector, mock_settings):
    client, _ = _fleet_client(mock_vector, mock_settings)
    mock_vector._client.retrieve = AsyncMock(return_value=[])
    resp = client.post("/skills", json=_skill_body(origin_job="reauthor_stale_skill",
                                                   reauthor_of="ghost"))
    assert resp.status_code == 404
    mock_vector._client.upsert.assert_not_called()


def test_reauthor_of_other_workspace_is_404(mock_vector, mock_settings, monkeypatch):
    client, _ = _fleet_client(mock_vector, mock_settings)
    other = _make_mock_point("old-2")
    other.payload["workspace_id"] = "ws-other"
    mock_vector._client.retrieve = AsyncMock(return_value=[other])
    monkeypatch.setattr("auth.principal.request_principal",
                        lambda req: {"workspace_id": "ws-mine", "member_id": "m1"})
    assert client.post("/skills", json=_skill_body(reauthor_of="old-2")).status_code == 404


def test_activation_stamps_approved_at_once_and_counts_approved(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    draft = _make_mock_point("d1", status="draft")
    draft.payload["origin_job"] = "reauthor_stale_skill"
    mock_vector._client.retrieve = AsyncMock(return_value=[draft])
    resp = client.patch("/skills/d1", json={"skill_status": "active"})
    assert resp.status_code == 200, resp.text
    written = mock_vector._client.set_payload.call_args.kwargs["payload"]
    assert written["skill_status"] == "active" and written["approved_at"]
    assert _await(r.hget("fleet:ledger:reauthor_stale_skill", "approved")) == "1"
    # Re-PATCHing an already-active skill neither re-stamps nor double-counts.
    draft.payload.update(written)
    client.patch("/skills/d1", json={"skill_status": "active"})
    assert _await(r.hget("fleet:ledger:reauthor_stale_skill", "approved")) == "1"
    assert "approved_at" not in mock_vector._client.set_payload.call_args.kwargs["payload"]


def test_activation_of_a_plain_skill_stamps_but_does_not_count(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    mock_vector._client.retrieve = AsyncMock(return_value=[_make_mock_point("p1", status="draft")])
    client.patch("/skills/p1", json={"skill_status": "active"})
    assert mock_vector._client.set_payload.call_args.kwargs["payload"]["approved_at"]
    assert _await(r.keys("fleet:*")) == []


def test_deleting_a_fleet_draft_counts_rejected_and_marks_the_original(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    draft = _make_mock_point("d2", status="draft")
    draft.payload.update({"origin_job": "reauthor_stale_skill", "reauthor_of": "old-9"})
    mock_vector._client.retrieve = AsyncMock(return_value=[draft])
    assert client.delete("/skills/d2").status_code == 204
    mock_vector._client.delete.assert_awaited_once()
    assert _await(r.hget("fleet:ledger:reauthor_stale_skill", "rejected")) == "1"
    assert _await(r.exists("fleet:rejected:reauthor_stale_skill:old-9")) == 1


def test_deleting_an_active_fleet_skill_is_not_a_rejection(mock_vector, mock_settings):
    client, r = _fleet_client(mock_vector, mock_settings)
    active = _make_mock_point("a2", status="active")
    active.payload["origin_job"] = "reauthor_stale_skill"
    mock_vector._client.retrieve = AsyncMock(return_value=[active])
    assert client.delete("/skills/a2").status_code == 204
    assert _await(r.keys("fleet:ledger:*")) == []
```

`cortex/tests/test_mcp_skill_create_fleet.py` — read `cortex/app/mcp_server.py:1349-1429` first to see how `skill_create` obtains its HTTP client (a module-level helper or an inline `httpx.AsyncClient`), then patch THAT so the test captures the posted body:

```python
"""skill_create forwards origin_job / reauthor_of to POST /skills only when given."""
import inspect

import pytest

from app import mcp_server


def test_signature_has_the_two_optional_params():
    sig = inspect.signature(mcp_server.skill_create)
    assert sig.parameters["origin_job"].default is None
    assert sig.parameters["reauthor_of"].default is None


class _Resp:
    status_code = 201

    def json(self):
        return {"id": "sk-new", "trigger": "t", "skill_status": "draft"}

    def raise_for_status(self):
        return None


class _Client:
    sent: dict = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, path, json=None, headers=None, **_k):
        _Client.sent = {"path": path, "json": json, "headers": headers}
        return _Resp()


@pytest.mark.asyncio
async def test_body_carries_them_only_when_truthy(monkeypatch):
    # Patch the constructor skill_create actually uses (see file header).
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _Client)
    await mcp_server.skill_create("t", "s", "steps", status="draft",
                                  origin_job="reauthor_stale_skill", reauthor_of="old-1")
    assert _Client.sent["json"]["origin_job"] == "reauthor_stale_skill"
    assert _Client.sent["json"]["reauthor_of"] == "old-1"
    await mcp_server.skill_create("t", "s", "steps", status="draft")
    assert "origin_job" not in _Client.sent["json"] and "reauthor_of" not in _Client.sent["json"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd cortex && python -m pytest tests/test_skill_api.py -q -k "origin or reauthor or approved or rejected or rejection" && python -m pytest tests/test_mcp_skill_create_fleet.py -q`
Expected: FAIL — unknown fields are ignored today so the payload assertions fail; `KeyError: 'origin_job'` in the signature test.

- [ ] **Step 3: Models** (`cortex/app/models.py`)

In `SkillRequest`, after `step_specs`:

```python
    # Fleet-as-GPU (spec 2026-09-02): which fleet job produced this draft, and — for
    # a stale-skill re-author — which skill it rewrites. origin_job feeds the
    # approval ledger; reauthor_of is validated server-side against the caller's
    # workspace (404 otherwise) so a worker cannot draft across a tenancy boundary.
    origin_job: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    reauthor_of: str | None = Field(default=None, min_length=1, max_length=128)
```

In `SkillResponse`, after `step_specs`:

```python
    # Fleet-as-GPU: absent on every pre-existing skill (None, no migration).
    origin_job: str | None = None
    reauthor_of: str | None = None
    # A REAL approval timestamp (draft->active), stamped for every skill from this
    # release on. stale_reviewed_at is NOT this: it is also written by "still valid".
    approved_at: str | None = None
```

- [ ] **Step 4: `create_skill`** (`cortex/app/skills/api.py`) — after `payload["member_id"] = principal["member_id"]`, before the `upsert`:

```python
        if req.reauthor_of:
            # The original must exist and belong to the caller's workspace — a
            # Night Shift worker enrolled elsewhere fails here, visibly, instead of
            # drafting across the tenancy boundary (spec decision 6).
            original = await vector._client.retrieve(
                collection_name=settings.QDRANT_COLLECTION, ids=[req.reauthor_of],
                with_payload=True, with_vectors=False,
            )
            orig_ws = ((original[0].payload or {}).get("workspace_id") if original else None)
            if not original or (orig_ws and principal.get("workspace_id")
                                and orig_ws != principal["workspace_id"]):
                raise HTTPException(status_code=404, detail="reauthor_of skill not found")
            payload["reauthor_of"] = req.reauthor_of
        if req.origin_job:
            payload["origin_job"] = req.origin_job
```

After the `upsert` (before the procedure-index rebuild block):

```python
        if req.origin_job and req.status == "draft":
            from app.fleet import ledger as _ledger
            await _ledger.record(getattr(request.app.state, "redis_client", None),
                                 req.origin_job, "produced")
```

Extend the returned `SkillResponse(...)` with `origin_job=payload.get("origin_job"), reauthor_of=payload.get("reauthor_of"),`.

- [ ] **Step 5: `patch_skill`** — replace the `if req.skill_status is not None:` block with:

```python
        current = points[0].payload or {}
        if req.skill_status is not None:
            updates["skill_status"] = req.skill_status
            if req.skill_status == "active":
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                # Promoting to active is a human blessing — stamp freshness so the
                # staleness sweep gives the newly-active skill a full window.
                updates["stale_reviewed_at"] = now_iso
                if current.get("skill_status") != "active":
                    # The REAL approval timestamp (Fleet-as-GPU spec decision 7):
                    # stamped once, on the draft->active transition only.
                    updates["approved_at"] = now_iso
                    if current.get("origin_job") and not current.get("approved_at"):
                        from app.fleet import ledger as _ledger
                        await _ledger.record(getattr(request.app.state, "redis_client", None),
                                             current["origin_job"], "approved")
```

- [ ] **Step 6: `delete_skill`** — new signature and body:

```python
    @router.delete("/skills/{skill_id}", status_code=204,
                  dependencies=[Depends(require_not_frozen)])
    async def delete_skill(
        skill_id: str,
        request: Request,
        vector: VectorClient = Depends(get_vector),
    ):
        settings = settings_fn()
        # Deleting a fleet DRAFT is the human saying "no" — the only rejection
        # signal that exists, and it vanishes with the point, so record it first.
        try:
            points = await vector._client.retrieve(
                collection_name=settings.QDRANT_COLLECTION, ids=[skill_id],
                with_payload=True, with_vectors=False,
            )
            current = (points[0].payload or {}) if points else {}
        except Exception:  # noqa: BLE001 — a lookup failure must not block the delete
            current = {}
        if current.get("origin_job") and current.get("skill_status") == "draft":
            from app.fleet import ledger as _ledger
            _r = getattr(request.app.state, "redis_client", None)
            await _ledger.record(_r, current["origin_job"], "rejected")
            if current["origin_job"] == _ledger.JOB_REAUTHOR and current.get("reauthor_of"):
                await _ledger.mark_rejected_reauthor(_r, current["reauthor_of"])
        await vector._client.delete(
            collection_name=settings.QDRANT_COLLECTION,
            points_selector=PointIdsList(points=[skill_id]),
        )
```

- [ ] **Step 7: `_point_to_response`** — add `origin_job=p.get("origin_job"), reauthor_of=p.get("reauthor_of"), approved_at=p.get("approved_at"),`.

- [ ] **Step 8: MCP `skill_create`** (`cortex/app/mcp_server.py`) — add parameters `origin_job: str | None = None, reauthor_of: str | None = None` after `step_specs`; document them in the docstring (`origin_job`: the fleet job that produced this draft, e.g. `reauthor_stale_skill`; `reauthor_of`: id of the stale skill being rewritten — server rejects one outside your workspace); after the `step_specs` body line add:

```python
    if origin_job:
        body["origin_job"] = origin_job
    if reauthor_of:
        body["reauthor_of"] = reauthor_of
```

- [ ] **Step 9: Run**

Run: `cd cortex && python -m pytest tests/test_skill_api.py tests/test_mcp_skill_create_fleet.py tests/test_skill_step_specs.py -q`
Expected: PASS.

- [ ] **Step 10: Commit**

Stage `cortex/app/models.py cortex/app/skills/api.py cortex/app/mcp_server.py cortex/tests/test_skill_api.py cortex/tests/test_mcp_skill_create_fleet.py`; message `feat(cortex): skills carry origin_job/reauthor_of, activation stamps approved_at, ledger hooks`.

---

### Task 5: `POST /memory/contested/propose`, resolve learns about proposals, inbox rows carry them

**Files:**
- Modify: `cortex/app/models.py` (after `ContestedResolveRequest` ~line 295)
- Modify: `cortex/app/lifecycle.py` (`resolve_contested` ~275-368; new route after it)
- Modify: `cortex/app/autopilot/inbox.py` (`contested_memories` ~194-222)
- Test: `cortex/tests/test_feedback_and_contested.py` (append), `cortex/tests/test_autopilot_api.py` (append one test)

**Interfaces:**
- Consumes: `app.fleet.ledger.record`, `JOB_VERDICT`.
- Produces: `POST /memory/contested/propose` body `{winner_id, loser_id, action: "supersede"|"coexist", rationale: str ≤1000}` → `200 {"status": "proposed", "action", "winner_id", "loser_id", "first": bool}`; `404` unknown memory; `409` pair not mutually contested. Writes on both points: `proposed_verdict: {"action", "winner_id": <id|None>}`, `proposed_rationale`, `proposed_by` (X-Agent-Id header, else identity agent_id, else `"unknown"`), `proposed_at`. Task 8's worker posts here with the member key. Inbox `contested_memories.pairs[]` rows gain `proposed_verdict`, `proposed_rationale`, `proposed_by`, `proposed_at` (Task 10 renders them).

- [ ] **Step 1: Write the failing tests** — append to `cortex/tests/test_feedback_and_contested.py` (it has `lifecycle_client`, `mock_vector`, `mock_graph`, `_real_shape`, `_contested_pair`):

```python
# ---------------------------------------------------------------------------
# /memory/contested/propose (Fleet-as-GPU) + resolve bookkeeping
# ---------------------------------------------------------------------------
import asyncio as _asyncio

import fakeredis.aioredis as _fr
from fastapi import FastAPI as _FastAPI
from fastapi.testclient import TestClient as _TestClient


def _ledger_client(mock_graph, mock_vector):
    r = _fr.FakeRedis(decode_responses=True)
    app = _FastAPI()
    app.include_router(create_lifecycle_router(graph=mock_graph, vector=mock_vector,
                                               redis_client=r))
    return _TestClient(app, raise_server_exceptions=False), r


def _await(coro):
    return _asyncio.get_event_loop().run_until_complete(coro)


def _proposed_pair(mock_vector, *, action="supersede", winner="w1"):
    prop = {"action": action, "winner_id": (winner if action == "supersede" else None)}
    async def _get(mid):
        return {
            "w1": _real_shape("w1", status="active", contested=True, contested_with="l1",
                              proposed_verdict=prop, proposed_at="2026-09-01T00:00:00+00:00"),
            "l1": _real_shape("l1", status="active", contested=True, contested_with="w1",
                              proposed_verdict=prop, proposed_at="2026-09-01T00:00:00+00:00"),
        }.get(mid)
    mock_vector.get_memory = AsyncMock(side_effect=_get)


class TestContestedPropose:
    def test_proposal_is_written_to_both_points_and_counted_once(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _contested_pair(mock_vector)
        resp = client.post("/memory/contested/propose", headers={"X-Agent-Id": "night-shift"},
                           json={"winner_id": "w1", "loser_id": "l1", "action": "supersede",
                                 "rationale": "w1 cites the newer runbook"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "proposed" and resp.json()["first"] is True
        call = mock_vector._client.set_payload.call_args
        assert set(call.kwargs["points"]) == {"w1", "l1"}
        written = call.kwargs["payload"]
        assert written["proposed_verdict"] == {"action": "supersede", "winner_id": "w1"}
        assert written["proposed_rationale"] == "w1 cites the newer runbook"
        assert written["proposed_by"] == "night-shift" and written["proposed_at"]
        # Nothing was resolved: no supersede, no confirm, no edge.
        mock_vector.update_status.assert_not_called()
        mock_vector.confirm_memory.assert_not_called()
        mock_graph.create_supersession.assert_not_called()
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "proposed")) == "1"

    def test_second_proposal_overwrites_but_is_not_counted_again(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _proposed_pair(mock_vector)
        resp = client.post("/memory/contested/propose",
                           json={"winner_id": "l1", "loser_id": "w1", "action": "supersede"})
        assert resp.status_code == 200 and resp.json()["first"] is False
        assert mock_vector._client.set_payload.call_args.kwargs["payload"]["proposed_verdict"] == {
            "action": "supersede", "winner_id": "l1"}
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "proposed")) is None

    def test_coexist_proposal_has_no_winner(self, mock_graph, mock_vector):
        client, _ = _ledger_client(mock_graph, mock_vector)
        _contested_pair(mock_vector)
        resp = client.post("/memory/contested/propose",
                           json={"winner_id": "w1", "loser_id": "l1", "action": "coexist"})
        assert resp.status_code == 200
        assert mock_vector._client.set_payload.call_args.kwargs["payload"]["proposed_verdict"] == {
            "action": "coexist", "winner_id": None}

    def test_not_a_pair_is_409_and_writes_nothing(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        async def _get(mid):
            return {"w1": _real_shape("w1", status="active", contested=True, contested_with="zz"),
                    "l1": _real_shape("l1", status="active")}.get(mid)
        mock_vector.get_memory = AsyncMock(side_effect=_get)
        resp = client.post("/memory/contested/propose",
                           json={"winner_id": "w1", "loser_id": "l1"})
        assert resp.status_code == 409
        mock_vector._client.set_payload.assert_not_called()
        assert _await(r.keys("fleet:*")) == []

    def test_unknown_memory_is_404(self, mock_graph, mock_vector):
        client, _ = _ledger_client(mock_graph, mock_vector)
        mock_vector.get_memory = AsyncMock(return_value=None)
        assert client.post("/memory/contested/propose",
                           json={"winner_id": "a", "loser_id": "b"}).status_code == 404

    def test_rationale_is_capped(self, mock_graph, mock_vector):
        client, _ = _ledger_client(mock_graph, mock_vector)
        _contested_pair(mock_vector)
        assert client.post("/memory/contested/propose",
                           json={"winner_id": "w1", "loser_id": "l1",
                                 "rationale": "x" * 1001}).status_code == 422


class TestResolveWithProposal:
    def _clear_payloads(self, mock_vector):
        return [c.kwargs["payload"] for c in mock_vector._client.set_payload.call_args_list]

    def test_matching_supersede_counts_resolved_and_matched_and_clears_proposal(
            self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _proposed_pair(mock_vector, action="supersede", winner="w1")
        resp = client.post("/memory/contested/resolve",
                           json={"winner_id": "w1", "loser_id": "l1", "action": "supersede"})
        assert resp.status_code == 200, resp.text
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "resolved")) == "1"
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "matched")) == "1"
        for p in self._clear_payloads(mock_vector):
            assert p["contested"] is False
            assert p["proposed_verdict"] is None and p["proposed_rationale"] is None
            assert p["proposed_by"] is None and p["proposed_at"] is None

    def test_disagreeing_verdict_counts_resolved_only(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _proposed_pair(mock_vector, action="supersede", winner="w1")
        client.post("/memory/contested/resolve",
                    json={"winner_id": "l1", "loser_id": "w1", "action": "supersede"})
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "resolved")) == "1"
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "matched")) is None

    def test_coexist_matches_coexist(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _proposed_pair(mock_vector, action="coexist")
        client.post("/memory/contested/resolve",
                    json={"winner_id": "w1", "loser_id": "l1", "action": "coexist"})
        assert _await(r.hget("fleet:ledger:propose_contested_verdict", "matched")) == "1"
        for p in self._clear_payloads(mock_vector):
            assert p["coexist_with"] in {"w1", "l1"} and p["proposed_verdict"] is None

    def test_resolve_without_a_proposal_records_nothing(self, mock_graph, mock_vector):
        client, r = _ledger_client(mock_graph, mock_vector)
        _contested_pair(mock_vector)
        client.post("/memory/contested/resolve",
                    json={"winner_id": "w1", "loser_id": "l1", "action": "supersede"})
        assert _await(r.keys("fleet:ledger:*")) == []
```

Append to `cortex/tests/test_autopilot_api.py` (inside/alongside the inbox tests, using its existing fake-Qdrant + admin-client fixtures — read the file's `contested_memories` test and copy its point-construction style):

```python
@pytest.mark.asyncio
async def test_contested_rows_carry_the_fleet_proposal(admin_client, qdrant):
    qdrant.add(id="m1", payload={"status": "active", "contested": True, "contested_with": "m2",
                                 "contested_at": iso(1), "text": "A",
                                 "proposed_verdict": {"action": "coexist", "winner_id": None},
                                 "proposed_rationale": "both true", "proposed_by": "night-shift",
                                 "proposed_at": iso(0.5)})
    qdrant.add(id="m2", payload={"status": "active", "contested": True, "contested_with": "m1",
                                 "contested_at": iso(1), "text": "B"})
    body = (await admin_client.get("/autopilot/inbox")).json()
    rows = {p["id"]: p for p in body["items"]["contested_memories"]["pairs"]}
    assert rows["m1"]["proposed_verdict"] == {"action": "coexist", "winner_id": None}
    assert rows["m1"]["proposed_by"] == "night-shift" and rows["m1"]["proposed_rationale"] == "both true"
    assert rows["m2"]["proposed_verdict"] is None and rows["m2"]["proposed_by"] == ""
```

(Adapt `admin_client` / `qdrant` / `iso` to the fixture names that file actually defines — the assertions are the contract.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd cortex && python -m pytest tests/test_feedback_and_contested.py -q -k "Propose or WithProposal" && python -m pytest tests/test_autopilot_api.py -q -k fleet_proposal`
Expected: FAIL — 404/405 on `/memory/contested/propose`; `KeyError: 'proposed_verdict'`.

- [ ] **Step 3: Model** (`cortex/app/models.py`, after `ContestedResolveRequest`)

```python
class ContestedProposeRequest(BaseModel):
    """A fleet worker's PROPOSED verdict on a contested pair (Fleet-as-GPU).

    Same shape as the human verdict plus a rationale, so the two can be compared
    later (the ledger's `matched` counter). Proposing never resolves: the pair
    stays contested until a human calls /memory/contested/resolve.
    """

    winner_id: str = Field(..., min_length=1, max_length=128)
    loser_id: str = Field(..., min_length=1, max_length=128)
    action: Literal["supersede", "coexist"] = "supersede"
    rationale: str = Field(default="", max_length=1000)
```

- [ ] **Step 4: Lifecycle** (`cortex/app/lifecycle.py`) — import `ContestedProposeRequest` alongside `ContestedResolveRequest`. Factor the pair guard so both routes share it; put this helper inside `create_lifecycle_router` above `resolve_contested`:

```python
    async def _load_contested_pair(winner_id: str, loser_id: str) -> tuple[dict, dict]:
        winner = await vector.get_memory(winner_id)
        loser = await vector.get_memory(loser_id)
        if not winner or not loser:
            raise HTTPException(status_code=404, detail="Memory not found")
        # contested_with is NOT a hoisted get_memory field — it lives under
        # "metadata". Reading it at the top level made this endpoint 409 on
        # every genuinely contested pair.
        winner_meta = winner.get("metadata") or {}
        loser_meta = loser.get("metadata") or {}
        if winner_meta.get("contested_with") != loser_id and loser_meta.get(
            "contested_with"
        ) != winner_id:
            raise HTTPException(
                status_code=409,
                detail="These memories are not contested with each other",
            )
        return winner_meta, loser_meta

    _PROPOSAL_CLEAR = {
        "proposed_verdict": None, "proposed_rationale": None,
        "proposed_by": None, "proposed_at": None,
    }
```

Replace the top of `resolve_contested` (from `winner = await vector.get_memory(...)` through the 409 raise) with `winner_meta, loser_meta = await _load_contested_pair(body.winner_id, body.loser_id)`, then **before** the `settings = get_settings()` line add:

```python
        proposal = winner_meta.get("proposed_verdict") or loser_meta.get("proposed_verdict")
```

In the supersede branch, merge `**_PROPOSAL_CLEAR` into the `set_payload` dict (alongside `"contested": False, ...`); in the coexist branch merge `**_PROPOSAL_CLEAR` into each per-side payload. Then, just before the final `return {...}` of `resolve_contested`:

```python
        if isinstance(proposal, dict):
            # The verdict is the ground truth the fleet's proposal is scored
            # against (spec decision 7). Best-effort, after the verdict is durable.
            from app.fleet import ledger as _ledger
            await _ledger.record(redis_client, _ledger.JOB_VERDICT, "resolved")
            agreed = proposal.get("action") == body.action and (
                body.action == "coexist" or proposal.get("winner_id") == body.winner_id
            )
            if agreed:
                await _ledger.record(redis_client, _ledger.JOB_VERDICT, "matched")
```

Add the new route directly after `resolve_contested`:

```python
    @router.post("/memory/contested/propose", dependencies=[Depends(require_not_frozen)])
    @limiter.limit(lambda: get_settings().RATE_LIMIT)
    async def propose_contested(
        request: Request,
        body: ContestedProposeRequest,
        identity: dict = Depends(require_scope("memory:write")),
    ) -> dict[str, Any]:
        """Record a PROPOSED verdict on a contested pair without resolving it.

        Written by Night Shift's `propose_contested_verdict` job (a local model on
        a developer's machine). It sets four `proposed_*` fields on both points
        and nothing else — the pair stays contested, recall keeps annotating it,
        and only /memory/contested/resolve (a human) supersedes or coexists. A
        second proposal overwrites the first; only the first is counted.
        """
        winner_meta, loser_meta = await _load_contested_pair(body.winner_id, body.loser_id)
        first = not (winner_meta.get("proposed_at") or loser_meta.get("proposed_at"))
        proposed_by = (
            request.headers.get("X-Agent-Id")
            or (identity or {}).get("agent_id")
            or "unknown"
        )
        await vector._client.set_payload(
            collection_name=get_settings().QDRANT_COLLECTION,
            payload={
                "proposed_verdict": {
                    "action": body.action,
                    "winner_id": body.winner_id if body.action == "supersede" else None,
                },
                "proposed_rationale": body.rationale,
                "proposed_by": str(proposed_by)[:128],
                "proposed_at": datetime.now(timezone.utc).isoformat(),
            },
            points=[body.winner_id, body.loser_id],
        )
        if first:
            from app.fleet import ledger as _ledger
            await _ledger.record(redis_client, _ledger.JOB_VERDICT, "proposed")
        return {
            "status": "proposed", "action": body.action,
            "winner_id": body.winner_id, "loser_id": body.loser_id, "first": first,
        }
```

(`lifecycle.py` must import `datetime, timezone` from `datetime` if it does not already.)

- [ ] **Step 5: Inbox rows** (`cortex/app/autopilot/inbox.py::contested_memories`) — extend each row:

```python
        pairs.append({
            "id": str(p.id),
            "contested_with": payload.get("contested_with") or "",
            "contested_at": payload.get("contested_at") or "",
            "text_preview": _preview(payload.get("text")),
            # Fleet-as-GPU: a Night Shift proposal, when one exists. Rendered
            # beside the pair; resolving stays a human API call.
            "proposed_verdict": payload.get("proposed_verdict"),
            "proposed_rationale": payload.get("proposed_rationale") or "",
            "proposed_by": payload.get("proposed_by") or "",
            "proposed_at": payload.get("proposed_at") or "",
        })
```

- [ ] **Step 6: Run**

Run: `cd cortex && python -m pytest tests/test_feedback_and_contested.py tests/test_lifecycle.py tests/test_autopilot_api.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

Stage `cortex/app/models.py cortex/app/lifecycle.py cortex/app/autopilot/inbox.py cortex/tests/test_feedback_and_contested.py cortex/tests/test_autopilot_api.py`; message `feat(cortex): POST /memory/contested/propose — fleet verdict proposals, scored on resolve`.

---

### Task 6: `fleet_enqueue_pass` — cortex posts relay tasks nightly

**Files:**
- Create: `cortex/app/fleet/enqueue.py`
- Modify: `cortex/app/workers/memory_agent.py:1122-1133` (passes list)
- Test: `cortex/tests/test_fleet_enqueue.py`, `cortex/tests/test_memory_agent.py` (one registration test)

**Interfaces:**
- Consumes: `Settings.FLEET_ENQUEUE_ENABLED`, `FLEET_ENQUEUE_MAX_PER_RUN`, `RELAY_URL`, `FIREKEEP_INTERNAL_KEY`; `app.skills.internal_key_headers`; `ledger.live_marker_key`, `ledger.rejected_reauthor_key`, `LIVE_MARKER_TTL_SECONDS`; relay `POST /tasks` (Task 1).
- Produces: `fleet_enqueue_pass(client=None, settings=None, redis_client=None, post=None, now=None) -> dict` (sync). `post(settings, task: dict) -> bool`. Task payloads (Task 8 parses these):
  - `reauthor_stale_skill`: `description = "skill_id=<id> workspace_id=<ws|->"`, `context` = JSON `{"skill_id", "trigger", "symptoms", "content", "domain", "project", "timestamp", "last_recalled_at", "stale_detected_at", "access_count", "skill_efficacy", "skill_efficacy_n"}`.
  - `propose_contested_verdict`: `description = "pair=<a>,<b> workspace_id=<ws|->"`, `context` = JSON `{"a": {"id","text","domain","timestamp","confirmed_count","contradicted_count"}, "b": {...}, "contested_at"}`.
  - Both: `assigner = "cortex-fleet"`, `priority = "normal"`.

- [ ] **Step 1: Write the failing tests**

```python
# cortex/tests/test_fleet_enqueue.py
"""The nightly fleet enqueue: state-based dedup, member-private exclusion, caps.

The fake Qdrant evaluates `must` (equality) and `must_not` (equality or
IsEmpty) because those are exactly the conditions this pass issues; anything
richer would be testing qdrant-client.
"""
from __future__ import annotations

import json

import fakeredis
import pytest
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue

from app.fleet import enqueue, ledger


class _Point:
    def __init__(self, pid, payload):
        self.id, self.payload = pid, payload


def _match(cond, payload) -> bool:
    if isinstance(cond, FieldCondition):
        return payload.get(cond.key) == cond.match.value
    if isinstance(cond, IsEmptyCondition):
        return payload.get(cond.is_empty.key) in (None, "", [], {})
    raise AssertionError(f"unsupported condition {cond!r}")


class _FakeQdrant:
    def __init__(self, points):
        self._points = points
        self.closed = False

    def scroll(self, collection_name, scroll_filter=None, limit=1000,
               with_payload=True, with_vectors=False, offset=None):
        out = []
        for p in self._points:
            ok = all(_match(c, p.payload) for c in (scroll_filter.must or []))
            ok = ok and not any(_match(c, p.payload) for c in (scroll_filter.must_not or []))
            if ok:
                out.append(p)
        return out[:limit], None

    def close(self):
        self.closed = True


class _Settings:
    QDRANT_COLLECTION = "test"
    FLEET_ENQUEUE_ENABLED = True
    FLEET_ENQUEUE_MAX_PER_RUN = 20
    RELAY_URL = "http://relay:8050"
    FIREKEEP_INTERNAL_KEY = "nxs_test"


def _stale_skill(pid="s1", **extra):
    return _Point(pid, {"memory_type": "skill", "skill_status": "active", "stale": True,
                        "trigger": "T", "symptoms": "S", "content": "C", "domain": "d",
                        "workspace_id": "ws", **extra})


def _pair(a="m1", b="m2", **extra):
    return [
        _Point(a, {"status": "active", "contested": True, "contested_with": b, "text": "A",
                   "contested_at": "2026-09-01T00:00:00+00:00", "workspace_id": "ws", **extra}),
        _Point(b, {"status": "active", "contested": True, "contested_with": a, "text": "B",
                   "contested_at": "2026-09-01T00:00:00+00:00", "workspace_id": "ws"}),
    ]


@pytest.fixture
def redis():
    return fakeredis.FakeRedis(decode_responses=True)


class _Recorder:
    def __init__(self, ok=True):
        self.ok, self.tasks = ok, []

    def __call__(self, settings, task):
        self.tasks.append(task)
        return self.ok


def _run(points, redis, post=None, settings=None):
    post = post or _Recorder()
    out = enqueue.fleet_enqueue_pass(client=_FakeQdrant(points), settings=settings or _Settings(),
                                     redis_client=redis, post=post)
    return out, post


def test_disabled_before_any_io(redis):
    s = _Settings(); s.FLEET_ENQUEUE_ENABLED = False
    q = _FakeQdrant([_stale_skill()])
    rec = _Recorder()
    out = enqueue.fleet_enqueue_pass(client=q, settings=s, redis_client=redis, post=rec)
    assert out == {"status": "disabled"} and rec.tasks == []


def test_stale_skill_becomes_a_reauthor_task_with_context(redis):
    out, post = _run([_stale_skill(access_count=3, skill_efficacy=0.4, skill_efficacy_n=6)], redis)
    assert out["reauthor_enqueued"] == 1
    t = post.tasks[0]
    assert t["title"] == "reauthor_stale_skill" and t["assigner"] == "cortex-fleet"
    assert t["description"] == "skill_id=s1 workspace_id=ws"
    ctx = json.loads(t["context"])
    assert ctx["skill_id"] == "s1" and ctx["trigger"] == "T" and ctx["content"] == "C"
    assert ctx["access_count"] == 3 and ctx["skill_efficacy_n"] == 6
    assert redis.exists(ledger.live_marker_key("reauthor_stale_skill", "s1")) == 1


def test_contested_pair_becomes_one_verdict_task(redis):
    out, post = _run(_pair(), redis)
    assert out["verdict_enqueued"] == 1 and len(post.tasks) == 1
    t = post.tasks[0]
    assert t["title"] == "propose_contested_verdict"
    assert t["description"] == "pair=m1,m2 workspace_id=ws"
    ctx = json.loads(t["context"])
    assert {ctx["a"]["id"], ctx["b"]["id"]} == {"m1", "m2"}
    assert ctx["a"]["text"] in {"A", "B"} and ctx["contested_at"]


def test_member_private_points_are_never_enqueued(redis):
    pts = [_stale_skill(visibility="member")] + _pair(a="p1", b="p2", visibility="member")
    out, post = _run(pts, redis)
    assert post.tasks == []
    assert out["reauthor_enqueued"] == 0 and out["verdict_enqueued"] == 0
    # The pair's other side is workspace-visible but its partner is private → unpaired.
    assert out["skipped_unpaired"] == 1


def test_pending_reauthor_draft_blocks_re_enqueue(redis):
    draft = _Point("d1", {"memory_type": "skill", "skill_status": "draft", "reauthor_of": "s1"})
    out, post = _run([_stale_skill(), draft], redis)
    assert post.tasks == [] and out["skipped_pending"] == 1


def test_existing_proposal_blocks_re_enqueue(redis):
    pts = _pair()
    pts[0].payload["proposed_verdict"] = {"action": "coexist", "winner_id": None}
    out, post = _run(pts, redis)
    assert post.tasks == [] and out["skipped_pending"] == 1


def test_rejected_marker_blocks_re_enqueue(redis):
    redis.set(ledger.rejected_reauthor_key("s1"), "1")
    out, post = _run([_stale_skill()], redis)
    assert post.tasks == [] and out["skipped_rejected"] == 1


def test_live_marker_blocks_double_post_and_expires_with_the_task(redis):
    out1, post1 = _run([_stale_skill()], redis)
    assert len(post1.tasks) == 1
    ttl = redis.ttl(ledger.live_marker_key("reauthor_stale_skill", "s1"))
    assert 0 < ttl <= ledger.LIVE_MARKER_TTL_SECONDS
    out2, post2 = _run([_stale_skill()], redis)
    assert post2.tasks == [] and out2["skipped_inflight"] == 1


def test_failed_post_releases_the_marker_and_counts_failed(redis):
    out, _ = _run([_stale_skill()], redis, post=_Recorder(ok=False))
    assert out["failed"] == 1 and out["reauthor_enqueued"] == 0
    assert redis.exists(ledger.live_marker_key("reauthor_stale_skill", "s1")) == 0


def test_cap_bounds_a_night(redis):
    s = _Settings(); s.FLEET_ENQUEUE_MAX_PER_RUN = 2
    pts = [_stale_skill(f"s{i}") for i in range(5)]
    out, post = _run(pts, redis, settings=s)
    assert len(post.tasks) == 2 and out["capped"] == 3


def test_context_is_truncated(redis):
    out, post = _run([_stale_skill(content="x" * 10000)], redis)
    assert len(json.loads(post.tasks[0]["context"])["content"]) == enqueue.SKILL_CONTENT_CAP


def test_post_relay_task_uses_rest_and_internal_key(monkeypatch):
    seen = {}

    class _Resp:
        status_code = 201

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    monkeypatch.setattr(enqueue.httpx, "post", fake_post)
    assert enqueue.post_relay_task(_Settings(), {"title": "t"}) is True
    assert seen["url"] == "http://relay:8050/tasks" and seen["json"] == {"title": "t"}
    assert seen["headers"] == {"X-API-Key": "nxs_test"} and seen["timeout"] == 10.0


def test_post_relay_task_swallows_transport_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("relay down")
    monkeypatch.setattr(enqueue.httpx, "post", boom)
    assert enqueue.post_relay_task(_Settings(), {"title": "t"}) is False
```

And in `cortex/tests/test_memory_agent.py` add:

```python
def test_fleet_enqueue_pass_is_registered_after_staleness(monkeypatch):
    """The enqueue pass must see TONIGHT's stale flags, so it runs after
    skill_staleness — and it is one of the isolated passes (an error there
    never stops the rest)."""
    import inspect
    from app.workers import memory_agent
    src = inspect.getsource(memory_agent.run_memory_agent)
    assert src.index('("skill_staleness", skill_staleness_pass)') < src.index(
        '("fleet_enqueue", fleet_enqueue_pass)')
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd cortex && python -m pytest tests/test_fleet_enqueue.py tests/test_memory_agent.py -q -k "fleet"`
Expected: FAIL — `ImportError: cannot import name 'enqueue'`.

- [ ] **Step 3: Implement `cortex/app/fleet/enqueue.py`**

```python
"""Nightly fleet enqueue (spec 2026-09-02, decisions 3, 5, 6, 10).

Runs inside the sync Celery memory agent after the staleness sweep and turns
what the nightly passes already FOUND into relay tasks a client Night Shift can
drain: one `reauthor_stale_skill` per stale active skill, one
`propose_contested_verdict` per contested pair. Cortex reaches relay the way the
briefing does (RELAY_URL + FIREKEEP_INTERNAL_KEY), through the REST twin of
relay_task_post.

DEDUP IS STATE-BASED. Relay tasks have no idempotency and expire in 7 days, so
neither "post on transition" (loses the finding if the task expires undrained)
nor "post on a marker" (re-drafts work a human has not acted on) is right. The
store is asked what is TRUE: a stale skill with a re-author draft in any status
is done; a pair carrying a proposal is done; a rejection marker means a human
threw the last rewrite away. A short live marker only stops double-posting
while a task is in flight, and expires with the task.

MEMBER-PRIVATE NEVER LEAVES. Relay tasks are Keep-global and the worker needs
the text in `context`, so `visibility == "member"` points are excluded at the
query (both sides of a pair must be workspace-visible).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

import httpx
from qdrant_client.models import FieldCondition, Filter, IsEmptyCondition, MatchValue, PayloadField

from app.fleet import ledger

logger = logging.getLogger(__name__)

ASSIGNER = "cortex-fleet"
SKILL_CONTENT_CAP = 6000
MEMORY_TEXT_CAP = 3000
POST_TIMEOUT = 10.0

_NOT_MEMBER_PRIVATE = FieldCondition(key="visibility", match=MatchValue(value="member"))


def post_relay_task(settings, task: dict) -> bool:
    """POST one task to relay. True only on 201; never raises."""
    from app.skills import internal_key_headers
    url = f"{str(settings.RELAY_URL).rstrip('/')}/tasks"
    try:
        resp = httpx.post(url, json=task, headers=internal_key_headers(settings.FIREKEEP_INTERNAL_KEY),
                          timeout=POST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — a relay outage means no fleet tasks tonight
        logger.warning("fleet enqueue: relay POST failed: %s", exc)
        return False
    if resp.status_code != 201:
        logger.warning("fleet enqueue: relay POST returned %s", resp.status_code)
        return False
    return True


def _scroll(client, settings, must: list, must_not: list | None = None) -> list:
    points, _ = client.scroll(
        collection_name=settings.QDRANT_COLLECTION,
        scroll_filter=Filter(must=must, must_not=must_not or []),
        limit=1000, with_payload=True, with_vectors=False,
    )
    return list(points)


def _claim(redis_client, key: str) -> bool:
    """SET NX EX — the live marker. A Redis failure claims nothing (no post)."""
    try:
        return bool(redis_client.set(key, "1", nx=True, ex=ledger.LIVE_MARKER_TTL_SECONDS))
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet enqueue: marker claim failed for %s: %s", key, exc)
        return False


def _release(redis_client, key: str) -> None:
    try:
        redis_client.delete(key)
    except Exception:  # noqa: BLE001
        pass


def _skill_context(pid: str, payload: dict) -> str:
    return json.dumps({
        "skill_id": pid,
        "trigger": str(payload.get("trigger") or "")[:1000],
        "symptoms": str(payload.get("symptoms") or "")[:2000],
        "content": str(payload.get("content") or "")[:SKILL_CONTENT_CAP],
        "domain": payload.get("domain") or "",
        "project": payload.get("project"),
        "timestamp": payload.get("timestamp"),
        "last_recalled_at": payload.get("last_recalled_at"),
        "stale_detected_at": payload.get("stale_detected_at"),
        "access_count": payload.get("access_count"),
        "skill_efficacy": payload.get("skill_efficacy"),
        "skill_efficacy_n": payload.get("skill_efficacy_n"),
    })


def _memory_side(pid: str, payload: dict) -> dict:
    return {
        "id": pid,
        "text": str(payload.get("text") or "")[:MEMORY_TEXT_CAP],
        "domain": payload.get("domain") or "",
        "timestamp": payload.get("timestamp"),
        "confirmed_count": payload.get("confirmed_count", 0),
        "contradicted_count": payload.get("contradicted_count", 0),
    }


def fleet_enqueue_pass(client=None, settings=None, redis_client=None,
                       post: Callable[[Any, dict], bool] | None = None, now=None) -> dict:
    """Sync; injectable client/settings/redis/post for tests. Never raises out
    of the per-pass try/except in run_memory_agent."""
    if settings is None:
        from app.config import get_settings
        settings = get_settings()
    if not getattr(settings, "FLEET_ENQUEUE_ENABLED", True):
        return {"status": "disabled"}

    out = {"status": "ok", "reauthor_enqueued": 0, "verdict_enqueued": 0,
           "skipped_private": 0, "skipped_pending": 0, "skipped_rejected": 0,
           "skipped_inflight": 0, "skipped_unpaired": 0, "capped": 0, "failed": 0}
    close_after = client is None
    if client is None:
        from app.workers.memory_agent import _get_qdrant_client
        client = _get_qdrant_client()
    if redis_client is None:
        from app.workers.memory_agent import _get_redis_client
        redis_client = _get_redis_client()
    post = post or post_relay_task
    budget = max(0, int(getattr(settings, "FLEET_ENQUEUE_MAX_PER_RUN", 20)))

    def _send(job: str, subject: str, task: dict) -> bool:
        nonlocal budget
        key = ledger.live_marker_key(job, subject)
        if budget <= 0:
            out["capped"] += 1
            return False
        if not _claim(redis_client, key):
            out["skipped_inflight"] += 1
            return False
        budget -= 1
        if post(settings, task):
            return True
        out["failed"] += 1
        _release(redis_client, key)
        return False

    try:
        # --- stale skills -> reauthor_stale_skill -----------------------------
        stale = _scroll(client, settings, [
            FieldCondition(key="memory_type", match=MatchValue(value="skill")),
            FieldCondition(key="skill_status", match=MatchValue(value="active")),
            FieldCondition(key="stale", match=MatchValue(value=True)),
        ], must_not=[_NOT_MEMBER_PRIVATE])
        # Private stale skills exist but were filtered — count them for honesty.
        out["skipped_private"] += sum(
            1 for p in _scroll(client, settings, [
                FieldCondition(key="memory_type", match=MatchValue(value="skill")),
                FieldCondition(key="skill_status", match=MatchValue(value="active")),
                FieldCondition(key="stale", match=MatchValue(value=True)),
                _NOT_MEMBER_PRIVATE,
            ])
        )
        already = {
            str((p.payload or {}).get("reauthor_of"))
            for p in _scroll(client, settings,
                             [FieldCondition(key="memory_type", match=MatchValue(value="skill"))],
                             must_not=[IsEmptyCondition(is_empty=PayloadField(key="reauthor_of"))])
        }
        for p in stale:
            pid, payload = str(p.id), (p.payload or {})
            if pid in already:
                out["skipped_pending"] += 1
                continue
            try:
                rejected = bool(redis_client.exists(ledger.rejected_reauthor_key(pid)))
            except Exception:  # noqa: BLE001
                rejected = False
            if rejected:
                out["skipped_rejected"] += 1
                continue
            task = {
                "title": ledger.JOB_REAUTHOR, "assigner": ASSIGNER, "priority": "normal",
                "description": f"skill_id={pid} workspace_id={payload.get('workspace_id') or '-'}",
                "context": _skill_context(pid, payload),
            }
            if _send(ledger.JOB_REAUTHOR, pid, task):
                out["reauthor_enqueued"] += 1

        # --- contested pairs -> propose_contested_verdict ---------------------
        contested = _scroll(client, settings, [
            FieldCondition(key="status", match=MatchValue(value="active")),
            FieldCondition(key="contested", match=MatchValue(value=True)),
        ], must_not=[_NOT_MEMBER_PRIVATE])
        by_id = {str(p.id): (p.payload or {}) for p in contested}
        for pid, payload in by_id.items():
            other = str(payload.get("contested_with") or "")
            if not other or pid > other:
                continue  # each pair once, from its lexically-smaller side
            other_payload = by_id.get(other)
            if other_payload is None:
                out["skipped_unpaired"] += 1  # inactive, or member-private (filtered)
                continue
            if payload.get("proposed_verdict") or other_payload.get("proposed_verdict"):
                out["skipped_pending"] += 1
                continue
            subject = f"{pid}:{other}"
            task = {
                "title": ledger.JOB_VERDICT, "assigner": ASSIGNER, "priority": "normal",
                "description": f"pair={pid},{other} workspace_id={payload.get('workspace_id') or '-'}",
                "context": json.dumps({
                    "a": _memory_side(pid, payload), "b": _memory_side(other, other_payload),
                    "contested_at": payload.get("contested_at") or other_payload.get("contested_at"),
                }),
            }
            if _send(ledger.JOB_VERDICT, subject, task):
                out["verdict_enqueued"] += 1
        return out
    except Exception:
        logger.exception("Error in fleet_enqueue_pass")
        out["status"] = "error"
        return out
    finally:
        if close_after:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
```

(The `skipped_private` second scroll counts private stale skills for the run report; if the fake or real filter cannot express it, drop the count to 0 and keep the exclusion — the exclusion is the invariant, the count is reporting. The test `test_member_private_points_are_never_enqueued` asserts only exclusion and `skipped_unpaired`.)

- [ ] **Step 4: Register the pass** (`cortex/app/workers/memory_agent.py`, in `run_memory_agent`)

```python
    from app.skills.staleness import skill_staleness_pass
    from app.fleet.enqueue import fleet_enqueue_pass

    passes = [
        ...
        ("skill_staleness", skill_staleness_pass),
        # Fleet-as-GPU: turns tonight's stale flags + contested pairs into relay
        # tasks for client Night Shift workers. After staleness on purpose.
        ("fleet_enqueue", fleet_enqueue_pass),
    ]
```

- [ ] **Step 5: Run**

Run: `cd cortex && python -m pytest tests/test_fleet_enqueue.py tests/test_memory_agent.py tests/test_skill_staleness.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

Stage `cortex/app/fleet/enqueue.py cortex/app/workers/memory_agent.py cortex/tests/test_fleet_enqueue.py cortex/tests/test_memory_agent.py`; message `feat(cortex): fleet_enqueue_pass — nightly stale-skill and contested-pair tasks for Night Shift`.

---

### Task 7: The digest's `fleet` block

**Files:**
- Modify: `cortex/app/autopilot/digest.py` (`build_digest`, after the gc block)
- Test: `cortex/tests/test_autopilot_api.py` (append)

**Interfaces:**
- Consumes: `ledger.summarize(redis_client, days=, now=)`, `Settings.FLEET_ENQUEUE_ENABLED`.
- Produces: `GET /autopilot/digest?days=N` → `payload["fleet"] = {"enabled": bool, "jobs": {...summarize()...}}`; on a Redis failure `payload["fleet"] = {"enabled": bool, "jobs": {}}` and `errors["fleet"]` is set (the existing `errors` mechanism). Task 10 renders `fleet.jobs`.

- [ ] **Step 1: Write the failing tests** (append to `cortex/tests/test_autopilot_api.py`, using its admin client + fakeredis fixtures)

```python
@pytest.mark.asyncio
async def test_digest_carries_the_fleet_ledger(admin_client, redis):
    from app.fleet import ledger
    await ledger.record(redis, ledger.JOB_REAUTHOR, "produced", now=NOW)
    await ledger.record(redis, ledger.JOB_REAUTHOR, "approved", now=NOW)
    body = (await admin_client.get("/autopilot/digest?days=7")).json()
    fleet = body["fleet"]
    assert fleet["enabled"] is True
    re = fleet["jobs"]["reauthor_stale_skill"]
    assert re["window"]["produced"] == 1 and re["window"]["approval_rate"] == 1.0
    assert fleet["jobs"]["propose_contested_verdict"]["window"]["match_rate"] is None
    assert "fleet" not in body.get("errors", {})


@pytest.mark.asyncio
async def test_digest_fleet_degrades_in_place(admin_client, redis, monkeypatch):
    from app.fleet import ledger
    async def boom(*a, **k):
        raise RuntimeError("redis gone")
    monkeypatch.setattr(ledger, "summarize", boom)
    body = (await admin_client.get("/autopilot/digest?days=7")).json()
    assert body["fleet"]["jobs"] == {} and "fleet" in body["errors"]
    assert "counts" in body  # the rest of the digest survived
```

(Adapt fixture names to the file; `NOW` and the frozen-datetime fixture already exist there.)

- [ ] **Step 2: Run to verify they fail**

Run: `cd cortex && python -m pytest tests/test_autopilot_api.py -q -k fleet`
Expected: FAIL — `KeyError: 'fleet'`.

- [ ] **Step 3: Implement** — in `build_digest`, after the gc block and before `payload: dict[str, Any] = {...}`:

```python
    fleet: dict[str, Any] = {"enabled": bool(getattr(settings, "FLEET_ENQUEUE_ENABLED", True)),
                             "jobs": {}}
    try:
        from app.fleet import ledger as _ledger
        fleet["jobs"] = await _ledger.summarize(redis_client, days=days, now=now)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Autopilot digest: fleet ledger read failed")
        errors["fleet"] = str(exc)[:200]
```

and add `"fleet": fleet,` to the `payload` dict, plus one note line in `notes`:

```python
            "fleet.jobs rates are null when nothing has been approved or rejected yet "
            "— a rate is never invented from a prior; window counts sum UTC days.",
```

- [ ] **Step 4: Run**

Run: `cd cortex && python -m pytest tests/test_autopilot_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

Stage `cortex/app/autopilot/digest.py cortex/tests/test_autopilot_api.py`; message `feat(cortex): autopilot digest carries the fleet ledger per job type`.

---

### Task 8: Night Shift becomes a job catalog (client)

**Files:**
- Modify: `client/firekeep_client/nightshift.py` (module docstring; `_TASK_TITLE`; `_synthesize`; `run()`), `client/firekeep_client/cli.py` (`cmd_night_shift` ~1913-1935)
- Test: `client/tests/test_nightshift.py` (append), `client/tests/test_cli.py` (append)

**Interfaces:**
- Consumes: relay tasks titled `reauthor_stale_skill` / `propose_contested_verdict` whose `context` is the JSON Task 6 writes; cortex MCP `skill_create(origin_job=, reauthor_of=)` (Task 4); cortex REST `POST /memory/contested/propose` (Task 5) reached via `resolver.resolve("cortex")` (`ep.rest_base`, `ep.headers`, `ep.verify`).
- Produces: `nightshift.JOB_TITLES = ("distill_session", "reauthor_stale_skill", "propose_contested_verdict")`; `run()` summary gains `reauthored`, `proposed`, `noop`, `draft_skills`; `nightshift.JOB_DISTILL/JOB_REAUTHOR/JOB_VERDICT` constants; `cmd_night_shift` writes scratch `night_shift_last` = JSON `{"at": epoch, "counts": {...}, "error": str|None, "reported": false}` with a 7-day TTL (Task 9 reads it).

- [ ] **Step 1: Write the failing tests** (append to `client/tests/test_nightshift.py`; reuse its `_Recorder`, `_task`, `_llm_ok`, `cfg_env`)

```python
# --- Fleet-as-GPU job catalog -------------------------------------------------
import json as _json


def _fleet_task(title, ctx, task_id="task-f1", description="skill_id=s1 workspace_id=ws"):
    return {"id": task_id, "title": title, "assigner": "cortex-fleet",
            "description": description, "context": _json.dumps(ctx), "created_at": 5.0}


_REAUTHOR_CTX = {"skill_id": "s1", "trigger": "old trigger", "symptoms": "old symptoms",
                 "content": "trigger: old\n---\n## Steps\nold steps\n\n## Gotchas\nnone",
                 "domain": "neo4j", "project": None, "timestamp": "2026-05-01T00:00:00+00:00",
                 "last_recalled_at": None, "stale_detected_at": "2026-09-01T00:00:00+00:00",
                 "access_count": 0, "skill_efficacy": None, "skill_efficacy_n": None}

_PAIR_CTX = {"a": {"id": "m1", "text": "Deploy with update.sh", "domain": "ops",
                   "timestamp": "2026-08-01T00:00:00+00:00", "confirmed_count": 0,
                   "contradicted_count": 0},
             "b": {"id": "m2", "text": "Deploy with install.sh", "domain": "ops",
                   "timestamp": "2026-08-20T00:00:00+00:00", "confirmed_count": 0,
                   "contradicted_count": 0},
             "contested_at": "2026-09-01T00:00:00+00:00"}


def _listing_by_title(*tasks):
    """A relay whose relay_task_list honours the exact `title` filter."""
    def respond(service, tool, arguments, **kw):
        if tool == "relay_task_list":
            title = arguments.get("title")
            return {"tasks": [t for t in tasks if not title or t["title"] == title],
                    "count": len(tasks)}
        return None
    return respond


class _Relay(_Recorder):
    """_Recorder with a per-title relay_task_list."""
    def __init__(self, tasks, canned=None):
        super().__init__(canned or {})
        self._tasks = tasks

    def __call__(self, service, tool, arguments, **kw):
        if tool == "relay_task_list":
            title = arguments.get("title")
            self.calls.append((service, tool, arguments))
            return {"tasks": [t for t in self._tasks if not title or t["title"] == title],
                    "count": len(self._tasks)}
        return super().__call__(service, tool, arguments, **kw)


def _get_json_ok(url, *, headers, timeout=None, verify=True):
    return {"data": []}


def test_reauthor_rewrite_becomes_a_draft_with_lineage(cfg_env):
    rec = _Relay([_fleet_task("reauthor_stale_skill", _REAUTHOR_CTX)],
                 {"skill_create": "Skill created: sk-new"})
    llm = _llm_ok({"verdict": "rewrite", "reason": "runbook moved",
                   "skill": {"trigger": "new trigger", "symptoms": "new symptoms",
                             "steps": "new steps", "gotchas": "g", "domain": "neo4j"}})
    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["reauthored"] == 1 and out["draft_skills"] == 1 and out["failed"] == 0
    created = rec.named("skill_create")[0][2]
    assert created["status"] == "draft"
    assert created["origin_job"] == "reauthor_stale_skill" and created["reauthor_of"] == "s1"
    assert created["trigger"] == "new trigger" and created["agent_id"] == "night-shift"
    lease = rec.named("relay_lease")[0][2]
    assert lease["resource_id"] == "fleet.task-f1"
    done = rec.named("relay_task_update")[-1][2]
    assert done["status"] == "completed" and "re-authored" in done["result"]


def test_reauthor_still_valid_writes_nothing_and_is_a_noop(cfg_env):
    rec = _Relay([_fleet_task("reauthor_stale_skill", _REAUTHOR_CTX)])
    llm = _llm_ok({"verdict": "still_valid", "reason": "steps unchanged", "skill": None})
    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["noop"] == 1 and out["reauthored"] == 0
    assert not rec.named("skill_create")
    done = rec.named("relay_task_update")[-1][2]
    assert done["status"] == "completed" and "still_valid" in done["result"]


def test_reauthor_unconfirmed_skill_create_fails_the_task(cfg_env):
    rec = _Relay([_fleet_task("reauthor_stale_skill", _REAUTHOR_CTX)],
                 {"skill_create": "Error: unknown argument origin_job"})
    llm = _llm_ok({"verdict": "rewrite", "reason": "r",
                   "skill": {"trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "", "domain": "d"}})
    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["failed"] == 1 and out["reauthored"] == 0
    assert rec.named("relay_task_update")[-1][2]["status"] == "failed"


def test_reauthor_malformed_verdict_retries_once_then_fails(cfg_env):
    rec = _Relay([_fleet_task("reauthor_stale_skill", _REAUTHOR_CTX)])
    llm = _llm_ok({"verdict": "shrug"})
    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["failed"] == 1 and llm.calls == 2


def test_propose_posts_to_cortex_with_member_headers(cfg_env, monkeypatch):
    rec = _Relay([_fleet_task("propose_contested_verdict", _PAIR_CTX,
                              description="pair=m1,m2 workspace_id=ws")])
    posts = []

    def post_json(url, body, *, headers, timeout=None, verify=True):
        if url.endswith("/memory/contested/propose"):
            posts.append((url, body, headers))
            return {"status": "proposed", "first": True}
        return _llm_ok({"action": "supersede", "winner_id": "m2",
                        "rationale": "m2 is newer and names the current script"})(
            url, body, headers=headers, timeout=timeout, verify=verify)

    out = nightshift.run(call_tool=rec, post_json=post_json, get_json=_get_json_ok)
    assert out["proposed"] == 1 and out["failed"] == 0
    url, body, headers = posts[0]
    assert body == {"winner_id": "m2", "loser_id": "m1", "action": "supersede",
                    "rationale": "m2 is newer and names the current script"}
    assert headers["X-Agent-Id"] == "night-shift"
    assert "X-API-Key" in headers or "Authorization" in headers  # the member key rides along
    assert rec.named("relay_lease")[0][2]["resource_id"] == "fleet.task-f1"
    assert rec.named("relay_task_update")[-1][2]["status"] == "completed"


def test_propose_coexist_needs_no_winner(cfg_env):
    rec = _Relay([_fleet_task("propose_contested_verdict", _PAIR_CTX)])
    posts = []

    def post_json(url, body, *, headers, timeout=None, verify=True):
        if url.endswith("/memory/contested/propose"):
            posts.append(body)
            return {"status": "proposed", "first": True}
        return _llm_ok({"action": "coexist", "winner_id": None, "rationale": "both scripts exist"})(
            url, body, headers=headers, timeout=timeout, verify=verify)

    out = nightshift.run(call_tool=rec, post_json=post_json, get_json=_get_json_ok)
    assert out["proposed"] == 1
    assert posts[0]["action"] == "coexist" and {posts[0]["winner_id"], posts[0]["loser_id"]} == {"m1", "m2"}


def test_propose_winner_outside_the_pair_is_malformed(cfg_env):
    rec = _Relay([_fleet_task("propose_contested_verdict", _PAIR_CTX)])
    llm = _llm_ok({"action": "supersede", "winner_id": "m9", "rationale": "?"})
    out = nightshift.run(call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["failed"] == 1 and llm.calls == 2


def test_propose_rejected_by_cortex_fails_the_task_not_the_shift(cfg_env):
    rec = _Relay([_fleet_task("propose_contested_verdict", _PAIR_CTX),
                  _task()])  # a distill task queued behind it must still run

    def post_json(url, body, *, headers, timeout=None, verify=True):
        if url.endswith("/memory/contested/propose"):
            raise transport.TransportError("409 not contested")
        if url.endswith("/chat/completions"):
            if "contested" in _json.dumps(body):
                return _llm_ok({"action": "coexist", "winner_id": None, "rationale": "r"})(
                    url, body, headers=headers, timeout=timeout, verify=verify)
            return _llm_ok(_SYNTH)(url, body, headers=headers, timeout=timeout, verify=verify)
        raise AssertionError(url)

    out = nightshift.run(call_tool=rec, post_json=post_json, get_json=_get_json_ok)
    assert out["failed"] == 1 and out["deferred"] == 0 and out["distilled"] == 1


def test_fifo_across_titles_under_one_budget(cfg_env):
    tasks = [_task(), _fleet_task("reauthor_stale_skill", _REAUTHOR_CTX, task_id="task-r"),
             _fleet_task("propose_contested_verdict", _PAIR_CTX, task_id="task-p")]
    rec = _Relay(tasks, {"skill_create": "Skill created: x"})
    llm = _llm_ok({"verdict": "still_valid", "reason": "ok", "skill": None,
                   **_SYNTH})  # distill reads memory/skill keys, reauthor reads verdict
    out = nightshift.run(max_tasks=2, call_tool=rec, post_json=llm, get_json=_get_json_ok)
    leased = [c[2]["resource_id"] for c in rec.named("relay_lease")]
    assert leased == ["distill.task-1", "fleet.task-r"]  # distill first, budget of two
    assert out["distilled"] == 1 and out["noop"] == 1


def test_unknown_task_context_fails_visibly(cfg_env):
    t = _fleet_task("reauthor_stale_skill", {})
    t["context"] = "not json"
    rec = _Relay([t])
    out = nightshift.run(call_tool=rec, post_json=_llm_ok({}), get_json=_get_json_ok)
    assert out["failed"] == 1
    assert "context" in rec.named("relay_task_update")[-1][2]["result"]


def test_dry_run_touches_no_review_surface_for_fleet_jobs(cfg_env):
    rec = _Relay([_fleet_task("reauthor_stale_skill", _REAUTHOR_CTX)])
    llm = _llm_ok({"verdict": "rewrite", "reason": "r",
                   "skill": {"trigger": "t", "symptoms": "s", "steps": "x", "gotchas": "", "domain": "d"}})
    out = nightshift.run(dry_run=True, call_tool=rec, post_json=llm, get_json=_get_json_ok)
    assert out["reauthored"] == 1
    assert not rec.named("skill_create") and not rec.named("relay_lease")
```

(Check `_task()`'s id in the file — the FIFO test assumes `task-1`; use whatever `_task()` returns. `_llm_ok` must expose a call counter — if the existing helper does not, add `.calls` to it: wrap the returned function in a small class with `__call__` that increments.)

Append to `client/tests/test_cli.py` near the existing night-shift CLI tests (~74-92):

```python
def test_night_shift_prints_per_job_counts_and_records_last_run(monkeypatch, capsys, tmp_path):
    from firekeep_client import cli, nightshift, state
    monkeypatch.setattr(nightshift, "run", lambda **kw: {
        "distilled": 1, "legacy": 0, "skipped": 0, "failed": 0, "duplicates": 0,
        "deferred": 0, "reauthored": 2, "proposed": 1, "noop": 1, "draft_skills": 3})
    written = {}
    monkeypatch.setattr(state, "write_scratch",
                        lambda name, value, ttl_seconds=None: written.update({name: (value, ttl_seconds)}))
    rc = cli.main(["night-shift", "--max", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 distilled" in out and "2 re-authored" in out and "1 verdict proposed" in out
    value, ttl = written["night_shift_last"]
    rec = json.loads(value)
    assert rec["counts"]["reauthored"] == 2 and rec["reported"] is False and rec["at"] > 0
    assert ttl == 7 * 86400
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd client && python -m pytest tests/test_nightshift.py -q -k "reauthor or propose or fifo or unknown_task or dry_run_touches" && python -m pytest tests/test_cli.py -q -k per_job`
Expected: FAIL — fleet tasks are never listed (`KeyError: 'reauthored'`).

- [ ] **Step 3: Constants, generic chat, validators** (`client/firekeep_client/nightshift.py`)

Replace `_TASK_TITLE = "distill_session"` with:

```python
JOB_DISTILL = "distill_session"
JOB_REAUTHOR = "reauthor_stale_skill"
JOB_VERDICT = "propose_contested_verdict"
# Listed in THIS order: distill first (the queue that existed before the
# catalog), then the fleet jobs cortex enqueues nightly. One max_tasks budget.
JOB_TITLES = (JOB_DISTILL, JOB_REAUTHOR, JOB_VERDICT)
_TASK_TITLE = JOB_DISTILL  # legacy alias — existing tests and messages use it
```

Add two prompts after `_SYSTEM_PROMPT`:

```python
_REAUTHOR_PROMPT = (
    "You review a team skill (a 'what to do when X happens' playbook) that nobody "
    "has recalled for a long time. Decide whether it is still worth keeping as "
    "written. Reply with STRICT JSON only — no prose, no code fences — matching:\n"
    '{"verdict": "rewrite" | "still_valid" | "retire", "reason": "<one sentence>", '
    '"skill": {"trigger": "<one sentence: when this applies>", '
    '"symptoms": "<observable signals>", "steps": "<the procedure>", '
    '"gotchas": "<pitfalls>", "domain": "<one word>"} | null}\n'
    "Use \"rewrite\" ONLY when you can make the skill materially clearer, more "
    "specific or more correct from the evidence given — then fill \"skill\" with the "
    "COMPLETE rewritten playbook (never a diff). \"still_valid\" = keep as is; "
    "\"retire\" = obsolete. For those two, \"skill\" must be null. Never invent "
    "commands, paths or facts absent from the evidence."
)

_VERDICT_PROMPT = (
    "Two memories in a team knowledge base contradict each other and neither has "
    "been confirmed by a human. Propose a verdict for a human to review. Reply with "
    "STRICT JSON only — no prose, no code fences — matching:\n"
    '{"action": "supersede" | "coexist", "winner_id": "<id of the memory to KEEP>" | null, '
    '"rationale": "<1-3 sentences citing the evidence>"}\n'
    "\"supersede\" = one is wrong or outdated: winner_id MUST be exactly one of the "
    "two ids given. \"coexist\" = both are true in their own contexts: winner_id must "
    "be null. Prefer the more specific, more recent, more confirmed memory. Never "
    "invent facts absent from the two texts."
)
```

Add a generic JSON chat next to `_synthesize`, and make `_synthesize` delegate to it (keep `_extract_json` as the distill validator):

```python
def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in model reply")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("model reply is not a JSON object")
    return data


def _chat_json(messages: list[dict], post_json: Callable[..., Any], base: str,
               native: str | None, validate: Callable[[str], dict]) -> dict:
    """One local-model call, `validate`d, with a single retry on malformed output.

    Raises transport.TransportError on a TRANSIENT loss (caller defers the shift)
    and ValueError when the model never produced valid output (caller fails the
    task). The Ollama native/`think:false` handling is the one documented in
    `_ollama_native_root`; a non-thinking model that rejects `think` is retried
    without it rather than deferring the shift.
    """
    if native:
        url = f"{native}/api/chat"
        body: dict[str, Any] = {"model": _llm_model(), "messages": messages,
                                "stream": False, "think": False, "format": "json",
                                "options": {"temperature": 0.2}}
    else:
        url = f"{base}/chat/completions"
        body = {"model": _llm_model(), "temperature": 0.2, "messages": messages}
    last_error: Exception | None = None
    for _attempt in (1, 2):
        try:
            resp = post_json(url, body, headers={"Content-Type": "application/json"},
                             timeout=_LLM_TIMEOUT)
        except transport.TransportError as e:
            if native and body.get("think") is not None and "think" in str(e).lower():
                body = {k: v for k, v in body.items() if k != "think"}
                hooklog.log_failure(
                    "nightshift", f"model rejects think:false, retrying without it: {e}")
                continue
            raise
        try:
            return validate(_content_of(resp))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            last_error = e
            hooklog.log_failure("nightshift", f"malformed LLM reply (attempt): {e!r}")
    raise ValueError(f"model never produced valid JSON: {last_error!r}")


def _synthesize(sid: str, assigner: str, evidence: str,
                post_json: Callable[..., Any], base: str,
                native: str | None = None) -> dict:
    """Distill one session (see module docstring). Kept as the named entry point
    the existing tests exercise; the mechanics live in `_chat_json`."""
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Session {sid} by agent {assigner}. Evidence:\n\n{evidence}"},
    ]
    return _chat_json(messages, post_json, base, native, _extract_json)


def _validate_reauthor(text: str) -> dict:
    data = _json_object(text)
    verdict = str(data.get("verdict") or "")
    if verdict not in ("rewrite", "still_valid", "retire"):
        raise ValueError(f"reauthor verdict must be rewrite|still_valid|retire, got {verdict!r}")
    skill = data.get("skill")
    if verdict == "rewrite":
        if not isinstance(skill, dict) or not str(skill.get("trigger") or "").strip() \
                or not str(skill.get("steps") or "").strip():
            raise ValueError("rewrite verdict without a complete skill")
    return data


def _validate_proposal(text: str, pair: set[str]) -> dict:
    data = _json_object(text)
    action = str(data.get("action") or "")
    winner = data.get("winner_id")
    if action == "supersede":
        if winner not in pair:
            raise ValueError(f"supersede winner_id {winner!r} is not one of the pair")
    elif action == "coexist":
        data["winner_id"] = None
    else:
        raise ValueError(f"action must be supersede|coexist, got {action!r}")
    if not str(data.get("rationale") or "").strip():
        raise ValueError("proposal without a rationale")
    return data
```

- [ ] **Step 4: Handlers** (add after the validators)

```python
def _task_context(task: dict) -> dict:
    try:
        ctx = json.loads(task.get("context") or "")
    except (TypeError, ValueError) as e:
        raise ValueError(f"task context is not JSON: {e}") from e
    if not isinstance(ctx, dict):
        raise ValueError("task context is not a JSON object")
    return ctx


def _handle_reauthor(ctx: dict, *, call_tool, cfg, post_json, base, native,
                     worker: str, dry_run: bool) -> tuple[str, str]:
    """Returns (summary_counter, relay result text). Raises to fail the task."""
    skill_id = str(ctx.get("skill_id") or "")
    if not skill_id:
        raise ValueError("task context has no skill_id")
    evidence = json.dumps({k: ctx.get(k) for k in (
        "trigger", "symptoms", "content", "domain", "project", "timestamp",
        "last_recalled_at", "stale_detected_at", "access_count",
        "skill_efficacy", "skill_efficacy_n")}, indent=1)
    messages = [{"role": "system", "content": _REAUTHOR_PROMPT},
                {"role": "user", "content": f"Stale skill {skill_id}:\n\n{evidence}"}]
    data = _chat_json(messages, post_json, base, native, _validate_reauthor)
    verdict, reason = data["verdict"], str(data.get("reason") or "")[:300]
    if verdict != "rewrite":
        # No artifact is written for these two — the stale flag stays in the
        # inbox for the human; the verdict rides the task (spec decision 9).
        return "noop", f"night-shift: {verdict} — {reason}"
    if dry_run:
        return "reauthored", "night-shift (dry run): would draft a re-authored skill"
    skill = data["skill"]
    created = call_tool("cortex", "skill_create", {
        "trigger": str(skill.get("trigger") or "")[:1000],
        "symptoms": str(skill.get("symptoms") or "")[:2000],
        "steps": str(skill.get("steps") or "")[:4000],
        "gotchas": str(skill.get("gotchas") or "")[:2000],
        "domain": str(skill.get("domain") or ctx.get("domain") or "")[:100],
        "status": "draft",
        "origin_job": JOB_REAUTHOR,
        "reauthor_of": skill_id,
        "agent_id": worker,
    }, cfg=cfg)
    if not (isinstance(created, str) and created.startswith("Skill created")):
        # Older server (unknown argument), cross-workspace 404, in-band error:
        # never complete a task whose draft was not confirmed.
        raise RuntimeError(f"skill_create did not confirm: {created!r}"[:300])
    return "reauthored", f"night-shift: re-authored draft awaiting review — {reason}"


def _handle_propose(ctx: dict, *, post_json, base, native, worker: str,
                    dry_run: bool) -> tuple[str, str]:
    a, b = ctx.get("a") or {}, ctx.get("b") or {}
    ids = {str(a.get("id") or ""), str(b.get("id") or "")} - {""}
    if len(ids) != 2:
        raise ValueError("task context does not describe a pair")
    evidence = json.dumps({"a": a, "b": b, "contested_at": ctx.get("contested_at")}, indent=1)
    messages = [{"role": "system", "content": _VERDICT_PROMPT},
                {"role": "user", "content": f"Contested pair:\n\n{evidence}"}]
    data = _chat_json(messages, post_json, base, native,
                      lambda text: _validate_proposal(text, ids))
    action = data["action"]
    if action == "supersede":
        winner = str(data["winner_id"])
        loser = next(i for i in ids if i != winner)
    else:
        winner, loser = str(a.get("id")), str(b.get("id"))
    body = {"winner_id": winner, "loser_id": loser, "action": action,
            "rationale": str(data.get("rationale") or "")[:1000]}
    if dry_run:
        return "proposed", f"night-shift (dry run): would propose {action}"
    ep = resolver.resolve("cortex")
    headers = dict(ep.headers)
    headers.update({"Content-Type": "application/json", "X-Agent-Id": worker})
    try:
        resp = post_json(f"{ep.rest_base}/memory/contested/propose", body,
                         headers=headers, verify=ep.verify)
    except transport.TransportError as e:
        # A cortex 404/409/5xx here is THIS task's failure, not the model going
        # away — it must not defer the whole shift.
        raise RuntimeError(f"propose rejected by cortex: {e}"[:300]) from e
    if not (isinstance(resp, dict) and resp.get("status") == "proposed"):
        raise RuntimeError(f"propose not confirmed: {resp!r}"[:300])
    return "proposed", (f"night-shift: proposed {action}"
                        + (f" (keep {winner})" if action == "supersede" else "")
                        + f" — {body['rationale'][:200]}")
```

- [ ] **Step 5: `run()` — list every title, dispatch fleet jobs** — in `run()`:

(a) extend the summary: `out = {"distilled": 0, "legacy": 0, "skipped": 0, "failed": 0, "duplicates": 0, "deferred": 0, "reauthored": 0, "proposed": 0, "noop": 0, "draft_skills": 0}`.

(b) replace the single listing (`list_args = {...}` through `tasks = sorted(...)[:max_tasks]`) with:

```python
        def _list(title: str) -> list[dict]:
            args = {"status": "pending", "title": title, "oldest_first": True, "limit": 50}
            try:
                listing = call_tool("relay", "relay_task_list", args, cfg=cfg)
            except transport.TransportError:
                listing = None
            if not isinstance(listing, dict) or "tasks" not in listing:
                if title != JOB_DISTILL:
                    return []  # a pre-catalog relay has no such tasks
                # Rolling upgrades: older Relay schemas reject title/oldest_first
                # (an isError tool RESULT unwraps to a STRING, not a dict — see
                # hooks/_mcp.py). Sort the compatibility page locally.
                listing = call_tool("relay", "relay_task_list",
                                    {"status": "pending", "limit": 50}, cfg=cfg)
            return sorted(
                (t for t in ((listing or {}).get("tasks") or []) if t.get("title") == title),
                key=_task_created_at,
            )

        tasks: list[dict] = []
        for title in JOB_TITLES:
            tasks.extend(_list(title))
        tasks = tasks[:max_tasks]
```

(c) inside the `for task in tasks:` loop, right after `assigner = ...` and the `if not task_id` guard, add the fleet branch BEFORE the distill-specific `dry_run` / `not sid` / duplicate logic:

```python
            title = task.get("title") or ""
            if title != JOB_DISTILL:
                _run_fleet_task(task, title, out=out, call_tool=call_tool, cfg=cfg,
                                post_json=post_json, base=base, native=native,
                                worker=worker, dry_run=dry_run)
                if out.get("_stop"):
                    stop_shift = True
                    out.pop("_stop", None)
                continue
```

(d) in the distill branch, where `made_skill` is computed and the task is completed, add `out["draft_skills"] += 1` when `made_skill` is true after the completion is confirmed; also pass `origin_job=JOB_DISTILL` in the distill `skill_create` arguments (so distill drafts are measured too).

(e) add the module-level fleet runner (above `run`):

```python
def _run_fleet_task(task: dict, title: str, *, out: dict, call_tool, cfg, post_json,
                    base, native, worker: str, dry_run: bool) -> None:
    """One catalog task: lease, dispatch, write through review surfaces, complete.

    Mirrors the distill branch's contract — honest counting, one bad task never
    stops the shift, a TRANSIENT model loss defers and stops it (`out["_stop"]`).
    """
    task_id = task.get("id") or ""
    if dry_run:
        try:
            ctx = _task_context(task)
            if title == JOB_REAUTHOR:
                counter, _ = _handle_reauthor(ctx, call_tool=call_tool, cfg=cfg, post_json=post_json,
                                              base=base, native=native, worker=worker, dry_run=True)
            else:
                counter, _ = _handle_propose(ctx, post_json=post_json, base=base, native=native,
                                             worker=worker, dry_run=True)
            out[counter] += 1
        except transport.TransportError:
            out["deferred"] += 1
            out["_stop"] = True
        except Exception as e:  # noqa: BLE001
            hooklog.log_failure("nightshift", f"dry-run {title} failed: {e}")
            out["failed"] += 1
        return

    token = 0
    try:
        lease = call_tool("relay", "relay_lease",
                          {"resource_id": f"fleet.{task_id}", "agent_id": worker}, cfg=cfg)
        if not (isinstance(lease, dict) and lease.get("acquired")):
            out["skipped"] += 1
            return
        token = int(lease.get("fencing_token") or 0)
        ctx = _task_context(task)
        try:
            if title == JOB_REAUTHOR:
                counter, result = _handle_reauthor(
                    ctx, call_tool=call_tool, cfg=cfg, post_json=post_json, base=base,
                    native=native, worker=worker, dry_run=False)
            elif title == JOB_VERDICT:
                counter, result = _handle_propose(
                    ctx, post_json=post_json, base=base, native=native, worker=worker,
                    dry_run=False)
            else:
                raise ValueError(f"unknown fleet job {title!r}")
        except transport.TransportError as e:
            hooklog.log_failure("nightshift", f"LLM transient, deferring: {e}")
            out["deferred"] += 1
            out["_stop"] = True
            return
        resp = call_tool("relay", "relay_task_update",
                         {"task_id": task_id, "status": "completed", "result": result[:500]},
                         cfg=cfg)
        if _relay_ok(resp):
            out[counter] += 1
            if counter == "reauthored":
                out["draft_skills"] += 1
        else:
            hooklog.log_failure("nightshift",
                                f"completion not confirmed for {task_id}: {resp!r}"[:300])
            out["failed"] += 1
    except Exception as e:  # noqa: BLE001 — one bad task never stops the shift
        hooklog.log_failure("nightshift", f"task {task_id} ({title}) failed: {e}")
        out["failed"] += 1
        try:
            call_tool("relay", "relay_task_update",
                      {"task_id": task_id, "status": "failed",
                       "result": f"night-shift: {e}"[:500]}, cfg=cfg)
        except Exception as e2:  # noqa: BLE001
            hooklog.log_failure("nightshift", f"task_update(failed) failed: {e2}")
    finally:
        if token:
            try:
                call_tool("relay", "relay_release",
                          {"resource_id": f"fleet.{task_id}", "agent_id": worker,
                           "fencing_token": token}, cfg=cfg)
            except Exception as e:  # noqa: BLE001
                hooklog.log_failure("nightshift", f"lease release failed: {e}")
```

(f) update the module docstring: first paragraph now says Night Shift drains the **fleet job catalog** — `distill_session` (the stop hook's), `reauthor_stale_skill` and `propose_contested_verdict` (cortex's nightly `fleet_enqueue_pass`) — and add a short "Job catalog" section describing each job's input, output and the drafts-only rule (spec decision 9).

- [ ] **Step 6: CLI** (`client/firekeep_client/cli.py::cmd_night_shift`) — replace the body:

```python
    from firekeep_client import nightshift, state

    out = nightshift.run(max_tasks=args.max, dry_run=args.dry_run)
    try:
        state.write_scratch("night_shift_last", json.dumps({
            "at": time.time(), "counts": {k: v for k, v in out.items() if k != "error"},
            "error": out.get("error"), "reported": False,
        }), ttl_seconds=7 * 86400)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the command
        pass
    if out.get("error"):
        print(f"firekeep night-shift: {out['error']}", file=sys.stderr)
        return 1
    mode = " (dry run)" if args.dry_run else ""
    print(f"firekeep night-shift{mode}: {out['distilled']} distilled, "
          f"{out['reauthored']} re-authored, {out['proposed']} verdict proposed, "
          f"{out['noop']} no-op, {out['legacy']} legacy, {out['duplicates']} duplicates, "
          f"{out['skipped']} skipped, {out['failed']} failed, {out['deferred']} deferred")
    if out.get("draft_skills") or out.get("proposed"):
        print("firekeep night-shift: draft skills and verdict proposals await review in "
              "the dashboard (Skills tab / Autopilot tab)")
    return 0
```

(`json`, `time`, `sys` are already imported at the top of `cli.py`; verify.) Update the subparser help to: `"drain the fleet queue (distill_session, reauthor_stale_skill, propose_contested_verdict) with a LOCAL model"`.

- [ ] **Step 7: Run**

Run: `cd client && python -m pytest tests/test_nightshift.py tests/test_cli.py -q`
Expected: PASS — including every pre-existing distill test (the distill path is unchanged apart from `origin_job` and `draft_skills`; if an old test asserts the exact `skill_create` argument dict, extend it with `origin_job: "distill_session"`).

- [ ] **Step 8: Commit**

Stage `client/firekeep_client/nightshift.py client/firekeep_client/cli.py client/tests/test_nightshift.py client/tests/test_cli.py`; message `feat(client): Night Shift job catalog — reauthor_stale_skill and propose_contested_verdict`.

---

### Task 9: Opportunistic drain at session start (`nightshiftdrain.py`)

**Files:**
- Create: `client/firekeep_client/nightshiftdrain.py`
- Modify: `client/firekeep_client/hooks/session_start.py:174-178` (the return chain)
- Test: `client/tests/test_nightshiftdrain.py`, `client/tests/hooks/test_session_start.py` (append)

**Interfaces:**
- Consumes: `firekeep_client.background.popen_kwargs()`, `state._scratch_file`, `state.read_scratch/write_scratch`, scratch `night_shift_last` (Task 8's shape).
- Produces: `nightshiftdrain.is_enabled(cfg) -> bool`, `local_llm_listening(timeout=0.25) -> bool`, `drain_interval_hours() -> float`, `should_drain(now=None) -> str | None` (the claim stamp), `maybe_spawn(cfg, stamp) -> bool`, `last_run_line() -> str`, `drain_nudge(cfg) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# client/tests/test_nightshiftdrain.py
"""The session-start night-shift drain: spawns only when a local model is
listening, once per interval across every window that opens, off with one
env var, and never costs the briefing anything."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time

import pytest

from firekeep_client import nightshiftdrain, state


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FIREKEEP_CONFIG", str(tmp_path / "config"))
    (tmp_path / "config").write_text("[dist]\nversion=1\n", encoding="utf-8")
    monkeypatch.delenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", raising=False)
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", raising=False)
    monkeypatch.delenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", raising=False)
    return tmp_path


def _cfg(auto_drain=None, hours=None):
    import configparser
    cfg = configparser.ConfigParser()
    if auto_drain is not None or hours is not None:
        cfg["nightshift"] = {}
        if auto_drain is not None:
            cfg["nightshift"]["auto_drain"] = auto_drain
        if hours is not None:
            cfg["nightshift"]["auto_drain_hours"] = hours
    return cfg


def _listening(monkeypatch, value: bool):
    monkeypatch.setattr(nightshiftdrain, "local_llm_listening", lambda timeout=0.25: value)


def _record_spawns(monkeypatch) -> list:
    real = subprocess.Popen
    seen: list = []

    def spy(argv, **kw):
        if any(str(a).endswith(("firekeep", "firekeep.exe")) for a in argv[:1]) and "night-shift" in argv:
            seen.append({"argv": argv, "kw": kw})
            return object()
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _forbid_spawn(monkeypatch, why: str) -> None:
    real = subprocess.Popen

    def guard(argv, **kw):
        if "night-shift" in argv:
            pytest.fail(why)
        return real(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", guard)


# --- gates -------------------------------------------------------------------

def test_enabled_by_default(home):
    assert nightshiftdrain.is_enabled(_cfg()) is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "anything"])
def test_env_off_switch(home, monkeypatch, val):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", val)
    assert nightshiftdrain.is_enabled(_cfg()) is False


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off"])
def test_env_falsey_values_keep_it_on(home, monkeypatch, val):
    monkeypatch.setenv("FIREKEEP_NO_AUTO_NIGHTSHIFT", val)
    assert nightshiftdrain.is_enabled(_cfg()) is True


def test_config_off_switch_only_on_explicit_false(home):
    assert nightshiftdrain.is_enabled(_cfg(auto_drain="false")) is False
    assert nightshiftdrain.is_enabled(_cfg(auto_drain="")) is True


def test_no_local_llm_means_silence_and_no_spawn(home, monkeypatch):
    _listening(monkeypatch, False)
    _forbid_spawn(monkeypatch, "spawned with no local model listening")
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


# --- probe -------------------------------------------------------------------

def test_probe_hits_configured_base_only(home, monkeypatch):
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_LLM_BASE", "http://10.0.0.5:8080/v1")
    asked = []

    def fake_connect(addr, timeout=None):
        asked.append(addr)
        raise OSError("closed")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert nightshiftdrain.local_llm_listening() is False
    assert asked == [("10.0.0.5", 8080)]


def test_probe_defaults_to_lm_studio_then_ollama(home, monkeypatch):
    asked = []

    class _Sock:
        def close(self):
            pass

    def fake_connect(addr, timeout=None):
        asked.append(addr)
        if addr == ("127.0.0.1", 11434):
            return _Sock()
        raise OSError("closed")

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert nightshiftdrain.local_llm_listening() is True
    assert asked == [("127.0.0.1", 1234), ("127.0.0.1", 11434)]


# --- cadence + claim ---------------------------------------------------------

def test_stamp_is_the_interval_bucket(home):
    six_h = 6 * 3600
    assert nightshiftdrain.should_drain(now=10 * six_h + 5) == "10"
    assert nightshiftdrain.should_drain(now=11 * six_h) == "11"


def test_interval_env_and_config(home, monkeypatch):
    assert nightshiftdrain.drain_interval_hours(_cfg()) == 6.0
    assert nightshiftdrain.drain_interval_hours(_cfg(hours="2")) == 2.0
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "3")
    assert nightshiftdrain.drain_interval_hours(_cfg(hours="2")) == 3.0
    monkeypatch.setenv("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "junk")
    assert nightshiftdrain.drain_interval_hours(_cfg()) == 6.0


def test_spawn_once_per_stamp_with_detached_argv(home, monkeypatch):
    _listening(monkeypatch, True)
    seen = _record_spawns(monkeypatch)
    import sys
    from pathlib import Path
    exe = Path(sys.executable).parent / ("firekeep.exe" if os.name == "nt" else "firekeep")
    monkeypatch.setattr(nightshiftdrain, "_firekeep_exe", lambda: exe)
    monkeypatch.setattr(exe.__class__, "exists", lambda self: True, raising=False)
    assert nightshiftdrain.maybe_spawn(_cfg(), "77") is True
    assert nightshiftdrain.maybe_spawn(_cfg(), "77") is True  # already claimed — in flight
    assert len(seen) == 1
    assert seen[0]["argv"][1:] == ["night-shift", "--max", "5"]
    kw = seen[0]["kw"]
    assert kw["stdin"] is subprocess.DEVNULL and kw["close_fds"] is True
    assert state._scratch_file("night_shift.77").exists()


def test_failed_spawn_releases_the_claim(home, monkeypatch):
    _listening(monkeypatch, True)
    import sys
    from pathlib import Path
    exe = Path(sys.executable)
    monkeypatch.setattr(nightshiftdrain, "_firekeep_exe", lambda: exe)

    def boom(argv, **kw):
        raise OSError("cannot exec")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert nightshiftdrain.maybe_spawn(_cfg(), "78") is False
    assert not state._scratch_file("night_shift.78").exists()


# --- the nudge ---------------------------------------------------------------

def test_nudge_names_the_off_switch(home, monkeypatch):
    _listening(monkeypatch, True)
    monkeypatch.setattr(nightshiftdrain, "maybe_spawn", lambda cfg, stamp: True)
    line = nightshiftdrain.drain_nudge(_cfg())
    assert line.startswith("\n\n[firekeep] night shift draining the fleet queue in background")
    assert "FIREKEEP_NO_AUTO_NIGHTSHIFT=1" in line


def test_nudge_is_silent_when_spawn_cannot_run(home, monkeypatch):
    _listening(monkeypatch, True)
    monkeypatch.setattr(nightshiftdrain, "maybe_spawn", lambda cfg, stamp: False)
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


def test_last_run_line_reports_once(home, monkeypatch):
    _listening(monkeypatch, False)  # no spawn today; the report still prints
    state.write_scratch("night_shift_last", json.dumps({
        "at": time.time() - 3600, "reported": False, "error": None,
        "counts": {"distilled": 1, "reauthored": 2, "proposed": 1, "draft_skills": 3}}))
    line = nightshiftdrain.drain_nudge(_cfg())
    assert "3 draft skill(s)" in line and "1 verdict proposal(s)" in line and "Autopilot" in line
    assert nightshiftdrain.drain_nudge(_cfg()) == ""  # marked reported
    assert json.loads(state.read_scratch("night_shift_last"))["reported"] is True


def test_last_run_with_nothing_to_review_is_silent(home, monkeypatch):
    _listening(monkeypatch, False)
    state.write_scratch("night_shift_last", json.dumps({
        "at": time.time(), "reported": False, "error": None,
        "counts": {"distilled": 2, "reauthored": 0, "proposed": 0, "draft_skills": 0}}))
    assert nightshiftdrain.drain_nudge(_cfg()) == ""


def test_nudge_never_raises(home, monkeypatch):
    monkeypatch.setattr(nightshiftdrain, "is_enabled", lambda cfg: (_ for _ in ()).throw(RuntimeError()))
    assert nightshiftdrain.drain_nudge(_cfg()) == ""
```

Append to `client/tests/hooks/test_session_start.py` (inside the file's existing structure, using its `client_env` fixture):

```python
class TestNightShiftDrainNudge:
    def test_the_drain_nudge_is_in_the_chain(self, client_env, monkeypatch):
        from firekeep_client import nightshiftdrain, transport
        from firekeep_client.hooks import _mcp, session_start
        monkeypatch.setattr(transport, "get_json", lambda url, **k: {"rendered": "BRIEFING"})
        monkeypatch.setattr(_mcp, "call_tool", lambda *a, **k: {})
        monkeypatch.setattr(nightshiftdrain, "drain_nudge",
                            lambda cfg: "\n\n[firekeep] night shift draining the fleet queue in background (test)")
        out = session_start.run({})
        assert "night shift draining" in out["systemMessage"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd client && python -m pytest tests/test_nightshiftdrain.py tests/hooks/test_session_start.py -q -k "drain or Drain"`
Expected: FAIL — `ModuleNotFoundError: firekeep_client.nightshiftdrain`.

- [ ] **Step 3: Implement `client/firekeep_client/nightshiftdrain.py`**

```python
"""Opportunistic Night Shift drain from session start (spec decision 2).

Nothing scheduled the drain before this: `firekeep night-shift` ran only when a
human typed it, so the fleet queue — distill tasks from every session end, and
since the job catalog the re-author and verdict tasks cortex enqueues nightly —
sat until someone remembered. This module is the fifth entry in the
session_start nudge chain, built like the other four (autoupdate, symdexindex,
docdexsync, maildexsync): a DETACHED spawn, an ATOMIC O_EXCL claim per interval
bucket so three windows opening together launch one shift, one env off-switch,
one banner line naming it, and it never raises.

It adds one precondition its siblings do not need: a LOCAL MODEL must be
listening. Night Shift refuses cloud models and aborts fast with no backend,
but a hook that spawned a process every six hours on a machine with no LM Studio
or Ollama would print a "draining" line about a shift that immediately quit. So
the nudge does a ≤250 ms TCP connect to the configured base (or the two default
ports) first and stays silent when nothing answers.

Private-session mode is not checked here on purpose: the dispatcher
short-circuits `session_start` while bypassed, and `nightshift.run()` refuses on
its own — two layers already.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from firekeep_client import background, state

_FALSEY = ("", "0", "false", "no", "off")
_DISABLE = ("0", "false", "no", "off")
SECTION = "nightshift"
DEFAULT_INTERVAL_HOURS = 6.0
DEFAULT_MAX_TASKS = 5
_DEFAULT_PORTS = (("127.0.0.1", 1234), ("127.0.0.1", 11434))  # LM Studio, Ollama
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
LAST_RUN_KEY = "night_shift_last"


def is_enabled(cfg) -> bool:
    """`FIREKEEP_NO_AUTO_NIGHTSHIFT` (env, wins) then `[nightshift] auto_drain`
    — the exact semantics of the four sibling triggers: the env var disables on
    any value not in _FALSEY; the config key disables only on an explicit false."""
    if os.environ.get("FIREKEEP_NO_AUTO_NIGHTSHIFT", "").strip().lower() not in _FALSEY:
        return False
    val = (cfg.get(SECTION, "auto_drain", fallback="true")
           if cfg.has_section(SECTION) else "true").strip().lower()
    return val not in _DISABLE


def drain_interval_hours(cfg=None) -> float:
    """Env `FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS`, then `[nightshift]
    auto_drain_hours`, default 6. Unparseable or non-positive → default."""
    raw = os.environ.get("FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS", "").strip()
    if not raw and cfg is not None and cfg.has_section(SECTION):
        raw = cfg.get(SECTION, "auto_drain_hours", fallback="").strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_HOURS
    return value if value > 0 else DEFAULT_INTERVAL_HOURS


def _probe_targets() -> list[tuple[str, int]]:
    base = os.environ.get("FIREKEEP_NIGHTSHIFT_LLM_BASE", "").strip()
    if not base:
        return list(_DEFAULT_PORTS)
    parts = urlsplit(base)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return [(host, port)]


def local_llm_listening(timeout: float = 0.25) -> bool:
    """A TCP connect, nothing more: cheap enough for a hook, and 'a port is open'
    is the only question worth asking before spawning — the shift does the real
    /models probe itself."""
    for host, port in _probe_targets():
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            continue
        try:
            sock.close()
        except OSError:
            pass
        return True
    return False


def should_drain(now: float | None = None, cfg=None) -> str:
    """The claim stamp: the interval bucket. Every session start inside one
    bucket shares a claim, and a shift that never landed retries next bucket."""
    now = time.time() if now is None else now
    return str(int(now // (drain_interval_hours(cfg) * 3600.0)))


def _claim_path(stamp: str) -> Path:
    tag = _UNSAFE.sub("_", stamp)[:40].strip("_") or "none"
    return state._scratch_file(f"night_shift.{tag}")


def _firekeep_exe() -> Path:
    """The `firekeep` console script next to the running interpreter — the venv
    this hook executes from; no PATH dependency (same as autoupdate)."""
    return Path(sys.executable).parent / ("firekeep.exe" if os.name == "nt" else "firekeep")


def maybe_spawn(cfg, stamp: str) -> bool:
    """True when a shift is in flight (spawned now, or already claimed for this
    stamp). False only when it can't run. Never raises."""
    try:
        if not is_enabled(cfg):
            return False
        exe = _firekeep_exe()
        if not exe.exists():
            return False
        claim = _claim_path(stamp)
        try:
            fd = os.open(str(claim), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            return True
        kwargs = background.popen_kwargs()
        # No cwd: the shift talks to the Keep and a local model, never the workspace.
        argv = [str(exe), "night-shift", "--max", str(DEFAULT_MAX_TASKS)]
        try:
            subprocess.Popen(argv, **kwargs)  # noqa: S603 — fixed argv, not shell-interpolated
        except Exception:  # noqa: BLE001
            try:
                claim.unlink()
            except OSError:
                pass
            return False
        return True
    except Exception:  # noqa: BLE001 — a drain must never cost a session
        return False


def last_run_line() -> str:
    """One line about the LAST shift, printed once: only when it left something
    for a human to review (draft skills or verdict proposals)."""
    try:
        raw = state.read_scratch(LAST_RUN_KEY)
        if not raw:
            return ""
        rec = json.loads(raw)
        if not isinstance(rec, dict) or rec.get("reported"):
            return ""
        counts = rec.get("counts") or {}
        drafts = int(counts.get("draft_skills") or 0)
        proposals = int(counts.get("proposed") or 0)
        if drafts <= 0 and proposals <= 0:
            return ""
        rec["reported"] = True
        state.write_scratch(LAST_RUN_KEY, json.dumps(rec), ttl_seconds=7 * 86400)
        parts = []
        if drafts:
            parts.append(f"{drafts} draft skill(s)")
        if proposals:
            parts.append(f"{proposals} verdict proposal(s)")
        return (f"\n\n[firekeep] night shift: {' and '.join(parts)} await review — "
                f"dashboard → Skills / Autopilot")
    except Exception:  # noqa: BLE001
        return ""


def drain_nudge(cfg) -> str:
    """What the session-start chain calls. Never raises; '' when nothing to say."""
    try:
        report = last_run_line()
        if not is_enabled(cfg) or not local_llm_listening():
            return report
        if not maybe_spawn(cfg, should_drain(cfg=cfg)):
            return report
        return (report + "\n\n[firekeep] night shift draining the fleet queue in background "
                "(local model; disable with `FIREKEEP_NO_AUTO_NIGHTSHIFT=1`)")
    except Exception:  # noqa: BLE001 — the nudge must never cost a session
        return ""
```

(Note the spawn-once test patches `_firekeep_exe` to a path and `exists` to True; keep `_firekeep_exe` a module-level function so it is patchable.)

- [ ] **Step 4: Wire it** (`client/firekeep_client/hooks/session_start.py`) — import `nightshiftdrain` with the other modules and append to the return chain:

```python
    return {"systemMessage": rendered + _update_nudge(cfg) + _unsigned_notice()
            + serverupdate.nudge_line(serverupdate.check(cfg))
            + symdexindex.index_nudge(cfg, payload)
            + docdexsync.sync_nudge(cfg)
            + maildexsync.sync_nudge(cfg)
            + nightshiftdrain.drain_nudge(cfg)}
```

Also extend the numbered comment above it (item 4) with: "…and, last, drain the fleet queue with Night Shift when a local model is listening (nightshiftdrain — spawns nothing and says nothing otherwise)."

- [ ] **Step 5: Run**

Run: `cd client && python -m pytest tests/test_nightshiftdrain.py tests/hooks/test_session_start.py -q`
Expected: PASS. If an existing session_start test asserts the exact `systemMessage` tail, the new nudge returns `""` there (no local model in tests) — confirm by monkeypatching `nightshiftdrain.local_llm_listening` to False in that test's fixture if the probe ever hits a real open port on the CI box.

- [ ] **Step 6: Commit**

Stage `client/firekeep_client/nightshiftdrain.py client/firekeep_client/hooks/session_start.py client/tests/test_nightshiftdrain.py client/tests/hooks/test_session_start.py`; message `feat(client): drain the fleet queue from session_start when a local model is listening`.

---

### Task 10: Dashboard — Fleet table, proposal rows, the missing `low_efficacy_skills` section

**Files:**
- Modify: `dashboard/index.html` (`renderAutopilotDigest` ~5840, `apContestedRow` ~5892, `AUTOPILOT_SECTIONS` ~5932 — all inside the `>>> autopilotPanel` … `<<< autopilotPanel` sentinels)
- Test: `tests/test_dashboard_autopilot.py` (append)

**Interfaces:**
- Consumes: digest `fleet.jobs` (Task 7 shape), inbox contested rows with `proposed_*` (Task 5), inbox `low_efficacy_skills` rows `{id, trigger, skill_efficacy, skill_efficacy_n}` (already emitted by the API).
- Produces: `apFleetTable(fleet) -> html`, `apRate(v) -> str`, `apEfficacyRow(it) -> html`. **Constraint:** no `'POST'|'PUT'|'PATCH'|'DELETE'` literal, no `method:`, no new `fetchJSON` inside the sentinels.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_dashboard_autopilot.py`, which provides `_render(fn, data)`, `_autopilot_js()`, `DIGEST`, `INBOX`, `_inbox(**overrides)`)

```python
# ------------------------------------------------------------------- fleet --

FLEET = {
    "enabled": True,
    "jobs": {
        "distill_session": {
            "window": {"produced": 0, "approved": 0, "rejected": 0, "approval_rate": None},
            "all_time": {"produced": 0, "approved": 0, "rejected": 0, "approval_rate": None, "pending": 0}},
        "reauthor_stale_skill": {
            "window": {"produced": 3, "approved": 2, "rejected": 1, "approval_rate": 0.667},
            "all_time": {"produced": 9, "approved": 5, "rejected": 2, "approval_rate": 0.714, "pending": 2}},
        "propose_contested_verdict": {
            "window": {"proposed": 2, "resolved": 1, "matched": 1, "match_rate": 1.0},
            "all_time": {"proposed": 4, "resolved": 1, "matched": 1, "match_rate": 1.0}},
    },
}


class TestFleet:
    def test_the_digest_renders_a_fleet_table(self):
        d = dict(DIGEST, fleet=FLEET)
        html = _render("renderAutopilotDigest", d)
        assert "Fleet" in html
        assert "Stale-skill re-author" in html and "Contested-verdict proposal" in html
        assert "67%" in html and "71%" in html   # window and all-time approval rates
        assert "100%" in html                    # match rate

    def test_a_null_rate_is_a_dash_never_zero_percent(self):
        html = _render("renderAutopilotDigest", dict(DIGEST, fleet=FLEET))
        assert "—" in html
        assert "0%" not in html.replace("100%", "")

    def test_no_fleet_block_renders_no_table(self):
        assert "Fleet" not in _render("renderAutopilotDigest", DIGEST)

    def test_a_contested_row_shows_the_proposal(self):
        row = {"id": "m1", "contested_with": "m2", "contested_at": "2026-09-01",
               "text_preview": "Deploy with update.sh",
               "proposed_verdict": {"action": "supersede", "winner_id": "m1"},
               "proposed_rationale": "m1 names the current script",
               "proposed_by": "night-shift", "proposed_at": "2026-09-02T03:00:00+00:00"}
        html = _render("apContestedRow", row)
        assert "Night Shift proposes" in html and "keep m1" in html and "supersede m2" in html
        assert "m1 names the current script" in html and "night-shift" in html

    def test_a_coexist_proposal_reads_as_both_true(self):
        row = {"id": "m1", "contested_with": "m2", "text_preview": "A",
               "proposed_verdict": {"action": "coexist", "winner_id": None},
               "proposed_rationale": "", "proposed_by": "night-shift", "proposed_at": ""}
        assert "both true" in _render("apContestedRow", row)

    def test_a_row_without_a_proposal_is_unchanged(self):
        row = {"id": "m1", "contested_with": "m2", "contested_at": "x", "text_preview": "A"}
        assert "proposes" not in _render("apContestedRow", row)

    def test_low_efficacy_section_is_listed(self):
        inbox = _inbox(items={"low_efficacy_skills": {"count": 1, "approximate": False, "items": [
            {"id": "s1", "trigger": "Rotate the key", "skill_efficacy": 0.31, "skill_efficacy_n": 7}]}})
        html = _render("renderAutopilotInbox", inbox)
        assert "Rotate the key" in html and "0.31" in html and "n=7" in html

    def test_every_api_section_key_has_a_dashboard_entry(self):
        """The class of drift low_efficacy_skills had: emitted, documented, and
        never rendered — so the headline total counted rows nobody could see."""
        api = (REPO_ROOT / "cortex/app/autopilot/api.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'"([a-z_]+)": await _section\(', api))
        block = _autopilot_js()
        listed = set(re.findall(r"\{ key: '([a-z_]+)'", block))
        assert emitted <= listed, f"API sections missing from AUTOPILOT_SECTIONS: {sorted(emitted - listed)}"
```

(`REPO_ROOT`/`re` — reuse the file's existing repo-root constant and imports; the read-only tests in the same file must still pass untouched.)

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_dashboard_autopilot.py -q -k "Fleet or fleet or efficacy or api_section"`
Expected: FAIL — `"Fleet" not in html`, `apContestedRow` lacks "proposes", `low_efficacy` missing.

- [ ] **Step 3: Implement** — inside the sentinels of `dashboard/index.html`:

After `apChip`:

```js
var AP_FLEET_JOBS = [
    ['distill_session', 'Session distill'],
    ['reauthor_stale_skill', 'Stale-skill re-author'],
    ['propose_contested_verdict', 'Contested-verdict proposal']
];

function apRate(v) {
    // null means "no evidence yet" — a rate is never invented from a prior, and
    // rendering it as 0% would read as "everything rejected".
    if (v == null) return '<span title="not enough verdicts yet">—</span>';
    return apEsc(Math.round(v * 100)) + '%';
}

function apFleetTable(fleet) {
    var jobs = fleet && fleet.jobs;
    if (!jobs) return '';
    var rows = AP_FLEET_JOBS.map(function(pair) {
        var j = jobs[pair[0]];
        if (!j) return '';
        var w = j.window || {}, a = j.all_time || {};
        var isVerdict = pair[0] === 'propose_contested_verdict';
        var produced = isVerdict ? (w.proposed || 0) : (w.produced || 0);
        var accepted = isVerdict ? (w.matched || 0) : (w.approved || 0);
        var declined = isVerdict ? ((w.resolved || 0) - (w.matched || 0)) : (w.rejected || 0);
        var rateW = isVerdict ? w.match_rate : w.approval_rate;
        var rateA = isVerdict ? a.match_rate : a.approval_rate;
        return '<tr><td>' + apEsc(pair[1]) + '</td>'
            + '<td style="text-align:right">' + apEsc(produced) + '</td>'
            + '<td style="text-align:right">' + apEsc(accepted) + '</td>'
            + '<td style="text-align:right">' + apEsc(declined) + '</td>'
            + '<td style="text-align:right">' + apRate(rateW) + '</td>'
            + '<td style="text-align:right">' + apRate(rateA) + '</td>'
            + '<td style="text-align:right;color:var(--text-dim)">' + apEsc(a.pending != null ? a.pending : '') + '</td></tr>';
    }).join('');
    if (!rows) return '';
    return '<div style="margin-top:14px"><div style="font-weight:600;font-size:13px;margin-bottom:6px">Fleet — what the night shift produced, and how much of it humans kept</div>'
        + '<div class="table-scroll"><table style="width:100%;font-size:12px">'
        + '<thead><tr><th style="text-align:left">Job</th><th>Produced</th><th>Approved / agreed</th>'
        + '<th>Rejected / disagreed</th><th>Rate (window)</th><th>Rate (all time)</th><th>Pending</th></tr></thead>'
        + '<tbody>' + rows + '</tbody></table></div>'
        + '<div style="font-size:11px;color:var(--text-dim);margin-top:4px">Approval rate = approved ÷ (approved + rejected); '
        + 'a verdict proposal counts as agreed when the human\'s resolve matched it. Read-only: approve in Skills, resolve pairs via the API.</div></div>';
}
```

In `renderAutopilotDigest`, right after `if (chips) out += ...;` add:

```js
    if (d.fleet) out += apFleetTable(d.fleet);
```

Replace `apContestedRow` with:

```js
function apContestedRow(it) {
    // The flag time is the point of the row: a contested pair is the one
    // lifecycle state the system deliberately refuses to decide on its own, so
    // how long it has sat unresolved is the number that decides whether it
    // matters. Both sides of a dispute are flagged, so one dispute is two rows.
    var html = '<div style="padding:7px 0;border-top:1px solid var(--border);font-size:13px">'
        + apEsc(it.text_preview || '')
        + '<span style="color:var(--text-dim);margin-left:8px">contests '
        + apEsc(it.contested_with || 'another memory')
        + (it.contested_at ? ' since ' + apEsc(it.contested_at) : '')
        + '</span>';
    var pv = it.proposed_verdict;
    if (pv && pv.action) {
        // A Night Shift PROPOSAL, never a verdict: the pair stays contested until
        // a human resolves it. Shown so the human starts from a reasoned draft.
        var verdict;
        if (pv.action === 'coexist') {
            verdict = 'both true (coexist)';
        } else {
            var keep = pv.winner_id || '?';
            var other = keep === it.id ? (it.contested_with || '?') : it.id;
            verdict = 'keep ' + apEsc(keep) + ', supersede ' + apEsc(other);
        }
        html += '<div style="margin-top:4px;font-size:12px;color:var(--amber)">Night Shift proposes: ' + verdict
            + (it.proposed_rationale ? ' <span style="color:var(--text-dim)">— ' + apEsc(it.proposed_rationale) + '</span>' : '')
            + '<span style="color:var(--text-dim)"> (' + apEsc(it.proposed_by || 'fleet')
            + (it.proposed_at ? ', ' + apEsc(it.proposed_at) : '') + ')</span></div>';
    }
    return html + '</div>';
}
```

After `apSkillRow` add:

```js
function apEfficacyRow(it) {
    // Score and n TOGETHER, always: a low-n score is mostly the neutral prior.
    return '<div style="padding:7px 0;border-top:1px solid var(--border);font-size:13px">'
        + apEsc(it.trigger || it.id)
        + '<span style="color:var(--text-dim);margin-left:8px">efficacy '
        + apEsc(it.skill_efficacy != null ? Number(it.skill_efficacy).toFixed(2) : '?')
        + ' (n=' + apEsc(it.skill_efficacy_n != null ? it.skill_efficacy_n : '?') + ')</span></div>';
}
```

In `AUTOPILOT_SECTIONS`, after the `rereview_skills` entry add:

```js
    { key: 'low_efficacy_skills', title: 'Skills scoring below neutral',
      cta: 'Open skills', action: "autopilotOpenSkills('active')",
      pick: function(s) { return s.items || []; }, row: apEfficacyRow },
```

- [ ] **Step 4: Run the whole dashboard guard**

Run: `python -m pytest tests/test_dashboard_autopilot.py -q`
Expected: PASS — including `TestRoundOneIsReadOnly` unchanged (grep the block yourself: `rg -n "'(POST|PUT|PATCH|DELETE)'|method:" dashboard/index.html` inside the sentinel range must return nothing new).

- [ ] **Step 5: Commit**

Stage `dashboard/index.html tests/test_dashboard_autopilot.py`; message `feat(dashboard): Fleet table in the Autopilot digest, proposals on contested rows, low-efficacy section`.

---

### Task 11: Repo documentation (guides, CLAUDE.md, README)

**Files:**
- Modify: `docs/guides/client-kit.md:877-878` (Night Shift section), `docs/guides/knowledge-autopilot.md` (§3 addendum, §4 addendum, new §8 before "What unlocks round 2"), `docs/guides/cortex-api-endpoints.md` (~line 35 and ~38), `CLAUDE.md` (kit paragraph + guide table row), `README.md` (feature table ~102, kit bullet ~362, dashboard row ~310)
- Test: existing guards — `client/tests/test_docs_reference_client_kit.py`, `tests/test_procedure_docs.py`, `cortex/tests/test_config_fleet.py`

- [ ] **Step 1: `docs/guides/client-kit.md`** — replace the section heading and paragraph at 877-878 with a section that keeps every existing sentence about the distill drain (the Ollama native path, cloud refusal, honest counting, dedup history) and prepends/appends the following:

Heading: `## Night Shift and the fleet job catalog (client kit — \`firekeep_client.nightshift\`, \`nightshiftdrain\`)`

Opening paragraph (new, before the existing text):

```markdown
**Night Shift is the Fleet-as-GPU drain, and since client 1.6.0 it drains a CATALOG, not one queue** (spec `docs/superpowers/specs/2026-09-02-fleet-as-gpu-mvp-design.md`). Three relay task titles, listed FIFO in this order under one `--max` budget: `distill_session` (enqueued by the `stop` hook — unchanged), `reauthor_stale_skill` and `propose_contested_verdict` (enqueued nightly by cortex's `fleet_enqueue_pass` for every stale active skill and every contested pair; see `docs/guides/knowledge-autopilot.md` §8). Every job runs against the same LOCAL model and writes only through review surfaces: a re-author produces a **new draft skill** carrying `origin_job="reauthor_stale_skill"` and `reauthor_of=<the stale skill's id>` (the original is untouched — a human activates the draft and deprecates the old one in the Skills tab); a verdict job writes a **proposal** (`POST /memory/contested/propose`: supersede-and-keep-X or coexist, with a rationale) onto the pair, which stays contested until a human calls `/memory/contested/resolve`. When the model judges a stale skill `still_valid` or `retire`, nothing is written — the verdict rides the relay task's result and the run summary (`noop`), and the stale flag stays in the inbox; writing that verdict onto the skill's inbox row is a named follow-up. New jobs lease `fleet.<task_id>`; distill keeps `distill.<task_id>`. An unconfirmed `skill_create` (an older cortex rejecting `origin_job`, a cross-workspace `reauthor_of` → 404, any in-band error) **fails the task loudly** — never a silent completion; a cortex 404/409 on a proposal fails that task, not the shift. The summary gains `reauthored`, `proposed`, `noop`, `draft_skills`, and `cmd_night_shift` records `night_shift_last` (`{at, counts, error, reported}`, 7-day TTL) in scratch for the next session start to read back.

**The drain is scheduled by session start, opportunistically** (`firekeep_client.nightshiftdrain`, the fifth entry in the `session_start` nudge chain after auto-update, symdex, docdex and maildex — same DETACHED `background.popen_kwargs()` spawn, same ATOMIC O_EXCL claim, same never-raises rule). One precondition its siblings do not need: a **local model must be listening** — a ≤250 ms TCP connect to `FIREKEEP_NIGHTSHIFT_LLM_BASE`'s host:port if set, else `127.0.0.1:1234` (LM Studio) then `:11434` (Ollama). Nothing answers → no spawn, no line: a machine without a local model never hears about the fleet. Otherwise one `firekeep night-shift --max 5` per interval bucket (`floor(now / interval)`; `FIREKEEP_NIGHTSHIFT_DRAIN_INTERVAL_HOURS` or `[nightshift] auto_drain_hours`, default `6`) across every window that opens, announced as `[firekeep] night shift draining the fleet queue in background (local model; disable with \`FIREKEEP_NO_AUTO_NIGHTSHIFT=1\`)`. Off-switches: `FIREKEEP_NO_AUTO_NIGHTSHIFT` (env, any value not in `"", 0, false, no, off`) or `[nightshift] auto_drain = false` (explicit false only). When the last shift left drafts or proposals behind, session start says so once — `[firekeep] night shift: N draft skill(s) and M verdict proposal(s) await review — dashboard → Skills / Autopilot` — then marks the record reported. Personal mode needs no new check: the dispatcher short-circuits `session_start` while bypassed and `nightshift.run()` refuses on its own. Guards: `client/tests/test_nightshift.py`, `client/tests/test_nightshiftdrain.py`, `client/tests/hooks/test_session_start.py::TestNightShiftDrainNudge`.
```

- [ ] **Step 2: `docs/guides/knowledge-autopilot.md`**

Append to §3 (after the resolution paragraph):

```markdown
**Proposed, then resolved (Fleet-as-GPU, 2026-09-02).** A client Night Shift worker can now file a *proposal* on a contested pair — `POST /memory/contested/propose` (`memory:write`), same shape as the verdict plus a `rationale` — which sets `proposed_verdict {action, winner_id}`, `proposed_rationale`, `proposed_by`, `proposed_at` on both points and nothing else: the pair stays contested, recall keeps annotating it, and the inbox row shows the proposal beside the pair. Only `/memory/contested/resolve` (a human) supersedes or coexists; it clears the four `proposed_*` fields with the contested flags and scores the proposal in the fleet ledger (`resolved`, plus `matched` when the human's action and winner equal the proposal's). A second proposal overwrites the first and is not counted again. Member-private points never get proposals because they are never enqueued (§8).
```

Append to §4 (after the `low_efficacy_skills` paragraph):

```markdown
**Fleet block in the digest (2026-09-02).** `GET /autopilot/digest` gains `fleet: {enabled, jobs}` — per job type (`distill_session`, `reauthor_stale_skill`, `propose_contested_verdict`) a `window` and an `all_time` block read from the fleet ledger (§8): `produced / approved / rejected / approval_rate` for skill jobs, `proposed / resolved / matched / match_rate` for verdicts, all-time `pending = produced − approved − rejected`. A rate is `null` when its denominator is zero — never a prior — and the dashboard renders `null` as `—`. The dashboard also now lists the `low_efficacy_skills` section it had been omitting (the headline total counted rows the panel never showed); `tests/test_dashboard_autopilot.py` pins that every section the API emits has a dashboard entry.
```

New section before `## What unlocks round 2`:

```markdown
## 8. The fleet: enqueue, drain, and the approval ledger (Fleet-as-GPU MVP, 2026-09-02)

The nightly passes already *find* the work a fleet could do — `skill_staleness_pass` flags skills nobody recalled in `SKILL_STALE_AFTER_DAYS`, `deep_contradiction_pass` contests unconfirmed pairs — and until now both sat in the inbox until a human got to them. `fleet_enqueue_pass` (`app/fleet/enqueue.py`, registered **after** `skill_staleness` in `run_memory_agent`, gated by `FLEET_ENQUEUE_ENABLED`, default on) posts one relay task per finding through relay's new `POST /tasks` using `RELAY_URL` + `FIREKEEP_INTERNAL_KEY` — the same seam the briefing reads `GET /tasks` through. (That route carries no per-route scope on purpose: the internal key has no `relay:*` scope and deployed keys are never re-scoped; see `docs/guides/relay-coordination.md`.) The tasks — `reauthor_stale_skill` with the skill's fields in `context`, `propose_contested_verdict` with both texts — are drained by client Night Shift workers against a **local** model (`docs/guides/client-kit.md`, Night Shift). Nothing generates on the server.

**Dedup is state-based, because relay tasks have no idempotency and expire in 7 days.** A stale skill is enqueued only if no skill with `reauthor_of == its id` exists in any status and no rejection marker names it (`fleet:rejected:reauthor_stale_skill:<id>`, set when a human deletes the fleet's draft, 90-day TTL); a contested pair only if neither side carries `proposed_verdict`. A live marker (`fleet:enqueued:<job>:<subject>`, `SET NX EX 7d`) stops double-posting while a task is in flight and expires with the task. Drained work never re-enqueues; expired work does; the whole night is capped at `FLEET_ENQUEUE_MAX_PER_RUN` (default 20, remainder reported as `capped`).

**Member-private never leaves.** Relay tasks are Keep-global (readable by every registered key, no workspace scoping) and the worker needs the text in `context`, so points with `visibility == "member"` are excluded at the query and a pair whose partner is private is skipped (`skipped_unpaired`). Cross-workspace writes fail server-side: `reauthor_of` must resolve inside the caller's workspace (404) and `propose` validates the pair like `resolve`.

**The ledger (`app/fleet/ledger.py`).** Rejection of a draft is *deletion* and no approval timestamp existed, so a rate read from Qdrant would forget every rejected draft. Monotonic Redis counters are written at the moments the store forgets: `produced` (a draft created with `origin_job`), `approved` (its draft→active PATCH — which now also stamps a real `approved_at` on every skill), `rejected` (DELETE of a fleet draft), `proposed` (first proposal on a pair), `resolved` / `matched` (a human verdict on a pair that carried one). `fleet:ledger:<job>` all-time plus `fleet:ledger:<job>:<YYYY-MM-DD>` per UTC day (400-day TTL) feed the digest's `fleet` block (§4) and the dashboard's Fleet table. **This is the kill metric**: a job type whose approval rate stays low after enough verdicts is a job type to switch off, on evidence.

Out of the MVP, named so nobody infers them: the Dreaming port onto the queue, capability tags, per-job token budgets in trace, the other catalog jobs (handoff brief, doc drift, evidence pack, calibration review, merge near-duplicates), a headless-agent tier, an OS scheduled task, and writing a `still_valid`/`retire` verdict onto the skill's inbox row.
```

- [ ] **Step 3: `docs/guides/cortex-api-endpoints.md`** — after the `/memory/contested/resolve` line add:

```markdown
- `POST /memory/contested/propose` — A fleet worker's PROPOSED verdict on a contested pair (`{winner_id, loser_id, action, rationale}`); sets `proposed_verdict/proposed_rationale/proposed_by/proposed_at` on both points, resolves nothing (409 when the pair is not mutually contested); requires `memory:write`. Cleared and scored by `/memory/contested/resolve`.
```

and extend the `GET /autopilot/inbox` line's list with "…contested pairs (with any fleet proposal), low-efficacy skills, eval DLQ" and the `/autopilot/digest` line (or add one) with "…plus a `fleet` block: per-job produced/approved/rejected (or proposed/resolved/matched) with null-when-no-evidence rates". Also, under skills: "`POST /skills` accepts `origin_job` and `reauthor_of` (404 when the referenced skill is outside the caller's workspace); `PATCH /skills/{id}` to `active` stamps `approved_at`; `DELETE` of a fleet draft records a rejection in the fleet ledger."

- [ ] **Step 4: `CLAUDE.md`** — in the "Install the client kit" section's closing paragraph ("Everything else about the kit — the five hook cores, night shift, …") leave the pointer, and add one sentence to the paragraph right after the field-failure paragraph:

```markdown
Night Shift is now the drain for a **fleet job catalog** (`distill_session`, `reauthor_stale_skill`, `propose_contested_verdict`): cortex's nightly `fleet_enqueue_pass` posts the latter two through relay's `POST /tasks`, `session_start` spawns `firekeep night-shift` in the background when a local model port answers (`FIREKEEP_NO_AUTO_NIGHTSHIFT=1` to stop), every output is a draft skill or a verdict *proposal* behind human review, and an approval-rate ledger per job type shows on the dashboard's Autopilot tab — see [`docs/guides/client-kit.md`](docs/guides/client-kit.md) "Night Shift and the fleet job catalog" and [`docs/guides/knowledge-autopilot.md`](docs/guides/knowledge-autopilot.md) §8.
```

In the "Feature guides" table, change the Knowledge Autopilot row's description to `Knowledge Autopilot — feedback, reaper, contested, inbox, the fleet ledger`.

- [ ] **Step 5: `README.md`**

Feature table, after the **Knowledge Autopilot** row:

```markdown
| **Fleet-as-GPU (Night Shift)** | The connected agents' own machines do the knowledge base's homework overnight, against a local model, and only ever produce drafts. The session-end hook queues a distill job per session; the server's nightly passes queue one job per stale skill (*re-author it*) and per contested memory pair (*propose a verdict*); `firekeep night-shift` drains the queue — started for you at session start whenever LM Studio or Ollama is listening. A human still activates every draft and resolves every pair, and an approval-rate ledger per job type on the Autopilot tab says whether the fleet's work is worth keeping. |
```

Replace the kit bullet at ~362 with:

```markdown
- Night Shift: `firekeep night-shift` drains the fleet job catalog — session distillation (queued by the session-end hook), stale-skill re-authoring and contested-verdict proposals (queued nightly by the server) — on your own machine against a local model (LM Studio or Ollama, auto-detected), writing memories, **draft** skills and verdict **proposals** attributed to their origin rather than the worker. Session start spawns it in the background when a local model port answers (`FIREKEEP_NO_AUTO_NIGHTSHIFT=1` to opt out). Generation stays off the server; cloud-hosted models are refused by default so session content cannot leave the machine
```

Dashboard row **Autopilot** (~310): append "…contested pairs with the fleet's proposed verdicts, and a Fleet table of per-job approval rates".

- [ ] **Step 6: Run the doc guards**

Run: `cd client && python -m pytest tests/test_docs_reference_client_kit.py -q && cd ../cortex && python -m pytest tests/test_config_fleet.py -q && cd .. && python -m pytest tests/test_procedure_docs.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

Stage the five docs files; message `docs: Fleet-as-GPU — catalog, auto-drain, enqueue pass, proposals and the approval ledger`.

---

### Task 12: firekeep.ai — docs page, then deploy

**Files (separate repo `E:\Documents\Projects\firekeep-site`, branch `master`, no remote):**
- Modify: `docs.html` — the CLI reference row (~line 1207) and the "The knowledge lifecycle, on autopilot" section (~1519-1545)

- [ ] **Step 1: CLI row** — replace the `firekeep night-shift` row with:

```html
          <tr><td><code>firekeep night-shift</code></td><td>Drain the fleet job queue with a local model (LM Studio or Ollama): distil queued sessions into memory and draft skills, re-author stale skills as new drafts, and propose verdicts on contested memories. Session start runs it for you when a local model is listening; <code>FIREKEEP_NO_AUTO_NIGHTSHIFT=1</code> opts out</td></tr>
```

- [ ] **Step 2: Autopilot section** — after the paragraph ending "…so every ranking has a visible why." insert:

```html
      <p>
        The homework gets done by your own machines. Overnight, the server queues one job per
        stale skill and one per contested pair; the next time an enrolled developer opens a
        session with LM Studio or Ollama running, <code>firekeep night-shift</code> starts in
        the background and works the queue against that <em>local</em> model — session content
        never leaves the machine, and nothing generates on the server. What comes back is
        never applied: a stale skill becomes a fresh <b>draft</b> that names the skill it
        rewrites, a contested pair gets a <b>proposed</b> verdict with its rationale shown
        beside the pair, and a human still activates every draft and resolves every pair. The
        Autopilot tab keeps score per job type — produced, approved, rejected — so you can see
        whether the fleet's work is worth keeping, and switch a job off on evidence rather than
        on hope.
      </p>
```

- [ ] **Step 3: Verify the HTML still parses** — `python -c "import html.parser,sys; p=html.parser.HTMLParser(); p.feed(open(r'E:\Documents\Projects\firekeep-site\docs.html',encoding='utf-8').read()); print('ok')"` and open the page locally to eyeball the two spots.

- [ ] **Step 4: Commit in the site repo** — `git -C E:\Documents\Projects\firekeep-site add docs.html && git -C E:\Documents\Projects\firekeep-site commit -m "docs: Night Shift drains the fleet job queue; autopilot section describes the fleet"` (run from the site repo, not the Firekeep worktree).

- [ ] **Step 5: Deploy per the recorded recipe** (memory `firekeep-site-publish`): backup first —
`ssh -p 65002 u784952002@82.180.175.177 'cd domains/firekeep.ai && tar czf public_html-backup-$(date +%Y%m%d-%H%M%S).tar.gz public_html'` — then from the site repo:
`tar czf - --exclude=.git --exclude=README.md --exclude=brand/README.md --exclude=.gitignore --exclude=scripts . | ssh -p 65002 u784952002@82.180.175.177 'tar xzf - -C domains/firekeep.ai/public_html'` (`--exclude=scripts` is load-bearing). Verify: `curl -fsS https://firekeep.ai/docs.html | grep -c "fleet job queue"` → `1` (Hostinger's bot check may 403 the first automated hit — retry once), `/README.md` → 404.

---

### Task 13: Final verification, branch finish

- [ ] **Step 1: Full suites** — run all four and the repo tests:

```bash
(cd relay && python -m pytest tests -q)
(cd cortex && python -m pytest tests -q)
(cd client && python -m pytest tests -q)
python -m pytest tests/test_dashboard_autopilot.py tests/test_no_dead_config.py tests/test_procedure_docs.py -q
```

Expected: all green; baseline was relay 182 / cortex 2828 / client 2016 passed — the new totals must be strictly higher with zero failures.

- [ ] **Step 2: Manual read-through of the critical path** — `cortex/app/fleet/enqueue.py` (the two `must_not` exclusions and the pairing loop), `client/firekeep_client/nightshift.py::_run_fleet_task` (TransportError handling in both places), `dashboard/index.html` sentinel block (`rg -n "'(POST|PUT|PATCH|DELETE)'|method:"` returns only pre-existing hits outside the block).

- [ ] **Step 3: Update the spec status line** to `**Status:** implemented on branch worktree-fleet-as-gpu (2026-09-02); see the plan for per-task commits.` and commit `docs(spec): mark fleet-as-gpu MVP implemented`.

- [ ] **Step 4: Finish the branch** — invoke `superpowers:finishing-a-development-branch` (rename the branch `feat/fleet-as-gpu-mvp` if desired), open the PR against `main` with the summary below and the required footer, then complete relay task `task-6e956e5f`, `memory_learn` the two non-obvious findings (internal key has no relay scope; state-based dedup vs relay TTL), and `ctx_complete_session`.

PR summary skeleton:

```markdown
## Fleet-as-GPU MVP

Spec: docs/superpowers/specs/2026-09-02-fleet-as-gpu-mvp-design.md · Plan: docs/superpowers/plans/2026-09-02-fleet-as-gpu-mvp.md

- **Relay** `POST /tasks` (REST twin of `relay_task_post`, shared helper, key-registered — the internal key has no relay scope)
- **Cortex** `fleet_enqueue_pass` (stale skills → `reauthor_stale_skill`, contested pairs → `propose_contested_verdict`; state-based dedup; member-private excluded; capped), `POST /memory/contested/propose`, `origin_job`/`reauthor_of`/`approved_at` on skills, the fleet ledger, `fleet` block in the autopilot digest
- **Client** Night Shift job catalog (drafts + proposals only), `nightshiftdrain` from `session_start` when a local model port answers
- **Dashboard** Fleet table, proposals on contested rows, the missing `low_efficacy_skills` section
- **Docs** guides, CLAUDE.md, README; firekeep.ai docs page deployed

Out of scope (recorded in the spec): Dreaming port, capability tags, token budgets, other catalog jobs, headless tier, OS scheduler.
```
