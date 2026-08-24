# Outcome Truth PR2 — receipts & honest measurement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the exposure→application half of the outcome-join PR1 made truthful: SSE recall gets full replay/staleness parity, `memory_feedback` emits the applied-signal receipt its dead headers were meant for, and long-session metrics stop truncating.

**Architecture:** Additive receipts on existing paths + reuse of PR1's snapshot/hydrate primitives for full-session metric scans. One new replay `EventType` (`memory_feedback`). No new services, no schema migration, no hot-path emit sites, no reader/emitter changes.

**Tech Stack:** Python 3.11, FastAPI, replay stream over Redis (fakeredis in tests), pytest.

**Spec:** `docs/superpowers/specs/2026-08-24-outcome-truth-pr2-receipts-design.md` (D1–D5). The plan argues from the spec; conflicts resolve against it.

## Global Constraints

- **Explicit-file commits only.** `git add <exact paths>` — never `git add -A`/`git add .`. Never stage the pre-existing dirty/untracked files (`scripts/installlab/lab.py`, `docs/marketing/`, `scripts/demo/`).
- **No PR1 regressions.** Do not touch `find_terminal_grade`, `recognized_grade_pair`, the WATCH/MULTI completion CAS, the reaper/abandon paths, or `eval:grade` scoping. Metrics changes must leave the *grade* path byte-unchanged.
- **Receipts never carry content.** Payloads contain ids and enums only: `memory_ids`, `result_count`, `trigger`, `namespace`, `useful`, `comment_present` (bool), `updated`. Never the memory text, never the comment body. Query text only where `memory_read` already caps it at 200 chars.
- **Best-effort, never break the caller.** Every new emit/bump is wrapped so it cannot raise into the request or the SSE stream. The SSE receipt runs after the last yield, in a `finally`.
- **Personal/bypass mode emits nothing new** — the new paths must be suppressed exactly as the existing `memory_read` emit is.
- **`_METRIC_SCAN_MAX = 5000`** — one module constant, matching `get_session_event_ids`'s default and `find_terminal_grade`'s cap.
- Event type is named exactly **`memory_feedback`** (not `artifact_applied`).

---

### Task 1: Add `memory_feedback` to the replay EventType contract

**Files:**
- Modify: `replay/models.py:30-41` (the `EventType` Literal)
- Modify: `replay/tests/test_models.py:108-114` (`test_all_event_types`)

**Interfaces:**
- Consumes: nothing.
- Produces: `"memory_feedback"` as a recognized `EventType` member — Task 3 emits it.

**Why first:** foundational and trivial; the read/write path already treats `event_type` as an opaque string (`compute.py` emits `agent.action.predict`/`reconcile`, absent from the Literal), so this is a contract-honesty change, not a functional gate. Doing it first lets Task 3's test assert against the declared contract.

- [ ] **Step 1: Extend the enumeration test to require the new member**

In `replay/tests/test_models.py`, add `"memory_feedback"` to the `valid_types` list in `test_all_event_types`:

```python
    def test_all_event_types(self):
        valid_types = [
            "session_start", "session_end", "memory_read", "memory_write",
            "ctx_update", "env_change", "claim", "release",
            "coordination", "webhook", "memory_feedback",
        ]
        for et in valid_types:
```

- [ ] **Step 2: Run it, expect failure**

Run: `cd replay && pytest tests/test_models.py::TestReplayEvent::test_all_event_types -v`
(If the class name differs, use `pytest tests/test_models.py -k test_all_event_types -v`.)
Expected: FAIL — the model rejects `"memory_feedback"` (ValidationError) because it is not yet in the Literal.

- [ ] **Step 3: Add the member to the Literal**

In `replay/models.py`, append to `EventType`:

```python
EventType = Literal[
    "session_start",
    "session_end",
    "memory_read",
    "memory_write",
    "ctx_update",
    "env_change",
    "claim",
    "release",
    "coordination",
    "webhook",
    "memory_feedback",
]
```

- [ ] **Step 4: Run it, expect pass**

Run: `cd replay && pytest tests/test_models.py -k test_all_event_types -v`
Expected: PASS.

- [ ] **Step 5: Full replay suite (guard against an over-tight enumeration test elsewhere)**

Run: `cd replay && pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add replay/models.py replay/tests/test_models.py
git commit -m "feat(replay): add memory_feedback event type to the contract"
```

---

### Task 2: SSE streaming recall — full parity with the non-streaming path (D1)

**Files:**
- Modify: `cortex/app/streaming.py` (the `recall_stream` endpoint / `event_generator`)
- Test: `cortex/tests/test_streaming.py` (create if absent)
- Reference (do not modify): `cortex/app/main.py:1291-1342` — the parity target; `cortex/app/engine/rag.py:408-505` — the `recall_streaming` source-frame shape.

**Interfaces:**
- Consumes: `_replay_emit(event_type, session_id, agent_id, payload, **kwargs)` and `_bump_untagged_counter(redis_client, session_id)` from `app.main` (module-level, best-effort); `get_redis` DI for the counter pipeline.
- Produces: an SSE recall now emits one `memory_read` replay event with the returned `memory_ids` and bumps `memory:access_counts` + `memory:last_recalled` — identical to `POST /memory/recall`.

**Implementation notes for the implementer (read before coding):**
- The ids are on the wire: each `{"type": "source", "data": source}` frame carries `source["metadata"]`. Accumulate `frame["data"].get("metadata", {}).get("id")`, truthy-filtered — the **same** derivation the non-streaming path uses for `accessed_ids` (`main.py:1293-1295`), so SSE parity holds by construction across rag.py's vector (`rag.py:470`) and graph (`rag.py:488-494`) source branches. Verify the `"id"` key against both branches in `rag.py::recall_streaming` before finalizing; if the graph branch omits `id`, that under-count is identical to the non-streaming path's and therefore correct parity, not a new bug.
- Avoid a circular import: `app.main` imports `create_streaming_router` from this module at load. Import `_replay_emit`/`_bump_untagged_counter` **inside** the generator's `finally` block (`from app.main import _replay_emit, _bump_untagged_counter`) — a request-time import, after `app.main` is fully loaded. Do **not** import them at module top.
- The receipt must run even if the client disconnects mid-stream: put the bump+emit in a `finally` around the `async for`, guarded so it never raises into the response.
- `top_score` is deliberately omitted (constant 1.0, no consumer — spec D1). Emit `{memory_ids, result_count, trigger, namespace}`.
- Read `X-Session-Id`/`X-Agent-Id` off `request.headers` with the same `"unknown"` defaults as `main.py:1310-1311`.

- [ ] **Step 1: Write the failing test**

`cortex/tests/test_streaming.py` — assert a streamed recall emits a `memory_read` with the source ids and bumps the access hashes. Match the repo's existing async/fakeredis test style (see `cortex/tests/test_main.py` for the app fixture + header-passing pattern; reuse it rather than inventing one).

```python
import json
import pytest

@pytest.mark.asyncio
async def test_stream_emits_memory_read_receipt(client, replay_redis, app_redis):
    # client is the cortex TestClient/httpx client with a stubbed rag engine whose
    # recall_streaming yields two source frames (ids "m1","m2") then a done frame.
    resp = await client.post(
        "/memory/recall/stream",
        json={"task": "how do I deploy", "top_k": 5, "namespace": "default"},
        headers={"X-Session-Id": "sess-stream", "X-Agent-Id": "agent-x"},
    )
    assert resp.status_code == 200
    _ = resp.text  # drain the stream so the finally-block receipt fires

    # A memory_read event was written to the replay stream for this session.
    events = await read_session_events(replay_redis, "sess-stream", event_type="memory_read")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert set(payload["memory_ids"]) == {"m1", "m2"}
    assert payload["result_count"] == 2
    assert "top_score" not in payload  # constant 1.0 — deliberately omitted (D1)

    # Access + staleness clocks advanced (parity with the non-streaming path).
    assert await app_redis.hget("memory:access_counts", "m1") == b"1"
    assert await app_redis.hexists("memory:last_recalled", "m2")


@pytest.mark.asyncio
async def test_stream_receipt_suppressed_in_personal_mode(client_personal, replay_redis):
    # Personal/bypass mode must emit nothing new — mirror how the existing
    # memory_read emit is suppressed (assert zero memory_read events).
    resp = await client_personal.post(
        "/memory/recall/stream",
        json={"task": "x", "top_k": 3, "namespace": "default"},
        headers={"X-Session-Id": "sess-personal", "X-Agent-Id": "a"},
    )
    _ = resp.text
    events = await read_session_events(replay_redis, "sess-personal", event_type="memory_read")
    assert events == []
```

If a `read_session_events` helper does not exist, add a tiny local one that reads the session index + hydrates (or reuse `replay.reader.get_session_timeline`). Confirm the personal-mode suppression mechanism the codebase actually uses (how the non-streaming `memory_read` is gated) and mirror it; if the non-streaming path is NOT gated in personal mode, drop the second test and note that in the ledger rather than inventing a gate.

- [ ] **Step 2: Run the tests, expect failure**

Run: `cd cortex && pytest tests/test_streaming.py -v`
Expected: FAIL — no `memory_read` emitted, access hashes empty.

- [ ] **Step 3: Implement parity in `recall_stream`**

Add `redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)]` to the endpoint signature (import `Depends`, `redis`, `get_redis` as `main.py` does). Accumulate ids in the generator; run the receipt in a `finally`:

```python
    @router.post("/memory/recall/stream")
    async def recall_stream(
        request: Request,
        query: ContextQuery,
        redis_client: Annotated[redis.asyncio.Redis, Depends(get_redis)],
    ) -> StreamingResponse:
        principal = request_principal(request)
        sid = request.headers.get("X-Session-Id", "unknown")
        aid = request.headers.get("X-Agent-Id", "unknown")

        async def event_generator():
            accessed_ids: list[str] = []
            try:
                async for event in rag_engine.recall_streaming(
                    query,
                    workspace_id=principal["workspace_id"],
                    member_id=principal["member_id"],
                ):
                    event_type = event["type"]
                    data = json.dumps(event["data"], default=str)
                    if event_type == "source":
                        mid = (event["data"].get("metadata") or {}).get("id")
                        if mid:
                            accessed_ids.append(mid)
                        yield f"event: sources\ndata: {data}\n\n"
                    elif event_type == "context":
                        yield f"event: context\ndata: {data}\n\n"
                    elif event_type == "done":
                        yield f"event: done\ndata: {data}\n\n"
            finally:
                await _emit_stream_receipt(
                    redis_client, sid, aid, query, accessed_ids
                )

        return StreamingResponse(event_generator(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

Add the helper in `streaming.py` (late imports inside it break the cycle):

```python
async def _emit_stream_receipt(redis_client, sid, aid, query, accessed_ids):
    """Best-effort parity with the non-streaming recall receipt. Never raises."""
    try:
        from app.main import _replay_emit, _bump_untagged_counter
        if accessed_ids:
            from datetime import datetime, timezone
            now_iso = datetime.now(timezone.utc).isoformat()
            pipe = redis_client.pipeline()
            for mem_id in accessed_ids[:50]:
                pipe.hincrby("memory:access_counts", mem_id, 1)
                pipe.hset("memory:last_recalled", mem_id, now_iso)
            await pipe.execute()
        await _bump_untagged_counter(redis_client, sid)
        await _replay_emit(
            "memory_read", session_id=sid, agent_id=aid,
            payload={
                "query": query.task[:200],
                "top_k": query.top_k,
                "trigger": query.trigger,
                "result_count": len(accessed_ids),
                "namespace": query.namespace,
                "memory_ids": accessed_ids[:50],
            },
        )
    except Exception as exc:  # noqa: BLE001 — a receipt must never break the stream
        logger.warning("stream recall receipt failed: %s", exc)
```

If the non-streaming `memory_read` is personal-mode-gated, apply the identical gate here (check the same condition before emitting).

- [ ] **Step 4: Run the tests, expect pass**

Run: `cd cortex && pytest tests/test_streaming.py -v`
Expected: PASS.

- [ ] **Step 5: Full cortex suite (no regression on the streaming router wiring)**

Run: `cd cortex && pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/streaming.py cortex/tests/test_streaming.py
git commit -m "feat(cortex): SSE streaming recall emits the memory_read receipt + staleness bumps"
```

---

### Task 3: `memory_feedback` replay receipt — read the dead headers, emit the applied signal (D2)

**Files:**
- Modify: `cortex/app/main.py:1595-1621` (the `memory_feedback` handler)
- Test: `cortex/tests/test_main.py` (or the existing feedback test module — search for `def test_*feedback*`)

**Interfaces:**
- Consumes: `_replay_emit` (same module); `FeedbackRequest{memory_ids, useful, comment}`; `_bump_untagged_counter` (same module).
- Produces: a `memory_feedback` replay event per feedback call carrying `{memory_ids, useful, comment_present, updated}` — the applied stage of the join.

**Implementation notes:**
- The id space is verified: `feedback.memory_ids` are the recall `metadata["id"]`s = `memory_read.memory_ids` = the Qdrant point id `set_feedback` uses. Emit them as-is.
- `comment_present = feedback.comment is not None` — never emit the comment body (Global Constraint).
- Read headers with the `"unknown"` defaults; call `_bump_untagged_counter` for discipline parity with `memory_recall`.
- Emit **after** the `set_feedback` loop, wrapped so it never affects the response.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_feedback_emits_replay_receipt(client, replay_redis, vector_stub):
    resp = await client.post(
        "/memory/feedback",
        json={"memory_ids": ["m1", "m2"], "useful": True, "comment": "spot on"},
        headers={"X-Session-Id": "sess-fb", "X-Agent-Id": "agent-x"},
    )
    assert resp.status_code == 200
    events = await read_session_events(replay_redis, "sess-fb", event_type="memory_feedback")
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["memory_ids"] == ["m1", "m2"]
    assert p["useful"] is True
    assert p["comment_present"] is True
    assert "comment" not in p               # body never leaves (Global Constraint)
    assert p["updated"] == 2
```

- [ ] **Step 2: Run it, expect failure**

Run: `cd cortex && pytest tests/test_main.py -k test_feedback_emits_replay_receipt -v`
Expected: FAIL — no `memory_feedback` event.

- [ ] **Step 3: Implement the receipt**

Add `request: Request` is already present; add `redis_client: Annotated[..., Depends(get_redis)]` to the signature (for the untagged bump). After the loop, before `return`:

```python
    sid = request.headers.get("X-Session-Id", "unknown")
    aid = request.headers.get("X-Agent-Id", "unknown")
    await _bump_untagged_counter(redis_client, sid)
    await _replay_emit(
        "memory_feedback",
        session_id=sid,
        agent_id=aid,
        payload={
            "memory_ids": feedback.memory_ids[:50],
            "useful": feedback.useful,
            "comment_present": feedback.comment is not None,
            "updated": updated,
        },
    )
    return FeedbackResponse(status="recorded", updated=updated)
```

`_replay_emit` is already best-effort (its own try/except), so no extra wrapper is needed; if the personal-mode gate applies to `memory_read`, apply the same gate here.

- [ ] **Step 4: Run it, expect pass**

Run: `cd cortex && pytest tests/test_main.py -k test_feedback_emits_replay_receipt -v`
Expected: PASS.

- [ ] **Step 5: Full cortex suite**

Run: `cd cortex && pytest tests/ -q`
Expected: green.

- [ ] **Step 6: Commit**

```bash
git add cortex/app/main.py cortex/tests/test_main.py
git commit -m "feat(cortex): memory_feedback emits an applied-signal replay receipt"
```

---

### Task 4: Whole-session metrics + OWM join, truncation made explicit (D3)

**Files:**
- Modify: `cortex/app/evals/compute.py:88-148` (the metrics/Brier/failure scan)
- Modify: `cortex/app/owm.py:113-127` (the memory_read join)
- Modify: `cortex/app/evals/models.py` or wherever `EvalResult` is defined — add optional `metrics_truncated: bool = False` (search: `class EvalResult`)
- Test: `cortex/tests/test_evals*.py` and `cortex/tests/test_owm*.py` (match existing filenames)

**Interfaces:**
- Consumes: `get_session_event_ids(r, session_id, *, limit=5000) -> list[str]` and `get_event_batch(r, event_ids) -> list[dict]` from `replay.reader` (PR1 primitives).
- Produces: metrics computed over the full session (up to the cap); `metrics_truncated` surfaced on the EvalResult.

**Implementation notes:**
- Define `_METRIC_SCAN_MAX = 5000` as a module constant in `compute.py` (and reuse the same value in `owm.py`, importing it or re-declaring with a comment tying them).
- `compute.py`: keep `get_session_summary` for `event_count` (uncapped, correct). Replace the `get_session_timeline(limit=1000)` fetch with:
  ```python
  ids = await get_session_event_ids(replay_redis, session_id, limit=_METRIC_SCAN_MAX)
  events = await get_event_batch(replay_redis, ids)
  metrics_truncated = len(ids) >= _METRIC_SCAN_MAX
  if metrics_truncated:
      logger.warning("eval metrics truncated at %d events for session %s", _METRIC_SCAN_MAX, session_id)
  ```
  `get_session_event_ids` returns oldest-first, so ordering for the Brier predict→reconcile join is preserved. Thread `metrics_truncated` onto the stored `EvalResult`.
- `owm.py`: replace `timeline_fn(replay_r, sid, event_type="memory_read", limit=1000)` with a full-session scan then a Python filter:
  ```python
  ids = await get_session_event_ids(replay_r, sid, limit=_METRIC_SCAN_MAX)
  all_events = await get_event_batch(replay_r, ids)
  events = [e for e in all_events if e.get("event_type") == "memory_read"]
  ```
  Keep the existing envelope-vs-list guard intent (the comment at `owm.py:113-115`): `get_event_batch` returns a plain list, so the `.get("events")` unwrap is dropped here — verify no other caller of this block relies on the envelope. `owm.py` takes `timeline_fn` as an injectable seam (`owm.py:69,91`); add `event_ids_fn`/`batch_fn` seams the same way (default to the real reader fns) so the OWM tests can inject fakes, OR inject a single `events_fn(replay_r, sid) -> list` — pick whichever matches the existing test doubles with the least churn, and record the choice in the ledger.

- [ ] **Step 1: Write the failing tests**

`compute.py` — a session with >1000 events where a `failure`/`reconcile` sits past index 1000 must now be counted:

```python
@pytest.mark.asyncio
async def test_metrics_include_late_events_beyond_1000(replay_redis):
    sid = "sess-long"
    await seed_events(replay_redis, sid, n_filler=1200)          # 1200 benign tool_call events
    await seed_event(replay_redis, sid, event_type="tool_call", outcome="failure", id="late-fail")
    result = await compute_session_eval(replay_redis, sid, task_result_hint=None)
    assert "late-fail" in result.failure_event_ids            # old oldest-1000 window dropped it
    assert result.metrics_truncated is False                  # 1201 < 5000


@pytest.mark.asyncio
async def test_metrics_truncated_flag_at_cap(replay_redis):
    sid = "sess-huge"
    await seed_events(replay_redis, sid, n_filler=5200)
    result = await compute_session_eval(replay_redis, sid, task_result_hint=None)
    assert result.metrics_truncated is True
```

`owm.py` — a late memory_read joins:

```python
@pytest.mark.asyncio
async def test_owm_join_sees_late_memory_read(replay_redis, ...):
    sid = "sess-late-read"
    await seed_events(replay_redis, sid, n_filler=1100)
    await seed_event(replay_redis, sid, event_type="memory_read",
                     payload={"memory_ids": ["late-mem"]}, id="late-read")
    # ...seed a session_end grade so session_success != None...
    out = await _owm_join(...)   # call the real join fn used by run_owm_scoring
    assert "late-mem" in collected_ids(out)
```

Adapt fixtures/helpers to the existing eval/owm test modules; do not invent a parallel harness.

- [ ] **Step 2: Run them, expect failure**

Run: `cd cortex && pytest tests/ -k "late_events_beyond_1000 or metrics_truncated_flag or owm_join_sees_late" -v`
Expected: FAIL (late ids dropped by the oldest-1000 window; `metrics_truncated` attribute missing).

- [ ] **Step 3: Implement the full-session scans + flag**

Apply the `compute.py` and `owm.py` changes from the implementation notes; add `metrics_truncated: bool = False` to `EvalResult`.

- [ ] **Step 4: Run them, expect pass**

Run: `cd cortex && pytest tests/ -k "late_events_beyond_1000 or metrics_truncated_flag or owm_join_sees_late" -v`
Expected: PASS.

- [ ] **Step 5: Full cortex suite — the grade path must be byte-unchanged**

Run: `cd cortex && pytest tests/ -q`
Expected: green, including all existing eval/OWM/procedures tests (the metrics change must not perturb `session_success` or the grade).

- [ ] **Step 6: Commit**

```bash
git add cortex/app/evals/compute.py cortex/app/owm.py cortex/app/evals/models.py cortex/tests/
git commit -m "fix(cortex): eval metrics + OWM join read the whole session, truncation made explicit"
```

---

### Task 5: Documentation + change-consistency

**Files:**
- Modify: `docs/guides/replay-evals-patterns.md` — the truncation fix (metrics/Brier/OWM now full-session, `metrics_truncated` marker, `_METRIC_SCAN_MAX`) and the new `memory_feedback` event type.
- Modify: `docs/guides/memory-and-recall.md` — SSE recall now has full receipt/staleness parity; `memory_feedback` emits an applied-signal receipt.
- Modify: `cortex/CLAUDE.md` — the MCP tool/event inventory line where the replay event surface is described, to name `memory_feedback`.
- Verify: root `CLAUDE.md` / `docs/guides/dexes.md` need no change (no tool/endpoint/env-var added — new *event type* only). If a doc asserts "10 event types" or lists them, update the count/list.

**Interfaces:** none (documentation).

- [ ] **Step 1: Grep for stale references to update**

Run: `git grep -n "oldest-1000\|limit=1000\|event type" docs/ cortex/CLAUDE.md CLAUDE.md`
Read each hit; update the ones describing the eval/OWM read window or the replay event-type set. (Do not rewrite unrelated prose.)

- [ ] **Step 2: Write the guide updates**

In `docs/guides/replay-evals-patterns.md`, add a short subsection: metrics/Brier/failure-ids and the OWM memory_read join now snapshot the whole session via `get_session_event_ids` + `get_event_batch` (capped at `_METRIC_SCAN_MAX = 5000`, matching the grade scan); beyond the cap, `metrics_truncated` is stamped and logged — the grade stays truthful regardless. Note `memory_feedback` as the applied-signal event joining `memory_read` (exposed) to the session grade (outcome).

In `docs/guides/memory-and-recall.md`, note that `/memory/recall/stream` now emits the same `memory_read` receipt and access/staleness bumps as `/memory/recall`, and that `/memory/feedback` emits a `memory_feedback` receipt.

In `cortex/CLAUDE.md`, add `memory_feedback` where replay event types / the feedback tool are described.

- [ ] **Step 3: Consistency check**

Confirm no doc still claims recall-stream emits nothing, that the eval window is 1000, or that there are exactly 10 event types. Re-run the Step 1 grep and eyeball.

- [ ] **Step 4: Commit**

```bash
git add docs/guides/replay-evals-patterns.md docs/guides/memory-and-recall.md cortex/CLAUDE.md
git commit -m "docs: PR2 receipts — SSE parity, memory_feedback event, full-session metrics"
```

---

## Self-Review

**Spec coverage:** D1→Task 2; D2→Task 3; D3→Task 4; D4→Task 1; D5→constraints + the personal-mode test in Task 2 and the no-comment-body assertion in Task 3. Docs (spec "Ship gates" bullet)→Task 5. All five decisions + docs mapped.

**Placeholder scan:** every code step carries real code or a named, verifiable edit; test steps carry concrete assertions. The two soft spots are explicitly flagged for the implementer to resolve against the codebase (the exact personal-mode gate in Task 2; the OWM injectable-seam shape in Task 4) rather than left as silent TODOs.

**Type consistency:** `memory_feedback` is the event-type string across Tasks 1/3/5; `_METRIC_SCAN_MAX = 5000` is the one constant in Task 4; `metrics_truncated: bool` is defined (Task 4 Step 3) before it is asserted (Task 4 Step 1 expects the attribute — the failing test drives its creation). `_replay_emit`/`_bump_untagged_counter` signatures match `main.py:99` and `main.py:154`. `FeedbackRequest` fields match `models.py:267-269`.
