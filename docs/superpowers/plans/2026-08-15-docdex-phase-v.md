# Docdex Phase V Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The five corpus changes from `docs/superpowers/specs/2026-08-15-docdex-design.md` §4 — typed document sources with bounded metadata, source-scoped point identity, server-stamped ownership with principal-aware authorization and dex-reserved prefixes, one shared visibility-filter builder applied at member-principal egress, and the committed-generation gate.

**Architecture:** All server-side (cortex + corpus module). The visibility builder is a new pure module consumed by `VectorClient` queries; corpus point identity moves to a deterministic source-scoped UUID passed through a new optional `point_id` parameter on `upsert`; ownership/authz live in `corpus/api.py` + `corpus/store.py`; the committed-generation gate is a `committed` payload flag flipped by one `set_payload` call at swap completion and excluded from recall via `must_not`.

**Tech Stack:** FastAPI, Pydantic, Qdrant (`qdrant_client.models`), Redis, pytest with the existing corpus/cortex fakes.

## Global Constraints

- **Foreign uncommitted work occupies `cortex/app/db/vector.py` (one hunk @ ~line 395) and `cortex/app/main.py` (hunks @ ~1171–1211).** Edit ONLY outside those regions. NEVER `git add` either file directly — Task 8's surgical commit (reset→commit→remerge, the README/`ce3ba7b` precedent) is the only way these two files reach the index. All other Phase V files are clean and commit normally.
- Back-compat is a test, not an intention: **the existing corpus + cortex suites must pass with ZERO edits** except where a task explicitly adds tests. Memory (non-corpus) point identity is untouched.
- Absent `visibility` == `"workspace"` everywhere; a caller with no member identity receives NO private chunks (fail closed).
- Threat boundary (spec §1): dashboard and `/memory/export` are OPERATOR surfaces — the builder is NOT applied there; Task 7 documents this in the spec.
- Never store raw absolute paths or command-style secrets in payloads; reserved metadata keys are server-controlled.
- Suites: `cd cortex && python -m pytest tests/ -q` and `python -m pytest tests/ -q` (repo root), plus `docker compose -f docker-compose.test.yml up -d` + `python -m pytest corpus/tests/ -v` for the corpus module (see root `CLAUDE.md` "Local Testing").

---

### Task 1: The visibility-filter builder (pure module)

**Files:**
- Create: `cortex/app/db/visibility.py`
- Test: `cortex/tests/test_visibility_builder.py`

**Interfaces:**
- Produces: `visibility_should(member_id: str | None) -> list` — Qdrant conditions implementing: payload `visibility` absent OR `"workspace"` OR (`"member"` AND `member_id == <caller>`). With `member_id=None` the member branch is OMITTED (fail closed).
- Produces: `GENERATION_GUARD: FieldCondition` — `must_not` condition excluding `committed == False` (Task 6 wires it).

- [ ] **Step 1: Write the failing test**

```python
# cortex/tests/test_visibility_builder.py
"""The one builder every member-principal egress consumes (spec §4.4).

Pure condition construction — no I/O. The shape tests pin the exact
Qdrant condition tree so an egress caller can trust 'apply the builder'
means the same thing everywhere.
"""
from qdrant_client.models import FieldCondition, IsEmptyCondition, MatchValue

from app.db.visibility import GENERATION_GUARD, visibility_should


def _match(cond):
    return (cond.key, cond.match.value)


def test_member_caller_gets_three_branches():
    conds = visibility_should("mem-alice")
    # absent visibility (legacy points), workspace, own-member
    assert any(isinstance(c, IsEmptyCondition) and c.is_empty.key == "visibility"
               for c in conds)
    flat = [_match(c) for c in conds if isinstance(c, FieldCondition)]
    assert ("visibility", "workspace") in flat
    # the member branch is a nested Filter: visibility==member AND member_id==caller
    nested = [c for c in conds if hasattr(c, "must")]
    assert len(nested) == 1
    pair = sorted(_match(c) for c in nested[0].must)
    assert pair == [("member_id", "mem-alice"), ("visibility", "member")]


def test_no_member_identity_fails_closed():
    conds = visibility_should(None)
    assert not [c for c in conds if hasattr(c, "must")], (
        "no member identity must mean NO private-chunk branch")
    conds_empty = visibility_should("")
    assert not [c for c in conds_empty if hasattr(c, "must")]


def test_generation_guard_excludes_uncommitted():
    assert GENERATION_GUARD.key == "committed"
    assert GENERATION_GUARD.match.value is False
```

- [ ] **Step 2: Run it — expect FAIL** (`cd cortex && python -m pytest tests/test_visibility_builder.py -q`; ModuleNotFoundError)

- [ ] **Step 3: Implement**

```python
# cortex/app/db/visibility.py
"""The shared visibility filter (Docdex spec §4.4).

ONE builder, consumed by every member-principal egress (VectorClient
queries, corpus source listing). A new egress path that skips this module
is the bug class it exists to prevent. Dashboard and /memory/export are
OPERATOR surfaces by the spec's threat boundary and deliberately do not
consume it.
"""
from __future__ import annotations

from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsEmptyCondition,
    MatchValue,
    PayloadField,
)

# Task 6 wires this as a must_not on recall: chunks written but never
# committed (mid-ingest failure) are invisible until the next successful
# ingest sweeps them. Absent field passes — every pre-Phase-V point.
GENERATION_GUARD = FieldCondition(key="committed", match=MatchValue(value=False))


def visibility_should(member_id: str | None) -> list:
    """Conditions for a `should` group: legacy OR workspace OR own-private.

    member_id None/"" omits the private branch entirely — a caller with
    no member identity sees no private chunks (fail closed, spec I1).
    """
    conds: list = [
        IsEmptyCondition(is_empty=PayloadField(key="visibility")),
        FieldCondition(key="visibility", match=MatchValue(value="workspace")),
    ]
    if member_id:
        conds.append(Filter(must=[
            FieldCondition(key="visibility", match=MatchValue(value="member")),
            FieldCondition(key="member_id", match=MatchValue(value=member_id)),
        ]))
    return conds
```

- [ ] **Step 4: Run it — expect PASS**
- [ ] **Step 5: Commit** (`git add cortex/app/db/visibility.py cortex/tests/test_visibility_builder.py && git commit -m "feat(corpus): the shared visibility-filter builder (Docdex Phase V.4)"`)

---

### Task 2: Source-scoped corpus point identity

**Files:**
- Modify: `cortex/app/db/vector.py` — `upsert()` only (~line 679; foreign hunk is @395 — stay clear)
- Modify: `corpus/store.py` — `store_chunks()`
- Test: `corpus/tests/test_point_identity.py`

**Interfaces:**
- Consumes: `VectorClient.upsert(text, metadata, namespace)` (existing).
- Produces: `upsert(..., point_id: str | None = None)` — caller-supplied ID wins; None keeps `uuid5(NAMESPACE, text)` byte-identical (memory dedup is a feature).
- Produces: `corpus_point_id(workspace_id, source_name, ingest_id, chunk_index) -> str` in `corpus/store.py` — `uuid5(FIREKEEP_UUID_NAMESPACE, f"{workspace_id}|{source_name}|{ingest_id}|{chunk_index}")`.

- [ ] **Step 1: Write the failing test**

```python
# corpus/tests/test_point_identity.py
"""Spec §4.2 — the worst reviewed bug. uuid5(text) collapsed identical
text ACROSS MEMBERS into one point; deleting Alice's source deleted
Bob's chunk. Corpus points are now source-scoped."""
import pytest

from corpus.store import corpus_point_id, store_chunks
from corpus.models import Chunk, ChunkMetadata


def _chunk(text="same words", name="docdex:aa:bb"):
    return Chunk(content=text, metadata=ChunkMetadata(
        source_name=name, source_type="document",
        chunk_index=0, total_chunks=1))


def test_identical_text_two_sources_two_points():
    a = corpus_point_id("ws1", "docdex:alice:f1", "run1", 0)
    b = corpus_point_id("ws1", "docdex:bob:f1", "run1", 0)
    assert a != b


def test_same_source_same_run_is_deterministic():
    assert corpus_point_id("ws1", "s", "r", 3) == corpus_point_id("ws1", "s", "r", 3)


@pytest.mark.asyncio
async def test_store_chunks_passes_scoped_point_id(fake_vector):
    # fake_vector: the module's existing recording fake (see corpus/tests
    # conftest); it records upsert kwargs.
    await store_chunks([_chunk()], fake_vector, ingest_id="r1",
                       workspace_id="ws1", member_id="m1")
    (call,) = fake_vector.upserts
    assert call["point_id"] == corpus_point_id("ws1", "docdex:aa:bb", "r1", 0)
```

(If the existing corpus fake lacks kwargs recording, extend the FAKE in
the test file — never the production signature to fit the fake.)

- [ ] **Step 2: Run — expect FAIL** (`corpus_point_id` undefined)
- [ ] **Step 3: Implement.** vector.py (inside `upsert`, replacing the one line):

```python
            point_id = point_id or str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, text))
```

with the signature gaining `point_id: str | None = None` and one docstring
line: "point_id: caller-scoped identity (corpus uses source-scoped IDs so
identical text never collapses across sources/members — Docdex §4.2);
None keeps text-derived dedup for memories." corpus/store.py:

```python
def corpus_point_id(workspace_id: str, source_name: str,
                    ingest_id: str, chunk_index: int) -> str:
    """Source-scoped identity: identical text across sources/members must
    NEVER share a point (uuid5(text) did; deleting one member's source
    then deleted the other's chunk — spec §4.2)."""
    from cortex.app.db.vector import FIREKEEP_UUID_NAMESPACE  # match import style used in this repo; if corpus cannot import cortex, redeclare the UUID constant locally with a comment pinning equality and add an equality test
    import uuid
    raw = f"{workspace_id}|{source_name}|{ingest_id}|{chunk_index}"
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, raw))
```

and in `store_chunks`, pass `point_id=corpus_point_id(workspace_id or "", chunk.metadata.source_name, ingest_id or "", chunk.metadata.chunk_index)` to `vector_client.upsert`. **Check how corpus already imports from cortex** (it may not — corpus is a shared lib): if it cannot, declare `FIREKEEP_UUID_NAMESPACE` locally in `corpus/store.py` copied verbatim, plus a test asserting the two constants are equal when cortex is importable.
- [ ] **Step 4: Run task test + FULL corpus suite — both green, corpus suite with zero edits to existing tests**
- [ ] **Step 5: Commit `corpus/store.py` + the test normally. vector.py is NOT committed here — Task 8.**

---

### Task 3: Ingest — document type, visibility, bounded metadata

**Files:**
- Modify: `corpus/api.py` (IngestRequest + ingest handler), `corpus/pipeline.py` (thread-through), `corpus/store.py` (payload fields)
- Test: `corpus/tests/test_ingest_visibility.py`

**Interfaces:**
- Produces (wire): `IngestRequest` gains `visibility: Literal["workspace","member"] = "workspace"`, `metadata: dict[str, str] = {}`, and `source_type` pattern becomes `^(text|wiki|jira|api-doc|document)$`.
- Produces: chunk payloads carry top-level `visibility`; client `metadata` lands under the nested metadata dict.
- Reserved keys (server-controlled, request rejected 422 if present in `metadata`): `workspace_id`, `member_id`, `visibility`, `ingest_id`, `source_name`, `chunk_index`, `total_chunks`, `committed`. Bounds: ≤16 keys, ≤2048 chars JSON-serialized, str→str only.

- [ ] **Step 1: Failing tests** — `document` source_type accepted (422 today); `visibility="member"` lands on the chunk payload top level and in the Redis source record; reserved key `member_id` in metadata → 422 naming the key; 17 keys → 422; a non-str value → 422; absent visibility → payload identical to today (assert no `visibility` key OR `"workspace"` — pick ONE: **stamp `"workspace"` explicitly on new writes**, absence remains the legacy meaning the filter honors).
- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement.** Follow the existing principal threading (the handler already resolves the request principal for workspace/member — see `test_ingest_tenancy.py`); `visibility` and bounded `metadata` ride the same path into `store_chunks`, which adds `metadata["visibility"] = visibility` beside the workspace/member stamps (upsert promotes known keys — confirm `visibility` promotes to top level; if promotion is allow-listed in vector.py's upsert, add `visibility` to that allow-list as part of the SAME disjoint upsert region as Task 2).
- [ ] **Step 4: Task tests + full corpus suite green**
- [ ] **Step 5: Commit (api/pipeline/store + tests; vector.py again deferred to Task 8 if touched)**

---

### Task 4: Source ownership, principal-aware authz, dex-reserved prefixes

**Files:**
- Modify: `corpus/api.py` (sources + delete handlers), `corpus/store.py` (`track_source`, `list_sources`, delete helpers)
- Test: `corpus/tests/test_source_authz.py`

**Interfaces:**
- Produces: source records gain `workspace_id`, `member_id`, `visibility`, `dex` (from the `source_name` prefix before the first `:` when it matches a known dex id, else `""`).
- Produces: `GET /corpus/sources` returns only the caller's workspace, and private sources ONLY to their owner or an admin-scoped key (a private source's NAME is private data — spec I1).
- Produces: `DELETE /corpus/sources/{source_name}`: caller must be in the source's workspace; `visibility=member` additionally owner-or-admin, else 403.
- Produces: writes AND deletes to `docdex:`-prefixed source names require the credential scope `dex:docdex` or `admin`, else 403 — generic corpus credentials cannot claim or mutate reserved sources. Implement as a small `_require_dex_scope(source_name, principal)` helper in `corpus/api.py` with the prefix table `{"docdex:": "dex:docdex"}`.

- [ ] **Step 1: Failing tests** — Bob (member key, same workspace) cannot list, delete, or overwrite (re-ingest) Alice's private source (403/absent); admin can; a generic `memory:write` corpus credential POSTing `source_name="docdex:x:y"` → 403; a `dex:docdex`-scoped key succeeds; cross-workspace delete → 404 (not 403 — do not confirm existence). Reuse the auth fixture pattern from `cortex/tests/test_autopilot_api.py` (`auth_keys` — real keys, enforcement on).
- [ ] **Step 2: Run — expect FAIL** (delete currently has NO principal — spec §9 finding 1)
- [ ] **Step 3: Implement** (delete/list handlers gain `Request` + `request_principal`; scope check via `auth.middleware.require_scope`'s underlying helpers the routers already use)
- [ ] **Step 4: Task tests + corpus + cortex suites green**
- [ ] **Step 5: Commit**

---

### Task 5: The committed-generation gate

**Files:**
- Modify: `corpus/store.py` (`store_chunks` writes `committed: False`; new `commit_generation()`), `corpus/pipeline.py` (call it at swap completion, before deleting the old generation)
- Test: `corpus/tests/test_committed_generation.py`

**Interfaces:**
- Produces: `async def commit_generation(vector_client, source_name: str, ingest_id: str) -> None` — one Qdrant `set_payload {"committed": True}` by `(source_name, ingest_id)` filter.
- Ordering in `ingest_document`: upsert all (committed=False) → `commit_generation` → delete previous generation (existing `exclude_ingest_id` path) → `track_source`.
- Recall's exclusion of `committed=False` is wired in Task 6 via `GENERATION_GUARD`.

- [ ] **Step 1: Failing tests** — chunks are written `committed=False`; after `ingest_document` completes they are `True`; a pipeline failure injected between store and commit leaves them `False` AND the previous generation's chunks untouched; the next successful ingest of the same source sweeps the orphaned `False` generation (existing sweep — assert it).
- [ ] **Step 2–4: Red, implement, green (full corpus suite).**
- [ ] **Step 5: Commit** — message states this is spec §4.5 option (a), and that the spec's fallback (b) is now dead.

---

### Task 6: Egress — wire the builder into search paths

**Files:**
- Modify: `cortex/app/db/vector.py` — `search()` (~line 816) and `list_memories()`: new param `member_id: str | None = None`; inside the filter build, wrap existing musts with `should=visibility_should(member_id)` + `must_not=[GENERATION_GUARD]` (Qdrant Filter supports must+should+must_not together; `should` with ≥1 matching branch is required when present)
- Modify: `cortex/app/main.py` — the recall handler (~1264) and SSE recall handler (~1385): pass `member_id=principal["member_id"]` into search. **Foreign hunks live at ~1171–1211; these call sites are outside them.**
- Modify: `cortex/app/mcp_server.py` — verify `corpus_sources` proxies with the CALLER's identity headers (the `_resolve_identity` order); if the shared client sends a service key, thread the caller session/member header per the tool's existing pattern for member-aware calls.
- Test: `cortex/tests/test_corpus_visibility_egress.py`

**Interfaces:**
- Consumes: `visibility_should`, `GENERATION_GUARD` (Task 1).
- Produces: `search(..., member_id=None)`, `list_memories(..., member_id=None)` — None = today's behavior for non-corpus callers plus fail-closed privacy.

- [ ] **Step 1: Failing tests** — the acceptance pair: with a scoring fake (the FakeRedis/FakeQdrant pattern — the fake MUST honor filters; a fake that ignores them proves nothing, the `search`-skill lesson), Alice's private chunk is top-hit for Alice and ABSENT for Bob on BOTH `search` paths; absent-visibility legacy points return for both; `committed=False` returns for neither; `member_id=None` returns no private chunks.
- [ ] **Step 2–4: Red, implement, green — then the FULL cortex suite with zero edits to pre-existing tests** (the back-compat assertion of Global Constraints).
- [ ] **Step 5: Commit ONLY the new test + mcp_server.py if touched. vector.py and main.py wait for Task 8.**

---

### Task 7: Bulk delete + spec amendment

**Files:**
- Modify: `corpus/api.py` (route), `corpus/store.py` (delete-by-dex-source)
- Modify: `docs/superpowers/specs/2026-08-15-docdex-design.md` (§4.4: dashboard/export named operator surfaces; §4.5: option (a) shipped)
- Modify: `docs/guides/memory-and-recall.md` (corpus section: visibility flag, document type, authz, generation gate — the consistency checklist's guide row)
- Test: `corpus/tests/test_bulk_delete.py`

**Interfaces:**
- Produces: `DELETE /corpus/dex-sources/{source_id}` — parses NO client filter; deletes every source in the caller's workspace whose `source_name` starts `docdex:<source_id>:`, via the tracked-source records (Redis scan of the caller's workspace records, then per-source Qdrant delete by exact `source_name` — bounded by the records, no Qdrant prefix query needed). Same authz as Task 4 (`dex:docdex` or admin; owner for private).
- Returns `{deleted_sources: n, deleted_chunks: "unknown"|n}` — chunk counts only if the store returns them; never fabricate.

- [ ] **Steps: red (multi-file source removed in one call; Bob 403; cross-workspace 404), green, corpus suite, commit (docs included).**

---

### Task 8: Surgical commit of the two foreign-occupied files + full verification

**Files:** `cortex/app/db/vector.py`, `cortex/app/main.py` (commit only), everything (verification)

- [ ] **Step 1:** Back up both working files to the session scratchpad (`cp`), then for each: `git show HEAD:<file> > <file>` restores base; re-apply ONLY the Phase V edits (from the backup, by region — upsert `point_id` + search/list `member_id`+filter in vector.py; the two call-site params in main.py). The foreign hunks (@395 / @1171–1211) must NOT be present: `git diff <file> | grep "^@@"` shows only Phase V regions.
- [ ] **Step 2:** `git add` both files, run BOTH full suites (`cd cortex && python -m pytest tests/ -q` with output redirect + `echo "exit: $?"`; repo root `python -m pytest tests/ -q`) — green, honest exit capture (never `| tail` alone).
- [ ] **Step 3:** Commit: `feat(corpus): Docdex Phase V — visibility, identity, authz, generation gate (spec §4)`. Push.
- [ ] **Step 4:** Restore the foreign work: three-way merge each backup over the new HEAD (`git merge-file <working-copy-from-backup> <base-at-old-HEAD> <new-HEAD-version>` — LF-normalize first, the `tr -d '\r'` lesson), leaving the foreign diffs uncommitted in the working tree, byte-equivalent to before (verify with `git diff --stat`).
- [ ] **Step 5:** Deploy to the VPS **through the deploy runbook skill** (its steps hold the host details; do not inline the hostname here — `test_forbidden_tokens` bans it from the tree): confirm CI green on the commit, run `update.sh` over SSH per the runbook, poll `/version` + `/health`. This is dogfood observation material for the deploy runbook.

## Self-review notes

- Spec coverage: §4.1→Task 3, §4.2→Task 2, §4.3→Task 4, §4.4→Tasks 1+6 (+7 for the operator-surface amendment), §4.5→Task 5, bulk delete→Task 7, acceptance tests distributed per task with the two-member pair in Task 6.
- Deliberately NOT here (client-side, Phase D1): everything under spec §2; the reviewer's client acceptance tests (unmounted-folder, remove-vs-sync race, tombstone retry) belong to D1's plan.
- Type consistency: `visibility_should`/`GENERATION_GUARD` (T1) consumed by T6; `corpus_point_id` (T2) internal to store; `point_id=` kwarg name used in T2 test and vector.py signature.
