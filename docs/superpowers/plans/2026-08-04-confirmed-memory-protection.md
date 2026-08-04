# Confirmed memories must not be buried by the memory-agent passes

**Date:** 2026-08-04
**Scope:** `cortex/app/workers/memory_agent.py`
**Status:** fixed, both defects verified by execution before and after

## The hole

`POST /memory/confirm` bumps `confirmed_count` — the strongest signal a human
can give that a memory is correct. Two of the memory agent's 6-hourly
maintenance passes ignored it as protection and treated it only as a ranking
input. Both run on the production VPS today.

Two places already treated confirmation as protection, and they are the
precedent this change follows:

* `cortex/app/workers/gc.py::_scan_candidates` — skips `confirmed_count > 0`
  as its **first** test.
* `cortex/app/db/vector.py::_similarity_filter` — gained a `must_not
  confirmed_count > 0` in commit `16c897f`, closing the same hole on the
  learn-time contradiction path.

## Verified before fixing

Reproduced by execution with a scratch harness in the style of
`cortex/tests/test_memory_agent.py` (fake Qdrant/Redis/Neo4j). Both audit
claims hold, and one is worse than reported.

### 1. `deep_contradiction_pass` supersedes confirmed memories — CONFIRMED

The pass ranks a pair by `(1 + confirmed_count) / (1 + contradicted_count)`
and writes `status="superseded"` on the loser — a permanent 0.5 recall
multiplier. Two distinct routes to burying a confirmed memory, both observed:

* confirmed once (`2/1 = 2.0`) loses to confirmed three times (`4/1 = 4.0`);
* confirmed once but contradicted twice (`2/3 = 0.67`) loses to a memory that
  was **never confirmed** (`1/1 = 1.0`).

The second matters for the shape of the fix: a naive
`if mem.confirmed > other.confirmed` comparison would miss it.

### 2. `duplicate_detection_pass` replaces confirmed text — CONFIRMED, and worse

Observed on the LLM merge path: the confirmed memory was superseded, the
surviving point carried the LLM's paraphrase, and — the part the audit did not
state — `_merge_lifecycle`'s `confirmed_count` **max fold carried the human's
confirmation onto the synthesized point**. So the defect was not only that the
confirmed wording was lost; it was that text nobody ever confirmed ended up
marked as confirmed, and thereby protected by GC and by
`vector.py::_similarity_filter` from then on. The confirmed original's content
was also sent to the merge model verbatim.

Also observed on the LLM-**fallback** path: a confirmed memory that is not the
top-confidence cluster member is superseded outright. Its text survives on
disk, but at a 0.5 multiplier. Relying on the LLM being down is not protection.

## The fix — two different shapes, deliberately

The obvious candidate was to add `confirmed_count > 0` to the existing
`_active_non_corpus_filter()` `must_not`. **That is wrong**, because the filter
is shared by three passes and blanket exclusion is only right for one:

| Pass | Uses shared filter | Blanket exclusion? |
|---|---|---|
| `duplicate_detection_pass` | yes | **correct** — see below |
| `deep_contradiction_pass` | yes | **too blunt** |
| `cluster_coherence_pass` | yes | **unwanted side effect** |

* **deep_contradiction_pass** — the filter scopes the similarity **query** as
  well as the scroll. Excluding confirmed memories would stop one being
  *found*, not just stop it being buried: a stale rival would no longer be
  matched against it and would survive. A confirmed memory must still be able
  to supersede others. So this pass keeps full scope and **refuses the write**:

  ```python
  if (stale.get("confirmed_count") or 0) > 0:
      continue
  ```

  Skip, do not invert. Inverting would bury the side the ratio says is better
  on no human signal at all, and when *both* sides are confirmed there is
  nothing to invert to. One rule in both directions: this pass never
  supersedes a confirmed memory, and a confirmed keeper still supersedes an
  unconfirmed rival exactly as before.

* **cluster_coherence_pass** — rewrites `domain` only. It never changes status
  or text, so it does not bury anything a human confirmed; the human vouched
  for the content, not the domain tag. Excluding confirmed memories would also
  drop them out of the per-domain **centroids**, changing outlier detection for
  the unconfirmed memories around them — a behaviour change to memories this
  protection is not about. Left alone deliberately.

* **duplicate_detection_pass** — takes the exclusion, via a new
  `_dedup_scope_filter()` derived from the shared filter (so the corpus/dream/
  dream_profile conditions cannot drift). Scope exclusion rather than a
  write-time refusal because there is no merge outcome that preserves a
  confirmed memory's wording: `_merge_cluster` re-embeds the synthesis, writes
  it under a new id, and supersedes every original *including the keeper*.

  The argument is specifically **not** "a refusal would leave a residual
  duplicate". Exclusion leaves one too, whenever two or more unconfirmed
  members remain to merge — the confirmed memory stays active beside their
  merged survivor, which is a near duplicate of it, and
  `test_confirmed_memory_is_not_merged_while_its_duplicates_still_are`
  demonstrates exactly that. (An earlier draft of this document and of the
  `_dedup_scope_filter` docstring made that claim; it was wrong, and the
  code's own test contradicted it.) What exclusion uniquely prevents is the
  two things a refusal cannot: a refused member is still a **cluster**
  member, so `_merge_lifecycle` still launders its `confirmed_count` onto the
  synthesis, and its text is still sent to the merge model in the prompt.
  Only keeping the point out of the cluster stops both — which is also what
  both standing precedents do.

`Range(gt=0)` is used, not an existence check: a point predating the field has
no `confirmed_count` to match, so `must_not` admits it and legacy memories stay
eligible for dedup.

## Tests

`cortex/tests/test_memory_agent_confirmed.py` (11 tests). They run against a
**filter-honouring** Qdrant double, not a `MagicMock`. Every pre-existing fake
in this suite ignores `scroll_filter`/`query_filter` entirely, so a test
written against one can only inspect the filter *object* — which is how a
`repr()`-shaped assertion can pass against a catastrophically inverted filter.
The double returns only the points a filter admits, so dropping or inverting a
condition changes which memories the pass **writes** to, and the assertions on
those writes fail.

Verified by three independent mutations of the fixed tree:

| Mutation | Tests that went red |
|---|---|
| A — dedup wired back to the shared filter | the 2 dedup behaviour tests |
| B — contradiction write-guard removed | the 2 contradiction burial tests |
| C — the **over-fix**: `confirmed_count` in the shared filter, no write guard | `test_shared_scope_still_admits_confirmed_memories`, `test_a_confirmed_memory_can_still_supersede_an_unconfirmed_rival` |

Mutation C is the one that matters for "do not over-fix": the blunt version of
this change is caught behaviourally, not just by an absence assertion.

Two traps worth recording, both of the same species — a test that passes
because the code under test never ran:

* The first draft used near-parallel fixture vectors (cosine ≈ 0.9996).
  `deep_contradiction_pass` only acts on `0.85 <= score <= 0.95`, so the pass
  was a no-op and every "nothing was superseded" assertion passed **without
  the guard ever being reached**.
  `test_fixture_vectors_are_inside_the_contradiction_window` now pins it.
* `test_confirmed_memory_is_not_superseded_on_the_llm_fallback_path`
  originally seeded two confirmed memories, so post-fix **both** were
  excluded and the pass returned at `len(memories) < 2` before any `httpx`
  call — the `Exception("LLM down")` side effect never fired and the test
  pinned the early return while its name promised the fallback. It now seeds
  one confirmed memory plus **two** unconfirmed duplicates, so a real cluster
  survives to the fallback, and `mock_httpx.assert_called()` holds that. The
  confirmed member is differentiated by `contradicted_count` rather than
  `confirmed_count` on purpose: as the highest-confidence member it would
  become the keeper and survive by accident, testing nothing.

## Existing tests that encoded the defect

Three fixtures in `cortex/tests/test_memory_agent.py` seeded a confirmed
member into `duplicate_detection_pass`. All three still passed after the fix —
only because their `MagicMock` doubles ignore the filter — so they now describe
states production cannot produce. Updated, not quietly:

* `test_llm_merge_reembeds_and_rekeys` asserted
  `point.payload["confirmed_count"] == 1  # max of cluster`. **This asserted
  the laundering as correct behaviour.** Replaced with an assertion that the
  earliest member's `created_at` survives the fold — same purpose (lifecycle is
  folded, not discarded), reachable input.
* `test_fallback_keeps_keeper_without_reembed` used `confirmed_count: 3` as the
  keeper differentiator. Switched to `contradicted_count`, same outcome.
* `test_embed_failure_aborts_merge_without_corruption` seeded
  `confirmed_count: 1`; set to 0. Its assertions were unaffected.

`test_deep_contradiction_found` was **left alone**: it has a confirmed memory
(`confirmed_count: 2`) superseding an unconfirmed one, which is still reachable
and still correct.

## Deliberately not changed

* `cluster_coherence_pass` — reasoning above.
* `_merge_lifecycle`'s `confirmed_count` max fold — with confirmed memories out
  of dedup scope the fold is now always `0 -> 0` on that path, but the function
  is shared with `vector.upsert`, where the fold is load-bearing (SP0 A3).
* The `(1 + confirmed) / (1 + contradicted)` ranking ratio itself. It is still
  a reasonable *ranking* signal; the defect was treating it as the only reading
  of confirmation. Both passes still use it to choose a winner.
* Neo4j `SUPERSEDES` edges — the skipped pairs write no edge because they never
  reach the write block.

## Gates

* `cortex/` → `python -m pytest tests/ -q` — 1588 passed, 30 skipped, 0 failed
  (baseline 1577 + 11 new).
* repo root → `python -m ruff check .` — clean.
* repo root → `python -m pytest tests/test_forbidden_tokens.py -q` — 21 passed.
