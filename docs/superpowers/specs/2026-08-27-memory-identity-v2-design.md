# Memory identity v2 — scoped point identity and the collision migration

**Status:** Design, pre-implementation. The live-store migration this spec
defines EXECUTES only on a separate, explicit user go — deploying the code is
not consent to migrate.
**Date:** 2026-08-27
**Provenance:** Root cause proven by the 2026-08-27 LongMemEval retest
(codex-root): isolated-Qdrant repro of cross-workspace overwrite; ingest
audits over 124,366 `/memory/learn` calls (96,084 text-only ids, 22,682 texts
duplicated across namespaces, 1,903 evidence texts colliding, 282/500
questions touched); two clean identical-input ingests differing on 3,677
point owners and 46/470 questions' evidence availability; a dataset-level
audit showing the v2 scheme yields 124,263 unambiguous points with 470/470
evidence retention. Artifacts: `.superpowers/longmemeval-retest-b58f9ee/`
(scoped-identity-audit.json, ingest-collision-audit.json,
ownership-gpu1-vs-gpu3-analysis.json, paired-old-vs-gpu1-bench.json).
Five-stream code scoping 2026-08-27 (this session) pinned every mint site,
join, and migration precedent cited below.

## Problem

A learned memory's identity is its text. `VectorClient.upsert()` mints the
Qdrant point id as `uuid5(FIREKEEP_UUID_NAMESPACE, text)`
(`cortex/app/db/vector.py:720`), and the graph chain nodes MERGE on a
sha256 content hash of the same bare text (`db/graph.py`), with
workspace_id/member_id as plain last-writer-wins SET properties. Identical
text learned in workspace B therefore lands on workspace A's point and node:
A's memory leaves A's recall (visibility 1→0), B gains it (0→1), and
`_merge_lifecycle` (vector.py:178-217) preserves A's `agent_id`/`project`/
`created_at` under B's ownership — provenance bleed. This is a live
cross-workspace integrity defect, independent of any benchmark; the
benchmark merely measured it (the fixture's nondeterminism and its 80
evidence-less questions are both this defect under concurrency).

Two adjacent defects share the class and are IN scope because they are the
same missing tenancy boundary on the same write path:

- **Cross-workspace auto-supersession.** `contradiction.detect_and_supersede`
  runs on every `/memory/learn` via `find_similar`, whose filter
  (vector.py:276-326) has NO workspace_id condition — a write in one
  workspace can supersede a near-duplicate memory in another.
- **Unattributed imports.** `transfer.py`'s import (transfer.py:159) never
  reads the principal: imported memories carry `workspace_id=None` and the
  same colliding text-only id.

## Decisions

**D1. Identity = uuid5 over an unambiguous canonical encoding of
[kind/version, verified workspace_id, canonical namespace, exact text].**
Concretely:

```python
seed = json.dumps(["mem2", workspace_id, namespace, text],
                  separators=(",", ":"), ensure_ascii=False)
point_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed))
```

JSON-array encoding, not delimiter-joined strings: text may contain any
delimiter, and an ambiguous encoding is this bug wearing a new hat. The
`"mem2"` kind tag versions the scheme and keeps the seed space disjoint from
corpus ids (`"corpus|…"` strings in the same uuid namespace) by construction.
Workspace-only scoping is insufficient — namespaces are deliberate write
categories, and an exact-text relearn into a different namespace must not
move the original's namespace/tags (the retest audit's finding). The
namespace input is the CANONICAL namespace (`normalize_namespace`), matching
what the payload stores.

**D2. One helper, four call sites, zero re-derivations.**
`memory_point_id(workspace_id, namespace, text)` lives in
`cortex/app/db/vector.py` beside the namespace constant. The v1 formula is
currently re-derived at three sites beyond the canonical one, each of which
must be rewired to call the helper (a missed one silently forks identity):
`main.py:1409-1411` (graph-handoff precompute), `main.py:1461-1467`
(vector-write-failure backfill enqueue), `workers/memory_agent.py:389` (the
merge pass, which today mints via a RAW QdrantClient bypassing VectorClient
— it imports only the namespace constant, so centralizing the algorithm does
NOT reach it without explicit rewiring). `VectorClient.upsert()` itself
computes the id via the helper; a guard test asserts `uuid5` over the bare
text appears nowhere outside the helper and the migration tooling.

**D3. Fail closed on scope.** `upsert()` (and the helper) REFUSE a memory
write with no verified workspace_id — raising, not defaulting: an unscoped
write is how v1's bleed happened, and every legitimate caller has a
principal (`anonymous_principal()` resolves the deployment owner even with
auth off). `transfer.py`'s import gains the request principal and stamps
`workspace_id`/`member_id` like `/memory/learn` does. The dreams/corpus/
skills paths are untouched: dreams and corpus ids are already scoped
(profile ids fold workspace; corpus ids fold workspace+source+chunk), and
they write via the caller-id `upsert_point()` path, which stays as the
documented escape hatch.

**D4. The graph gets the same boundary.** Chain-node identity
(`_content_hash`) becomes a hash of the same canonical [kind, workspace,
namespace, text] encoding, so two workspaces' identical action text builds
two nodes; workspace_id/member_id remain properties but no longer flip
owners. `find_similar` gains a required workspace_id filter (closing the
cross-workspace supersession hole); `detect_and_supersede`'s callers pass
the verified principal's workspace. Graph recall's existing post-retrieval
scoping (`_scope_verdict` in engine/rag.py, admit-by-default for
unattributable rows) is unchanged in this PR and disclosed as the standing
residual it already is; Domain/Concept nodes stay globally text-keyed by
their documented design.

**D5. Compatibility window: v2 writes beside v1 points, duplication is the
honest transitional state.** Recall retrieves by embedding + payload
filters, never by id shape, so v1 points stay recallable after the code
deploys. A post-deploy write of text that exists as a v1 point creates a NEW
v2 point rather than colliding — transiently, both may surface in recall.
That duplication is bounded (relearns of pre-existing text between deploy
and migration), visible, and strictly safer than one more day of
cross-workspace overwrites. No read path changes.

**D6. Migration: shadow copy, alias flip, quarantine — never guess owners.**
The live store's collided points are single points holding the LAST writer's
payload; the overwritten workspaces' data was destroyed at write time and is
**unrecoverable** — this migration stops future losses and re-homes what
exists; it cannot resurrect what v1 already destroyed. Mechanism, borrowing
`workspace_migration.py`'s verify-then-marker pattern and `reembed.py`'s
scroll shape:

1. **Cold backup first** (the existing whole-volume tar; restore is the only
   full rollback, and rollback becomes lossy once v2 writes exist — so the
   runbook is roll-forward after the flip, restore+replay only for
   catastrophe).
2. **Dry run (read-only, mandatory):** scroll every point. A point is a
   v1 MEMORY point iff `point.id == uuid5(ns, payload.text)` — under v1
   semantics payload text never diverges from the minting text (colliding
   writes shared the text; the merge pass mints from merged_text), and
   corpus/dream/skill/v2 points all fail this predicate. Classify:
   migratable (verified workspace_id + namespace present) / **quarantine**
   (workspace_id None — imports, pre-workspace-era points) / non-memory
   (untouched). Report counts, predicted v2 ids, and any predicted v2-id
   collision (two v1 points mapping to one v2 id — same workspace, same
   namespace, same text — merged by `_merge_lifecycle` rules, counters
   maxed, earliest created_at kept). No writes.
3. **Shadow copy:** create `<collection>_v2`; for each migratable point,
   copy vector (retrieved with vectors — no re-embedding) + payload to the
   v2 id; quarantined points copy at their EXISTING id with
   `legacy_unscoped: true` stamped (still recallable, excluded from v2
   dedup semantics, listed in the report for manual adoption); non-memory
   points copy verbatim. Write the old→new id map (JSONL artifact + Redis
   hash `mem:idmap:v2`, TTL ≥ the replay/eval retention window).
4. **Graph remap, before the flip:** rewrite `MemoryRef.vector_id` and the
   `memory_ids` arrays on chain nodes through the map (ids absent from the
   map pass through unchanged — they are corpus/dream/skill or already-v2).
   Graph chain-node re-keying applies to NEW writes only; existing merged
   nodes are left as-is (splitting them requires the destroyed ownership
   facts — same never-guess rule).
5. **Verify, then flip:** full rescan of the shadow collection — zero
   points satisfying the v1 predicate outside quarantine, counts reconciled
   against the dry run — then swap via Qdrant collection alias
   (`update_collection_aliases`, atomic): `QDRANT_COLLECTION` becomes an
   alias to the v2 collection. qdrant-client 1.18.0 supports aliases;
   nothing in cortex uses them today; search/scroll/upsert through an alias
   are transparent. The old collection is retained until the operator
   deletes it (that deletion is a second explicit act).
6. **Completion marker** in Redis (the `workspace_migration` precedent), so
   the migration is idempotent and a re-run resumes/verifies rather than
   repeats.

**D7. Stranded-join mitigation.** Historical replay events
(`memory_read`/`memory_feedback` payloads) and the access-count/
last-recalled Redis hashes name OLD ids and are immutable or unowned by any
migration precedent. OWM's nightly join (owm.py — keys stats by event
memory_ids, then `set_payload` onto those ids) consults the `mem:idmap:v2`
map as a fallback when an event id misses, for as long as the map lives;
the access-count and last-recalled hash keys are renamed through the map
once, during step 4. After the map's TTL, pre-migration events age out of
their own retention anyway. The client kit's proactive-recall seen-cache
(12h TTL) self-heals and needs nothing.

**D8. Benchmark closure — the original goal.** After deploy+migration, one
clean full ingest of the fixture must show ~124,263 points, 470/470
questions with owned evidence, and a repeat ingest must produce an
IDENTICAL ownership map (the determinism the retest proved impossible under
v1). Only then is a ranker A/B meaningful; the published 0.819 metric is
retired/dated on the site per the retest's resolution (site copy is a
separate act). The bench harness itself needs no change — the schedule
sensitivity was never the harness's fault.

**D9. Disclosed follow-ups, out of scope:** skills' `SKILL_NS` ids don't
fold workspace (same defect shape, different surface — document-sourced
skills colliding across workspaces); graph Domain/Concept global text
keying (documented residual); engine/rag.py's admit-by-default for
unattributable graph rows; `_merge_lifecycle`'s field-level semantics
(unchanged by this spec).

**D10. Rollout order.** (1) Ship v2 code (helper, four sites, fail-closed
scope, graph key, contradiction scoping, transfer principal) behind no flag
— it changes only NEW writes and is strictly safer; (2) deploy; (3) the
migration (D6) runs as a separate, explicitly-approved operation with its
own dry-run report reviewed first; (4) the benchmark rerun (D8).

## Testing

- Helper: canonical-encoding vectors (text containing `|`, `"`, newlines,
  json metacharacters; workspace/namespace permutations produce distinct
  ids; identical inputs produce identical ids across processes).
- Guard: no `uuid5(...text)` derivation outside the helper + migration
  (grep-shaped test, the D2 lockstep risk).
- Fail-closed: upsert without workspace raises; transfer import stamps the
  principal; contradiction cannot see across workspaces (two-workspace
  fixture: near-duplicate text does NOT supersede).
- Graph: same text, two workspaces → two chain nodes; memory_ids no longer
  cross-accrete.
- Collision end-to-end: the retest's repro shape as a regression test —
  same text learned in workspaces A and B; both recalls see their own copy;
  neither's provenance moves.
- Migration (against a seeded local Qdrant): v1-predicate classification
  (memory vs corpus vs dream vs skill points); quarantine path; predicted
  v2-collision merge; idempotent resume; verify-pass failure on a planted
  stray; alias flip leaves search results intact; old→new map completeness.
- OWM fallback join through the map; access-count key rename.

## Deploy

Cortex containers only (standard pull + rebuild). The migration tooling
ships as a script/worker but DOES NOT run at deploy. Office/K8s inherit on
their next update. Migration execution: separate user go, preceded by the
dry-run report and a fresh cold backup, per D6/D10.
