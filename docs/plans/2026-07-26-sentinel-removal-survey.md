# Sentinel removal — consumer survey

**Status:** survey only. No removal work has been done. This exists so the decision
is made against measurements rather than an estimate.

**Why this document exists.** The commercialization design listed Neo4j and Sentinel
as its two expensive removals and had verified neither. Neo4j was assumed to be
flag-gated, was not, and a survey put its removal at 107 h against an assumed 25–40 h
— so the removal was reversed. The design's own risk table then said: run the same
check on Sentinel *before* sizing the work, not during it. This is that check.

## Verdict up front

Sentinel is a **leaf**. Nothing computes a result from it; every consumer displays it.
That is the opposite of Neo4j's shape, and it is why the two removals are not
comparable despite sitting in the same row of the same plan.

Cost is **~10–16 h**, with **two product decisions**, not twenty.

## Area 1 — Boot path

Neo4j's reversal turned on this: `MULTIHOP_ENABLED` gated *traversal*, while the
lifespan connected unconditionally and raised, so Cortex could not boot without it.

Sentinel has no such edge.

| Check | Result |
|---|---|
| Any service with `depends_on: sentinel` | `dashboard` only — and only for start ordering |
| `cortex-api` / `cortex-mcp` dependency | None. They hold `SENTINEL_URL` and call it over HTTP |
| Sentinel's own dependencies | `redis` only |
| Failure containment | `environment_section()` catches per section; a dead Sentinel yields `status: unavailable` and `degraded: true`, and `GET /briefing` still returns **200** |

There is no `SENTINEL_ENABLED` flag — but unlike Neo4j, the absence of a flag does
not imply a hard dependency here, because the call path is an outbound HTTP fan-in
already written to tolerate failure. **Stopping the container is a supported state
today.** That is the single most important difference from Neo4j and it is directly
verifiable: `docker compose stop sentinel` and the stack keeps serving.

## Area 2 — Consumer surface

73 references across 23 files, but the distribution is what matters:

| Consumer | Sites | Nature |
|---|---|---|
| `sentinel/` itself | ~1,324 LOC (854 app / 470 test) | Deleted wholesale |
| `dashboard/index.html` | 5 call sites + ~14 CSS rules | One tab, one health bar, two overview widgets |
| `cortex/app/briefing/sections.py` | 2 | The `environment` section |
| `cortex/tests/test_briefing_*` | 9 | Tests of that section |
| `client/firekeep_client/adapters/base.py` | 2 | `SERVICES` tuple, `FIREKEEP_MCP_KEYS` |
| compose / Caddyfile / nginx / `.env.example` | 6 | Config plumbing |
| docs | ~9 | Prose |

Most of the count is test and doc text, which deletes rather than migrates.

## Area 3 — What is actually lost

Three MCP tools (`sentinel_get_events`, `sentinel_get_health`, `sentinel_push_event`),
four HTTP routes (`/health`, `/environment`, `/events`, `/version`), and three
collectors (docker container states, git activity, file-mtime snapshots).

**The finding that decides this: Sentinel's telemetry feeds no intelligence.**

`store.py:98` emits its replay events with a hardcoded `session_id="sentinel"`.
Evals and pattern feature extraction both key strictly by a real session id
(`evals/compute.py` computes per `session_id`, then hands that same event list to
`patterns.extractor`). So every `env_change` event lands in an orphan bucket that no
eval, no pattern detector, and no memory path ever reads. Repo-wide, `env_change`
appears in exactly three non-test places: the replay type enum, the emit above, and a
comment.

This is the check Neo4j failed and Sentinel passes. Removing Neo4j deleted four
features and introduced a durability regression on `/memory/learn`. Removing Sentinel
deletes a **display surface**: no memory is lost, no recall changes, no write path is
touched, nothing becomes less durable.

## Area 4 — Genuine losses, stated plainly

1. **The briefing's `environment` section.** Agents currently see container health and
   up to three recent error events at session start. This is real and it goes away.
2. **The alert path** (`store.py:104`): `error`+ severity events broadcast to Relay and
   fire Cortex webhooks. A real outbound integration with no replacement.
3. **The dashboard Events tab** — the only place a human sees infrastructure state.

The git collector's best-effort POST to `SYMDEX_URL` is already dead (there is no
server-side symdex) and costs nothing.

## The two product decisions

1. **Does a memory product ship infrastructure monitoring at all?** Every customer
   who would run this already runs something that does container and git observability
   better. Keeping Sentinel means maintaining a weak second copy of a solved problem.
2. **If it goes, does the alert path survive?** Relay broadcast on error is the one
   piece with no equivalent elsewhere. It could move to a ~40-line webhook receiver on
   Cortex, or be dropped.

## Recommendation

Remove it, and keep the alert path only if decision 2 says so. But note the honest
counter-argument, since the Neo4j reversal came from ignoring one: at 1,324 LOC with
a green test suite and zero coupling to the write path, Sentinel is **cheap to keep**.
Its removal buys a smaller surface and one less container, not a faster or more
correct product. If the goal is shipping sooner, deleting a working, isolated,
already-tested component is not on the critical path — and unlike Neo4j, it can be
removed later at the same cost, because leaf components do not accrete coupling.

**The measurement that would decide it:** nothing here needs one. Neo4j's open
question was empirical (does the graph beat pure vector on recall?) and remains
unanswered. Sentinel's is a product-scope question with the facts already in hand.
