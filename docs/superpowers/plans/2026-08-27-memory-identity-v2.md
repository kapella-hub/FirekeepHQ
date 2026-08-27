# Memory Identity v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship scoped memory-point identity (workspace+namespace+text), the tenancy fail-closed guards, and the inert freeze-migration tool — everything in spec D10 step 1. Migration EXECUTION is not in this plan.

**Architecture:** One `memory_point_id` helper in `cortex/app/db/vector.py`; every mint site rewired through it; a flag-gated v1 lifecycle bridge covers the compat window; graph chain keys and contradiction detection gain the workspace boundary; startup code that would defeat quarantine is gated; the migration tool ships as an admin-invoked module with a Redis state machine, dry-run mode, and an exact verify pass — inert until run.

**Tech Stack:** Python 3.11, FastAPI, Qdrant (qdrant-client 1.18.0; tests use `QdrantClient(":memory:")` local mode where the existing suite does, else mocks per file convention), Neo4j (mocked per existing graph tests), fakeredis, pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-memory-identity-v2-design.md` (revision 1bafa99 — read it first; decisions cited D1–D10; its Revision record explains why the migration is freeze-based and alias-free).

## Global Constraints

- **The v2 seed is EXACTLY** `json.dumps(["mem2", workspace_id, namespace, text], separators=(",", ":"), ensure_ascii=False)` fed to `uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed)` (spec D1). Never a delimiter-joined string.
- **Identity inputs are immutable:** no code introduced by this plan may `set_payload` on `namespace`, `workspace_id`, or `text` without re-keying the point.
- **Fail-closed applies ONLY to `upsert()`'s `point_id is None` branch** (spec D3). `upsert(point_id=..., metadata without workspace_id)` must keep working — corpus depends on it (corpus/store.py:175-184).
- **Never guess owners:** no code path may stamp a workspace onto a point that lacks one, EXCEPT the two named in the spec — backfill `_drain`'s legacy-entry stamping (deployment owner) and the operator's explicit quarantine adoption (not built here).
- **`_merge_lifecycle` (vector.py:178-217) is not modified** — only its reachability changes (the D5 bridge and the migration twin-merge CALL it).
- **The migration tool never runs at import, startup, or deploy.** It executes only via its own explicit entry point, refuses without `MIGRATION_FREEZE=true`, and its write steps refuse without a completed prior step in the state machine.
- **Everything is additive on the read path:** recall filters, RAG scoring, and the compliance/optimism surfaces must be byte-identical for existing stores (existing test suites are the freeze guards).
- No changes under `client/`, `bridge/`, `benchmarks/` (the harness needs none — spec D8).
- Run cortex tests from `cortex/`: `python -m pytest tests/ -v`. The whole suite must be green before the branch finishes.

---

### Task 1: The helper, normalization, and the fail-closed minting branch

**Files:**
- Modify: `cortex/app/db/vector.py` (helper beside `FIREKEEP_UUID_NAMESPACE` at :44; `upsert()` at :694-817)
- Test: `cortex/tests/test_memory_point_id.py` (new)

**Interfaces:**
- Produces: `memory_point_id(workspace_id: str, namespace: str, text: str) -> str` — raises `ValueError` on falsy workspace_id; normalizes namespace itself (idempotent). Every later task imports exactly this.
- `upsert()` behavior change, minting branch only: normalizes `namespace` ONCE at the top (`from app.models import normalize_namespace` — note the new import direction, `db.vector → models`, which is cycle-free: models imports nothing from db) and uses the normalized value for BOTH payload and seed; when `point_id is None`, reads `metadata.get("workspace_id")` and mints via the helper, raising `VectorStoreError("memory write refused: no verified workspace_id ...")` when absent; when `point_id` is passed, behavior is byte-identical to today.

- [ ] **Step 1: Write the failing tests** — encoding vectors:

```python
import json, uuid
import pytest
from app.db.vector import FIREKEEP_UUID_NAMESPACE, memory_point_id

def _expected(ws, ns, text):
    seed = json.dumps(["mem2", ws, ns, text], separators=(",", ":"), ensure_ascii=False)
    return str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed))

def test_seed_is_the_registered_encoding():
    assert memory_point_id("ws1", "default", "hello") == _expected("ws1", "default", "hello")

@pytest.mark.parametrize("text", [
    'a|b|c', 'a","b', 'line\nline', '["mem2","ws1","default","x"]',
    'unicode — em dash → arrow', '{"json": true}',
])
def test_hostile_texts_cannot_forge_identity(text):
    a = memory_point_id("ws1", "default", text)
    b = memory_point_id("ws2", "default", text)
    c = memory_point_id("ws1", "other", text)
    assert len({a, b, c}) == 3            # scope always separates
    assert a == memory_point_id("ws1", "default", text)  # deterministic

def test_namespace_is_normalized_before_seeding():
    from app.models import normalize_namespace
    raw = "My Namespace"
    assert memory_point_id("ws1", raw, "t") == memory_point_id("ws1", normalize_namespace(raw), "t")

def test_falsy_workspace_refuses():
    for bad in (None, ""):
        with pytest.raises(ValueError):
            memory_point_id(bad, "default", "t")
```

Plus the upsert-branch tests (use the file conventions of `cortex/tests/test_lifecycle_upsert.py` for the VectorClient fixture — read it first): minting-branch refusal (no point_id, metadata without workspace_id → `VectorStoreError`); explicit-point_id exemption (corpus shape: `point_id=` given, no workspace_id → succeeds, payload as today); payload namespace equals seeded namespace for a raw un-normalized input.

- [ ] **Step 2: Run to verify failure** — `cd cortex; python -m pytest tests/test_memory_point_id.py -v` → ImportError.

- [ ] **Step 3: Implement** the helper (docstring citing spec D1 + the immutability invariant), the normalize-at-top, and the branch-scoped fail-closed check. Do NOT touch `_merge_lifecycle` or any read path.

- [ ] **Step 4: Run** the new file plus `tests/test_lifecycle_upsert.py` and the corpus suite (`python -m pytest ../corpus/tests -q` from repo root if that is where they live — verify path) → PASS.

- [ ] **Step 5: Commit** `feat(cortex): memory_point_id helper — scoped identity, fail-closed minting (identity-v2 D1/D3)`

---

### Task 2: Rewire every mint site; backfill legacy stamping; the guard test

**Files:**
- Modify: `cortex/app/main.py:1409-1411` and `:1461-1467` (the two re-derivations)
- Modify: `cortex/app/workers/memory_agent.py:389` (merge-pass mint) — it imports `FIREKEEP_UUID_NAMESPACE` and `_merge_lifecycle` at :31
- Modify: `cortex/app/workers/backfill.py` (`_drain`, ~:110-118)
- Test: `cortex/tests/test_identity_guard.py` (new), extend the existing memory_agent + backfill test files

**Interfaces:**
- Consumes: `memory_point_id` (Task 1).
- The `/memory/learn` route computes `memory_id = memory_point_id(principal_workspace, log.namespace, text)` ONCE and passes it BOTH to the graph write AND as `point_id=` into `vector.upsert(...)` — ending the parallel-computation coincidence the spec flags (the two values can no longer diverge). The fallback enqueue site: FIRST verify whether it is reachable (the review suspects one of the four sites is dead code) — if dead, delete it and note in the report; if live, rewire identically.
- memory_agent merge pass mints via the helper using the KEEPER's workspace_id and namespace from its payload (the merged point inherits the keeper's scope — both cluster members already share workspace by the dedup pass's own grouping; assert that and skip the cluster with a warning if they differ, never merge across workspaces).
- backfill `_drain`: entry payload lacking `workspace_id` → stamp the deployment owner principal (`auth`'s deployment workspace resolver — find it via `anonymous_principal` in `auth/principal.py:30-40`) before upsert, with a log line; never let it grind retries.

- [ ] **Step 1: Write the failing guard test**

```python
# cortex/tests/test_identity_guard.py
"""No bare-text uuid5 derivation may exist outside the helper + migration
tooling (identity-v2 D2). A re-derivation is a silent identity fork."""
import re
from pathlib import Path

ALLOWED = {"app/db/vector.py", "app/workers/memory_identity_migration.py"}

def test_no_stray_uuid5_over_memory_text():
    root = Path(__file__).resolve().parents[1] / "app"
    offenders = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root.parent).as_posix()
        if rel in ALLOWED:
            continue
        src = py.read_text(encoding="utf-8")
        if re.search(r"uuid5\(\s*FIREKEEP_UUID_NAMESPACE", src):
            offenders.append(rel)
    assert not offenders, f"identity forked outside the helper: {offenders}"
```

(Corpus's separate constant lives outside `cortex/app` and is exempt by construction; dreams/skills use their own NS constants and don't match the pattern.)

- [ ] **Step 2: RED** — it fails today naming main.py and memory_agent.py.
- [ ] **Step 3: Rewire** the sites per the Interfaces block; extend the memory_agent tests (cross-workspace cluster → skipped with warning) and backfill tests (legacy entry stamped and written; modern entry untouched) in their files' own fixture styles.
- [ ] **Step 4: Run** `tests/test_identity_guard.py`, the memory_agent, backfill, and learn-route test files → PASS. RED/GREEN captured.
- [ ] **Step 5: Commit** `feat(cortex): all mint sites route through memory_point_id (identity-v2 D2)`

---

### Task 3: The D5 lifecycle bridge

**Files:**
- Modify: `cortex/app/config.py` (`MEMORY_ID_V1_BRIDGE: bool = True`)
- Modify: `cortex/app/db/vector.py` (`upsert()` lifecycle prefetch, ~:783-799)
- Test: `cortex/tests/test_lifecycle_upsert.py` (extend)

**Interfaces:** In `upsert()`'s minting branch, when the v2-id prefetch finds no existing point AND the bridge flag is on, additionally `retrieve([_v1_point_id(text)])` — a private `_v1_point_id(text) -> str` helper (the bare-text v1 formula) lives in vector.py, which is already on the guard test's ALLOWED list, so no exemption machinery is needed; the migration tool imports the same private helper for its classification predicate — and feed that payload as `existing` into `_merge_lifecycle`. v2 text/vector/namespace/workspace win; status, counters, archive provenance, created_at/agent_id/project survive per the function's existing rules.

- [ ] **Step 1: Failing test** — the resurrection regression: create a v1-shaped point (id = bare uuid5(text), status "archived", `archived_at`/`purge_eligible_at` set), then `upsert()` the same text under a workspace/namespace; assert the new v2 point has `status == "archived"` and carries the archive provenance; with the flag off, assert today's raw behavior (fresh active) so the flag's meaning is pinned both ways.
- [ ] **Step 2: RED.** — new point comes back active.
- [ ] **Step 3: Implement** (~10 lines + flag).
- [ ] **Step 4: GREEN** + full `test_lifecycle_upsert.py`.
- [ ] **Step 5: Commit** `feat(cortex): v1 lifecycle bridge for the compat window (identity-v2 D5)`

---

### Task 4: Transfer principal override

**Files:**
- Modify: `cortex/app/transfer.py` (:81 scope, :152-159 import loop)
- Test: `cortex/tests/test_transfer.py` (extend — find the existing import tests)

**Interfaces:** The import route resolves the request principal (same dependency the learn route uses) and OVERRIDES `metadata["workspace_id"]`/`metadata["member_id"]` with it after reading each item — assignment, never `setdefault`. Namespace passes through `upsert()`'s new normalize-at-top (Task 1) untouched here.

- [ ] Steps: failing test (import body asserting a foreign `workspace_id` lands under the principal's; a body with none gets the principal's) → RED → implement → GREEN + existing transfer tests → commit `fix(cortex): transfer import stamps the verified principal (identity-v2 D3)`.

---

### Task 5: The graph boundary

**Files:**
- Modify: `cortex/app/db/graph.py` (`_content_hash` call sites :356-357, :401 — the hash gains the canonical scope encoding for NEW writes; `CONTENT_HASH_LENGTH` unchanged)
- Modify: `cortex/app/db/vector.py` (`find_similar` + `_similarity_filter` :276-326 gain a REQUIRED workspace_id parameter/condition)
- Modify: `cortex/app/contradiction.py` (thread workspace through `detect_and_supersede`) and its caller `cortex/app/main.py:1502-1506`
- Modify: `cortex/app/engine/rag.py` (`_scope_verdict` DENIES rows whose node carries `legacy_unscoped: true`, under every `unattributed` setting)
- Test: extend the graph, contradiction, and rag test files per their conventions

**Interfaces:**
- Graph node id for new writes: `_content_hash(json.dumps(["mem2", workspace_id, namespace, node_text], separators=(",", ":"), ensure_ascii=False))` — same canonical encoding, per-node text (action/outcome/resolution separately, as today). workspace/namespace come from the ActionLog the route already holds.
- `find_similar(text, namespace, domain, threshold, top_k, workspace_id)` — workspace_id required, added to must-conditions; the ONLY caller is contradiction.

- [ ] **Steps:** failing tests first — (a) same text two workspaces → two Action nodes (mock-Cypher assertion style per existing graph tests: the MERGE key strings differ); (b) near-duplicate text in workspace B does NOT supersede A's memory (the retest's repro as a regression: two workspaces, find_similar filtered); (c) a graph row carrying `legacy_unscoped` is denied by `_scope_verdict` for `unattributed` in both `admit` and `deny`. RED → implement → GREEN + the full rag/graph/contradiction files → commit `feat(cortex): workspace boundary on graph identity and contradiction (identity-v2 D4)`.

---

### Task 6: Startup hardening and the freeze gate

**Files:**
- Modify: `cortex/app/db/vector.py` (`initialize()` :351-377 — alias-aware)
- Modify: `cortex/app/workspace_migration.py` + its call site `cortex/app/main.py:730-734` (gate on its own `MEMORY_MIGRATION_KEY` marker — which today is written and never read — and skip points with `legacy_unscoped: true` or the `__quarantine__` sentinel)
- Delete or hard-gate: `cortex/app/workers/migrate_namespaces.py` (remove from the celery include list in `workers/sleep_cycle.py:48`; keep the file only if gated behind an env flag defaulting off, with a comment naming this spec)
- Modify: `cortex/app/config.py` (`MIGRATION_FREEZE: bool = False`) + `cortex/app/main.py`: when true, `/memory/learn`, `/memory/stream`, transfer import, corpus/knowledge ingest routes, and the lifecycle/feedback mutating routes return 503 `{"detail": "memory store migration in progress; retry shortly"}` (a dependency/middleware — pick the pattern the codebase already uses for gates; read how PROCEDURE_ENABLED-style gates decorate routes first)
- Modify: `docker-compose.yml` (the two new env passthroughs — cortex services use env_file, so only defaults documentation) and the checklist files per CLAUDE.md
- Test: `cortex/tests/test_identity_startup.py` (new) + freeze-gate route tests

**Interfaces:** `initialize()` decides "collection exists" via `get_collection(name)` succeeding (alias-transparent), falling back to creation only on a not-found error. `migrate_single_workspace` writes its marker as today AND checks it first; a sentinel/legacy point is never stamped.

- [ ] **Steps:** failing tests — initialize() no-op when get_collection succeeds (mock); boots-twice test: store holding `workspace_id: "__quarantine__"` + `legacy_unscoped: true` point → after two `migrate_single_workspace` runs the workspace is unchanged; marker present → backfill skipped entirely; freeze on → 503 on every gated route, 200 on recall/read routes; freeze off → all normal. RED → implement → GREEN + `tests/test_workspace_backfill.py` updated expectations → commit `feat(cortex): alias-aware init, gated workspace backfill, MIGRATION_FREEZE (identity-v2 D6)`.

---

### Task 7: The migration tool

**Files:**
- Create: `cortex/app/workers/memory_identity_migration.py` (the module — pure functions + an async driver; NOT registered with celery beat; invoked via `python -m app.workers.memory_identity_migration --dry-run|--execute|--verify|--resume` inside the container)
- Create: `cortex/tests/test_identity_migration.py`
- Test fixture style: `QdrantClient(":memory:")` if any existing cortex test uses local mode; otherwise the repo's established Qdrant mock — read `cortex/tests/` first and match.

**Interfaces (the spec's D6 steps 2-3-5-6, code-shaped):**
- `classify(point) -> Bucket` — provenance-first per spec D6.2: corpus (source=="corpus" OR id==corpus-seeded), dream/profile/skill (their NS/`memory_type`), v2 (id == memory_point_id(payload)), v1-migratable (memory payload + truthy workspace_id; absent namespace key → "default"), repaired-text (memory payload, truthy workspace, id matches NEITHER v2 nor bare-v1 of current text — re-homed from payload), quarantine (memory payload, falsy workspace → copied at same id with `workspace_id: "__quarantine__"`, `legacy_unscoped: true`).
- `dry_run(client) -> Report` — bucket counts, predicted ids, occupancy list (predicted v2 id already present in source), pre-existing dangling `superseded_by`/`contested_with` baseline, per-bucket examples. Read-only, runnable anytime.
- `execute(client, redis)` — refuses unless `MIGRATION_FREEZE=true` in the environment AND source `points_count` equals the state machine's recorded fingerprint (or none recorded yet). State machine hash `mem:migration:v2:state` = `{run_id, source_collection, source_points_count_at_start, step, cursor, started_at}` — written before each step, confirmed after; resume continues from cursor; fingerprint mismatch refuses. Shadow `firekeep_memory_v2` created from `get_collection(source).config.params` verbatim + the three payload indexes (`tags`, `namespace`, `workspace_id`) + `indexing_threshold=0` during bulk (restored after). Copy per bucket; twin-merge on occupied v2 id via `_merge_lifecycle(v1_payload, v2_payload)` — v2 text/vector kept, order-independent; rewrite `superseded_by`/`contested_with` through the in-progress map (two-pass: build map first from the classification scan — it is deterministic — then copy with rewrites, so ordering can't miss). idmap: JSONL at `/data/mem-idmap-v2.jsonl` (a mounted volume path — verify what the container mounts; else the qdrant snapshot dir convention) + Redis hash `mem:idmap:v2` mirror.
- `graph_remap(neo4j, map)` and `fold_redis_hashes(redis, map)` — separate steps in the machine, executed post-flip per the runbook (the tool enforces order only via the state machine; the FLIP itself is the operator's env change between steps, recorded by `--mark-flipped`).
- `verify(client) -> VerifyReport` — exact counts per bucket vs dry run (fatal mismatch), fidelity sample (N=100: vector equality + payload equality excluding id and rewritten fields), config.params equality, payload-index presence, search parity (50 stored query vectors from the source run against both, same ids-after-mapping and scores within float tolerance), dangling-reference check vs baseline, zero v1-predicate memory points outside quarantine.

- [ ] **Step 1: Failing tests** — every behavior above gets a seeded-store test, including: legacy corpus chunk (id==uuid5(text), source=corpus) lands in corpus bucket untouched; repaired-text point re-homed; twin merge produces identical result regardless of copy order (run both orders); planted crash (kill after N points, resume from cursor, final state equals uncrashed run); fingerprint mismatch refusal; verify failures each demonstrated (planted count drift / vector mutation / dangling ref); quarantine sentinel invisible via a workspace-filtered search on BOTH a real search call and the sentinel value.
- [ ] **Step 2: RED** (module missing). **Step 3: Implement.** **Step 4: GREEN** + full cortex suite. **Step 5: Commit** `feat(cortex): memory identity migration tool — dry-run, freeze-gated execute, exact verify (identity-v2 D6)`.

---

### Task 8: OWM join safety

**Files:**
- Modify: `cortex/app/owm.py` (translation at join top; sweep-skip guard around :265-292)
- Test: `cortex/tests/test_owm.py` (extend)

**Interfaces:** At the top of the join, every event `memory_id` is mapped through `mem:idmap:v2` (HGET batch; miss → keep original). The stale-reset sweep is skipped entirely, with a WARNING log, when the migration completion marker exists but the idmap Redis hash is empty/absent (expired cache) — no-update, never wipe. Same guard covers the `skill_efficacy` parallel block.

- [ ] **Steps:** failing tests — (a) events naming v1 ids + populated idmap → scores written to v2 ids, sweep does not wipe them; (b) marker present + empty map → sweep skipped, warning logged, existing scores untouched; (c) no marker (pre-migration deploy) → behavior byte-identical to today. RED → implement → GREEN + full test_owm.py → commit `fix(cortex): OWM joins through the identity map; sweep never wipes on a missing map (identity-v2 D7)`.

---

### Task 9: Guides and runbook

**Files:**
- Modify: `docs/guides/memory-and-recall.md` (dated section: the v2 identity scheme, the invariant, the bridge, the quarantine sentinel, D9 follow-ups)
- Modify: `docs/guides/backup-and-restore.md` (the migration runbook: freeze → backup-at-freeze-start with stated RPO and `--exclude-models` → dry-run review → execute → flip (env change + recreate) → graph remap + hash folds → verify → unfreeze; rollback rules: forward-only after flip)
- CLAUDE.md untouched (guides carry it); compose/env documented per the Change Consistency Checklist in Task 6.

- [ ] **Steps:** write both dated additions verified against the shipped code; run the doc-guard subset (`cd cortex; python -m pytest tests/ -q -k "doc or guide"`); commit `docs(guides): memory identity v2 — scheme, invariants, migration runbook`.

---

## Post-plan (controller, not a task)

Full cortex suite + corpus/auth suites on the merged tree; final whole-branch review; finishing-a-development-branch. Deploy ships the code + inert tool. Then, as separate user-gated acts per spec D10: the dry-run report → review → the freeze-migration at an agreed window → the benchmark rerun (D8) → site metric retirement.
