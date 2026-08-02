# Derived Architecture Map — Generating the Topology CLAUDE.md Transcribes by Hand

**Status:** design, approved 2026-08-01

**Relationship to the relationship-graph idea:** this spec is the *first* of two separable
projects that came out of one question ("can we accumulate an architecture/relationship
map as we work?"). This one — deterministic derivation of topology from artifacts — is
buildable now. The general entity/relationship graph is **deliberately deferred**; see
§11 for the argument and the gate it must pass first.

## Problem

Roughly 20% of `CLAUDE.md` by volume is enumeration-shaped, and ~10% of it is a
hand-transcribed graph of this system (§14): the
service table, the Redis DB allocation, port assignments, MCP tool inventories, REST
endpoint lists, env-var surfaces. It is loaded into the prompt prefix of every session,
by every agent, on every machine — and it has drifted.

Measured drift (spike, 2026-08-02 — see §14 for method):

- **24 of 113** real routes are undocumented — **21%**. Counting basis, since it changes
  the number: unique `(method, path)` pairs after AST extraction; there are 102 distinct
  *paths*, so a path-based basis gives a different denominator. A further **19** routes
  score as undocumented only under a raw substring test because `CLAUDE.md` documents
  them with a different path-parameter placeholder (`{id}` vs `{memory_id}`). Those are
  a much weaker finding class — cosmetic mismatch, not a gap — and the generator must
  normalise placeholders before the coverage test or it will over-report by ~79%.
- **13 of 62** MCP tools are never mentioned, including `ctx_get_shadow`,
  `ctx_list_sessions`, `memory_health`, `memory_stream`, `audit_memory`, and every one of
  the three `sentinel_*` tools.
- The briefing is documented as **11 sections**; `cortex/app/briefing/sections.py` defines
  **12** section functions (`:99, :136, :182, :207, :226, :267, :279, :311, :330, :357,
  :368, :379` — `observed_patterns_section` is the missing one).

`ctx_get_shadow` is the sharpest case: the global cognitive-stack instructions mandate it
as the **first action after context compression**, and it appears nowhere in this repo's
`CLAUDE.md` tool inventories.

This is the worst shape a document can take: authoritative-looking, expensive on every
prompt, and wrong in ways nobody can see. An agent that trusts it is misled; an agent
that verifies it paid for the prose twice.

Meanwhile the underlying facts are almost entirely machine-derivable. `docker-compose.yml`
alone yields 28 service→service edges via `depends_on` + `environment:` +
`dashboard/nginx.conf.template`; the Redis DB 0–7 table reconstructs exactly from
`redis://redis:6379/N` URLs; FastAPI decorators and `@mcp.tool()` definitions AST-extract
cleanly. Nothing in the repo does this today. The closest artifacts are
`client/firekeep_client/resolver.py`'s hand-maintained `SERVICES`/`MCP_PORTS`/`REST_PORTS`
registry, and `tests/test_no_dead_config.py`, which already establishes the config↔code
cross-check pattern this work extends.

## Goal

A deterministic generator that derives the topology and inventories from repository
artifacts, renders them into a marker-delimited block inside `CLAUDE.md`, and is guarded
by a regenerate-and-diff test so the map cannot silently rot.

**Non-goals, explicitly:**

- **No LLM, anywhere.** Every fact is parsed or AST-extracted. This is not an extraction
  feature.
- **No new MCP tool, and no new server surface.** The active `token-reduction` plan states
  "**No new MCP tool.** The spec rejects growing the tool surface; that applies to our own
  additions." This work honours that: it is a repo-local build tool.
- **No replacement of `CLAUDE.md`'s reasoning.** The generator owns tables and
  enumerations. It never owns explanations — the Sentinel docker-collector rationale, the
  chunked-ollama history, the `ANONYMOUS_SCOPES` argument. Those live outside the markers
  and are never touched.
- **Not Neo4j.** Nothing here writes to the knowledge graph or depends on it.

## Why this feature specifically, and why now

This repo has twice built relationship structure that nothing read, and both deaths were
audit discoveries rather than monitored alarms: the corpus entity/relationship graph
(`docs/HISTORY-NOTES.md:20-25` — "an audit found 0 entities had ever been extracted in
production, and ~500 LOC of write-only LLM extraction machinery was deleted") and ~161K
`BACKLINK` edges written and never traversed.

The design constraint that follows is not "avoid graphs." It is: **a feature must have a
reader that cannot lapse, and a mechanism that makes non-use visible.** This design has
both structurally rather than by promise:

- The reader is **the prompt of every session**. The block is in `CLAUDE.md`.
- The alarm is the **drift test**. A stale map turns CI red, so "nobody noticed" is not a
  reachable state.

A table is also denser than the paragraphs it replaces, so this reduces prompt tokens
rather than adding to them — aligned with the branch it lands on.

---

## 1. What is derived

| Artifact | Sources | Expected yield |
|---|---|---|
| Service topology | `depends_on`, `environment:`, `nginx.conf.template` proxy_pass, code call sites | ~28 config edges + code-only edges |
| Redis DB allocation | `redis://redis:6379/N` across compose + `.env.example` | 8 rows |
| REST route census | FastAPI decorator + `@mcp.custom_route` AST per service | 113 unique `(method, path)` (measured) |
| MCP tool census | `@mcp.tool()` AST; proxy target where resolvable | 62 tools (measured); 27/27 pairs on cortex |
| Port / bind map | compose `ports:` + `BIND_ADDR` | complete |
| Env var surface | compose + `.env.example` + each `config.py`, declared-vs-consumed | extends `test_no_dead_config.py` |

## 2. Module layout

`archmap/` at repo root, mirroring the shared-module convention (`replay/`, `auth/`,
`vault/`, `provenance/`), with its own `archmap/tests/`.

```
archmap/
  model.py          Node / Edge / Finding / CollectorResult
  profile.py        which collectors run, and where they look (the only Firekeep-specific file)
  collect/
    compose.py      services, ports, depends_on, env-URL edges, Redis DB allocation
    nginx.py        proxy_pass edges
    routes.py       FastAPI decorator AST walk
    mcp_tools.py    @mcp.tool() AST walk + proxy target resolution
    envvars.py      declared-vs-consumed cross-check
    code_edges.py   httpx call sites attributed to settings attributes
  render.py         Facts -> deterministic markdown
  block.py          marker-delimited upsert into CLAUDE.md
  __main__.py       `python -m archmap render|check`
  tests/
```

`python -m archmap` rather than a console script, matching
`python -m firekeep_symdex.reindex`: resolvable from `sys.executable` alone, no PATH
dependency, no shim to keep in sync.

## 3. The record

```python
@dataclass(frozen=True)
class Edge:
    src: str          # "cortex-api"
    dst: str          # "bridge"
    relation: str     # calls | starts_after | proxies | stores_in
    kind: str         # depends_on | env | proxy | code   (HOW it was derived)
    evidence: str     # "docker-compose.yml:293 BRIDGE_URL"
    confidence: str   # certain | probable
    route: str | None # "/sessions" when the code collector resolved it
```

`relation` and `kind` are orthogonal and both required. Conflating them is how the "28
edges" figure overstates the topology: **18 of the 28 are `depends_on`, which means
"start after", not "calls"**. They render as separate tables.

`evidence` is a `file:line` string on every row. This is what makes the output auditable
and lets a human re-derive any claim — the same discipline that makes the census
trustworthy rather than merely tidy.

```python
@dataclass(frozen=True)
class CollectorResult:
    facts: list[Node | Edge]
    skipped: int
    reasons: list[str]
```

Every collector returns its own yield. See §7.

## 4. Derivation: config ∧ code, and the findings that fall out

Config alone gives 28 edges. Folding in `.env.example` raises it to ~40 but introduces
~7 false positives, because `env_file: ['.env']` hands every cortex container the same
variables regardless of whether that process makes the call — `cortex-beat` receives
`RELAY_URL` while its command is `celery -A app.workers.sleep_cycle beat`, a pure
scheduler with no outbound HTTP.

Neither the precise nor the permissive answer is correct on its own. Taking 28 silently
omits `sentinel→cortex-api`, a real production call (`sentinel/app/config.py:32`
`CORTEX_API_URL` as a Pydantic class default; called at `sentinel/app/store.py:37`
`f"{cortex_url}/webhooks/internal/fire"`) that appears in **no config file at all**.
Taking 40 asserts an edge that does not exist.

**Resolution: derive from both sides and treat disagreements as output.**

| | present in code | absent from code |
|---|---|---|
| **present in config** | confirmed edge, `kind=env` | **finding: unused config** — not an edge |
| **absent from config** | **edge, `kind=code`** | — |

Three finding classes, all deterministic:

1. **Unused config** — a service is handed a URL its code never calls (`cortex-beat` /
   `RELAY_URL`).
2. **Dangling target** — config and code agree, but the target service does not exist.
   Live example: `NS_SYMDEX_URL=http://symdex:8090` (`.env.example:170`,
   `sentinel/app/config.py:29`), called at
   `sentinel/app/collectors/git.py:134 _trigger_reindex(settings.SYMDEX_URL, repo)` —
   and no `symdex` service exists in `docker-compose.yml` or `docker-compose.office.yml`.
3. **Undocumented surface** — routes/tools present in code, absent from the block's
   previous generation.

**The generator therefore has two outputs: the map, and a findings list.** The findings
list is a config linter obtained as a byproduct, and it already has a real catch waiting.

## 5. The code collector's contract

Deliberately narrow, because an over-reaching resolver that guesses is worse than one
that abstains and says so.

1. Resolve each service's settings class (`<svc>/app/config.py`); collect attributes whose
   name ends `_URL` **or** whose default value matches `^https?://`.
2. AST-walk the service for `httpx` / `requests` call sites.
3. Emit an edge **only** when the URL argument is an f-string whose first interpolation
   resolves to one of those settings attributes. Capture the literal remainder as `route`.

This is demonstrated, not hoped-for: it is how the briefing's seven route-level calls were
recovered. **Line numbers below (and §14's `CLAUDE.md` baseline) were taken from the
working tree during the spike, not from a committed revision, so they will not resolve at
this spec's own commit** — re-derive them rather than trusting them; §8.5's known-truth
test is what pins the facts, not these citations. —
`cortex/app/briefing/sections.py:339` `{SENTINEL_URL}/environment`, `:343`
`{SENTINEL_URL}/events`, `:360` `{RELAY_URL}/tasks`, `:371` `{RELAY_URL}/bulletin`, `:393`
and `:395` `{BRIDGE_URL}/sessions`, `:401` `{RELAY_URL}/presence/{agent_id}`.

**Anything built from a runtime variable is out of scope, and the collector reports how
many call sites it skipped.** This rule is non-negotiable and is the direct lesson of
symdex's import graph, which resolves **8% (425/5321)** of this repo's imports, reports
nothing about the remaining 92%, and manufactures a phantom `cortex→symdex` edge by
resolving a stdlib `import types` to `click/src/click/types.py`. A derivation tool that
does not publish its own yield is indistinguishable from a broken one.

## 6. Rendering and the block

Output is a single marker-delimited block in `CLAUDE.md`, written with
`upsert_marked_block` / `strip_marked_block` — the primitive already proven in
`client/firekeep_client/adapters/base.py` (`INSTRUCTIONS_BEGIN`/`END`): idempotent, and
only the block's *interior* is replaced. Precise about the guarantee, since an earlier
draft of this spec overstated it — surrounding content is **not** byte-for-byte, because
the functions `lstrip`/`rstrip` around the block and therefore normalise blank lines on
both sides. When extracting to `textblock/`, either drop those two calls or make the
surrounding-whitespace policy an explicit parameter; a generator that silently reflows
blank lines in `CLAUDE.md` on every run would churn the prompt prefix and fight §7's
idempotency requirement.

**Decision (approved): extract that primitive to a small shared root module —
`textblock/`, sized like `provenance/` — which both `client` and `archmap` import, along
with its existing tests.** A second copy is precisely the failure the
`search_skill_points` consolidation was written to end — one bug living in two files —
and this repo has paid for that lesson recently enough not to re-buy it.

Rendering is deterministic: sorted rows, relative paths, no hostname, and **no
`generated_at` field**. A timestamp would fail the drift test on every run and churn the
prompt prefix. Writes go through the `write_text_if_changed` discipline introduced on the
`token-reduction` branch, so an identical re-render never moves mtime.

## 7. Failure modes

The governing rule: **silent under-emission is the disease this feature exists to cure**,
so every degradation is loud or counted.

- **Unparseable required file → hard fail, nonzero exit.** The generator does not emit a
  smaller map. This deliberately inverts the fail-soft choice made in
  `cortex/app/skills/search.py`, and correctly: there, a down embeddings backend must not
  take out skill *listing*, because the degraded path is still honest. Here there is no
  honest degraded path — a partial map is a lying map.
- **Optional file absent → explicit skip, rendered as a line.** A missing
  `docker-compose.office.yml` says so; it never renders as silence.
- **Collector yield is always rendered** as a coverage line (`facts`, `skipped`,
  `reasons`).
- **`check` prints the exact fix command** (`python -m archmap render`). Friction that
  looks arbitrary gets disabled; friction with a one-line remedy does not.

## 8. Testing

1. **Per-collector unit tests** against small fixture trees in `archmap/tests/fixtures/`:
   a 3-service compose, a FastAPI module with 4 routes, an `mcp_server` with 2 tools.
2. **Golden-file renderer test** — Facts in, exact markdown out.
3. **Idempotency test** — run `render` twice; assert byte-identical output *and* that the
   second run performs no write.
4. **Drift test** (`tests/test_archmap_drift.py`) — regenerate against the real repo, diff
   the committed block, fail with the diff. Runs in the **`repo-scripts` CI job**,
   alongside `tests/test_image_pins.py`, and is **blocking, not advisory**: this is the
   anti-rot mechanism, and a non-blocking version of it is the same feature without the
   property that justifies it. The drift count is also the feature's own metric, baselined
   by the §14 spike at **24 routes + 13 tools** (placeholder-normalised; see §14).
5. **Known-truth test** — assert the generator finds facts verified by hand:
   `sentinel→cortex-api` as `kind=code`; `sentinel→symdex` as a dangling target;
   `cortex-beat`'s `RELAY_URL` as unused config; 12 briefing sections. Hand-verified
   ground truth as a regression corpus, and the concrete reason Firekeep is the first
   target rather than an arbitrary customer repo.

## 9. First-run migration

The first `render` will disagree with `CLAUDE.md` in 24+ places. The first commit
therefore does two things together, or the map appears twice: **add the generated block,
and delete the hand-written prose it supersedes.**

The boundary is tables versus reasoning. Enumerations move into the block; explanations
stay outside it and are never touched. `CLAUDE.md`'s value is disproportionately its
reasoning, and none of that is derivable.

## 10. Scope-3 readiness

Collectors take `repo_root` and return `CollectorResult`. No Firekeep paths are hardcoded
anywhere except `profile.py`. Shipping this client-side against arbitrary repositories
(the symdex model) is then `pyproject.toml` plus an entry point.

Whether it ships beside symdex or **replaces** symdex's `get_architecture_map` is left
open. Symdex already claims that surface, but its 8% import resolution is the reason this
design derives topology from config and settings-attributed call sites instead.

## 11. Deferred: the general relationship graph

The broader idea — an accumulating, domain-agnostic entity/relationship map covering
non-code relationships — is **not** specified here, and should not be built until a
prerequisite lands.

The existing Neo4j graph is on written probation (`docs/STRATEGY.md:40`: "prove
graph+vector beats pure vector on recall *with your own evals*, or cut the container"),
and that probation is **currently unsatisfiable**: all Tier-1 metrics in
`cortex/app/evals/scorers.py` measure counts, rates and durations, and
`_memory_freshness_at_recall` is the mean top_score of whatever was returned —
self-referential, and would score a graph leg emitting pure junk identically. No metric
measures whether a recalled memory was correct.

Three verified defects also mean the graph currently contributes near-nothing, so there
is no baseline to beat: `Domain`/`Concept` nodes are created without a `description`
(`cortex/app/db/graph.py:344, :361`) and are therefore always dropped
(`cortex/app/engine/rag.py:574`); the namespace filter admits only `Domain` nodes
(`graph.py:723`, sole `CONTAINS` writer at `:346`), so graph recall returns provably zero
rows in any non-default namespace; and the traversal reads no relationship type at all
(`graph.py:744` — a bare `-[r*1..3]-` wildcard). `_GRAPH_LABELS` (`graph.py:73`) exists as
the centralizing constant and is referenced by nothing, so every reader hardcodes five
labels and a new entity type would be invisible to all of them.

**Gate:** build the recall-quality measurement and repair those three defects first; let
the resulting number decide. If the graph that already exists cannot be shown to beat pure
vector, a richer one will not.

## 12. Risks and open questions

- **Maintenance obligation.** Every legitimate architecture change now requires
  `python -m archmap render` in the same commit or CI fails. That is the point, and it is
  friction. Mitigated by the one-line fix message and by keeping the block tightly scoped.
- **AST fragility.** Route and tool extraction depends on decorator shapes staying
  conventional. A dynamically-registered route is invisible. Mitigation: the skip count
  makes the blind spot visible rather than silent.
- **Two compose files.** `docker-compose.office.yml` uses `!override` literals, so its
  rows are not safely mergeable deltas on base rows. **Decision: render office as its own
  section**, never merged into base topology — a merged view would silently misreport
  whichever deployment the reader assumed.

## 13. Suggested phasing

The five config collectors are independent of `code_edges.py`, which is the only one
requiring settings-attribution AST work. The natural split:

**Phase 1 — config only.** Topology from `depends_on` + *inline* `environment:` + nginx
(the 28 edges), plus the route, tool, port, Redis and env-var censuses. Deliberately does
**not** read `.env.example` for edges: that file is what introduces the 7 false positives,
and without the code side there is no way to filter them. Dangling-target findings already
work here, since "config names a host that is not a service" is decidable from config
alone — so the `symdex:8090` catch lands in phase 1.

**Phase 2 — `code_edges.py`.** Adds code-only edges (`sentinel→cortex-api`), route-level
detail, and *unlocks* the unused-config finding by cross-checking `.env.example`-derived
candidates against real call sites.

Phase 1 must state its own limitation in the rendered coverage line — "code-derived edges
not collected" — so the gap is known by design rather than mistaken for completeness. That
is §7's rule applied to the roadmap. §8.5's known-truth assertions split the same way:
dangling-target in phase 1, `kind=code` and unused-config in phase 2.

## 14. Measured baseline (spike, 2026-08-02)

**Method:** `ast` walk over `cortex/ bridge/ sentinel/ relay/ auth/ vault/ corpus/
replay/` collecting `@router.<method>`, `@app.<method>`, `@mcp.custom_route` and
`@mcp.tool()`, resolving `APIRouter(prefix=)` where unambiguous; documentation coverage
tested by literal substring against `CLAUDE.md`; tokens counted with `tiktoken`
`cl100k_base`. **Zero parse failures, zero unresolved decorators.**

| Measure | Value |
|---|---|
| `CLAUDE.md` | 30,102 tokens / 120,718 chars |
| Unique routes | **113** `(method, path)` — **24** undocumented (21%), plus 19 placeholder-only mismatches |
| MCP tools | **62** — 13 undocumented |
| Rendered census block | 1,854 tokens (routes 1,418 + tools 420) |
| Rendered topology block | ~764 tokens |
| **Generated total** | **~2,618 tokens** |
| Replaceable: endpoint/tool enumeration, **backticked literals only** | 652 tokens / 111 literals |
| Replaceable: endpoint/tool enumeration, **whole lines** | 2,709 tokens / 36 lines |
| Replaceable: service, Redis and scope table rows | 349 tokens / 28 lines |
| **Replaced total** | **1,001 tokens** (literals) to **3,058** (whole lines) |
| **Net** | **+1,617 to −440 tokens** — sign is not established |

Config-var tables (31 rows, 1,501 tokens) are deliberately excluded from "replaceable":
their `Purpose` column is hand-written explanation and is not derivable. §9's
tables-versus-reasoning boundary applies.

**Honest reading of the net — corrected 2026-08-02 after adversarial review.** The
original figure (−440) compared the generated block against *whole lines* of replaced
prose, while the sibling row counted only literals. Those are incompatible rules, and the
whole-line side counts ~2,000 tokens of hand-written explanation that §9 explicitly says
the generator never owns. Measured consistently — literals against literals — the block
*costs* ~1,617 tokens. The true figure lies between the two, because the generator can
own some of the connecting prose but not all of it, and nothing here establishes how
much.

So: **the sign of the token effect is unknown, and the earlier "approximately
token-neutral" claim was not supported by its own measurement.** Treat this as a
correctness feature that plausibly costs prompt tokens. The return is +21% route
coverage, +27% tool coverage, and a drift alarm — bought with tokens, not for free.
Settle the sign by generating the block once and diffing real token counts before
landing it; that is cheap and it is what §8.4's metric will measure anyway.

**Amendment considered and rejected.** Before this spike, a per-row estimate put the
census at ~5,700 tokens and concluded it had to live outside `CLAUDE.md` with the drift
test as its only reader. That estimate was ~3× too high. At 1,854 tokens the census is
cheap enough to stay prompt-resident, so the single-block design in §6 stands unchanged
and both halves keep the every-session reader.

## Evidence provenance

Independently re-verified in-session against source: the 12 briefing section functions;
the unscoped `transfer.py` router; `_GRAPH_LABELS` unreferenced; the three graph defects
in §11; zero callers for the `Person`/`Skill`/`Preference`/`Goal` writers.

Measured by the §14 spike, **superseding** the recon estimates they replace: route count
(114, was 93), undocumented routes (40, was 24), undocumented tools (13, was "6+"), the
CLAUDE.md volume share, and all token arithmetic. The MCP tool total (62) is confirmed by
two independent counts.

Still **single-sourced from automated recon and unverified**: the 28 compose edges, the
27/27 cortex proxy-pair yield, and symdex's 425/5321 import resolution. Treat as estimates
until phase 1's first run re-derives them — which §8.5 asserts for free.

## Related

- **Security defect found during this work, unrelated to the design and worth fixing
  independently:** `POST /memory/import` and `GET /memory/export` sit on an unscoped
  router (`cortex/app/transfer.py` has no `require_scope`; registered at
  `cortex/app/main.py:273` with no `dependencies=`). Any valid non-admin key can bulk
  exfiltrate every memory and bulk-write arbitrary graph structure.
