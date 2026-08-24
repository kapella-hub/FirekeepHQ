# Outcome Truth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Revision:** v9 — extends verified-principal authorization to the sibling public
> abandon tool without changing the reaper's manager path, preserves the authoritative
> grade across both terminal channels, makes active-pointer cleanup WATCH-safe, pipelines
> replay hydration, makes final webhook authority fail-silent, and replaces every v7
> false-green test sketch.

**Goal:** An authorized session completion carries an optional structured self-grade (`task_result` + `task_evidence`) that is bound to the verified principal, reaches the eval durably (service-only-scoped trigger hint AND snapshot-backed replay lift), and is accepted downstream only as the atomic recognized (grade, source) pair — with non-abandoned ungraded, sourceless, and legacy completions scoring as unknown, while an impersonated bound completion, public abandon, or takeover mutates nothing.

**Architecture:** Two new optional flat params on `ctx_complete_session` flow into the session hash (grade fields written monotonically under WATCH — never erasable), BOTH terminal replay events, and the eval-trigger query string. `owner_member` binds the verified principal at start and is immutable: a mismatch refuses the whole completion before any hash/pointer/queue/event/eval/skill side effect, refuses the public abandon wrapper before manager/post-abandon effects, and blocks bound cross-member takeover until an owner-authorized handoff exists. The reaper still calls the unchanged manager-level abandon path directly. The manager returns the authoritative stored grade pair, which both terminal events and the trigger use even when a submitted re-grade loses first-graded-wins. `find_terminal_grade` snapshots the last event IDs once and hydrates backward through a pipelined batch reader so neither concurrent appends, missing bodies, nor serial Redis round trips hide the grade. One shared normalizer set (`recognized_grade_pair` / `grade_from_events` / `binary_outcome`) plus a `mode="before"` EvalResult validator enforce the pair at every Cortex boundary. `store_eval` and `store_features` use WATCH/MULTI CAS for first-graded-wins and grade dominance; compute aborts downstream when nothing authoritative persisted and suppresses webhooks when the final authoritative reread is unavailable. The hint requires the SERVICE-ONLY `eval:grade` scope on a dedicated container-isolated `FIREKEEP_BRIDGE_KEY`; harden drops the I4 gate; patterns gain `"unknown"` + provenance, live card mutations use `xx=True, keepttl=True`, and the dead auto-analysis import stays dead. The replay event schema is untouched.

**Tech Stack:** Python 3.11+, FastMCP (in-memory wire tests for schema validation; lifespan-managed raw ASGI for the real auth-middleware path), Pydantic v2 (`model_validator(mode="before")`), Redis (WATCH/MULTI CAS, pipelining, `xx=True, keepttl=True`), pytest + AsyncMock for tool seams + shared-server fakeredis for deterministic CAS races, POSIX sh (deploy key-minting tests).

**Spec:** `docs/superpowers/specs/2026-08-23-outcome-truth-design.md` — read it before any task; decisions D1–D13 are cited below.

## Global Constraints

- **No instruction-text changes** (D12). Only the bridge tool docstring may change.
- **The manager/reaper abandon path is byte-identical** (D4): do not modify
  `SessionManager.abandon_session`, `bridge/app/reaper.py`, or the direct `reap_pass`
  assertions. The public `ctx_abandon_session` wrapper and its existing tool mocks do
  change; one such mock lives in `test_reaper.py` and must gain the new preflight reads.
- **`replay/models.py` untouched; event schema unchanged.** `replay/reader.py` gains `get_session_event_ids` and rewrites the existing `get_event_batch` implementation without changing its contract (D5): one MGET + one XRANGE pipeline per window, requested order preserved.
- **The pair/projection logic has ONE implementation** (D2): `recognized_grade_pair`, `grade_from_events`, `binary_outcome` in `cortex/app/evals/models.py`. No cortex task may inline a grade membership check — including `("success", "partial", "failure")` tuples. Bridge (a separate service, the pair's producer) owns one `TASK_RESULTS` constant in `app.session`, imported by its tool layer.
- **The dead auto-analysis import stays dead** (D11); **`compute_tip_effectiveness` must not extend card TTLs** (KEEPTTL).
- **Public terminal operations and bound-session takeover bind to the verified principal**
  (D13): complete, public abandon, and resume/takeover require the caller's verified
  `member_id` to match stored `owner_member`; `agent_id` is not authority. Invalid grade
  values from that owner remain non-fatal. Legacy unbound sessions may complete or be
  publicly abandoned under the old label check; their completions never grade.
- The recognized source is exactly `"self_reported"`, server-stamped. The hint is honored ONLY with `eval:grade` — not `admin`, not `"*"` (D8c).
- Bridge tool layer returns recoverable errors through MCP; it never reports success after CAS exhaustion or authorization failure. Eval-path code sanitizes before Pydantic construction; never-raise conventions throughout. Session hash values are flat strings; lists JSON-encoded.
- Commit after every task with EXPLICIT paths — never `git add -A` (unrelated work in tree: `scripts/installlab/lab.py`, `docs/marketing/`, `scripts/demo/`). Trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run each task's named tests before AND after implementing; full suites in Task 11.

## Phases

| Phase | Tasks | Ships on its own as |
|---|---|---|
| 1. Bridge | 1–2 | terminal operation authorized; authoritative grade stored + emitted on both events and trigger |
| 2. Replay tail | 3 | snapshot tail-ID reads + bounded-round-trip hydration |
| 3. Cortex evals | 4–5 | snapshot-scanned lift, first-graded-wins store, service-only hint scope on a dedicated key |
| 4. Graders | 6–8 | OWM + Tier B accept only the recognized pair; `_failure_rate` symmetric; I4 gate gone |
| 5. Patterns | 9 | provenance-filtered rates; legacy cards actually age out |
| 6. Docs + verify | 10–11 | guides truthful, guard tests green, all suites green |

---

### Task 1: Bridge storage — authorize completion and bound takeover; return the authoritative grade

**Files:**
- Modify: `bridge/app/session.py` (`start_session`, `complete_session`, `resume_session`, `get_session_data`).
- Create: `bridge/tests/test_outcome_truth_storage.py`.
- Modify: `bridge/tests/test_session.py` for bound-takeover authorization/immutability and existing signature assertions that gain `verified_member`.

**Interfaces:**
- `start_session(..., owner_member: str | None = None)` stores `owner_member or ""` exactly once.
- `bridge/app/session.py` owns `TASK_RESULTS = ("success", "partial", "failure")`; `mcp_server.py` imports it. Existing hash state is authoritative only when `owner_member` is non-empty, the grade is recognized, and `task_result_source == "self_reported"` — Bridge never fabricates the source for a sourceless stored string or treats an unbound session's stray pair as authority.
- `resume_session(..., verified_member: str | None = None)` refuses a bound cross-member resume before Lua/HSET/pointer effects, even with `takeover=True`; same-member label takeover and legacy-unbound behavior remain.
- `complete_session(..., task_result=None, task_evidence=None, verified_member=None) -> dict[str, Any]` WATCHes the session key, reads the full meta inside the watched section, then WATCHes and reads the relevant active-pointer keys before deciding which ones still point at this session. Every retry re-runs label + principal authorization and the pointer decision; status/outcome/distill enqueue/safe pointer cleanup plus the first grade commit in one MULTI, and the method returns the authoritative stored pair. Bound principal mismatch mutates nothing. A concurrent start/resume cannot have its newly-repointed active key deleted. Legacy unbound sessions complete ungraded. Retry exhaustion raises `RuntimeError` before any emit.
- Preserve positional compatibility: append `owner_member` at the end of `start_session`; add the three completion fields after the current `(session_id, outcome, agent_id)` arguments as keyword-only; add `verified_member` after resume's existing keyword-only `takeover`.
- `session.completed`, the return dict, and Task 2's outer event/hint all consume the same authoritative pair. `task_result_dropped` is diagnostic only: on a re-grade it accompanies the existing winner; on a legacy-unbound session it accompanies `(None, None)`. No caller derives authority from the presence of that note.
- The completion CAS guarantees one authoritative grade and safe pointer cleanup, not
  exactly-once effects: two authorized callers can each eventually commit after a
  WatchError retry, queueing/emitting twice. That pre-existing at-least-once behavior is
  disclosed in D13 and deliberately not expanded into an outbox in PR1.

- [ ] **Step 1: Add real-Redis-semantics tests.** Create `bridge/tests/test_outcome_truth_storage.py`; do NOT use the suite's `AsyncMock` pipeline for WATCH behavior (`await AsyncMock().hget(...)` is truthy). Use two fakeredis clients sharing one `FakeServer`:

```python
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
import redis

from app.config import Settings
from app.distill_worker import QUEUE_KEY
from app.session import SessionManager

SID = "sess-v9"
SKEY = f"nb:session:{SID}"


@pytest_asyncio.fixture
async def real_manager():
    server = fakeredis.FakeServer()
    r = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    mgr = SessionManager(r, Settings())
    yield mgr, r, server
    await r.aclose()


async def _seed(r, *, owner="member-alice", agent="alice-agent",
                status="active", grade=""):
    mapping = {
        "goal": "ship truth", "status": status, "agent_id": agent,
        "owner_member": owner, "tags": "[]", "task_result": grade,
        "task_result_source": "self_reported" if grade else "",
    }
    await r.hset(SKEY, mapping=mapping)
    await r.set(f"nb:active:{agent}", SID)


@pytest.mark.asyncio
async def test_enumerate_takeover_grade_attack_has_zero_terminal_side_effects(
    real_manager,
):
    """Mallory knows SID + Alice's label (ctx_list_sessions exposes both),
    tries the formerly-legal takeover, then tries completing with that known
    label. Neither half may mutate the bound session."""
    mgr, r, _ = real_manager
    await _seed(r)
    before = await r.hgetall(SKEY)
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        with pytest.raises(ValueError, match="verified owner"):
            await mgr.resume_session(
                SID, agent_id="mallory-agent", takeover=True,
                verified_member="member-mallory")
        with pytest.raises(ValueError, match="verified owner"):
            await mgr.complete_session(
                SID, outcome="pwned", agent_id="alice-agent",
                task_result="success", verified_member="member-mallory")
    assert await r.hgetall(SKEY) == before
    assert await r.get("nb:active:alice-agent") == SID
    assert await r.xlen(QUEUE_KEY) == 0
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_grade_is_stored_emitted_and_returned(real_manager):
    mgr, r, _ = real_manager
    await _seed(r)
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="done", agent_id="alice-agent",
            task_result="success", task_evidence=["pytest passed"],
            verified_member="member-alice")
    stored = await r.hgetall(SKEY)
    assert stored["task_result"] == "success"
    assert stored["task_result_source"] == "self_reported"
    assert json.loads(stored["task_evidence"]) == ["pytest passed"]
    assert result["task_result"] == "success"
    assert result["task_result_source"] == "self_reported"
    assert emit.await_args.kwargs["payload"]["task_result"] == "success"
    assert (await mgr.get_session_data(SID))["task_evidence"] == ["pytest passed"]


@pytest.mark.asyncio
async def test_existing_grade_is_authoritative_on_regrade(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="success")
    await r.hset(SKEY, "task_evidence", '["original"]')
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="again", agent_id="alice-agent",
            task_result="failure", verified_member="member-alice")
    assert (await r.hget(SKEY, "task_result")) == "success"
    assert result["task_result"] == "success"                 # authority, not attempt
    assert result["task_result_dropped"] == "session already has a stored grade"
    assert emit.await_args.kwargs["payload"]["task_result"] == "success"
    assert await r.hget(SKEY, "task_evidence") == '["original"]'


@pytest.mark.asyncio
async def test_ungraded_recompletion_cannot_erase_the_pair(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="failure")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, outcome="reworded", agent_id="alice-agent",
            task_result=None, verified_member="member-alice")
    assert await r.hget(SKEY, "task_result") == "failure"
    assert await r.hget(SKEY, "task_result_source") == "self_reported"
    assert result["task_result"] == "failure"
    assert emit.await_args.kwargs["payload"]["task_result"] == "failure"


@pytest.mark.asyncio
async def test_sourceless_hash_grade_is_not_fabricated_as_authoritative(real_manager):
    mgr, r, _ = real_manager
    await _seed(r, status="completed", grade="success")
    await r.hset(SKEY, "task_result_source", "")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, agent_id="alice-agent", task_result=None,
            verified_member="member-alice")
    assert result["task_result"] is None
    assert result["task_result_source"] is None
    assert "task_result" not in emit.await_args.kwargs["payload"]


@pytest.mark.asyncio
async def test_legacy_session_completes_but_never_grades(real_manager):
    mgr, r, _ = real_manager
    # Model partial-deploy/corrupt state too: recognized strings in an unbound
    # hash are not authority and must not escape through the terminal event.
    await _seed(r, owner="", grade="success")
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        result = await mgr.complete_session(
            SID, agent_id="alice-agent", task_result="failure",
            verified_member="member-alice")
    assert await r.hget(SKEY, "task_result") == "success"  # preserved, never trusted
    assert result["task_result"] is None
    assert result["task_result_source"] is None
    assert "pre-upgrade" in result["task_result_dropped"]
    assert "task_result" not in emit.await_args.kwargs["payload"]
```

Add the takeover refusal to `bridge/tests/test_session.py`, where the existing `manager` / `mock_redis` fixtures live (it exits before Lua support matters):

```python
@pytest.mark.asyncio
async def test_cross_member_takeover_of_bound_session_is_refused(manager, mock_redis):
    mock_redis.hgetall = AsyncMock(return_value={
        "status": "paused", "agent_id": "alice-agent",
        "owner_member": "member-alice"})
    with pytest.raises(ValueError, match="verified owner"):
        await manager.resume_session(
            SID, agent_id="mallory-agent", takeover=True,
            verified_member="member-mallory")
    mock_redis.eval.assert_not_awaited()
    mock_redis.hset.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_member_may_take_over_label_without_rebinding_owner(
    manager, mock_redis
):
    mock_redis.hgetall = AsyncMock(return_value={
        "status": "paused", "agent_id": "old-label",
        "owner_member": "member-alice"})
    result = await manager.resume_session(
        SID, agent_id="new-label", takeover=True,
        verified_member="member-alice")
    assert result["status"] == "active"
    mapping = mock_redis.hset.await_args.kwargs["mapping"]
    assert mapping["agent_id"] == "new-label"
    assert "owner_member" not in mapping
```

Add a deterministic two-client WATCH race. Gate the first `execute()` from each shared-server client until both have WATCHed the empty grade; the loser must retry and return the winner it re-read:

```python
@pytest.mark.asyncio
async def test_conflicting_completions_return_one_authoritative_winner(real_manager):
    mgr1, r1, server = real_manager
    r2 = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    mgr2 = SessionManager(r2, Settings())
    await _seed(r1)
    ready = 0
    ready_lock = asyncio.Lock()
    release = asyncio.Event()

    def gate_first_execute(client):
        nonlocal ready
        real_pipeline = client.pipeline
        first = {"pending": True}

        def factory(*args, **kwargs):
            pipe = real_pipeline(*args, **kwargs)
            real_execute = pipe.execute

            async def execute(*ea, **ek):
                nonlocal ready
                if first["pending"]:
                    first["pending"] = False
                    async with ready_lock:
                        ready += 1
                        if ready == 2:
                            release.set()
                    await asyncio.wait_for(release.wait(), timeout=2)
                return await real_execute(*ea, **ek)

            pipe.execute = execute
            return pipe

        client.pipeline = factory

    gate_first_execute(r1)
    gate_first_execute(r2)
    with patch("app.session._replay_emit", new=AsyncMock()):
        a, b = await asyncio.gather(
            mgr1.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice"),
            mgr2.complete_session(
                SID, agent_id="alice-agent", task_result="failure",
                verified_member="member-alice"),
        )
    winner = await r1.hget(SKEY, "task_result")
    assert winner in {"success", "failure"}
    assert a["task_result"] == b["task_result"] == winner
    await r2.aclose()
```

Add the exhaustion gate:

```python
@pytest.mark.asyncio
async def test_cas_exhaustion_commits_and_emits_nothing(real_manager):
    mgr, r, _ = real_manager
    await _seed(r)
    before = await r.hgetall(SKEY)
    real_pipeline = r.pipeline

    def always_stale(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)

        async def execute(*_args, **_kwargs):
            raise redis.WatchError("forced contention")

        pipe.execute = execute
        return pipe

    r.pipeline = always_stale
    with patch("app.session._replay_emit", new=AsyncMock()) as emit:
        with pytest.raises(RuntimeError, match="contended repeatedly"):
            await mgr.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice")
    assert await r.hgetall(SKEY) == before
    assert await r.get("nb:active:alice-agent") == SID
    assert await r.xlen(QUEUE_KEY) == 0
    emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_does_not_delete_a_concurrently_repointed_active_key(
    real_manager,
):
    """The pointer is part of the watched decision. Without WATCH(active_key),
    completion reads SID, a concurrent start/resume repoints it, and the stale
    transaction deletes the NEW session's pointer."""
    mgr, r, server = real_manager
    await _seed(r)
    competitor = fakeredis.aioredis.FakeRedis(
        server=server, decode_responses=True)
    active_key = "nb:active:alice-agent"
    real_pipeline = r.pipeline
    state = {"repointed": False}

    def repoint_before_first_exec(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        async def execute(*ea, **ek):
            if not state["repointed"]:
                state["repointed"] = True
                await competitor.set(active_key, "new-session")
            # Correct code WATCHed active_key, so this EXEC raises WatchError;
            # the retry re-reads "new-session" and omits the delete. Broken
            # code executes its stale DEL and makes the assertion below fail.
            return await real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    r.pipeline = repoint_before_first_exec
    try:
        with patch("app.session._replay_emit", new=AsyncMock()):
            result = await mgr.complete_session(
                SID, agent_id="alice-agent", task_result="success",
                verified_member="member-alice")
        assert result["task_result"] == "success"
        assert state["repointed"] is True
        assert await r.get(active_key) == "new-session"
    finally:
        await competitor.aclose()
```

- [ ] **Step 2: Run to verify failures** — `cd bridge && python -m pytest tests/test_outcome_truth_storage.py tests/test_session.py -v`.

- [ ] **Step 3: Implement the watched decision.** Add `import redis` and `_COMPLETE_CAS_RETRIES = 8`. `start_session` stores the immutable field. `resume_session` performs the bound-member refusal immediately after `hgetall`, before status/Lua/HSET work. `complete_session` keeps the submitted grade immutable and performs every mutable decision inside this loop shape:

```python
        submitted_grade = task_result if task_result in TASK_RESULTS else None
        session_key = self._session_key(session_id)
        for _attempt in range(_COMPLETE_CAS_RETRIES):
            try:
                async with self._r.pipeline(transaction=True) as pipe:
                    await pipe.watch(session_key)
                    meta = await pipe.hgetall(session_key)
                    if not meta:
                        await pipe.unwatch()
                        raise ValueError(f"Session {session_id} not found")

                    label_owner = meta.get("agent_id") or ""
                    if label_owner and label_owner != agent_id:
                        await pipe.unwatch()
                        raise ValueError(
                            f"Session {session_id} belongs to agent '{label_owner}'")

                    owner_member = meta.get("owner_member") or ""
                    if owner_member and verified_member != owner_member:
                        await pipe.unwatch()
                        raise ValueError(
                            f"Session {session_id} belongs to a different verified owner")

                    candidate = submitted_grade if owner_member else None
                    existing_grade = meta.get("task_result") or ""
                    if (
                        not owner_member
                        or existing_grade not in TASK_RESULTS
                        or meta.get("task_result_source") != "self_reported"
                    ):
                        existing_grade = ""
                    write_grade = bool(candidate) and not existing_grade
                    authoritative_grade = candidate if write_grade else existing_grade or None
                    pointer_keys = [
                        self._active_key(a)
                        for a in dict.fromkeys(
                            a for a in (label_owner, agent_id) if a)
                    ]
                    if pointer_keys:
                        # A start/resume may repoint one after this read. WATCH
                        # makes that invalidate EXEC instead of letting our
                        # stale transaction delete the new session's pointer.
                        await pipe.watch(*pointer_keys)
                        pointer_values = await pipe.mget(pointer_keys)
                        stale_pointers = [
                            key for key, value in zip(
                                pointer_keys, pointer_values, strict=True)
                            if value == session_id
                        ]
                    else:
                        stale_pointers = []

                    mapping = {
                        "status": "completed", "updated_at": now,
                        "outcome": outcome or "", "distillation": "queued",
                    }
                    if write_grade:
                        mapping.update({
                            "task_result": candidate,
                            "task_result_source": "self_reported",
                            "task_evidence": json.dumps(task_evidence or []),
                        })
                    pipe.multi()
                    pipe.hset(session_key, mapping=mapping)
                    for key in stale_pointers:
                        pipe.delete(key)
                    pipe.xadd(QUEUE_KEY, {
                        "session_id": session_id, "attempts": "0",
                        "next_attempt_at": str(time.time()),
                    })
                    await pipe.execute()
                    # State/grade correctness, not exactly-once effects: another
                    # authorized completion may retry and also commit, producing a
                    # duplicate queue row/event with this same authoritative grade.
                    break
            except redis.WatchError:
                continue
        else:
            raise RuntimeError(
                f"Session {session_id} completion contended repeatedly; retry")
```

After the successful EXEC, emit `session.completed` and return from the same pair:

```python
        completed_payload = {"outcome": outcome or ""}
        if authoritative_grade:
            completed_payload.update({
                "task_result": authoritative_grade,
                "task_result_source": "self_reported",
            })
        await _replay_emit(
            event_type="session.completed",
            session_id=session_id,
            agent_id=label_owner or "unknown",
            payload=completed_payload,
        )
```

Then return:

```python
        result: dict[str, Any] = {
            "status": "completed", "session_id": session_id,
            "task_result": authoritative_grade,
            "task_result_source": "self_reported" if authoritative_grade else None,
        }
        if submitted_grade and not owner_member:
            result["task_result_dropped"] = (
                "pre-upgrade session has no verified owner binding")
        elif submitted_grade and not write_grade:
            result["task_result_dropped"] = "session already has a stored grade"
        return result
```

`get_session_data` decodes `task_evidence`; `**meta` already carries the two scalar grade fields. Grep after implementation: `owner_member` appears in a write mapping only in `start_session`.

- [ ] **Step 4: Run** `cd bridge && python -m pytest tests/test_outcome_truth_storage.py tests/test_session.py -v`.

- [ ] **Step 5: Commit**

```bash
git add bridge/app/session.py bridge/tests/test_session.py bridge/tests/test_outcome_truth_storage.py
git commit -m "feat(bridge): authorize terminal sessions and return the authoritative grade" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Bridge tools — verified terminal authority, truthful emits, grade on the trigger

**Files:**
- Modify: `bridge/app/mcp_server.py` (`ctx_start_session` :343-382,
  `ctx_complete_session` :621-682, `ctx_abandon_session` :685-713,
  `_trigger_eval` :244-293, new `_verified_member_id` helper)
- Test: Create `bridge/tests/test_outcome_truth.py`
- Modify: `bridge/tests/test_fire_and_forget_eval.py` (pin the grade query parameter while retaining the no-hint exact dict)
- Modify: `bridge/tests/test_header_identity.py`, `bridge/tests/test_mcp_tools.py`, and
  the public-tool fixture only in `bridge/tests/test_reaper.py` (configure the public
  abandon wrapper's new SID/binding reads; production reaper assertions remain intact)

**Interfaces:**
- Consumes Task 1's signatures and authoritative return value.
- `_verified_member_id() -> str | None` reads the auth middleware's verified identity from `principal_from_scope(get_http_request().scope)`. It returns `None` outside a request; an auth-disabled request resolves to the deployment owner through `principal_from_scope`.
- `ctx_start_session` stores that principal. `ctx_resume_session` passes it to the manager. `ctx_complete_session` adds `task_result`/`task_evidence`, coerces an invalid string without losing the session, and passes the principal.
- `ctx_abandon_session` resolves one exact SID using explicit > header > active-pointer
  precedence, reads its immutable `owner_member`, and refuses a bound missing/mismatched
  principal before `SessionManager.abandon_session` or `after_abandon`. It passes the
  resolved SID to the manager — never `None` after authorizing a fallback target.
  `SessionManager.abandon_session` and the reaper remain unchanged. Legacy-unbound
  public abandon retains the label check and is an explicit D13 residual.
- A manager authorization/CAS failure returns an error before replay, eval, or skill side effects. A successful call uses only the manager's authoritative grade pair for the outer `session_end`, response, and `_trigger_eval` hint. A losing re-grade can therefore report `task_result_dropped` while still emitting the already-stored grade.
- Verified facts this design rests on: bridge `/mcp` runs `FirekeepKeyAuthMiddleware` in production (`__main__`, mcp_server.py:925-935) which attaches `{workspace_id, member_id, credential_id, scopes}` to `scope["state"]["identity"]` (auth/asgi.py:119-126); `get_http_request` is importable from `fastmcp.server.dependencies` on 3.1.1; no bridge tool reads the principal today — this is the first, so a propagation test is mandatory. Client sends a distinct per-member `X-API-Key` (resolver.py:379-386); `X-Agent-Id` is decorative.

- [ ] **Step 1: Write the failing tests** — create `bridge/tests/test_outcome_truth.py`. Unit tests use the `test_fire_and_forget_eval.py` patch set PLUS `patch("app.mcp_server._verified_member_id", return_value="member-alice")`:

```python
"""The session_end event tells the truth about the task (spec D1-D3, D8, D13)."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import mcp_server


def _mgr(complete_result=None, complete_error=None):
    mgr = AsyncMock()
    default = {
        "status": "completed", "session_id": "s1",
        "task_result": None, "task_result_source": None,
    }
    mgr.complete_session = AsyncMock(
        return_value=complete_result or default, side_effect=complete_error)
    return mgr


async def _complete(member="member-alice", mgr=None, **kwargs):
    mgr = mgr or _mgr()
    emit = AsyncMock()
    trigger = AsyncMock(return_value=True)
    skill = AsyncMock(return_value=True)
    with patch("app.mcp_server._get_manager", new=AsyncMock(return_value=mgr)), \
         patch("app.mcp_server.get_http_headers", return_value={}), \
         patch("app.mcp_server._verified_member_id", return_value=member), \
         patch("app.mcp_server._trigger_eval", new=trigger), \
         patch("app.mcp_server._trigger_skill_evaluate", new=skill), \
         patch("app.mcp_server._replay_emit", new=emit):
        result = await mcp_server.ctx_complete_session(**kwargs)
    tasks = list(mcp_server._background_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    return result, emit, mgr, trigger, skill


@pytest.mark.asyncio
async def test_graded_completion_threads_principal_and_emits_the_grade():
    authoritative = {
        "status": "completed", "session_id": "s1",
        "task_result": "failure", "task_result_source": "self_reported",
    }
    result, emit, mgr, trigger, _ = await _complete(
        mgr=_mgr(authoritative), outcome="done", task_result="failure",
        task_evidence=["3 tests red"])
    assert mgr.complete_session.await_args.kwargs["verified_member"] == "member-alice"
    assert mgr.complete_session.await_args.kwargs["task_result"] == "failure"
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] == "failure"
    assert call.args[3]["task_result"] == "failure"
    assert call.args[3]["task_result_source"] == "self_reported"
    assert trigger.call_args.kwargs.get("task_result") == "failure"
    assert result["task_result"] == "failure"


@pytest.mark.asyncio
async def test_losing_regrade_emits_the_authoritative_existing_grade():
    mgr = _mgr({
        "status": "completed", "session_id": "s1",
        "task_result": "success", "task_result_source": "self_reported",
        "task_result_dropped": "session already has a stored grade",
    })
    result, emit, _, trigger, _ = await _complete(
        mgr=mgr, outcome="x", task_result="failure")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] == "success"
    assert call.args[3]["task_result"] == "success"
    assert trigger.call_args.kwargs["task_result"] == "success"
    assert result["task_result"] == "success"
    assert "task_result_dropped" in result


@pytest.mark.asyncio
async def test_ungraded_completion_emits_no_outcome():
    result, emit, _, trigger, _ = await _complete(outcome="done")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] is None
    assert "task_result" not in call.args[3]
    assert result["task_result"] is None


@pytest.mark.asyncio
async def test_sourceless_manager_pair_is_not_forwarded():
    mgr = _mgr({
        "status": "completed", "session_id": "s1",
        "task_result": "success", "task_result_source": None,
    })
    result, emit, _, trigger, _ = await _complete(mgr=mgr, outcome="done")
    call = next(c for c in emit.await_args_list if c.args[0] == "session_end")
    assert call.kwargs["outcome"] is None
    assert "task_result" not in call.args[3]
    assert trigger.call_args.kwargs["task_result"] is None
    assert result["task_result"] is None


@pytest.mark.asyncio
async def test_invalid_grade_string_is_coerced_not_fatal():
    result, emit, mgr, _, _ = await _complete(
        outcome="done", task_result="great success")
    assert mgr.complete_session.await_args.kwargs["task_result"] is None
    assert result["task_result"] is None
    assert "task_result_note" in result


@pytest.mark.asyncio
async def test_principal_refusal_has_no_tool_side_effects():
    mgr = _mgr(complete_error=ValueError("different verified owner"))
    result, emit, _, trigger, skill = await _complete(
        member="member-mallory", mgr=mgr, outcome="x", task_result="success")
    assert "different verified owner" in result["error"]
    emit.assert_not_awaited()
    trigger.assert_not_awaited()
    skill.assert_not_awaited()


@pytest.mark.parametrize("explicit_sid", [True, False])
@pytest.mark.asyncio
async def test_bound_abandon_refuses_forged_label_before_side_effects(explicit_sid):
    """D13: ctx_list_sessions exposes SID + label; neither an explicit SID nor
    the legacy active-pointer fallback may turn that label into abandon authority."""
    mgr = AsyncMock()
    mgr.get_active_session_id = AsyncMock(return_value="s1")
    mgr.get_session_data = AsyncMock(return_value={
        "session_id": "s1", "agent_id": "alice-agent",
        "owner_member": "member-alice",
    })
    after = AsyncMock()
    with patch("app.mcp_server._get_manager", new=AsyncMock(return_value=mgr)), \
         patch("app.mcp_server._header_session_id", return_value=None), \
         patch("app.mcp_server._verified_member_id", return_value="member-mallory"), \
         patch("app.mcp_server.after_abandon", new=after):
        result = await mcp_server.ctx_abandon_session(
            session_id="s1" if explicit_sid else None,
            agent_id="alice-agent")
    assert "different verified owner" in result["error"]
    mgr.get_session_data.assert_awaited_once_with("s1")
    if explicit_sid:
        mgr.get_active_session_id.assert_not_awaited()
    else:
        mgr.get_active_session_id.assert_awaited_once_with("alice-agent")
    mgr.abandon_session.assert_not_awaited()
    after.assert_not_awaited()


@pytest.mark.asyncio
async def test_evidence_is_trimmed_and_needs_a_grade():
    _, _, mgr, _, _ = await _complete(
        outcome="done", task_result="success",
        task_evidence=["x" * 400] + [f"e{i}" for i in range(11)])
    ev = mgr.complete_session.await_args.kwargs["task_evidence"]
    assert len(ev) == 10 and len(ev[0]) == 300
    _, _, mgr2, _, _ = await _complete(
        outcome="done", task_evidence=["orphan"])
    assert mgr2.complete_session.await_args.kwargs["task_evidence"] == []


def test_verified_member_id_parses_a_scope_identity(monkeypatch):
    """Unit test of the HELPER only — the real propagation is pinned by the
    integration test below."""
    class _Req:
        scope = {"state": {"identity": {
            "workspace_id": "w", "member_id": "member-alice",
            "credential_id": "c", "scopes": []}}}
    monkeypatch.setattr("app.mcp_server.get_http_request", lambda: _Req(),
                        raising=False)
    assert mcp_server._verified_member_id() == "member-alice"


def test_verified_member_id_is_none_outside_a_request(monkeypatch):
    def _boom():
        raise RuntimeError("no active request")
    monkeypatch.setattr("app.mcp_server.get_http_request", _boom, raising=False)
    assert mcp_server._verified_member_id() is None


@pytest.mark.asyncio
async def test_verified_member_propagates_through_middleware_and_fastmcp(monkeypatch):
    """D13 INTEGRATION: the REAL path — accepted credential → FirekeepKeyAuthMiddleware
    → scope['state']['identity'] → get_http_request() inside the tool — over
    lifespan-managed ASGI (the fake-scope test above proves only the helper's
    parsing). The raw-ASGI path was probe-confirmed on fastmcp 3.1.1."""
    import httpx
    from auth.asgi import build_auth_middleware
    from auth.config import AuthSettings

    # 1. Auth ON, and validate_key resolves exactly one test credential. The ASGI
    #    middleware imports validate_key INTO auth.asgi (auth/asgi.py:25:
    #    `from auth.keys import ... validate_key`), so the patch target is the
    #    NAME IN auth.asgi, not auth.middleware (the binding is verified at
    #    auth/asgi.py:25 and this raising=True patch will fail on drift).
    async def _fake_validate(api_key, redis_client=None):
        if api_key == "nxs_test-key":
            return {"workspace_id": "w", "member_id": "member-alice",
                    "credential_id": "c", "scopes": ["session:write"],
                    "authenticated": True}
        return None
    monkeypatch.setattr("auth.asgi.validate_key", _fake_validate, raising=True)
    # 2. Stub the lifespan workers and the tool's side effects; capture the manager.
    async def _noop():
        return None
    monkeypatch.setattr("app.distill_worker.distill_worker_loop", _noop)
    monkeypatch.setattr("app.reaper.reaper_loop", _noop)
    monkeypatch.setattr("app.distill_worker.close_distiller", _noop)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr("app.mcp_server._replay_emit", AsyncMock())
    monkeypatch.setattr("app.mcp_server._trigger_eval", AsyncMock(return_value=True))
    monkeypatch.setattr("app.mcp_server._trigger_skill_evaluate",
                        AsyncMock(return_value=True))

    # 3. Drive a raw streamable-HTTP tools/call through the real middleware.
    from app.mcp_server import mcp
    app = mcp.http_app(
        middleware=build_auth_middleware(
            AuthSettings(ENABLED=True, REDIS_URL="redis://unused/7")),
        stateless_http=True,
    )
    headers = {
        "X-API-Key": "nxs_test-key",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "ctx_complete_session",
                   "arguments": {"outcome": "done"}},
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/mcp", json=body, headers=headers)

    assert response.status_code == 200
    assert mgr.complete_session.await_args.kwargs["verified_member"] == "member-alice"
```

Plus the module-level `_stub_lifespan` helper (referenced by the wire tests — `Client(mcp)` runs the lifespan, and patching `app.mcp_server._lifespan` does nothing since FastMCP captured the reference at construction) and the two wire tests:

```python
def _stub_lifespan(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr("app.distill_worker.distill_worker_loop", _noop)
    monkeypatch.setattr("app.reaper.reaper_loop", _noop)
    monkeypatch.setattr("app.distill_worker.close_distiller", _noop)


@pytest.mark.asyncio
async def test_wire_wrong_typed_grade_is_rejected_before_the_tool_runs(monkeypatch):
    """D1: FastMCP validates annotations pre-function — a numeric task_result
    is a recoverable client error, the session untouched (fastmcp 3.1.1)."""
    _stub_lifespan(monkeypatch)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    from fastmcp import Client
    from app.mcp_server import mcp
    async with Client(mcp) as client:
        res = await client.call_tool_mcp("ctx_complete_session", {"task_result": 123})
        assert res.isError
    mgr.complete_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_wire_invalid_grade_string_reaches_coercion(monkeypatch):
    """A wrong VALUE (valid type) passes validation and is coerced in-body."""
    _stub_lifespan(monkeypatch)
    mgr = _mgr()
    monkeypatch.setattr("app.mcp_server._get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr("app.mcp_server._verified_member_id", lambda: "member-alice")
    monkeypatch.setattr("app.mcp_server._replay_emit", AsyncMock())
    monkeypatch.setattr("app.mcp_server._trigger_eval", AsyncMock(return_value=True))
    monkeypatch.setattr("app.mcp_server._trigger_skill_evaluate",
                        AsyncMock(return_value=True))
    from fastmcp import Client
    from app.mcp_server import mcp
    async with Client(mcp) as client:
        res = await client.call_tool_mcp(
            "ctx_complete_session", {"outcome": "done", "task_result": "great success"})
        assert not res.isError
    mgr.complete_session.assert_awaited_once()
    tasks = list(mcp_server._background_tasks)
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
```

Add the start/resume wiring gates:

```python
@pytest.mark.asyncio
async def test_start_binds_the_verified_member(monkeypatch):
    mgr = AsyncMock()
    mgr.start_session.return_value = {"session_id": "s1"}
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-alice")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(mcp_server, "_replay_emit", AsyncMock())
    monkeypatch.setattr(mcp_server, "assemble_prior_art",
                        AsyncMock(return_value={}))
    await mcp_server.ctx_start_session("goal")
    assert mgr.start_session.await_args.kwargs["owner_member"] == "member-alice"


@pytest.mark.asyncio
async def test_resume_threads_verified_member(monkeypatch):
    mgr = AsyncMock()
    mgr.get_session_data.return_value = {"goal": "g", "status": "active"}
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-alice")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    monkeypatch.setattr(mcp_server, "assemble_shadow", lambda data: "shadow")
    await mcp_server.ctx_resume_session("s1", takeover=True)
    assert mgr.resume_session.await_args.kwargs["verified_member"] == "member-alice"


@pytest.mark.asyncio
async def test_refused_resume_does_not_read_shadow(monkeypatch):
    mgr = AsyncMock()
    mgr.resume_session.side_effect = ValueError("different verified owner")
    monkeypatch.setattr(mcp_server, "_get_manager", AsyncMock(return_value=mgr))
    monkeypatch.setattr(mcp_server, "_verified_member_id",
                        lambda: "member-mallory")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    result = await mcp_server.ctx_resume_session("s1", takeover=True)
    assert "different verified owner" in result["error"]
    mgr.get_session_data.assert_not_awaited()
```

Still in Step 1, update every pre-existing public-abandon mock explicitly — never let
an unconfigured `AsyncMock().get_session_data()` stand in for a dict (it is truthy and
creates a false authorization result):

- `test_header_identity.py`: header and explicit cases return
  `{"owner_member": ""}` from `get_session_data`; the no-header case also returns
  `"sess-ptr"` from `get_active_session_id` and now asserts that exact frozen SID reaches
  `abandon_session` (public fallback behavior is unchanged; only resolution moved up).
- `test_mcp_tools.py`: the success case resolves `"s1"`, returns an explicit unbound data
  dict, makes `abandon_session` return that SID, and patches `after_abandon`; the no-active
  error case returns `None` from `get_active_session_id` and asserts `abandon_session`
  was never awaited.
- `test_fire_and_forget_eval.py::test_abandon_returns_fast_when_eval_hangs`: resolve
  `"s1"` and return an explicit unbound data dict before the existing timing assertion.
- `test_reaper.py::test_human_abandon_payload_stays_reaped_free`: resolve `"s1"`, return
  `{"owner_member": "member-alice"}`, and patch `_verified_member_id` to
  `"member-alice"`; this pins an authorized bound public abandon. All direct `reap_pass`
  tests stay byte-identical.

- [ ] **Step 2: Run to verify they fail** — `cd bridge && python -m pytest tests/test_outcome_truth.py tests/test_header_identity.py tests/test_mcp_tools.py tests/test_fire_and_forget_eval.py tests/test_reaper.py -v`.

- [ ] **Step 3: Implement.** Import `TASK_RESULTS` beside `SessionManager`; add the limits + helper in `bridge/app/mcp_server.py`:

```python
_MAX_EVIDENCE_ITEMS = 10
_MAX_EVIDENCE_CHARS = 300


def _verified_member_id() -> str | None:
    """The authenticated member behind this request, or None when unknowable.

    FirekeepKeyAuthMiddleware (installed on /mcp in __main__) validates
    X-API-Key and attaches the verified identity to scope['state'];
    principal_from_scope also handles the auth-disabled case (anonymous owner
    principal). Outside an authenticated HTTP context (in-memory tests, auth
    enabled but identity missing) this returns None — and a None principal
    can never authorize a bound public terminal operation (D13)."""
    try:
        from auth.principal import principal_from_scope
        return principal_from_scope(get_http_request().scope).get("member_id")
    except Exception:
        return None
```

(Import `get_http_request` at module level in the same guarded block as `get_http_headers`; the tests patch the exact `app.mcp_server.get_http_request` binding.)

`ctx_start_session` resolves `owner = _verified_member_id()` and passes `owner_member=owner`. `ctx_resume_session` passes `verified_member=_verified_member_id()`; the manager, not the label or `takeover` flag, decides whether the operation is authorized.

`_trigger_eval` preserves its existing third positional argument and adds a keyword-only hint:

```python
async def _trigger_eval(
    api_url: str, session_id: str, max_retries: int = 3,
    *, task_result: str | None = None,
):
    # ...inside each attempt, before client.post:
    params = {"trigger": "session_complete"}
    if task_result is not None:
        params["task_result"] = task_result
    # client.post(..., params=params)
```

The docstring notes D8: the hint survives a lost replay emit and is honored only under `eval:grade`.

Extend the existing captured-params test in `test_fire_and_forget_eval.py`: its no-hint assertion remains exactly `{"trigger": "session_complete"}`; a second call with `task_result="failure"` must capture `{"trigger": "session_complete", "task_result": "failure"}`.

`ctx_complete_session`:

```python
@mcp.tool()
async def ctx_complete_session(
    session_id: str | None = None, outcome: str | None = None,
    agent_id: str = "default", skill_worthy: bool = False,
    task_result: str | None = None, task_evidence: list[str] | None = None,
) -> dict:
    """Mark the current session as completed and save learnings to long-term memory.

    Call this when your task is done. The session is enqueued for distillation into a
    FirekeepCortex memory (a background worker drains the queue with retry/backoff) so
    future sessions can benefit from what you learned.

    Args:
        session_id: Session to complete (defaults to the connection's
            X-Session-Id header, then your active session).
        outcome: Summary of what was accomplished (free text — prose, not the grade).
        agent_id: Your agent identifier.
        skill_worthy: Set True if this session involved a hard-won fix worth saving as a skill.
        task_result: Structured self-grade of the TASK itself: "success" (the goal
            was verifiably achieved), "partial" (real progress, goal not reached),
            or "failure". Omit when genuinely unsure — an honest absence beats a
            guessed grade. Accepted only from the session's verified owner.
        task_evidence: Up to 10 short verifiable claims backing the grade
            (tests run, commands that passed, files changed). Ignored without a grade.
    """
    agent_id = _default_agent_id(agent_id)
    if session_id is None:
        session_id = _header_session_id()

    # Spec D1: coerce invalid VALUES, never fail. (Wrong TYPES are rejected by
    # FastMCP validation pre-function; wire-tested.)
    graded = task_result if task_result in TASK_RESULTS else None
    evidence = [
        e.strip()[:_MAX_EVIDENCE_CHARS]
        for e in (task_evidence or []) if isinstance(e, str) and e.strip()
    ][:_MAX_EVIDENCE_ITEMS] if graded else []

    mgr = await _get_manager()
    try:
        result = await mgr.complete_session(
            session_id=session_id, outcome=outcome, agent_id=agent_id,
            task_result=graded, task_evidence=evidence,
            verified_member=_verified_member_id(),
        )
    except (ValueError, RuntimeError) as e:
        return {"error": str(e)}

    sid = result["session_id"]
    # Task 1 returns the stored winner. Never reconstruct authority from input.
    authoritative_grade = result.get("task_result")
    authoritative_source = result.get("task_result_source")
    if (
        authoritative_grade not in TASK_RESULTS
        or authoritative_source != "self_reported"
    ):
        authoritative_grade = None
        authoritative_source = None
        result["task_result"] = None
        result["task_result_source"] = None

    # Replay: trace session end. Spec D3 — the emit kwarg carries the GRADE,
    # or None (event carries no outcome) when ungraded. The old hard-coded
    # outcome="success" meant "the RPC worked" and fed every downstream
    # success metric; see docs/superpowers/specs/2026-08-23-outcome-truth-design.md.
    payload: dict = {"outcome": outcome or "", "distilled": False}
    if authoritative_grade and authoritative_source:
        payload["task_result"] = authoritative_grade
        payload["task_result_source"] = authoritative_source
    await _replay_emit(
        "session_end", sid, agent_id, payload, outcome=authoritative_grade)

    if task_result is not None and task_result not in TASK_RESULTS:
        result["task_result_note"] = (
            f"ignored invalid task_result {task_result!r}; "
            f"expected one of {', '.join(TASK_RESULTS)}"
        )
```

and the trigger dispatch:

```python
    _spawn_background(_trigger_eval(
        settings.FIREKEEP_API_URL, sid, task_result=authoritative_grade))
```

Keep `ctx_abandon_session`'s public signature and explicit > header prelude, then replace
its manager call with this principal preflight. `owner_member` is immutable (Task 1), so
the authorization fact cannot change between this read and the existing manager call:

```python
    mgr = await _get_manager()
    try:
        resolved_sid = session_id
        if resolved_sid is None:
            resolved_sid = await mgr.get_active_session_id(agent_id)
        if not resolved_sid:
            raise ValueError("No active session")

        data = await mgr.get_session_data(resolved_sid)
        if not data:
            raise ValueError(f"Session {resolved_sid} not found")
        owner_member = data.get("owner_member") or ""
        if owner_member and _verified_member_id() != owner_member:
            raise ValueError(
                f"Session {resolved_sid} belongs to a different verified owner")

        result = await mgr.abandon_session(
            session_id=resolved_sid, agent_id=agent_id)
    except ValueError as e:
        return {"error": str(e)}

    sid = result.get("session_id", resolved_sid)
    if sid:
        await after_abandon(sid, agent_id)
    return result
```

Do not move this check into `SessionManager.abandon_session`: the reaper is a trusted
internal caller of that method and must retain its exact path. Resolving the active
pointer in the tool is not optional; reading one SID's owner and then passing `None`
would reintroduce a race/bypass when the manager resolves the pointer later.

`after_abandon`, its fire-and-forget eval trigger, `SessionManager.abandon_session`, and
`bridge/app/reaper.py` remain byte-identical.

Only after the manager succeeds may `ctx_complete_session` call `_replay_emit`, spawn the eval, or call `_trigger_skill_evaluate`. In `ctx_resume_session`, return immediately on manager refusal and do not fetch/render the shadow.

- [ ] **Step 4: Run** `cd bridge && python -m pytest tests/ -v` — all pass; the direct
  reaper path and assertions are unmodified.

- [ ] **Step 5: Commit**

```bash
git add bridge/app/mcp_server.py bridge/tests/test_outcome_truth.py bridge/tests/test_fire_and_forget_eval.py bridge/tests/test_header_identity.py bridge/tests/test_mcp_tools.py bridge/tests/test_reaper.py
git commit -m "feat(bridge): principal-bound terminal tools; truthful session outcomes" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Replay reader — `get_session_event_ids` (tail-ID snapshot)

**Files:**
- Modify: `replay/reader.py` (add the snapshot reader; batch the existing hydrator)
- Create: `replay/tests/test_reader_tail.py`
- Modify: `replay/tests/test_reader_perf.py`

**Interfaces:**
- Consumes: `rp:session_idx:{sid}` zset (constants at reader.py:19-22).
- Produces: `get_session_event_ids(r, session_id, *, limit: int = 5000) -> list[str]` — the newest `limit` event IDs, oldest-first, via a SINGLE `zrange(idx, -limit, -1)`. Task 4's `find_terminal_grade` snapshots this once and hydrates locally, which is snapshot-stable against concurrent appends (the ID list is fixed) AND immune to missing bodies (iteration walks IDs, not hydrated events) — the two hazards D7 addresses (round-6 finding 2). Do NOT add a live-paging `get_session_tail` — a rank-relative window read between appends is exactly the non-stable primitive being avoided.
- `get_event_batch` preserves request order and missing-ID behavior, but performs one `MGET` for event-index keys and one non-transactional pipeline of exact `XRANGE`s. A 5,000-event grade scan must not make roughly 10,000 serial Redis round trips.
- **Test fixture:** use the same real-Redis fixture shape as `replay/tests/test_e2e.py`; the fixture parameter is `setup_emitter`, not `redis_fixture`.

- [ ] **Step 1: Write the failing test** — create `replay/tests/test_reader_tail.py` with the `redis_client` + `setup_emitter` fixtures copied from `test_e2e.py`, importing `emit` from `replay.emitter`:

```python
"""get_session_event_ids: a snapshot-stable tail-ID list (outcome truth D7).

The grade lift snapshots the newest event IDs once, then hydrates locally —
immune to concurrent appends (fixed ID list) and to missing bodies (walks
IDs, not hydrated events)."""
import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from replay.config import ReplaySettings
from replay.emitter import close_emitter, emit, init_emitter
from replay.reader import get_event_batch


@pytest_asyncio.fixture
async def redis_client():
    r = aioredis.from_url("redis://localhost:6379/6", decode_responses=True)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not available on localhost:6379")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest_asyncio.fixture
async def setup_emitter(redis_client):
    settings = ReplaySettings(
        ENABLED=True,
        REDIS_URL="redis://localhost:6379/6",
        STREAM_MAXLEN=10000,
    )
    await init_emitter(redis_client=redis_client, settings=settings)
    yield redis_client
    await close_emitter()


@pytest.mark.asyncio
async def test_ids_are_newest_last_and_bounded(setup_emitter):
    r = setup_emitter
    from replay.emitter import emit
    for i in range(5):
        await emit("ctx_update", "sess-n", "agent", {"i": str(i)})
    from replay.reader import get_session_event_ids
    ids = await get_session_event_ids(r, "sess-n", limit=3)
    assert len(ids) == 3                              # newest 3 only
    events = await get_event_batch(r, ids)
    assert [e["payload"]["i"] for e in events] == [2, 3, 4]
    assert await get_session_event_ids(r, "nope") == []
    assert await get_session_event_ids(r, "sess-n", limit=0) == []


@pytest.mark.asyncio
async def test_snapshot_is_stable_under_appends(setup_emitter):
    """Round-6 finding 2: a live rank-relative window would shift under
    appends and skip the grade; the ID snapshot does not."""
    r = setup_emitter
    from replay.emitter import emit
    from replay.reader import get_session_event_ids
    for i in range(10):
        await emit("ctx_update", "s", "agent", {"i": str(i)})
    snap = await get_session_event_ids(r, "s", limit=10)
    for i in range(200):                              # heavy concurrent appends
        await emit("memory_read", "s", "agent", {"j": str(i)})
    # the snapshot still names the original 10 events, unshifted
    assert await get_session_event_ids(r, "s", limit=10) != snap  # live read moved
    assert len(snap) == 10                            # our captured list did not
```

- [ ] **Step 2: Run to verify it fails** — `cd replay && python -m pytest tests/test_reader_tail.py -v` → ImportError.

- [ ] **Step 3: Implement** in `replay/reader.py`:

```python
async def get_session_event_ids(
    r: aioredis.Redis,
    session_id: str,
    *,
    limit: int = 5000,
) -> list[str]:
    """The newest `limit` event IDs for a session, oldest-first — ONE zrange.

    The grade lift (find_terminal_grade) snapshots this once and hydrates it
    locally in backward windows. A single ID snapshot is snapshot-stable
    against concurrent appends (the list is fixed) and immune to missing
    bodies (callers iterate IDs, not hydrated events) — the two hazards that
    sank live rank-relative paging."""
    if limit <= 0:
        return []
    idx_key = f"{_SESSION_IDX_PREFIX}{session_id}"
    ids = await r.zrange(idx_key, -limit, -1)
    return [i.decode() if isinstance(i, bytes) else i for i in ids]
```

(The bytes guard preserves compatibility with callers that do not set `decode_responses`.) Rewrite `get_event_batch` without changing its return contract:

```python
    unique_ids = list(dict.fromkeys(event_ids))
    stream_ids = await r.mget(
        [f"{_EVENT_IDX_PREFIX}{event_id}" for event_id in unique_ids])

    indexed = [
        (event_id, stream_id)
        for event_id, stream_id in zip(unique_ids, stream_ids, strict=True)
        if stream_id
    ]
    async with r.pipeline(transaction=False) as pipe:
        for _, stream_id in indexed:
            pipe.xrange(_STREAM_KEY, min=stream_id, max=stream_id, count=1)
        rows = await pipe.execute()

    found: dict[str, dict[str, Any]] = {}
    for (event_id, _), entries in zip(indexed, rows, strict=True):
        if entries:
            stream_id, fields = entries[0]
            if fields.get("id") == event_id:
                found[event_id] = _parse_event(stream_id, fields)
    return [found[event_id] for event_id in event_ids if event_id in found]
```

Add a duplicate-and-missing-ID regression test to `test_reader_tail.py`: request `[id2, "missing", id1, id2]` and assert the returned IDs are `[id2, id1, id2]`.

Extend `TestGetEventBatchBenchmark` in `test_reader_perf.py` with this real-Redis gate (import `get_session_event_ids`):

```python
    @pytest.mark.asyncio
    async def test_grade_scan_hydrates_5000_bodies_under_ten_seconds(
        self, setup_emitter
    ):
        r = setup_emitter
        await _emit_n_events(5000, session_id="grade-scan")  # seed: not timed
        ids = await get_session_event_ids(r, "grade-scan", limit=5000)
        assert len(ids) == 5000

        hydrated = []
        started = time.monotonic()
        for end in range(len(ids), 0, -200):
            hydrated.extend(await get_event_batch(
                r, ids[max(0, end - 200):end]))
        elapsed = time.monotonic() - started

        assert len(hydrated) == 5000
        assert {event["id"] for event in hydrated} == set(ids)
        assert elapsed < 10.0, f"5k hydration took {elapsed:.2f}s"
```

The deliberately loose ceiling catches the former serial-round-trip implementation without making ordinary CI jitter a failure. The fixture already skips when real Redis is unavailable.

- [ ] **Step 4: Run** `cd replay && python -m pytest tests/test_reader_tail.py tests/test_reader_perf.py -v`, then `python -m pytest tests/ -v`.

- [ ] **Step 5: Commit**

```bash
git add replay/reader.py replay/tests/test_reader_tail.py replay/tests/test_reader_perf.py
git commit -m "feat(replay): get_session_event_ids — snapshot-stable tail-ID list" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Cortex evals — normalizers, snapshot-scanned lift, first-graded-wins store, authoritative downstream

**Files:**
- Modify: `cortex/app/evals/models.py`, `cortex/app/evals/compute.py:46-230`, `cortex/app/evals/store.py:21-42`, `cortex/app/evals/api.py:48-94`
- Modify: `cortex/tests/test_evals.py` (extend) and `cortex/tests/test_eval_attribution.py` (its compute helper must persist authority)

**Interfaces:**
- Consumes: Task 2's trigger param + terminal-event pairs; Task 3's ID snapshot and pipelined hydrator.
- Produces (D2's single implementation — every later task imports these):
  - In `cortex/app/evals/models.py`: `recognized_grade_pair(result, source)`, `grade_from_events(events)` (both terminal types, last recognized pair wins), and `binary_outcome(result) -> str` (success/failure pass through; partial/None → `"unknown"`).
  - `EvalResult.task_result` / `task_result_source` Literal fields + `model_validator(mode="before")` normalizing the RAW mapping — `mode="after"` never runs for invalid literals (field validation raises first; verified under Pydantic 2.12.5), so normalization must precede field parsing and junk records must parse to an ungraded pair rather than fail wholesale.
  - `find_terminal_grade(replay_redis, session_id) -> tuple[str | None, str | None]` in `cortex/app/evals/compute.py` — SNAPSHOTS the newest `_GRADE_SCAN_MAX = 5000` event IDs once via `get_session_event_ids`, then hydrates them in local 200-ID windows newest-first through `grade_from_events`, stopping at the first recognized pair (snapshot-stable + missing-body-immune, D7). Used by the lift AND by harden (Task 8).
  - `compute_session_eval(replay_redis, session_id, trigger=..., task_result_hint=None)` — hint validated via `recognized_grade_pair(task_result_hint, "self_reported")` (no inline tuples); `EvalResult.outcome = task_result or ("failure" if failure_event_ids else "unknown")`; after the store: authoritative reload on rejection, and **ABORT (DLQ `failure_type="store"`, return None, no features/webhooks)** when neither write nor reload yields a record (D9c).
  - `store_eval`: ungraded is NX-create-only; graded runs a WATCH/MULTI CAS loop (read → decide replace-only-if-missing-or-ungraded → atomic set) → **deterministic first-graded-wins, no expiry window, nothing to fence** (D9b).
  - Route gains the `task_result` Query param (gated in Task 5).

- [ ] **Step 1: Write the failing tests** — append to `cortex/tests/test_evals.py`. First add the following self-contained `_compute` helper and `rr` fixture at module scope; do not import a test helper across modules:

```python
import json

import fakeredis.aioredis
import pytest_asyncio


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def _compute(monkeypatch, events, *, id_events=None, task_result_hint=None,
                   replay_redis=None, webhook_sink=None):
    """Drive compute_session_eval with patched replay readers.

    id_events models the session-tail SNAPSHOT as an ordered (oldest→newest)
    list of (event_id, event_or_None); None means the body is MISSING from
    get_event_batch (trimmed/expired) — the snapshot ID stays but hydration
    omits it. This exercises find_terminal_grade's real shape: snapshot the
    IDs once, hydrate backward in windows.

    replay_redis defaults to a fresh fakeredis so store_eval actually persists
    (under the authoritative-store rule an unstored candidate returns None)."""
    import replay.reader as reader_mod
    from app.evals import compute as compute_mod

    owned_redis = replay_redis is None
    if owned_redis:
        import fakeredis.aioredis
        replay_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    pairs = list(id_events or [])
    id_to_ev = {i: ev for i, ev in pairs if ev is not None}

    async def fake_summary(*a, **k):
        return {"event_count": max(len(events), 1), "duration_ms": 1000,
                "agents": ["default"]}

    async def fake_timeline(*a, **k):
        return {"events": events}

    async def fake_ids(r, sid, *, limit=5000):
        return [i for i, _ in pairs][-limit:]

    async def fake_batch(r, ids):
        return [id_to_ev[i] for i in ids if i in id_to_ev]

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_timeline", fake_timeline)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)
    # Keep every test in-process: the production webhook client targets the
    # compose hostname `redis`, which is deliberately unreachable here.
    from unittest.mock import AsyncMock
    import app.webhooks as webhooks_mod
    sink = webhook_sink if webhook_sink is not None else AsyncMock()
    webhook_redis = AsyncMock()
    monkeypatch.setattr(compute_mod.aioredis, "from_url",
                        lambda *a, **k: webhook_redis)
    monkeypatch.setattr(webhooks_mod, "fire_webhooks", sink)
    try:
        return await compute_mod.compute_session_eval(
            replay_redis=replay_redis, session_id="s1",
            task_result_hint=task_result_hint)
    finally:
        if owned_redis:
            await replay_redis.aclose()
```

Then the tests:

```python
class TestNormalizers:
    def test_pair_normalizer(self):
        from app.evals.models import recognized_grade_pair
        assert recognized_grade_pair("success", "self_reported") == ("success", "self_reported")
        assert recognized_grade_pair("success", None) == (None, None)
        assert recognized_grade_pair("success", "vibes") == (None, None)
        assert recognized_grade_pair("amazing", "self_reported") == (None, None)

    def test_binary_outcome_projection(self):
        from app.evals.models import binary_outcome
        assert binary_outcome("success") == "success"
        assert binary_outcome("failure") == "failure"
        assert binary_outcome("partial") == "unknown"
        assert binary_outcome(None) == "unknown"

    def test_before_validator_normalizes_invalid_literals_without_raising(self):
        """mode='after' would never run: Literal field validation raises
        first. The before-validator normalizes the RAW mapping (verified
        under Pydantic 2.12.5)."""
        from app.evals.models import EvalResult
        m = EvalResult(session_id="s", trigger="manual",
                       task_result="amazing", task_result_source="self_reported")
        assert m.task_result is None and m.task_result_source is None
        m = EvalResult(session_id="s", trigger="manual",
                       task_result="success", task_result_source="vibes")
        assert m.task_result is None and m.task_result_source is None
        m = EvalResult(session_id="s", trigger="manual", task_result="success")
        assert m.task_result is None and m.task_result_source is None
        raw = ('{"session_id": "s", "trigger": "manual", '
               '"task_result": "amazing", "task_result_source": 3}')
        m = EvalResult.model_validate_json(raw)     # junk stored record parses
        assert m.task_result is None

    def test_grade_from_events_reads_both_terminal_channels(self):
        from app.evals.models import grade_from_events
        ok = {"task_result": "success", "task_result_source": "self_reported"}
        assert grade_from_events(
            [{"event_type": "session.completed", "payload": ok}]) == ("success", "self_reported")
        assert grade_from_events(
            [{"event_type": "session.completed",
              "payload": {"task_result": "success"}}]) == (None, None)
        assert grade_from_events(
            [{"event_type": "ctx_update", "payload": ok}]) == (None, None)

    def test_grade_from_events_tolerates_a_non_dict_payload(self):
        """Round-6 finding 6: a non-empty list/string payload must degrade to
        (None, None), not raise into the eval catch-all and DLQ the whole
        computation — `p = payload or {}` keeps a truthy non-dict, so the
        isinstance guard is load-bearing."""
        from app.evals.models import grade_from_events
        assert grade_from_events(
            [{"event_type": "session_end", "payload": ["not", "a", "dict"]}]) == (None, None)
        assert grade_from_events(
            [{"event_type": "session_end", "payload": "junk"}]) == (None, None)


class TestTaskResultLifting:
    @pytest.mark.asyncio
    async def test_hint_wins_and_survives_a_lost_emit(self, monkeypatch):
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=[], task_result_hint="failure")
        assert result.task_result == "failure"
        assert result.task_result_source == "self_reported"
        assert result.outcome == "failure"

    @pytest.mark.asyncio
    async def test_lift_finds_the_grade_under_post_completion_noise(self, monkeypatch):
        """D7: the graded session_end sits early, then 250 newer events. The
        snapshot names all of them; hydrating backward in windows finds the
        grade in a later window."""
        grade_ev = {"event_type": "session_end",
                    "payload": {"task_result": "success",
                                "task_result_source": "self_reported"}}
        # oldest→newest: the grade, then 250 noise events (all newer)
        pairs = [("g", grade_ev)] + [
            (f"n{i}", {"event_type": "memory_read", "payload": {}}) for i in range(250)]
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=pairs)
        assert result.task_result == "success"

    @pytest.mark.asyncio
    async def test_lift_walks_past_a_hole_of_missing_bodies(self, monkeypatch):
        """Finding 6: some IDs in the newest window have NO body (trimmed /
        expired). Iterating IDs (not hydrated events) must keep walking to the
        grade; a hydrated-count terminator would stop early."""
        grade_ev = {"event_type": "session_end",
                    "payload": {"task_result": "failure",
                                "task_result_source": "self_reported"}}
        # grade oldest; then 50 present noise; then 200 MISSING bodies (newest)
        pairs = ([("g", grade_ev)]
                 + [(f"n{i}", {"event_type": "memory_read", "payload": {}})
                    for i in range(50)]
                 + [(f"m{i}", None) for i in range(200)])
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=pairs)
        assert result.task_result == "failure"

    @pytest.mark.asyncio
    async def test_append_after_snapshot_cannot_shift_the_scan(self, monkeypatch, rr):
        """Capture IDs once, then append 300 newer IDs while the first window
        hydrates. The frozen list still reaches the original terminal grade."""
        from app.evals import compute as compute_mod
        from replay import reader as reader_mod

        grade = {"event_type": "session_end", "payload": {
            "task_result": "success", "task_result_source": "self_reported"}}
        frozen = ["grade"] + [f"old-{i}" for i in range(250)]
        bodies = {"grade": grade, **{
            f"old-{i}": {"event_type": "memory_read", "payload": {}}
            for i in range(250)}}
        calls = {"ids": 0, "batches": 0}

        async def snapshot(*args, **kwargs):
            calls["ids"] += 1
            return list(frozen)

        async def hydrate(r, ids):
            calls["batches"] += 1
            if calls["batches"] == 1:
                # Mutate the live backing set after the snapshot. A second
                # rank-relative read would now shift; this implementation has none.
                frozen.extend(f"new-{i}" for i in range(300))
            return [bodies[i] for i in ids if i in bodies]

        monkeypatch.setattr(reader_mod, "get_session_event_ids", snapshot)
        monkeypatch.setattr(reader_mod, "get_event_batch", hydrate)
        assert await compute_mod.find_terminal_grade(rr, "s1") == (
            "success", "self_reported")
        assert calls["ids"] == 1

    @pytest.mark.asyncio
    async def test_ungraded_session_reads_unknown_not_success(self, monkeypatch):
        result = await _compute(monkeypatch,
                                _make_events([{"type": "session_end"}]), id_events=[])
        assert result.task_result is None
        assert result.outcome == "unknown"


class TestRaceSafeStore:
    @pytest.mark.asyncio
    async def test_graded_replaces_ungraded_but_nothing_else(self, rr):
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval
        ungraded = EvalResult(session_id="s1", trigger="session_complete")
        graded = EvalResult(session_id="s1", trigger="manual",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, ungraded) is True
        assert await store_eval(rr, ungraded) is False           # idempotent
        assert await store_eval(rr, graded) is True              # upgrade
        assert (await get_eval(rr, "s1")).task_result == "success"
        regraded = EvalResult(session_id="s1", trigger="manual",
                              task_result="failure", task_result_source="self_reported")
        assert await store_eval(rr, regraded) is False           # first-graded-wins

    @pytest.mark.asyncio
    async def test_an_ungraded_writer_can_never_clobber_a_grade(self, rr):
        """D9a: the ungraded path writes ONLY via SET NX — under ANY
        interleaving it cannot overwrite."""
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval
        graded = EvalResult(session_id="s2", trigger="session_complete",
                            task_result="failure", task_result_source="self_reported")
        assert await store_eval(rr, graded) is True
        assert await store_eval(rr, EvalResult(session_id="s2", trigger="manual")) is False
        assert (await get_eval(rr, "s2")).task_result == "failure"

    @pytest.mark.asyncio
    async def test_concurrent_mixed_writers_leave_a_graded_record(self, rr):
        """D9b (round-5): the time-limited claim is GONE — WATCH/MULTI CAS.
        Gather ungraded + graded writers for one session; postconditions hold
        under any scheduling: the final record is graded, and exactly one
        graded writer reports True."""
        import asyncio
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval

        def _u(): return EvalResult(session_id="sc", trigger="session_complete")
        def _g(tag): return EvalResult(session_id="sc", trigger="manual",
                                       task_result=tag,
                                       task_result_source="self_reported")
        results = await asyncio.gather(
            store_eval(rr, _u()), store_eval(rr, _g("success")),
            store_eval(rr, _u()), store_eval(rr, _g("failure")),
            store_eval(rr, _u()),
        )
        final = await get_eval(rr, "sc")
        assert final.task_result in ("success", "failure")     # a grade won
        assert sum(1 for i, ok in enumerate(results)
                   if ok and i in (1, 3)) == 1                  # one graded True

    @pytest.mark.asyncio
    async def test_a_stale_watcher_retries_and_observes_the_competing_grade(
        self, rr, monkeypatch
    ):
        """D9b: a graded writer whose WATCHed key changed under it must
        EXEC-fail and RETRY (re-read → re-decide), never overwrite from a
        stale read — the successor-lock-deletion class the old fixed-TTL claim
        reintroduced. Wrap rr.pipeline so the FIRST execute() raises
        WatchError; before raising, inject the competing grade. The retry must
        re-read it, lose first-graded-wins, and return False."""
        import redis
        from app.evals.models import EvalResult
        from app.evals.store import store_eval, get_eval

        real_pipeline = rr.pipeline
        state = {"failed": False, "pipelines": 0}
        competitor = EvalResult(
            session_id="sw", trigger="manual", task_result="failure",
            task_result_source="self_reported")

        def flaky_pipeline(*a, **k):
            state["pipelines"] += 1
            pipe = real_pipeline(*a, **k)
            real_execute = pipe.execute

            async def once_failing_execute(*ea, **ek):
                if not state["failed"]:
                    state["failed"] = True
                    # A second client won between this pipeline's WATCH/read
                    # and EXEC. Use the unwrapped client SET to make that fact real.
                    await rr.set("rp:eval:sw", competitor.model_dump_json(), ex=86400)
                    raise redis.WatchError("simulated concurrent change")
                return await real_execute(*ea, **ek)

            pipe.execute = once_failing_execute
            return pipe

        monkeypatch.setattr(rr, "pipeline", flaky_pipeline)
        graded = EvalResult(session_id="sw", trigger="session_complete",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, graded) is False    # competing grade already won
        assert state["failed"] is True                  # the first EXEC did fail
        assert state["pipelines"] >= 2                  # it re-read on a fresh pipeline
        assert (await get_eval(rr, "sw")).task_result == "failure"


class TestAuthoritativeDownstream:
    @pytest.mark.asyncio
    async def test_a_rejected_write_yields_the_stored_record(self, monkeypatch, rr):
        from app.evals.models import EvalResult
        from app.evals.store import store_eval
        graded = EvalResult(session_id="s1", trigger="session_complete",
                            task_result="success", task_result_source="self_reported")
        assert await store_eval(rr, graded) is True
        result = await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                                id_events=[], replay_redis=rr)
        assert result.task_result == "success"                    # the STORED record

    @pytest.mark.asyncio
    async def test_no_persisted_record_aborts_downstream(self, monkeypatch, rr):
        """D9c: store False + reload None (infra failure) must NOT let the
        candidate drive features/webhooks/the response."""
        import app.evals.compute as compute_mod
        extracted: list = []

        async def _no_store(r, result, **kw):
            return False

        async def _no_get(r, sid):
            return None

        async def _spy_extract(*a, **kw):
            extracted.append(1)

        monkeypatch.setattr(compute_mod, "store_eval", _no_store)
        monkeypatch.setattr(compute_mod, "get_eval", _no_get)
        monkeypatch.setattr("app.patterns.extractor.extract_session_features",
                            _spy_extract, raising=False)
        fired = AsyncMock()
        result = await _compute(
            monkeypatch, _make_events([{"type": "memory_read"}]),
            id_events=[], replay_redis=rr, webhook_sink=fired)
        assert result is None
        assert extracted == []
        fired.assert_not_awaited()
        dlq = json.loads(await rr.get("rp:eval_dlq:s1"))
        assert dlq["failure_type"] == "store"

    @pytest.mark.asyncio
    async def test_superseded_candidate_webhook_uses_stored_winner(
        self, monkeypatch, rr
    ):
        """D9f: accepted-ungraded → graded upgrade before the final read.
        Both webhook payloads must carry the complete stored pair."""
        from app.evals import compute as compute_mod
        from app.evals.models import EvalResult
        from app.evals.store import store_eval

        winner = EvalResult(
            session_id="s1", trigger="manual", task_result="success",
            task_result_source="self_reported", outcome="success")
        real_get = compute_mod.get_eval
        reads = {"n": 0}

        async def superseding_get(r, sid):
            reads["n"] += 1
            if reads["n"] == 1:
                assert await store_eval(r, winner) is True
            return await real_get(r, sid)

        fired = AsyncMock()
        monkeypatch.setattr(compute_mod, "get_eval", superseding_get)
        result = await _compute(
            monkeypatch, _make_events([{"type": "memory_read"}]),
            id_events=[], replay_redis=rr, webhook_sink=fired)
        assert result.task_result == "success"
        assert fired.await_count == 2
        for call in fired.await_args_list:
            payload = call.args[2]
            assert (payload["task_result"], payload["task_result_source"]) == (
                "success", "self_reported")

    @pytest.mark.asyncio
    async def test_unreadable_final_authority_suppresses_webhooks(
        self, monkeypatch, rr, caplog
    ):
        from app.evals import compute as compute_mod
        fired = AsyncMock()
        monkeypatch.setattr(compute_mod, "get_eval", AsyncMock(return_value=None))
        await _compute(monkeypatch, _make_events([{"type": "memory_read"}]),
                       id_events=[], replay_redis=rr, webhook_sink=fired)
        fired.assert_not_awaited()
        assert "authoritative eval unreadable" in caplog.text
```

Import `AsyncMock` at the top of the test module. `_compute` patches Cortex Redis creation and the real webhook module, so none of these tests attempts `redis://redis:6379/0`.

Update the pre-existing `test_compute_includes_brier_score_when_predict_events_present` for the new authoritative-store contract; its old patch targets `app.evals.store.store_eval`, but `compute.py` holds a module binding and the old fake returned `None`:

```python
    saved = {}

    async def fake_store_eval(_r, result):
        saved["result"] = result
        return True

    async def fake_get_eval(_r, _sid):
        return saved.get("result")

    monkeypatch.setattr(compute_mod, "store_eval", fake_store_eval)
    monkeypatch.setattr(compute_mod, "get_eval", fake_get_eval)
    monkeypatch.setattr(
        compute_mod.aioredis, "from_url", lambda *a, **k: AsyncMock())
    monkeypatch.setattr("app.webhooks.fire_webhooks", AsyncMock())
```

Also add no-op `get_session_event_ids`/`get_event_batch` patches to this existing test so the grade lift does not touch its `replay_redis=None` sentinel.

Replace `test_eval_attribution.py`'s local `_compute` helper with:

```python
async def _compute(monkeypatch, events, agents=("default",)):
    import fakeredis.aioredis
    import replay.reader as reader_mod
    from unittest.mock import AsyncMock
    from app.evals import compute as compute_mod

    async def fake_summary(*args, **kwargs):
        return {"event_count": max(len(events), 1), "duration_ms": 1000,
                "agents": list(agents)}

    async def fake_timeline(*args, **kwargs):
        return {"events": events}

    async def fake_ids(*args, **kwargs):
        return []

    async def fake_batch(*args, **kwargs):
        return []

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_timeline", fake_timeline)
    monkeypatch.setattr(reader_mod, "get_session_event_ids", fake_ids)
    monkeypatch.setattr(reader_mod, "get_event_batch", fake_batch)
    monkeypatch.setattr(compute_mod.aioredis, "from_url",
                        lambda *a, **k: AsyncMock())
    monkeypatch.setattr("app.webhooks.fire_webhooks", AsyncMock())
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        return await compute_mod.compute_session_eval(r, "s1")
    finally:
        await r.aclose()
```

The real `store_eval`/`get_eval` then satisfy the authoritative-store contract while the attribution assertions remain unchanged.

- [ ] **Step 2: Run to verify they fail** — `cd cortex && python -m pytest tests/test_evals.py -k "Normalizers or TaskResult or RaceSafe or Authoritative" -v`.

- [ ] **Step 3: Implement.**

(a) `cortex/app/evals/models.py` — helpers above the models (add `model_validator` to the pydantic import):

```python
_GRADES = ("success", "partial", "failure")
_RECOGNIZED_SOURCES = ("self_reported",)
_GRADE_EVENT_TYPES = ("session_end", "session.completed")


def recognized_grade_pair(
    task_result: object, task_result_source: object,
) -> tuple[str, str] | tuple[None, None]:
    """The (grade, source) pair is atomic (spec D2): both recognized, or neither.

    The ONLY grade-validity check in cortex — every consumer imports this."""
    if task_result in _GRADES and task_result_source in _RECOGNIZED_SOURCES:
        return task_result, task_result_source  # type: ignore[return-value]
    return None, None


def binary_outcome(task_result: str | None) -> str:
    """Project a grade onto the binary feature space: success/failure pass
    through; partial and None are 'unknown' (binary-ambiguous, excluded)."""
    return task_result if task_result in ("success", "failure") else "unknown"


def grade_from_events(events: list[dict]) -> tuple[str | None, str | None]:
    """Last recognized grade pair on a TERMINAL event (session_end from the
    tool layer, session.completed from SessionManager — redundant channels
    that fail independently, spec D7). Junk degrades to (None, None)."""
    task_result: str | None = None
    task_result_source: str | None = None
    for e in events:
        if e.get("event_type") not in _GRADE_EVENT_TYPES:
            continue
        p = e.get("payload")
        if not isinstance(p, dict):   # round-6 finding 6: a non-empty non-dict
            continue                  # payload must degrade, not raise on .get
        tr, src = recognized_grade_pair(p.get("task_result"),
                                        p.get("task_result_source"))
        if tr:
            task_result, task_result_source = tr, src
    return task_result, task_result_source
```

On `EvalResult`:

```python
    # Structured task grade (outcome truth, 2026-08-23). The pair is atomic;
    # the BEFORE-validator normalizes the raw mapping because Literal field
    # validation would raise before an after-validator ever ran — a junk
    # stored record must parse as ungraded, not fail wholesale.
    task_result: Literal["success", "partial", "failure"] | None = None
    task_result_source: Literal["self_reported"] | None = None

    @model_validator(mode="before")
    @classmethod
    def _atomic_grade_pair(cls, data):
        if isinstance(data, dict) and ("task_result" in data or "task_result_source" in data):
            data = dict(data)
            tr, src = recognized_grade_pair(data.get("task_result"),
                                            data.get("task_result_source"))
            data["task_result"] = tr
            data["task_result_source"] = src
        return data
```

(b) `cortex/app/evals/compute.py` — import `get_eval` beside `store_eval`; the signature drops `outcome`, gains the hint; add the snapshot lift:

Update `_record_eval_failure`'s inline vocabulary comment from `"infra" or "scoring"` to `"infra", "scoring", or "store"`.

```python
_GRADE_SCAN_MAX = 5000  # one-shot snapshot cap; disclosed in spec D7


_GRADE_HYDRATE_WINDOW = 200


async def find_terminal_grade(
    replay_redis, session_id: str,
) -> tuple[str | None, str | None]:
    """Newest recognized grade pair, via a ONE-SHOT ID snapshot + local
    backward hydration.

    Snapshot-first is load-bearing on two counts (round-6 finding 2 + round-5
    finding 6): (a) a live rank-relative window read is not stable — events
    appended between page reads shift negative ranks, so a later page can
    repeat a prior page and skip the grade; (b) get_event_batch omits IDs
    whose bodies were trimmed/expired. Capturing the ID list ONCE fixes (a)
    (the list can't shift), and walking IDs in windows (not hydrated events)
    fixes (b) (a window with missing bodies is walked past)."""
    from app.evals.models import grade_from_events
    from replay.reader import get_session_event_ids, get_event_batch

    ids = await get_session_event_ids(replay_redis, session_id,
                                      limit=_GRADE_SCAN_MAX)
    # walk newest-first in windows; ids is oldest->newest
    for end in range(len(ids), 0, -_GRADE_HYDRATE_WINDOW):
        window = ids[max(0, end - _GRADE_HYDRATE_WINDOW):end]
        events = await get_event_batch(replay_redis, window)
        tr, src = grade_from_events(events)
        if tr:
            return tr, src
    return None, None
```

The lift, after `failure_event_ids` (compute.py:112-116) — the hint goes through the shared normalizer (Global Constraint: no inline tuples):

```python
        from app.evals.models import recognized_grade_pair
        task_result, task_result_source = recognized_grade_pair(
            task_result_hint, "self_reported")
        if task_result is None:
            task_result, task_result_source = await find_terminal_grade(
                replay_redis, session_id)
```

The `EvalResult(...)` construction:

```python
            outcome=task_result or ("failure" if failure_event_ids else "unknown"),
            task_result=task_result,
            task_result_source=task_result_source,
```

The store + authoritative/abort block replacing `await store_eval(replay_redis, result)` (compute.py:192) — feature extraction, webhooks, and the return stay BELOW it and use `result`:

```python
        stored = await store_eval(replay_redis, result)
        if not stored:
            # The store kept a different record, or persistence failed. The
            # candidate must not drive features/webhooks/the response unless
            # something authoritative exists (spec D9b/c).
            authoritative = await get_eval(replay_redis, session_id)
            if authoritative is None:
                await _record_eval_failure(
                    replay_redis, session_id,
                    "store_eval rejected and no authoritative record readable",
                    failure_type="store",
                )
                return None
            result = authoritative
```

Then, at the webhook-fire site (compute.py:~218) — the accepted-then-superseded case (D9f): an ungraded computation can store True, stall, let a graded upgrade fire, then resume and fire last with its stale grade. Re-read the authoritative record IMMEDIATELY before building the webhook payload so a superseded computation emits the WINNER's grade, and fold in the D9f disclosure comment:

```python
        # D9f: webhook DELIVERY ORDER is not authoritative — the eval store
        # (GET /evals/sessions/{id}) is the sole truth, and a consumer must
        # re-fetch on session_id, never infer grade order from arrival order.
        # Re-read right here so a superseded computation still emits the
        # current authoritative grade, not the stale one it computed.
        latest = await get_eval(replay_redis, session_id)
        if latest is None:
            logger.error(
                "Suppressing eval webhooks for %s: authoritative eval unreadable",
                session_id,
            )
        else:
            pair = {
                "task_result": latest.task_result,
                "task_result_source": latest.task_result_source,
            }
            # Build BOTH existing session.completed / eval.computed payloads
            # from latest.outcome/event_count/has_failures, len(latest.metrics),
            # plus `pair` — never the local `metrics`/candidate variables.
            # Fire only inside this branch. There is deliberately no
            # `or result` fallback: a known-stale candidate has no authority.
```

Set `result = latest` inside the successful branch before returning, so the HTTP caller and feature/webhook consumers converge on the same stored record. Webhook delivery is best-effort zero-or-more: failure is swallowed and eval-trigger retries may duplicate. `session_id` is the refetch key and the eval GET is the authority.

(c) `cortex/app/evals/store.py` — `store_eval` (D9a/b — WATCH/MULTI CAS; the time-limited claim is GONE, round-5 finding 3):

```python
        key = f"{_EVAL_PREFIX}{result.session_id}"
        data = result.model_dump_json()
        ttl = ttl_days * 86400

        if result.task_result is None:
            # D9a: ungraded writers are NX-create-only — no overwrite path
            # exists for them, under any interleaving.
            created = await r.set(key, data, ex=ttl, nx=True)
            if not created:
                logger.debug("Eval already exists for session %s, skipping",
                             result.session_id)
                return False
        else:
            # D9b: graded writes are first-graded-wins via WATCH/MULTI CAS.
            # A time-limited claim is NOT a correctness primitive (a writer
            # stalling past its TTL lets a successor acquire, then the first
            # writer overwrites from a stale read and deletes the successor's
            # lock — the fencing problem relay/app/leases.py already solves).
            # Here the decision (replace only a missing-or-ungraded record)
            # and the write are ONE atomic transaction, so a stale writer's
            # EXEC fails and it retries — no window, nothing to fence.
            for _attempt in range(8):
                try:
                    async with r.pipeline() as pipe:
                        await pipe.watch(key)
                        existing_raw = await pipe.get(key)
                        if existing_raw:
                            try:
                                existing = EvalResult.model_validate_json(existing_raw)
                                if existing.task_result is not None:
                                    await pipe.unwatch()
                                    logger.debug(
                                        "Graded eval already stored for %s, keeping it",
                                        result.session_id)
                                    return False
                            except Exception:
                                pass  # unparseable stored record: graded wins
                        pipe.multi()
                        pipe.set(key, data, ex=ttl)
                        await pipe.execute()
                        break
                except redis.WatchError:
                    continue          # the key changed under us; re-read and retry
            else:
                logger.warning("store_eval CAS exhausted retries for %s",
                               result.session_id)
                return False
```

(`import redis` for `redis.WatchError`; index `zadd` runs only on True paths. TTL reset + index re-score on a graded replacement are accepted. The tests exercise the same WATCH/MULTI surface on shared-server fakeredis.)

(d) Feature grade dominance (D9e) is implemented in **Task 9** — `store_features` lives in `cortex/app/patterns/store.py`, that task's file — so all patterns edits commit together. It is called out here because the concurrency hazard it closes is the same family as this task's store CAS.

(e) `cortex/app/evals/api.py` — the `task_result` Query param (after `trigger`, before `identity`; gate in Task 5):

```python
        task_result: str | None = Query(
            default=None,
            pattern=r"^(success|partial|failure)$",
            description="Structured task grade from the completing caller "
                        "(spec D8: survives a lost session_end emit; honored "
                        "only with the eval:grade scope).",
        ),
```

```python
        result = await compute_session_eval(r, session_id, trigger=trigger,
                                            task_result_hint=task_result)
```

- [ ] **Step 4: Run** `cd cortex && python -m pytest tests/test_evals.py tests/test_eval_attribution.py tests/test_evals_brier.py tests/test_evals_compute_scope.py -v` (`test_evals.py` and `test_eval_attribution.py` gain the snapshot-reader patches above; ungraded `outcome == "success"` pins update to `"unknown"` — note in commit body).

- [ ] **Step 5: Commit**

```bash
git add cortex/app/evals/models.py cortex/app/evals/compute.py cortex/app/evals/store.py cortex/app/evals/api.py cortex/tests/test_evals.py cortex/tests/test_eval_attribution.py
git commit -m "feat(evals): snapshot grade lift, first-graded-wins store, authoritative downstream" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `eval:grade` — a SERVICE-ONLY scope on a dedicated Bridge credential

**Files:**
- Modify: `auth/keys.py`, `auth/api.py`, `auth/tests/test_enrollable_scopes.py`, `auth/tests/test_anonymous_scopes.py`, `auth/tests/test_enrolled_ceiling.py`, `auth/tests/test_middleware.py`, `deploy/bootstrap-keys.sh`, `deploy/firekeep-admin`, `deploy/tests/test_bootstrap_keys.sh`, `deploy/tests/test_auth_posture.sh`, `deploy/tests/test_firekeep_admin.sh`, `docker-compose.yml`, `.env.example`, `bridge/.env.example`, `install.sh`, `cortex/app/evals/api.py`, `cortex/tests/test_evals_compute_scope.py`, `cortex/tests/test_auth_consolidation.py`, `cortex/tests/test_confused_deputy.py`, `.github/workflows/install-smoke.yml`, `docs/CONFIGURATION.md`, `docs/DEPLOYMENT.md`, `docs/DEPLOYMENT-OFFICE.md`, `docs/guides/auth-and-provenance.md`
- Create: `auth/tests/test_service_only_scopes.py`, `tests/test_compose_secrets.py` (root — static compose parsing)
- Test: `auth/tests/`, `cortex/tests/test_evals_compute_scope.py`, deploy shell tests, root `tests/`

**Interfaces:**
- Produces: `SERVICE_ONLY_SCOPES = frozenset({"eval:grade"})` in `auth/keys.py`, subtracted from BOTH `ENROLLABLE_SCOPES` and `ANONYMOUS_SCOPES` (the retroactive union at keys.py:495-507 then excludes it by construction), **rejected by `create_key`** (D8e — admin `POST /auth/keys` cannot mint it onto member credentials; bootstrap's `register_hash` writes Redis directly and is unaffected), and **listed separately by `GET /auth/scopes`** (`{"scopes": [...mintable...], "service_only": [...]}`). New `FIREKEEP_BRIDGE_KEY` has exactly `["memory:write","session:read","eval:read","eval:write","eval:grade"]`, is minted by bootstrap, wired ONLY to the bridge compose service, and **explicitly blanked (`FIREKEEP_BRIDGE_KEY: ""`) in every OTHER `env_file: .env` service** — cortex-api, cortex-mcp, cortex-worker, cortex-beat, sentinel, relay. `FIREKEEP_INTERNAL_KEY` is untouched and never gains `eval:grade`. The route gate honors EXACTLY `eval:grade` — not `admin`, not `"*"` — via `_hint_authorized(identity, request)`: scope on the enforced identity, OR direct validation of the presented `X-API-Key`. Cortex already initializes the DB-7 auth client regardless of `AUTH_ENABLED` (`cortex/app/main.py:714-724`), so this direct validation works in both modes without lazy initialization. Unauthorized hints are dropped with one ERROR; auth-store outages fail closed with a distinct exception log.
- Verified facts this rests on: `ENROLLABLE_SCOPES = frozenset(SCOPES - {"admin", "*"})` (keys.py:73); enrollment stamps the full ceiling (enroll/store.py:82,222); old enrolled keys union to the CURRENT ceiling at validation (keys.py:495-507); `ANONYMOUS_SCOPES` derives from SCOPES (keys.py:180-182); the parity test asserts set-equality with `deploy/firekeep-admin:10` (which needs NO edit once the scope is withheld); `ensure_env_key` is CREATE-ONLY (bootstrap-keys.sh:148-166) so a scope edit on the existing internal key would propagate to NO existing deployment — the dedicated key's create branch fires everywhere on the next `update.sh`; relay's `RELAY_INTERNAL_API_KEY` is the dedicated-key precedent.

- [ ] **Step 1: Write the failing tests.**

`auth/tests/test_enrollable_scopes.py:13` becomes (import `SERVICE_ONLY_SCOPES` too):

```python
    assert ENROLLABLE_SCOPES == frozenset(SCOPES - {"admin", "*"} - SERVICE_ONLY_SCOPES)
    assert "eval:grade" in SCOPES and "eval:grade" not in ENROLLABLE_SCOPES
```

`auth/tests/test_anonymous_scopes.py:68`: add `"eval:grade"` to `WITHHELD_FROM_ANONYMOUS`. Add to `auth/tests/test_enrolled_ceiling.py` an assertion that the retroactive union never grants it. In `auth/tests/test_middleware.py`, add `"eval:grade"` to the exact registry and change the count from 13 to 14; update the anonymous-set expectation to subtract `SERVICE_ONLY_SCOPES`. Create `auth/tests/test_service_only_scopes.py` (D8e) with real initialized fakeredis and a direct call to the real scopes endpoint:

```python
import fakeredis.aioredis
import pytest
import pytest_asyncio

from auth import keys
from auth.api import create_auth_router


@pytest_asyncio.fixture
async def auth_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await keys.init_auth(redis_client=r, enabled=True)
    yield r
    await keys.init_auth(redis_client=None, enabled=False)
    await r.aclose()


@pytest.mark.asyncio
async def test_create_key_rejects_service_only_scopes(auth_redis):
    with pytest.raises(ValueError, match="service-only"):
        await keys.create_key(
            agent_id="mallory", scopes=["memory:write", "eval:grade"])


@pytest.mark.asyncio
async def test_scopes_endpoint_separates_service_scopes():
    route = next(
        route for route in create_auth_router().routes
        if route.path == "/auth/scopes" and "GET" in route.methods)
    body = await route.endpoint(identity={"scopes": ["admin"]})
    assert body["service_only"] == ["eval:grade"]
    assert "eval:grade" not in body["scopes"]
```

Add this executable auth-off store check to
`cortex/tests/test_auth_consolidation.py`. The route's fallback depends on
`validate_key` still consulting DB 7 when enforcement is disabled; a source
inspection of `main.py` is not enough to pin that behavior:

```python
@pytest.mark.asyncio
async def test_initialized_key_store_remains_usable_with_enforcement_off(redis):
    await keys.init_auth(redis_client=redis, enabled=True)
    created = await keys.create_key("auth-off-probe", ["eval:write"])
    await keys.init_auth(redis_client=redis, enabled=False)
    try:
        identity = await keys.validate_key(created["api_key"])
        assert identity is not None
        assert identity["credential_id"] == created["credential_id"]
        assert keys._AUTH_ENABLED is False
        assert keys._redis is redis
    finally:
        await keys.init_auth(redis_client=None, enabled=False)
```

In `cortex/tests/test_evals_compute_scope.py`, import `_hint_authorized` and add this executable five-cell matrix plus separate invalid/outage logging assertions:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scopes", "header", "direct", "expected"),
    [
        (["eval:write", "eval:grade"], None, None, True),
        (["eval:write"], None, None, False),
        (["admin"], None, None, False),
        (["*"], None, None, False),
        (["eval:write"], "nxs_bridge",
         {"scopes": ["eval:grade"]}, True),
    ],
)
async def test_grade_hint_authorization_matrix(
    monkeypatch, scopes, header, direct, expected
):
    request = SimpleNamespace(headers={"X-API-Key": header} if header else {})
    validate = AsyncMock(return_value=direct)
    monkeypatch.setattr("app.evals.api.validate_key", validate)
    assert await _hint_authorized(
        {"scopes": scopes}, request, "s1") is expected


@pytest.mark.asyncio
async def test_invalid_direct_key_logs_unauthorized_once(monkeypatch, caplog):
    monkeypatch.setattr("app.evals.api.validate_key", AsyncMock(return_value=None))
    request = SimpleNamespace(headers={"X-API-Key": "wrong"})
    assert not await _hint_authorized({"scopes": []}, request, "s1")
    assert caplog.text.count("eval:grade") == 1


@pytest.mark.asyncio
async def test_auth_store_outage_is_distinct_and_fail_closed(monkeypatch, caplog):
    monkeypatch.setattr(
        "app.evals.api.validate_key",
        AsyncMock(side_effect=ConnectionError("DB 7 down")),
    )
    request = SimpleNamespace(headers={"X-API-Key": "nxs_bridge"})
    assert not await _hint_authorized({"scopes": []}, request, "s1")
    assert "auth store" in caplog.text
    assert "Re-run deploy/bootstrap-keys.sh" not in caplog.text
```

Add route-level wiring tests:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(("authorized", "expected"), [(False, None), (True, "success")])
async def test_compute_route_drops_or_forwards_hint_exactly_once(
    monkeypatch, caplog, authorized, expected
):
    endpoint = _route("/sessions/{session_id}/compute", "POST").endpoint
    authorize = AsyncMock(return_value=authorized)
    computed = AsyncMock(return_value=SimpleNamespace(
        model_dump=lambda mode="python": {"session_id": "s1"}))
    monkeypatch.setattr("app.evals.api._hint_authorized", authorize)
    monkeypatch.setattr("app.evals.api.compute_session_eval", computed)
    request = SimpleNamespace(headers={"X-API-Key": "nxs_bridge"})

    await endpoint(
        session_id="s1", request=request, r=object(),
        trigger="session_complete", task_result="success",
        identity={"scopes": ["eval:write"]},
    )

    assert computed.await_args.kwargs["task_result_hint"] == expected
    authorize.assert_awaited_once()
    assert "task_result hint" not in caplog.text  # helper owns all logging
```

Update `cortex/tests/test_auth_consolidation.py` and `test_confused_deputy.py` to derive teammate scopes from `ENROLLABLE_SCOPES`, never `SCOPES - {"admin"}` (which would now request the forbidden service scope). Update `deploy/tests/test_firekeep_admin.sh` to compare its literal against `ENROLLABLE_SCOPES` and retain the exact-set assertion. Update `deploy/tests/test_bootstrap_keys.sh` with minted/format/idempotency assertions while leaving the internal-key scope-set assertion unchanged; update `deploy/tests/test_auth_posture.sh` with the bridge-key transcript/count. Create root `tests/test_compose_secrets.py` (D8f):

```python
def test_bridge_key_reaches_only_the_bridge_container():
    """env_file imports the WHOLE .env: every non-bridge service that uses it
    must explicitly blank FIREKEEP_BRIDGE_KEY (the cortex-mcp confused-deputy
    pin is the precedent)."""
    from pathlib import Path
    import yaml
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    for name, svc in compose["services"].items():
        env_files = svc.get("env_file") or []
        env = svc.get("environment") or {}
        if not env_files or name == "bridge":
            continue
        assert isinstance(env, dict), f"service {name} environment must be a mapping"
        assert env.get("FIREKEEP_BRIDGE_KEY") == "", (
            f"service {name} imports .env but does not blank FIREKEEP_BRIDGE_KEY")
    bridge_env = compose["services"]["bridge"]["environment"]
    assert bridge_env["NB_FIREKEEP_API_KEY"] == "${FIREKEEP_BRIDGE_KEY:-}"
```

- [ ] **Step 2: Run to verify failures** — `cd auth && python -m pytest tests/ -v`; `cd cortex && python -m pytest tests/test_evals_compute_scope.py -v`.

- [ ] **Step 3: Implement.**

(a) `auth/keys.py`: add `"eval:grade"` to `SCOPES` (comment: honored only by POST /evals/sessions/{id}/compute's hint; service-only). Replace line 73:

```python
# Scopes a SERVICE key may carry but no member credential ever receives:
# not enrollable, not anonymous, never unioned onto old enrolled credentials
# (the keys.py:495 union adds ENROLLABLE_SCOPES, which excludes these by
# construction — same mechanism that keeps `admin` out).
SERVICE_ONLY_SCOPES: frozenset[str] = frozenset({"eval:grade"})
ENROLLABLE_SCOPES: frozenset[str] = frozenset(SCOPES - {"admin", "*"} - SERVICE_ONLY_SCOPES)
```

and append `- SERVICE_ONLY_SCOPES` to the `ANONYMOUS_SCOPES` subtraction chain (:180-182). In `create_key` (:313-316), after the existing invalid-scope check (D8e):

```python
    service_only = set(scopes) & SERVICE_ONLY_SCOPES
    if service_only:
        raise ValueError(
            f"Scopes {sorted(service_only)} are service-only: minted exclusively "
            f"by deploy/bootstrap-keys.sh onto dedicated service credentials, "
            f"never onto member keys."
        )
```

In `auth/api.py` `/auth/scopes` (:120-125):

```python
        return {
            "scopes": sorted(SCOPES - SERVICE_ONLY_SCOPES),
            "service_only": sorted(SERVICE_ONLY_SCOPES),
        }
```

`deploy/firekeep-admin:10` keeps its literal value but gains a comment that `eval:grade` is deliberately absent; its parity test now compares to `ENROLLABLE_SCOPES`.

(b) `deploy/bootstrap-keys.sh` — after the RELAY line (:189), via `ensure_env_key` ONLY (install.sh's single-`nxs_`-token admin-key capture breaks on any hand-rolled echo — constraint recorded at bootstrap-keys.sh:192-200):

```bash
ensure_env_key FIREKEEP_BRIDGE_KEY firekeep-bridge '["memory:write","session:read","eval:read","eval:write","eval:grade"]'
```

Update the header inventory (:6-25). The `FIREKEEP_INTERNAL_KEY` line is untouched.

(c) `docker-compose.yml:817`: `NB_FIREKEEP_API_KEY: ${FIREKEEP_BRIDGE_KEY:-}` (comment updated: dedicated bridge key, scopes incl. eval:grade; sentinel :863 and cortex's bare pass-through untouched) — AND add `FIREKEEP_BRIDGE_KEY: ""` to the `environment` block of every OTHER `env_file: .env` service: cortex-api, cortex-mcp, cortex-worker, cortex-beat, sentinel, relay, each with a one-line comment mirroring cortex-mcp's confused-deputy pin (D8f). `bridge/app/config.py` needs NO change. `.env.example`: add `FIREKEEP_BRIDGE_KEY=` (~:287), fix the stale comment at :188 and the stale symdex note at :237. `bridge/.env.example:13`: point at the new var. `install.sh:376-378`: extend the .env-contents comment to name `FIREKEEP_BRIDGE_KEY` alongside `FIREKEEP_INTERNAL_KEY`.

(d) `cortex/app/evals/api.py` — import `Request`, `validate_key`, and use this gate helper. Put the required `request: Request` parameter immediately after `session_id`, before the dependency/query defaults (valid Python signature ordering):

```python
async def _hint_authorized(identity: dict, request, session_id: str) -> bool:
    """eval:grade on the enforced identity, or — because the disabled-mode
    FastAPI scope dependency returns the anonymous identity and ignores presented keys
    entirely (auth/asgi.py; validate_key at auth/asgi.py:25) — direct
    validation of the presented X-API-Key. Mirrors the vault doctrine:
    service-only assertions stay authenticated even with enforcement off
    (D8d). Never raises. Does ALL of its own logging and distinguishes an
    UNAUTHORIZED caller (the actionable 'mint the key' ERROR) from an auth-
    store OUTAGE (an infra ERROR) — the CALLER logs nothing, so an outage no
    longer also emits the misleading 'rerun bootstrap' line (round-6 small)."""
    if "eval:grade" in (identity.get("scopes") or []):
        return True
    api_key = request.headers.get("X-API-Key")
    if api_key:
        try:
            direct = await validate_key(api_key)   # module binding is testable
            if direct and "eval:grade" in (direct.get("scopes") or []):
                return True
        except Exception:
            logger.exception(
                "eval:grade check could not reach the auth store for session "
                "%s; hint dropped fail-closed (INFRA, not a credential or "
                "minting problem — do NOT rerun bootstrap for this)", session_id)
            return False
    # Genuinely unauthorized: the actionable message (ERROR, not WARNING — a
    # silent WARNING here cost 12 days once).
    logger.error(
        "task_result hint for session %s without eval:grade (enforced "
        "scopes=%s) — hint DROPPED. Grade still lands via the terminal-event "
        "lift when the emit succeeded. Re-run deploy/bootstrap-keys.sh "
        "(update.sh does) to mint FIREKEEP_BRIDGE_KEY.",
        session_id, identity.get("scopes"))
    return False
```

and before the compute call (the caller logs NOTHING — the helper already did):

```python
        if task_result is not None and not await _hint_authorized(
                identity, request, session_id):
            task_result = None   # D8c: dropped; _hint_authorized logged why
```

(e) `.github/workflows/install-smoke.yml:301`: extend the minted-vars loop with `FIREKEEP_BRIDGE_KEY`. Docs: `docs/CONFIGURATION.md:105` (key/scope table row), `docs/DEPLOYMENT.md:135,194`, `docs/DEPLOYMENT-OFFICE.md:50`, `docs/guides/auth-and-provenance.md` (scopes list :17 gains `eval:grade` marked service-only; key inventory :12-21 gains the bridge key).

- [ ] **Step 4: Run** `cd auth && python -m pytest tests/ -v`; `cd cortex && python -m pytest tests/test_evals_compute_scope.py tests/test_auth_consolidation.py tests/test_confused_deputy.py -v`; from the repo root run `bash -n deploy/bootstrap-keys.sh`, `bash deploy/tests/test_bootstrap_keys.sh`, `bash deploy/tests/test_auth_posture.sh`, `bash deploy/tests/test_firekeep_admin.sh`, and `python -m pytest tests/test_compose_secrets.py -v`.

- [ ] **Step 5: Commit**

```bash
git add auth/keys.py auth/api.py auth/tests/test_enrollable_scopes.py auth/tests/test_anonymous_scopes.py auth/tests/test_enrolled_ceiling.py auth/tests/test_middleware.py auth/tests/test_service_only_scopes.py deploy/bootstrap-keys.sh deploy/firekeep-admin deploy/tests/test_bootstrap_keys.sh deploy/tests/test_auth_posture.sh deploy/tests/test_firekeep_admin.sh docker-compose.yml .env.example bridge/.env.example install.sh cortex/app/evals/api.py cortex/tests/test_evals_compute_scope.py cortex/tests/test_auth_consolidation.py cortex/tests/test_confused_deputy.py .github/workflows/install-smoke.yml docs/CONFIGURATION.md docs/DEPLOYMENT.md docs/DEPLOYMENT-OFFICE.md docs/guides/auth-and-provenance.md tests/test_compose_secrets.py
git commit -m "feat(auth): eval:grade is service-only on a dedicated, container-isolated Bridge key" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: OWM — `session_success` accepts only the recognized pair

**Files:**
- Modify: `cortex/app/owm.py:37-63`
- Test: `cortex/tests/test_owm.py`

**Interfaces:**
- Consumes: Task 4's `recognized_grade_pair` (top-level import; `evals/models.py` is import-leaf-safe).
- Produces: same signature, new derivation: `abandoned → False`; recognized success → True; recognized failure → False; partial/sourceless/absent → None. OWM reads RAW stored JSON, which is exactly why this consumer checks the pair itself (D2c).

- [ ] **Step 1: Rewrite the failing tests** (replace :44-54; update every eval-dict fixture in the file to carry the full pair):

```python
def test_grades_come_from_the_recognized_pair_only():
    g = {"task_result_source": "self_reported"}
    assert session_success({"task_result": "success", **g}, "completed") is True
    assert session_success({"task_result": "failure", **g}, "completed") is False
    assert session_success({"task_result": "partial", **g}, "completed") is None


def test_a_sourceless_grade_is_not_evidence():
    assert session_success({"task_result": "success"}, "completed") is None
    assert session_success({"task_result": "success",
                            "task_result_source": "vibes"}, "completed") is None


def test_legacy_records_are_unknown_never_success():
    legacy = {"outcome": "success", "metrics": {"failure_rate": 0.0}}
    assert session_success(legacy, "completed") is None
    assert session_success({"metrics": {}}, None) is None
```

Keep `test_abandoned_session_is_failure_regardless_of_metrics`, extending its fixture with the full graded pair.

- [ ] **Step 2: Run to verify failures** — `cd cortex && python -m pytest tests/test_owm.py -v`.

- [ ] **Step 3: Implement.** Delete `_SUCCESS_MAX_FR`/`_FAILURE_MIN_FR`; `from app.evals.models import recognized_grade_pair`; replace `session_success`:

```python
def session_success(eval_data: dict, bridge_status: str | None) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    2026-08-23 (outcome truth): grades come from the recognized
    (task_result, task_result_source) pair, replacing the failure_rate
    heuristic whose 0.0 was produced by Bridge's hard-coded completion stamp.
    This function reads RAW stored eval JSON, so it checks the pair itself
    (D2c): a sourceless grade is not evidence. "partial" and ungraded/legacy
    records return None — excluded rather than guessed. Bridge `abandoned`
    still overrides: a walked-away session is a failure regardless of grade.
    """
    if bridge_status == "abandoned":
        return False
    tr, _src = recognized_grade_pair(
        eval_data.get("task_result"), eval_data.get("task_result_source"))
    if tr == "success":
        return True
    if tr == "failure":
        return False
    return None
```

- [ ] **Step 4: Run** `cd cortex && python -m pytest tests/test_owm.py -v`.

- [ ] **Step 5: Commit**

```bash
git add cortex/app/owm.py cortex/tests/test_owm.py
git commit -m "feat(owm): session_success accepts only the recognized grade pair" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Scorers — `_failure_rate` freed to say "cannot tell"

**Files:**
- Modify: `cortex/app/evals/scorers.py:151-185`
- Test: `cortex/tests/test_quality_verdict_is_not_a_constant.py`

**Interfaces:**
- Consumes: safety from Task 6 (nothing load-bearing keys off `failure_rate`).
- Produces: `_failure_rate(events) -> float | None` (None on no-outcome input, stripped from metrics). `outcome_event_count` unchanged. NO dashboard change (index.html:4297 already guards absence — verified).

- [ ] **Step 1: Update the pins.** In `TestTheCounterItself`, replace `test_it_is_zero_when_nothing_reports_an_outcome`:

```python
    def test_it_is_absent_when_nothing_reports_an_outcome(self):
        """Outcome truth (2026-08-23): grading moved to the task_result pair;
        nothing keys off failure_rate and the documented asymmetry is
        resolved — both ratios say 'cannot tell' on an empty population."""
        events = [{"event_type": "memory_read"} for _ in range(48)]
        m = compute_tier1_metrics(events)
        assert m["outcome_event_count"] == 0.0
        assert "failure_rate" not in m
        assert "tool_success_rate" not in m
```

Keep the other two tests; update the module docstring's "deliberately unchanged" paragraph with the supersession + date.

- [ ] **Step 2: Run to verify the failure** — `cd cortex && python -m pytest tests/test_quality_verdict_is_not_a_constant.py -v`.

- [ ] **Step 3: Implement.** Replace `_failure_rate`:

```python
def _failure_rate(events: list[dict]) -> float | None:
    """Rate of events with outcome=failure; None when nothing carries one.

    SUPERSEDES the 2026-08-06 decision that pinned this at 0.0 on no-outcome
    input "because owm.session_success and the Living Procedures Tier B gate
    both key off it": since 2026-08-23 (outcome truth) both grade from the
    EvalResult task-grade pair, nothing load-bearing reads this metric, and
    the asymmetry with _tool_success_rate is resolved — an empty population
    answers "cannot tell", not "no failures". Read `outcome_event_count`
    beside this number; policy's SessionHealthRule defaults an absent metric
    to 0.0 (allow), the correct no-signal posture.
    """
    with_outcome = [e for e in events if e.get("outcome")]
    if not with_outcome:
        return None
    failures = sum(1 for e in with_outcome if e["outcome"] == "failure")
    return round(failures / len(with_outcome), 4)
```

Trim `_outcome_event_count`'s "denominator above" reference.

- [ ] **Step 4: Run** `cd cortex && python -m pytest tests/test_quality_verdict_is_not_a_constant.py tests/test_evals.py tests/test_policy.py tests/test_briefing_sections_inprocess.py -v`.

- [ ] **Step 5: Commit**

```bash
git add cortex/app/evals/scorers.py cortex/tests/test_quality_verdict_is_not_a_constant.py
git commit -m "feat(evals): failure_rate says cannot-tell on empty input; the 0.0 decision is superseded" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Living Procedures — drop the I4 gate; recompute only on recognized tail evidence

**Files:**
- Modify: `cortex/app/procedures/harden.py:69-116` (`_resolve_outcome`)
- Test: `cortex/tests/test_procedures_harden.py:255-293`, `cortex/tests/test_procedures_round2.py` (its real `_resolve_outcome` fixture)

**Interfaces:**
- Consumes: Task 4's `get_eval` / `compute_session_eval` / `find_terminal_grade`; Task 6's `session_success`.
- Produces: `_resolve_outcome` — a missing eval is computed once; a stored ungraded eval is recomputed ONLY when `find_terminal_grade` returns a recognized pair; then `session_success` decides. I4 gate REMOVED (D10). The predicate is the shared snapshot extractor: result-only payloads cannot loop futile recomputes, and post-completion noise cannot hide the evidence (D7).

- [ ] **Step 1: Rewrite the `_resolve_outcome` tests** (replace :259-289). First make the existing `_Eval` double expose the fields the production code reads; without this change a "graded stored eval" test silently enters the recompute branch:

```python
class _Eval:
    def __init__(self, data):
        self._data = data
        self.task_result = data.get("task_result")
        self.task_result_source = data.get("task_result_source")

    def model_dump(self):
        return self._data
```

Then add:

```python
@pytest.mark.asyncio
async def test_a_graded_eval_resolves_without_any_timeline_read(monkeypatch):
    from unittest.mock import AsyncMock
    async def _get_eval(rr, sid):
        return _Eval({"metrics": {}, "task_result": "success",
                      "task_result_source": "self_reported"})
    find = AsyncMock()
    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)
    monkeypatch.setattr("app.evals.compute.find_terminal_grade", find)
    assert await harden._resolve_outcome(object(), "sess") is True
    find.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_recognized_tail_evidence_means_no_recompute(monkeypatch):
    computed: list = []

    async def _get_eval(rr, sid):
        return _Eval({"metrics": {"failure_rate": 0.0}, "task_result": None})

    async def _find(rr, sid):
        return (None, None)

    async def _compute(rr, sid, **kw):
        computed.append(sid)

    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)
    monkeypatch.setattr("app.evals.compute.find_terminal_grade", _find)
    monkeypatch.setattr("app.evals.compute.compute_session_eval", _compute)
    assert await harden._resolve_outcome(object(), "sess") is None
    assert computed == []


@pytest.mark.asyncio
async def test_recognized_tail_evidence_triggers_one_recompute(monkeypatch):
    async def _get_eval(rr, sid):
        return _Eval({"metrics": {}, "task_result": None})

    async def _find(rr, sid):
        return ("failure", "self_reported")

    async def _compute(rr, sid, **kw):
        return _Eval({"metrics": {}, "task_result": "failure",
                      "task_result_source": "self_reported"})

    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)
    monkeypatch.setattr("app.evals.compute.find_terminal_grade", _find)
    monkeypatch.setattr("app.evals.compute.compute_session_eval", _compute)
    assert await harden._resolve_outcome(object(), "sess") is False
```

In `cortex/tests/test_procedures_round2.py`, migrate the real
`_resolve_outcome` fixture too; otherwise its `_Eval` double has no
`task_result` attribute, silently enters the new tail/recompute branch, and turns
the existing abandon-status regression test into a false failure. Give `_Eval`
the same two exposed fields as above, and replace `_clean_session`'s obsolete
timeline/failure-rate setup with the recognized pair:

```python
def _clean_session(monkeypatch):
    """A genuinely graded successful session; abandoned still overrides it."""
    async def _get_eval(rr, sid):
        return _Eval({
            "metrics": {},
            "task_result": "success",
            "task_result_source": "self_reported",
        })

    monkeypatch.setattr("app.evals.store.get_eval", _get_eval)
```

Update that file's historical comments from the failure-rate heuristic to the
recognized-pair contract. `test_procedures_defects.py` stubs `_resolve_outcome`
at every relevant site and stays unmodified.

- [ ] **Step 2: Run to verify failures** — `cd cortex && python -m pytest tests/test_procedures_harden.py tests/test_procedures_round2.py -v`.

- [ ] **Step 3: Implement.** Replace `_resolve_outcome`'s body (signature + never-raise envelope kept):

```python
async def _resolve_outcome(replay_r, session_id: str,
                           bridge_status: str | None = None) -> bool | None:
    """True/False when the session's outcome is knowable, None to exclude it.

    Outcome truth (2026-08-23): the grade lives on the EvalResult pair. The
    old I4 pre-gate is GONE — it read the oldest-1000 window, where the one
    outcome-bearing event never appears for a >1000-event session, excluding
    exactly the long sessions Tier B most needs. An ungraded STORED eval
    triggers one recompute only when find_terminal_grade (snapshot-scanned,
    shared normalizer) finds a RECOGNIZED pair — result-only payloads cannot
    loop futile recomputes; the store's first-graded-wins rule persists the
    upgrade (spec D9/D10).
    """
    if replay_r is None or not session_id:
        return None
    try:
        from app.evals.store import get_eval

        ev = await get_eval(replay_r, session_id)
        needs_compute = ev is None
        if ev is not None and getattr(ev, "task_result", None) is None:
            from app.evals.compute import find_terminal_grade
            needs_compute = (await find_terminal_grade(replay_r, session_id))[0] is not None
        if needs_compute:
            from app.evals.compute import compute_session_eval
            fresh = await compute_session_eval(replay_r, session_id, trigger="manual")
            ev = fresh or ev
        if ev is None:
            return None

        from app.owm import session_success

        data = ev.model_dump() if hasattr(ev, "model_dump") else dict(ev)
        return session_success(data, bridge_status)
    except Exception as exc:  # noqa: BLE001
        logger.debug("outcome unresolved for %s: %s", session_id, exc)
        return None
```

- [ ] **Step 4: Run** `cd cortex && python -m pytest tests/test_procedures_harden.py tests/test_procedures_round2.py tests/test_procedures_defects.py -v` — the two real `_resolve_outcome` fixture sets carry the full pair; `test_procedures_defects.py` stays unmodified.

- [ ] **Step 5: Commit**

```bash
git add cortex/app/procedures/harden.py cortex/tests/test_procedures_harden.py cortex/tests/test_procedures_round2.py
git commit -m "feat(procedures): resolve outcomes from the grade pair; the I4 gate excluded every long session" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Patterns — `"unknown"` + provenance; graded-only rates; KEEPTTL; the dead path stays dead

**Files:**
- Modify: `cortex/app/patterns/models.py:11-29`, `cortex/app/patterns/extractor.py:96-105`, `cortex/app/patterns/analyzer.py:38-43,366-391`, `cortex/app/patterns/statistics.py:153-165`, `cortex/app/patterns/store.py:37-53,271-296,352-360,432-440,538-597`, `cortex/app/patterns/api.py:376-382`, `cortex/app/policy/rules.py:164-200`, `cortex/app/main.py:551-575`, `cortex/app/evals/compute.py:194-211` (comment ONLY)
- Test: `cortex/tests/test_patterns.py`, `cortex/tests/test_policy.py`, `cortex/tests/test_tip_reconciliation.py`

**Interfaces:**
- Consumes: Task 4's validated `EvalResult` + `binary_outcome`.
- Produces: `SessionFeatures.outcome: Literal["success", "failure", "unknown"] = "unknown"`, `outcome_source: Literal["task_result", "legacy"] = "legacy"` (the default is load-bearing), `graded_only(features)`, and grade-dominant `store_features` (D9e). Rates count graded features only; `outcome_filter` requires provenance; `compute_tip_effectiveness` persists card stats with **`xx=True, keepttl=True`**; and **`record_tip_shown`'s in-place SessionFeatures rewrite is DELETED** (D9e/round-6 finding 3 — it read a whole legacy features object and wrote it back, clobbering a concurrent graded write even under XX+KEEPTTL, bypassing `store_features`' dominance guard; a repo-wide search finds NO reader of `SessionFeatures.tips_shown` — only the field def and this writer — so it is dead). The dead import at compute.py:205 is NOT fixed; a KNOWN-DEAD comment is added.

- [ ] **Step 1: Write the failing tests.** In `cortex/tests/test_patterns.py`, extend `_make_features` to accept `outcome_source` and add a module fixture for the real store tests:

```python
import fakeredis.aioredis
import pytest_asyncio


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()
```

Then add:

```python
class TestGradedProvenance:
    def test_defaults_mean_legacy_and_unknown(self):
        from app.patterns.models import SessionFeatures
        f = SessionFeatures(session_id="s")
        assert f.outcome == "unknown" and f.outcome_source == "legacy"

    def test_old_cached_json_parses_as_legacy(self):
        from app.patterns.models import SessionFeatures, graded_only
        old = SessionFeatures.model_validate_json(
            '{"session_id": "s", "outcome": "success"}')
        assert old.outcome_source == "legacy"
        assert graded_only([old]) == []

    def test_graded_only_keeps_real_grades(self):
        from app.patterns.models import SessionFeatures, graded_only
        real = SessionFeatures(session_id="a", outcome="failure",
                               outcome_source="task_result")
        fab = SessionFeatures(session_id="b", outcome="success")  # legacy default
        unk = SessionFeatures(session_id="c", outcome="unknown",
                              outcome_source="task_result")  # graded "partial"
        assert graded_only([real, fab, unk]) == [real]


@pytest.mark.asyncio
async def test_extractor_stamps_grade_and_provenance(monkeypatch):
    """A REAL extractor call; reader import is function-local — patch
    replay.reader, not the extractor module."""
    import replay.reader as reader_mod
    from app.evals.models import EvalResult
    from app.patterns.extractor import extract_session_features

    async def fake_summary(*a, **k):
        return {"event_count": 2, "duration_ms": 10}

    async def fake_timeline(*a, **k):
        return {"events": [
            {"event_type": "memory_read", "outcome": None, "payload": {}, "tags": []},
            {"event_type": "session_end", "outcome": "success", "payload": {}, "tags": []},
        ]}

    monkeypatch.setattr(reader_mod, "get_session_summary", fake_summary)
    monkeypatch.setattr(reader_mod, "get_session_timeline", fake_timeline)

    graded = await extract_session_features(
        None, "s1",
        eval_result=EvalResult(session_id="s1", trigger="session_complete",
                               task_result="success",
                               task_result_source="self_reported"))
    assert graded.outcome == "success" and graded.outcome_source == "task_result"

    ungraded = await extract_session_features(None, "s1", eval_result=None)
    assert ungraded.outcome == "unknown" and ungraded.outcome_source == "legacy"


def test_success_rate_is_none_when_nothing_is_graded():
    from app.patterns.analyzer import _success_rate
    from app.patterns.models import SessionFeatures
    assert _success_rate([SessionFeatures(session_id=str(i)) for i in range(6)]) is None


@pytest.mark.asyncio
async def test_effectiveness_does_not_extend_card_ttl(rr):
    """D11: the dashboard calls GET /patterns/effectiveness on load; before
    this change the persist used ex=_DEFAULT_TTL, giving fabricated-era cards
    a fresh 30 days per visit. KEEPTTL preserves the remaining TTL."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (
        _PATTERN_PREFIX, compute_tip_effectiveness, record_tip_shown,
        store_features, store_patterns,
    )
    await store_patterns(rr, [PatternCard(id="p1")])
    for i in range(6):
        await store_features(rr, SessionFeatures(
            session_id=f"s{i}", outcome="success",
            outcome_source="task_result"))
    await record_tip_shown(rr, "s0", ["p1"], group="treatment")
    key = f"{_PATTERN_PREFIX}p1"
    await rr.expire(key, 1000)
    before = await rr.pttl(key)
    await compute_tip_effectiveness(rr)
    after = await rr.pttl(key)
    assert 0 < after <= before


@pytest.mark.asyncio
async def test_outcome_filtered_datasets_exclude_legacy(rr):
    """'Success only' membership must not include fabricated legacy successes."""
    from app.patterns.models import SessionFeatures, Dataset
    from app.patterns.store import store_features, materialize_dataset
    await store_features(rr, SessionFeatures(session_id="leg", outcome="success"))  # legacy
    await store_features(rr, SessionFeatures(session_id="grd", outcome="success",
                                             outcome_source="task_result"))
    ds = Dataset(id="d1", name="n", outcome_filter="success")
    out = await materialize_dataset(rr, ds)
    assert out.session_ids == ["grd"]
    assert out.metrics_summary["success_count"] == 1
    assert out.metrics_summary.get("unknown_count", 0) == 0     # legacy filtered, not counted


@pytest.mark.asyncio
async def test_unfiltered_dataset_reports_unknown_instead_of_fabricated_success(rr):
    from app.patterns.models import Dataset, SessionFeatures
    from app.patterns.store import materialize_dataset, store_features
    await store_features(rr, SessionFeatures(session_id="u1", outcome="success"))
    await store_features(rr, SessionFeatures(session_id="u2", outcome="unknown"))
    out = await materialize_dataset(rr, Dataset(id="d2", name="all"))
    assert set(out.session_ids) == {"u1", "u2"}
    assert out.metrics_summary == {
        "success_count": 0,
        "failure_count": 0,
        "unknown_count": 2,
        "success_rate": None,
        "avg_duration_ms": 0,
    }


@pytest.mark.asyncio
async def test_ungraded_features_never_overwrite_graded(rr):
    """D9e: store_features is dominance-guarded — a stalled ungraded writer
    (or any later legacy re-extract) must not regress a graded record."""
    from app.patterns.models import SessionFeatures
    from app.patterns.store import store_features
    from app.patterns.store import get_all_features
    assert await store_features(rr, SessionFeatures(
        session_id="s", outcome="failure", outcome_source="task_result")) is True
    assert await store_features(rr, SessionFeatures(session_id="s")) is False  # legacy refused
    # re-read: still the graded record
    feats = {f.session_id: f for f in await get_all_features(rr)}
    assert feats["s"].outcome == "failure" and feats["s"].outcome_source == "task_result"


@pytest.mark.asyncio
async def test_stale_legacy_feature_writer_retries_then_loses(
    rr, monkeypatch
):
    """Force graded state to appear after the legacy writer's WATCH/read and
    before EXEC. Its retry must observe provenance and refuse the regression."""
    import redis
    from app.patterns.models import SessionFeatures
    from app.patterns.store import (
        _FEATURE_INDEX, _FEATURE_PREFIX, get_all_features, store_features)

    graded = SessionFeatures(
        session_id="race", outcome="success", outcome_source="task_result")
    legacy = SessionFeatures(session_id="race")
    key = f"{_FEATURE_PREFIX}race"
    real_pipeline = rr.pipeline
    state = {"raced": False}

    def racing_pipeline(*args, **kwargs):
        pipe = real_pipeline(*args, **kwargs)
        real_execute = pipe.execute

        async def execute(*ea, **ek):
            if not state["raced"]:
                state["raced"] = True
                await rr.set(key, graded.model_dump_json(), ex=86400)
                await rr.zadd(
                    _FEATURE_INDEX, {"race": graded.created_at.timestamp()})
                raise redis.WatchError("graded writer won")
            return await real_execute(*ea, **ek)

        pipe.execute = execute
        return pipe

    monkeypatch.setattr(rr, "pipeline", racing_pipeline)
    assert await store_features(rr, legacy) is False
    stored = {f.session_id: f for f in await get_all_features(rr)}["race"]
    assert (stored.outcome, stored.outcome_source) == (
        "success", "task_result")
```

Change the pre-existing `TestSessionFeatures.test_creation_defaults` assertion from `"success"` to `"unknown"`. Across `test_patterns.py`, `test_policy.py`, and `test_tip_reconciliation.py`, every fixture whose explicit success/failure is intended as experimental evidence gains `outcome_source="task_result"`; fixtures specifically exercising legacy behavior leave the source absent. This is a semantic migration, not a blanket search/replace.

`Dataset` requires only `id` and `name`; `get_all_features` is the existing reader. Add this discriminator to `cortex/tests/test_policy.py` (and add `outcome_source="task_result"` to that file's existing explicit success/failure fixtures):

```python
@pytest.mark.asyncio
async def test_unknown_sessions_do_not_dilute_recent_file_failure_rate():
    from app.patterns.models import SessionFeatures
    graded_failures = [
        SessionFeatures(
            session_id=f"g{i}", outcome="failure",
            outcome_source="task_result", file_paths=["src/buggy.py"])
        for i in range(3)
    ]
    unknown = [
        SessionFeatures(session_id=f"u{i}", file_paths=["src/buggy.py"])
        for i in range(17)
    ]
    with patch(
        "app.patterns.store.get_all_features",
        new_callable=AsyncMock,
        return_value=graded_failures + unknown,
    ):
        rule = RecentFailureRule(get_replay_redis=lambda: MagicMock())
        action, _, reason = await rule.evaluate(
            PolicyContext(file_path="src/buggy.py"))
    assert action == "warn"
    assert "3/3" in reason
```

In `cortex/tests/test_tip_reconciliation.py`, add `outcome_source="task_result"` to every existing `SessionFeatures` fixture that supplies an explicit success/failure; these are intentionally graded A/B samples, not legacy-cache tests.

- [ ] **Step 2: Run to verify failures** — `cd cortex && python -m pytest tests/test_patterns.py tests/test_policy.py -v`.

- [ ] **Step 3: Implement.**

(a) `cortex/app/patterns/models.py`:

```python
    outcome: Literal["success", "failure", "unknown"] = "unknown"
    # Provenance of `outcome` (outcome truth, 2026-08-23). The default MUST
    # mean legacy/ungraded: ~30d of cached records carry a fabricated
    # outcome="success" indistinguishable by value.
    outcome_source: Literal["task_result", "legacy"] = "legacy"
```

```python
def graded_only(features: list["SessionFeatures"]) -> list["SessionFeatures"]:
    """Features with a real grade — the only population any rate may count."""
    return [
        f for f in features
        if f.outcome_source == "task_result" and f.outcome in ("success", "failure")
    ]
```

(b) `cortex/app/patterns/extractor.py:96-105` — via the shared projection (no inline membership; D2/F8):

```python
        # Outcome truth (2026-08-23): graded task_result projected through
        # the shared binary_outcome — never invented from silence or event
        # counts. eval_result is a VALIDATED EvalResult, so a non-None
        # task_result implies the recognized source pair.
        from app.evals.models import binary_outcome
        tr = getattr(eval_result, "task_result", None) if eval_result else None
        session_outcome = binary_outcome(tr)
        outcome_source = "task_result" if tr is not None else "legacy"

        return SessionFeatures(
            session_id=session_id,
            duration_ms=summary.get("duration_ms"),
            outcome=session_outcome,
            outcome_source=outcome_source,
```

(c) `cortex/app/patterns/analyzer.py` — `_success_rate` over `graded_only`, None on empty; `analyze_patterns` computes `graded_features = graded_only(all_features)`, gates `min_sessions` and `baseline_rate is None` on it, and hands `graded_features` to every detector (their existing `len < 2/3` gates then cover all-unknown and one-sided cohorts with zero detector edits).

(d) `cortex/app/patterns/statistics.py:158`: `features = graded_only(features)` before the counting loop (the else branch pools every unmatched feature into control).

(e) `cortex/app/patterns/store.py`: in `compute_tip_effectiveness`, `features = graded_only(features)` after the load and before the `len < 5` gate; the card persist at :438 becomes:

```python
            await r.set(pk, pattern.model_dump_json(), xx=True, keepttl=True)
```

AND in `record_tip_shown` (round-4 finding 1 — this path runs from briefing generation on validation-enabled deployments and from the non-admin `/patterns/tip-shown` route, keeping frequently-shown legacy cards immortal): **DELETE the SessionFeatures rewrite block entirely** (store.py:277-286 — the `raw = get(feature_key)` → mutate `tips_shown` → `set(...)`; round-6 finding 3: XX+KEEPTTL would still clobber a concurrent graded record with the stale legacy object it read, and nothing reads `tips_shown`), and change the per-card `times_shown` persist at :296 to `xx=True, keepttl=True` (the tip-log write at :275 keeps `ex=_DEFAULT_TTL` — it CREATES a new record). **`xx=True` is not optional** (round-5 finding 5): bare `KEEPTTL` on a key that EXPIRED between the GET and the SET creates a new PERSISTENT key (TTL −1, reproduced); `xx=True` makes the write update-only, so an expired record stays gone. Append the following to `cortex/tests/test_tip_reconciliation.py` (add the `pytest_asyncio` import and its own `rr` fixture):

```python
import fakeredis.aioredis
import pytest_asyncio


@pytest_asyncio.fixture
async def rr():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_record_tip_shown_does_not_extend_or_resurrect_the_card(rr):
    """D11: the times_shown counter is bookkeeping — it must never renew a
    card's 30-day life NOR resurrect an expired card (xx=True). The features
    rewrite is DELETED, so the feature record is untouched here (finding 3)."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (record_tip_shown, _PATTERN_PREFIX,
                                     _FEATURE_PREFIX)
    card = PatternCard(id="p1", description="d", recommendation="r")
    await rr.set(f"{_PATTERN_PREFIX}p1", card.model_dump_json(), ex=1000)
    await rr.set(f"{_FEATURE_PREFIX}s1",
                 SessionFeatures(session_id="s1").model_dump_json(), ex=1000)
    card_before = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    feature_before = await rr.pttl(f"{_FEATURE_PREFIX}s1")
    feature_raw = await rr.get(f"{_FEATURE_PREFIX}s1")
    await record_tip_shown(rr, "s1", ["p1"])
    ttl = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    assert 0 < ttl <= card_before, f"card TTL={ttl} (a bare <= would accept -1)"
    # the feature record's TTL is unchanged because record_tip_shown no longer
    # rewrites it at all
    feature_after = await rr.pttl(f"{_FEATURE_PREFIX}s1")
    assert 0 < feature_after <= feature_before
    assert await rr.get(f"{_FEATURE_PREFIX}s1") == feature_raw


@pytest.mark.asyncio
async def test_record_tip_shown_does_not_resurrect_after_get_set_race(
    rr, monkeypatch
):
    """Deterministically expire the card after record_tip_shown's GET and
    immediately before its SET. Bare KEEPTTL would recreate TTL=-1; XX must
    leave it absent."""
    from app.patterns.models import PatternCard
    from app.patterns.store import record_tip_shown, _PATTERN_PREFIX
    key = f"{_PATTERN_PREFIX}p1"
    await rr.set(key, PatternCard(id="p1").model_dump_json(), ex=1000)
    real_set = rr.set
    seen = []

    async def expiring_set(name, value, *args, **kwargs):
        if name == key and kwargs.get("keepttl"):
            seen.append(dict(kwargs))
            await rr.delete(key)
        return await real_set(name, value, *args, **kwargs)

    monkeypatch.setattr(rr, "set", expiring_set)
    await record_tip_shown(rr, "s1", ["p1"])
    assert seen == [{"xx": True, "keepttl": True}]
    assert await rr.exists(key) == 0


@pytest.mark.asyncio
async def test_effectiveness_does_not_extend_or_resurrect(rr):
    """compute_tip_effectiveness's card persist uses xx=True, keepttl=True."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (store_features, record_tip_shown,
                                     compute_tip_effectiveness, _PATTERN_PREFIX)
    card = PatternCard(id="p1", description="d", recommendation="r")
    await rr.set(f"{_PATTERN_PREFIX}p1", card.model_dump_json(), ex=1000)
    # >=5 graded features, some shown p1, so compute reaches the persist
    for i in range(6):
        sid = f"s{i}"
        await store_features(rr, SessionFeatures(
            session_id=sid, outcome="success", outcome_source="task_result"))
        if i < 3:
            await record_tip_shown(rr, sid, ["p1"], group="treatment")
        elif i < 5:
            await record_tip_shown(rr, sid, ["p1"], group="control")
    before = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    await compute_tip_effectiveness(rr)
    ttl = await rr.pttl(f"{_PATTERN_PREFIX}p1")
    assert 0 < ttl <= before, f"card TTL={ttl}"


@pytest.mark.asyncio
async def test_effectiveness_does_not_resurrect_after_get_set_race(
    rr, monkeypatch
):
    """Delete the card after get_patterns loaded it but before the stats SET."""
    from app.patterns.models import PatternCard, SessionFeatures
    from app.patterns.store import (
        _PATTERN_PREFIX, compute_tip_effectiveness, record_tip_shown,
        store_features, store_patterns,
    )
    key = f"{_PATTERN_PREFIX}p1"
    await store_patterns(rr, [PatternCard(id="p1")])
    for i in range(6):
        await store_features(rr, SessionFeatures(
            session_id=f"r{i}", outcome="success",
            outcome_source="task_result"))
    await record_tip_shown(rr, "r0", ["p1"], group="treatment")
    real_set = rr.set
    seen = []

    async def expiring_set(name, value, *args, **kwargs):
        if name == key and kwargs.get("keepttl"):
            seen.append(dict(kwargs))
            await rr.delete(key)
        return await real_set(name, value, *args, **kwargs)

    monkeypatch.setattr(rr, "set", expiring_set)
    await compute_tip_effectiveness(rr)
    assert seen == [{"xx": True, "keepttl": True}]
    assert await rr.exists(key) == 0
```

The `0 < ttl <= before` assertion rejects Redis TTL `-1`; the two deletion hooks prove both update sites send exactly `xx=True, keepttl=True` after a successful GET and cannot resurrect the just-expired key.

In `materialize_dataset` (:579-592), initialize `unknown_count = 0` beside the two existing counters, then replace the outcome filter/counting and metrics summary with this exact shape:

```python
        # An outcome-filtered dataset admits only measured task-result
        # provenance. A fabricated legacy "success" is excluded from
        # membership entirely, not relabeled inside the filtered cohort.
        if dataset.outcome_filter and (
            f.outcome_source != "task_result"
            or f.outcome != dataset.outcome_filter
        ):
            continue

        matched.append(f.session_id)
        if f.outcome_source == "task_result" and f.outcome == "success":
            success_count += 1
        elif f.outcome_source == "task_result" and f.outcome == "failure":
            failure_count += 1
        else:
            unknown_count += 1
        if f.duration_ms is not None:
            total_duration += f.duration_ms
            duration_count += 1

    graded_count = success_count + failure_count
    dataset.session_ids = matched
    dataset.session_count = len(matched)
    dataset.metrics_summary = {
        "success_count": success_count,
        "failure_count": failure_count,
        "unknown_count": unknown_count,
        "success_rate": (
            round(success_count / graded_count, 3) if graded_count else None
        ),
        "avg_duration_ms": (
            round(total_duration / duration_count) if duration_count else 0
        ),
    }
```

Guard that nullable rate explicitly in `patterns/api.py:376-382` rather than relying on the broad exception handler:

```python
                baseline = ds.metrics_summary.get("success_rate")
                if isinstance(baseline, (int, float)):
                    try:
                        result["recommended_sample_size"] = minimum_sample_size(
                            baseline_rate=baseline)
                    except Exception:
                        result["recommended_sample_size"] = None
                else:
                    result["recommended_sample_size"] = None
```

(f) `cortex/app/policy/rules.py:166-170` and `cortex/app/main.py:562-566` (both copies identically): count only graded features inside the `file_match` branch.

(g) `cortex/app/patterns/store.py` — `store_features` grade dominance (D9e). It is an unconditional SET today (:37-53), so a stalled ungraded writer, or any later legacy re-extract of a session an upgrade already graded, silently regresses stored provenance to legacy. Guard it with the same WATCH/MULTI CAS shape as the eval store, refusing to replace a `task_result` record with a non-graded one:

```python
async def store_features(r, features, ttl_days: int = 30) -> bool:
    key = f"{_FEATURE_PREFIX}{features.session_id}"
    data = features.model_dump_json()
    ttl = ttl_days * 86400
    incoming_graded = features.outcome_source == "task_result"
    for _attempt in range(8):
        try:
            async with r.pipeline() as pipe:
                await pipe.watch(key)
                existing_raw = await pipe.get(key)
                if existing_raw and not incoming_graded:
                    try:
                        existing = SessionFeatures.model_validate_json(existing_raw)
                        if existing.outcome_source == "task_result":
                            await pipe.unwatch()
                            return False        # never regress graded -> legacy
                    except Exception:
                        pass
                pipe.multi()
                pipe.set(key, data, ex=ttl)
                pipe.zadd(_FEATURE_INDEX, {features.session_id: features.created_at.timestamp()})
                await pipe.execute()
                return True
        except redis.WatchError:
            continue
    return False
```

(`import redis` at module top if absent. A graded write always proceeds; only ungraded-over-graded is refused. Callers ignore the bool today, so the added False path is safe. `record_tip_shown`'s features rewrite is DELETED, so nothing in the tip path fights this guard.)
Keep the function's current outer `try/except Exception` reliability envelope around the loop; after eight `WatchError`s log the exhausted CAS and return `False`.

(h) `cortex/app/evals/compute.py:194-211` — KNOWN-DEAD comment above the broken lifecycle import; the import itself UNCHANGED:

```python
        # KNOWN-DEAD, LEFT DEAD (outcome truth D11): the lifecycle import
        # below has never resolved (promote_all_patterns lives in store.py),
        # so this auto-analyze/promote block never runs — and reviving it
        # would rewrite EVERY stored card (promote_all_patterns stores all
        # cards back even when analysis returns []), resurrecting
        # fabricated-era cards. Revival needs card provenance — PR3.
```

- [ ] **Step 4: Run** `cd cortex && python -m pytest tests/test_patterns.py tests/test_policy.py tests/test_tip_reconciliation.py -v`. Existing fixtures relying on the old `"success"` default gain explicit graded pairs.

- [ ] **Step 5: Commit**

```bash
git add cortex/app/patterns/models.py cortex/app/patterns/extractor.py cortex/app/patterns/analyzer.py cortex/app/patterns/statistics.py cortex/app/patterns/store.py cortex/app/patterns/api.py cortex/app/policy/rules.py cortex/app/main.py cortex/app/evals/compute.py cortex/tests/test_patterns.py cortex/tests/test_policy.py cortex/tests/test_tip_reconciliation.py
git commit -m "feat(patterns): provenance-filtered grading; effectiveness stops refreshing card TTLs" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Documentation — the guides tell the new truth

**Files:**
- Modify: `README.md`, `docs/guides/knowledge-autopilot.md`, `docs/guides/replay-evals-patterns.md`, `docs/guides/memory-and-recall.md` (OWM bullet :85), `docs/guides/bridge-context-and-briefing.md`
- Test: root `tests/test_procedure_docs.py`, `tests/test_dashboard_procedures.py` (stay green)

- [ ] **Step 1: `README.md` + `docs/guides/knowledge-autopilot.md`** — amend README's “Session completes” paragraph with the optional structured grade and unknown-on-absence contract. In the guide, append a dated update after the founding framing (do NOT rewrite the historical measurement): the structured grade + evidence params; both terminal events carry the atomic pair; principal-bound completion/public-abandon/takeover (a copied agent label is not terminal authority); the unchanged trusted reaper path; the Bridge-only `eval:grade` service scope on the dedicated key; first-graded-wins store; legacy/sourceless/ungraded completions excluded, never success.

- [ ] **Step 2: `docs/guides/replay-evals-patterns.md`** — `_failure_rate` → None supersession; the pair lifted hint-first then via a snapshot-and-hydrate `find_terminal_grade` (5,000-event cap; head-window residuals for Tier-1 metrics and OWM's memory_read join; webhook order non-authoritative — D9f); `SessionFeatures.outcome_source`, graded-only rates, and grade-dominant `store_features`; `xx=True, keepttl=True` on the effectiveness card persist plus the deleted `record_tip_shown` features rewrite; the KNOWN-DEAD auto-analysis path deliberately left dead until PR3 card provenance.

- [ ] **Step 3: `docs/guides/memory-and-recall.md` OWM bullet** — grading by the recognized pair; past-tense the degeneracy RETAINING the literal substrings `outcome=` and `_failure_rate` (guard: `tests/test_procedure_docs.py:117-141`).

- [ ] **Step 4: `docs/guides/bridge-context-and-briefing.md`** — the two new params, coercion rule, FastMCP type boundary, immutable `owner_member` binding, whole-completion and public-abandon refusal on bound principal mismatch, unchanged trusted reaper path, the legacy-unbound terminal residual, refusal of cross-member takeover until an owner-authorized handoff exists, first-graded-wins authoritative re-completion, the disclosed at-least-once duplicate-completion effects, and the trigger-borne grade under `eval:grade`.

- [ ] **Step 5: Run** `python -m pytest tests/test_procedure_docs.py tests/test_dashboard_procedures.py -v` (repo root).

Consistency check: `CLAUDE.md` has no tool signature, env-key inventory, or eval-grade contract to update; client adapters/instruction blocks stay untouched under D12; dashboard already guards the absent metric.

- [ ] **Step 6: Commit**

```bash
git add README.md docs/guides/knowledge-autopilot.md docs/guides/replay-evals-patterns.md docs/guides/memory-and-recall.md docs/guides/bridge-context-and-briefing.md
git commit -m "docs(guides): outcome truth — principal-bound grading via the task_result pair" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Full verification sweep

**Files:** none new.

- [ ] **Step 1: Run all suites**

```bash
cd bridge && python -m pytest tests/ -q
cd ../cortex && python -m pytest tests/ -q
cd ../replay && python -m pytest tests/ -q
cd ../client && python -m pytest tests/ -q     # must pass UNMODIFIED (D12)
cd ../auth && python -m pytest tests/ -q
cd .. && python -m pytest tests/ -q            # root guard tests
bash deploy/tests/test_bootstrap_keys.sh
bash deploy/tests/test_auth_posture.sh
bash deploy/tests/test_firekeep_admin.sh
```

The replay run includes the real-Redis 5,000-body hydration gate; a skip is acceptable only when Redis is unavailable locally and CI must run it with Redis present.

- [ ] **Step 2: Grep for stragglers**

```bash
rg -n "_SUCCESS_MAX_FR|_FAILURE_MIN_FR" cortex
rg -n "outcome=outcome or" cortex/app/evals
rg -n '"success", "partial", "failure"' cortex/app -g "*.py"
rg -n "ex=_DEFAULT_TTL" cortex/app/patterns/store.py
rg -n "from app.patterns.lifecycle import promote_all_patterns" cortex/app
rg -n "FIREKEEP_BRIDGE_KEY" docker-compose.yml
```

Expected: the first two have zero hits. The grade-vocabulary scan may hit only `evals/models.py`; Bridge's producer-side tuple lives outside this search and `evals/api.py`'s wire regex is deliberately not a tuple. The TTL scan must not match `compute_tip_effectiveness` or the remaining `record_tip_shown` PatternCard write; the tip-log create and admin-triggered `store_patterns` may retain `ex=`. The dead lifecycle import must still occur exactly once with the KNOWN-DEAD comment above it. The bridge-key scan must show exactly one substitution (`bridge`) and six explicit blanks; `tests/test_compose_secrets.py` pins this structurally. Finally inspect `record_tip_shown` and confirm the whole SessionFeatures read/mutate/write block is gone.

- [ ] **Step 3: Manual read-through** of the five critical-path diffs (`bridge/app/session.py`, `bridge/app/mcp_server.py`, `cortex/app/evals/compute.py`, `cortex/app/evals/store.py`, `cortex/app/owm.py`) against D1–D4, D7–D9, D13. Verify especially that a refused complete/resume returns before all secondary effects; a refused bound public abandon calls neither `abandon_session` nor `after_abandon`; the fallback abandon target is resolved and passed explicitly; reaper still calls the untouched manager method directly; manager/tool emits use the stored pair; the final webhook read has no stale fallback; and every WATCH retry re-runs its decision from fresh state.

- [ ] **Step 4: Final commit if the sweep produced fixes** — EXPLICIT paths only.

```bash
git add <each file the sweep actually touched>
git commit -m "fix(outcome-truth): sweep fixes from full-suite verification" -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
