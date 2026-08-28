# Memory identity v2 — scoped point identity and the collision migration

**Status:** Design, pre-implementation, REVISED same-day after adversarial
review (26-agent: claims-vs-code with live Qdrant probes, hostile migration
review, skeptic verification — 33 findings, every major absorbed; record at
bottom). The live-store migration EXECUTES only on a separate, explicit user
go — deploying the code is not consent to migrate.
**Date:** 2026-08-27
**Provenance:** Root cause proven by the 2026-08-27 LongMemEval retest
(codex-root): isolated-Qdrant repro of cross-workspace overwrite; ingest
audits over 124,366 `/memory/learn` calls (96,084 text-only ids, 22,682 texts
duplicated across namespaces, 1,903 evidence texts colliding, 282/500
questions touched); two clean identical-input ingests differing on 3,677
point owners and 46/470 questions' evidence availability; a dataset-level
audit showing the v2 scheme yields 124,263 unambiguous points with 470/470
evidence retention. Artifacts: `.superpowers/longmemeval-retest-b58f9ee/`.
Five-stream code scoping + the review's own probes pinned every mint site,
join, precedent, and Qdrant behavior cited below.

## Problem

A learned memory's identity is its text. `VectorClient.upsert()` mints the
Qdrant point id as `uuid5(FIREKEEP_UUID_NAMESPACE, text)`
(`cortex/app/db/vector.py:720`), and the graph chain nodes MERGE per-node on
a truncated sha256 of each node's own text (`_content_hash`,
`db/graph.py:188-191`, applied separately to action/outcome/resolution —
NOT the vector point's concatenated text), with workspace_id/member_id as
plain last-writer-wins SET properties. Identical text learned in workspace B
therefore lands on workspace A's point and node: A's memory leaves A's
recall (visibility 1→0), B gains it (0→1), and `_merge_lifecycle`
(vector.py:178-217) preserves A's `agent_id`/`project`/`created_at` under
B's ownership — provenance bleed. This is a live cross-workspace integrity
defect, independent of any benchmark; the benchmark merely measured it (the
fixture's nondeterminism and its 80 evidence-less questions are both this
defect under concurrency).

Adjacent defects in the same class, IN scope because they are the same
missing tenancy boundary on the same write path:

- **Cross-workspace auto-supersession.** `contradiction.detect_and_supersede`
  runs on every `/memory/learn` via `find_similar`, whose filter
  (vector.py:276-326) has NO workspace_id condition.
- **Unattributed OR caller-asserted imports.** `transfer.py`'s import
  (transfer.py:153-159) never reads the principal: a round-trip import
  carries `workspace_id=None`, and a hand-written body can assert ANY
  workspace_id — `upsert` promotes it verbatim (vector.py:743-748). Both
  directions violate the boundary.

## Decisions — identity (code)

**D1. Identity = uuid5 over an unambiguous canonical encoding of
[kind/version, verified workspace_id, canonical namespace, exact text].**

```python
seed = json.dumps(["mem2", workspace_id, namespace, text],
                  separators=(",", ":"), ensure_ascii=False)
point_id = str(uuid.uuid5(FIREKEEP_UUID_NAMESPACE, seed))
```

JSON-array encoding, not delimiter-joined strings — text may contain any
delimiter. The `"mem2"` tag versions the scheme and keeps the seed space
disjoint from corpus ids (`"corpus|…"`) by construction; the one residual
(a v1 memory whose literal text is shaped like a v2 seed) is covered by the
dry run's occupancy check (D6.2). **Invariant: namespace, workspace_id and
text are identity inputs and therefore immutable payload fields under v2 —
no code may `set_payload` any of them without re-keying the point.**
`upsert()` normalizes the namespace ONCE at its top (`normalize_namespace`,
imported from `app.models`) and uses the normalized value for BOTH the
payload and the seed, so the payload-derivability invariant holds by
construction even for callers that pass raw namespaces (transfer.py does).
The shipped one-time task `workers/migrate_namespaces.py` mutates payload
namespaces in place with no re-keying — it is retired (deleted or
hard-gated) in this PR.

**D2. One helper; the mint sites rewired; the lockstep risk closed.**
`memory_point_id(workspace_id, namespace, text)` lives in
`cortex/app/db/vector.py`. The v1 formula is currently derived at the
canonical site (vector.py:720) and re-derived at `main.py:1409-1411`
(graph-handoff precompute), `main.py:1461-1467` (vector-write-failure
backfill enqueue), and `workers/memory_agent.py:389` (the merge pass, which
mints via a raw QdrantClient and imports `FIREKEEP_UUID_NAMESPACE` and
`_merge_lifecycle` directly — centralizing the algorithm does NOT reach it
without explicit rewiring). Implementation verifies each site live (one may
prove dead code — if so it is deleted, not rewired). A guard test asserts
no `uuid5` derivation over memory text exists outside the helper and the
migration tooling.

**D3. Fail closed on scope — on the MINTING branch only.** The fail-closed
check (no verified workspace_id → raise) applies to `upsert()`'s
`point_id is None` branch — the memory-minting path. Callers that pass an
explicit `point_id` (corpus at corpus/store.py:175-184, which goes through
`upsert()`, NOT `upsert_point()`; only dreams use `upsert_point`) are
exempt: corpus ids are already scoped, and corpus must keep ingesting even
when its metadata lacks a workspace (a state its own docstring records).
`transfer.py`'s import gains the request principal and OVERRIDES
`workspace_id`/`member_id` from it after reading item metadata — never
`setdefault` — closing both the None case and the caller-asserted spoof.
`workers/backfill._drain`: a queued entry whose stored payload lacks
`workspace_id` (pre-deploy enqueues) is stamped with the deployment owner's
principal rather than ground through retries into the DLQ — it is a
pre-existing memory being retried, not a new unscoped write.

**D4. The graph gets the same boundary, stated precisely.** Chain-node
identity becomes `_content_hash` over the canonical [kind, workspace,
namespace, node-text] encoding, applied per node text (action / outcome /
resolution) as today; `CONTENT_HASH_LENGTH` truncation is retained and
disclosed (shortened hash, larger input space — collision odds remain
negligible at this store's scale). `find_similar` gains a required
workspace_id filter (closing cross-workspace supersession);
`detect_and_supersede` callers pass the verified principal's workspace.
Existing chain nodes are NOT re-keyed (splitting merged nodes requires the
destroyed ownership facts); instead the migration stamps
`legacy_unscoped: true` on every chain node whose id matches the v1 hash of
its own text, and `_scope_verdict` DENIES `legacy_unscoped` rows regardless
of the `unattributed` lever — the graph analogue of vector quarantine,
disclosed as permanent. Graph recall's post-retrieval scoping design and
Domain/Concept global text keying are unchanged (standing residuals).

**D5. Compatibility window, with the lifecycle bridge.** v2 code deploys
before the migration (it stops NEW cross-workspace overwrites immediately).
Recall retrieves by embedding + filters, so v1 points stay recallable. A
post-deploy relearn of pre-existing text creates a v2 twin beside the v1
point — and without mitigation that twin would resurrect archived/
superseded/deprecated memories as fresh ACTIVE points, defeating
`_merge_lifecycle`'s core invariant (vector.py:184-185) and dropping
archive provenance. So `upsert()` carries a transitional bridge: on a v2-id
lifecycle-prefetch miss, it ALSO retrieves the v1 id (`uuid5(NS, text)`)
and feeds that payload into `_merge_lifecycle` — status, counters, archive
provenance, created_at/agent_id/project all survive into the v2 twin. The
bridge is ~10 lines, flag-gated (`MEMORY_ID_V1_BRIDGE=true` default), and
retired after the migration. The remaining window cost is plain
duplication: bounded, visible, resolved by the migration's twin-merge
(D6.3). Regression test: archive a memory, re-learn its exact text, assert
it does not come back active.

## D6. The migration — freeze, copy, cut over, verify

Governing facts the design now builds on (all empirically verified in
review): Qdrant v1.13.2 refuses an alias whose name collides with an
existing collection and has no rename, so "the old name becomes an alias
while the old collection is retained" is impossible; `initialize()`
(vector.py:351-377) would try to re-create a collection at an alias name
and abort startup; a no-freeze scroll-copy of a live collection loses ~half
of concurrent writes (id-ordered cursor vs uniformly-distributed new ids)
and races every in-place mutator (gc, owm, contradiction supersession,
access-count flushes) with no constructible catch-up.

**Cut-over model: new canonical name, not an alias.** The v2 collection is
`firekeep_memory_v2` and BECOMES the configured name: the flip is
`QDRANT_COLLECTION=firekeep_memory_v2` in the deployment env plus container
recreate, inside the freeze. Optionally, after the operator later deletes
the old collection, an alias `firekeep_memory → firekeep_memory_v2` may be
created for out-of-band tools. Independent hardening in the same PR:
`initialize()` becomes alias-aware (resolve via `get_collection(name)`
success, not membership in `get_collections()`), with a test that startup
is a no-op against a name that resolves to an alias.

**The whole migration runs inside one maintenance freeze:**

1. **Freeze.** Stop celery beat and every worker (gc, memory_agent, owm,
   dreams, sleep_cycle, skills, collectors, backfill drain — they bypass
   the API via raw clients); gate the write API: `/memory/learn`,
   `/memory/stream`, transfer import, corpus/knowledge ingest, lifecycle
   and feedback mutators return 503 with a retry hint
   (`MIGRATION_FREEZE=true`). Read API and dashboard may stay up until
   step 5. **Cold backup at freeze start** (`deploy/backup.sh` — a
   disclosed stack outage of minutes; `--exclude-models` mandated). The
   freeze is what makes the backup a true restore point: the runbook states
   the RPO plainly — restoring discards everything after freeze start.
2. **Dry run (read-only, mandatory; also runnable pre-freeze for
   planning).** Scroll every point; classify by PROVENANCE, with the id
   predicate as a confirming signal, never the definition:
   - **corpus** (`source == "corpus"` or `corpus|`-seeded id — includes
     legacy pre-65606df chunks whose ids ARE bare uuid5(text)),
     **dream/profile/skill** (their id schemes / `memory_type`): copy
     verbatim, untouched.
   - **v2 points** (id already matches `memory_point_id` of their own
     payload): copy verbatim.
   - **v1 migratable** (memory-shaped payload, verified workspace_id;
     absent `namespace` key reads as `"default"`, matching
     `namespace_condition`'s legacy semantics): re-key to the v2 id.
     Includes the **repaired-text bucket** — memory points whose id
     matches NO current text (the ~19 mojibake repairs): re-homed FROM
     PAYLOAD like any migratable point, counted separately in the report.
   - **quarantine** (memory-shaped, workspace_id None): copied at their
     existing id with `workspace_id: "__quarantine__"` (a sentinel no
     principal holds) plus `legacy_unscoped: true` — uniformly invisible
     to BOTH recall legs (never "recallable but only via the graph"), and
     adopted only by an explicit admin act. `migrate_single_workspace`
     (main.py:730-734, currently unconditional every startup) is gated in
     this PR on its own completion marker AND skips
     `legacy_unscoped`/sentinel points — without this, the first restart
     silently adopts the whole bucket.
   - Report: counts per bucket, predicted v2 ids, **occupancy check**
     (predicted v2 id already present in the source — the D5 twins, plus
     the contrived seed-shaped-text case), and pre-existing dangling
     `superseded_by`/`contested_with` references (baseline for step 6).
3. **Shadow copy** into `firekeep_memory_v2`, created from
   `get_collection(source).config.params` VERBATIM (never from env), with
   the three payload indexes (`tags`, `namespace`, `workspace_id`) created
   explicitly and `indexing_threshold: 0` during bulk load, restored after.
   Copy vector + payload; when the target v2 id is already occupied (a D5
   twin), apply `_merge_lifecycle(v1_payload as existing, v2_payload as
   fresh)` deterministically — v2 text and vector win, counters max,
   earliest created_at survives, archive provenance survives. During the
   payload copy, rewrite the point-id-valued payload FIELDS through the
   map: `superseded_by` (contradiction.py:121, memory_agent.py:433,
   vector.py:1277) and `contested_with` (memory_agent.py:730) — both are
   user-rendered (rag.py:1435,1441) and autopilot-read. Write the old→new
   id map as a durable JSONL artifact; mirror into Redis
   (`mem:idmap:v2`) as a cache of the file, not the source of truth.
   **State machine** in Redis: `{run_id, source_collection,
   source_points_count_at_start, step, cursor, started_at}` — advanced
   per step, cursor-resumable, refusing to resume if the source fingerprint
   disagrees; step N+1 refuses until step N is marked complete and the map
   count equals the dry run's migratable count.
4. **Flip:** update `QDRANT_COLLECTION`, recreate cortex containers (still
   inside the freeze; workers stay stopped).
5. **Graph remap — after the flip**, so graph rows never point at ids the
   live collection lacks (before the flip, remapped rows would be dropped
   by `_scope_verdict`'s resolve-fail path — rag.py:929-933 — emptying the
   graph leg): rewrite `MemoryRef.vector_id` and chain-node `memory_ids`
   through the map (unmapped ids pass through — corpus/dream/skill/v2);
   stamp `legacy_unscoped` chain nodes per D4; add a uniqueness constraint
   on `MemoryRef.vector_id` after de-duplicating. Then the Redis hash
   folds: `memory:access_counts` and `memory:last_recalled` fields
   translated through the map (flusher is already stopped; skill-id fields
   pass through unmapped by design; the `:flushing` key is confirmed empty
   first), with a reconciliation count reported — a nonzero residual is
   the measured loss.
6. **Verify — exact and fatal, only possible because of the freeze:**
   source `points_count` unchanged since freeze (else abort); shadow
   counts reconcile exactly per bucket (no tolerance); fidelity sample —
   N random migratable points, vector equality and field-by-field payload
   equality (excluding id and the rewritten reference fields); payload
   indexes present; config.params equality; **search parity** — ~50
   recorded queries against source and shadow return the same texts and
   scores; no `superseded_by`/`contested_with` in the shadow naming an id
   absent from the shadow beyond the dry run's pre-existing-dangling
   baseline; zero v1-predicate memory points outside quarantine.
7. **Unfreeze:** restart workers/beat, lift the write gate, completion
   marker written. The old collection is retained until the operator
   deletes it (a later explicit act; its name is now unreferenced, so
   retention conflicts with nothing).

Rollback: before step 4, delete the shadow and unfreeze — nothing changed.
After step 4, roll forward or restore the freeze-start backup (RPO stated
above). Never both directions.

## D7. Stranded-join mitigation

Historical replay events name OLD ids. **OWM's join is the dangerous one:
a miss is not a no-op — after writing scores for ids it found, owm.py's
stale-reset sweep (owm.py:265-292) DELETES `owm_efficacy`/`owm_n` from
every point not written this pass. Post-migration, with every event naming
v1 ids, an unmapped run would write nothing and wipe every migrated
memory's efficacy (and `skill_efficacy` in the parallel block), degrading
recall ranking store-wide in one night.** Therefore: owm.py translates
event `memory_ids` through the idmap AT THE TOP of the join, before
building stats; and the stale-reset sweep is SKIPPED entirely (loudly
logged) whenever the map is expected (migration marker present) but
unavailable — an expired cache degrades to no-update, never to wipe. Fix
round 1: "unavailable" is a completeness check against the entry count
`verify` records alongside the marker, not mere presence — a hash that
still exists but holds fewer fields than recorded (a Redis restart or AOF
loss can drop some without dropping the key) is a PARTIALLY degraded cache
and is treated exactly like an absent one. The JSONL artifact is the
durable form; Redis is a cache. The client kit's proactive-recall seen-cache
(12h TTL) self-heals. Replay events themselves stay immutable history and
age out on their own retention.

## D8. Benchmark closure — the original goal

After deploy + migration: one clean full ingest of the LongMemEval fixture
must show ~124,263 points and 470/470 questions with owned evidence, and a
REPEAT ingest must produce an identical ownership map (the determinism the
retest proved impossible under v1). Only then is a ranker A/B meaningful.
The published 0.819 homepage metric is retired/dated per the retest's
resolution (site copy is a separate act). The bench harness needs no
change.

## D9. Disclosed follow-ups, out of scope

Skills' `SKILL_NS` ids don't fold workspace (same defect shape; skills
also write via a raw client upsert — synthesizer.py:647); graph
Domain/Concept global text keying; engine/rag.py's admit-by-default for
unattributable rows (narrowed here only for `legacy_unscoped`);
`_merge_lifecycle` field semantics (function unchanged; D5's bridge changes
only its reachability); `CONTENT_HASH_LENGTH` sizing.

## D10. Rollout order

(1) Ship v2 code: helper, mint-site rewiring, fail-closed minting branch,
transfer principal override, backfill legacy stamping, graph key +
workspace-scoped contradiction, D5 bridge, alias-aware `initialize()`,
gated `migrate_single_workspace`, retired `migrate_namespaces`, the
`MIGRATION_FREEZE` gate, and the migration tool itself (inert). (2) Deploy
(cortex containers; compose env additions per the checklist). (3) Dry run;
its report reviewed by the user. (4) The freeze-migration (D6), on the
user's separate go, at an agreed time (the write API is down for the
window; reads stay up until the flip). (5) The benchmark rerun (D8).

## Testing

- Helper: canonical-encoding vectors (delimiters, quotes, newlines, JSON
  metacharacters in text; workspace/namespace permutations distinct;
  deterministic across processes). Guard: no bare-text uuid5 outside
  helper + migration tooling.
- Fail-closed: minting-branch-only (upsert with point_id and no workspace
  succeeds — the corpus shape; without point_id raises); transfer import
  overrides an asserted foreign workspace_id with the principal's;
  backfill legacy entry stamped, not DLQ-ground.
- Namespace: upsert normalizes once for payload AND seed; guard that every
  migration-written point satisfies
  `payload.namespace == normalize_namespace(payload.namespace)`.
- Bridge (D5): archive → relearn exact text → not active; provenance
  fields survive into the v2 twin; bridge off → documented raw behavior.
- Graph: same text two workspaces → two chain nodes; `legacy_unscoped`
  rows denied by `_scope_verdict` under every `unattributed` setting.
- Cross-workspace regression: the retest's repro — learn same text in A
  and B, both recalls see their own, neither's provenance moves; near-dup
  in B does NOT supersede A's.
- `initialize()`: no-op against an alias-resolving name; double-start test.
- `migrate_single_workspace`: boots twice against a store holding a
  sentinel/`legacy_unscoped` point → workspace stays `__quarantine__`.
- Migration (seeded local Qdrant): provenance classification incl. legacy
  corpus chunks (id-predicate-true, source=corpus → untouched) and
  repaired-text bucket; occupancy/twin merge determinism (order-independent
  result, v2 text+vector win); quarantine sentinel invisible on BOTH legs;
  cursor resume mid-copy (planted crash) + refusal on fingerprint
  mismatch; superseded_by/contested_with rewritten; verify-pass failures
  each demonstrated (planted count drift, planted vector mutation, planted
  dangling reference); config.params carried from source.
- OWM: idmap translation at join top; sweep skipped + logged when marker
  present and map absent; hash-fold reconciliation counts.

## Deploy

Cortex containers (standard pull + rebuild) + the compose env additions
(`MIGRATION_FREEZE` default false, bridge flag default true). The migration
tool ships inert. Office/K8s inherit on their next update. Migration
execution: separate user go, preceded by the reviewed dry-run report and
the freeze-start cold backup, per D6/D10.

## Revision record

**2026-08-27, same day, pre-implementation.** Adversarial review (claims
verifier with live Qdrant v1.13.2 probes, hostile migration reviewer,
skeptic verification; 33 findings, 24 verified) rebuilt the migration
design and corrected the identity sections: the alias flip was DISPROVEN
empirically (name-collision 409, no rename, `initialize()` bricking) →
cut-over is a new canonical collection name + config flip, with
alias-aware `initialize()` as hardening; a mandatory write freeze replaced
the no-freeze copy (concurrent-write loss was shown unfixable by catch-up)
and collapsed the verify pass to exact-and-fatal; classification moved
from id-shape to provenance (legacy corpus chunks and mojibake-repaired
points both defeated the id predicate — the latter proving "payload text
never diverges from minting text" false); the D5 window gained the
lifecycle bridge (archived/superseded resurrection was undisclosed) and
the migration a deterministic twin-merge for occupied v2 ids; quarantine
became a sentinel workspace after the review showed "recallable" was false
on the vector leg, cross-workspace-leaky on the graph leg, and the whole
bucket was auto-adopted by `migrate_single_workspace` on the next restart;
graph remap moved AFTER the flip (before, it emptied the graph leg);
`superseded_by`/`contested_with` joined the remap; OWM's stale-reset was
found to DELETE scores on a missed join → translation at the join top +
sweep-skip guard; hash folds moved inside the freeze post-flip with the
flusher fenced; the state machine, source fingerprint, occupancy check,
fidelity/search-parity verification, shadow config inheritance, backfill
legacy stamping, transfer principal OVERRIDE (caller-asserted spoof),
namespace normalize-at-top invariant, and `migrate_namespaces` retirement
were all added; the resource-envelope finding was refuted at this store's
scale but yielded operational preconditions (disk/memory checks, measured
dry-run numbers in the report). No registered goal changed: same identity
scheme, same never-guess-owners rule, same user-gated execution.
