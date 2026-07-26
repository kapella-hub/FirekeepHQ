# Sentinel removal — consumer survey

**Status: SETTLED 2026-07-26 — Sentinel is KEPT.** The owner ratified the survey's
recommendation. Removal is off the roadmap; this document is now the record of why,
not a proposal. Do not re-open it without new evidence — the facts below were
measured, not estimated, and the cost of re-deriving them is a re-read.

This is the second of the commercialization design's two proposed removals to be
reversed on survey (Neo4j was the first, at 107 h against an assumed 25–40 h). Two for
two is a signal about the design's removal estimates generally, not about these two
components.

**Why this document exists.** The commercialization design listed Neo4j and Sentinel
as its two expensive removals and had verified neither. Neo4j was assumed to be
flag-gated, was not, and a survey put its removal at 107 h against an assumed 25–40 h
— so the removal was reversed. The design's own risk table then said: run the same
check on Sentinel *before* sizing the work, not during it. This is that check.

## Verdict up front

**Defer. Do not remove Sentinel.** Not because it is expensive — it is cheap, ~10–16 h
against Neo4j's 107 h — but because nothing is bought. Sentinel is a leaf: nothing
computes a result from it, every consumer displays it, and it is coupled to no write
path. That is the opposite of Neo4j's shape, and it is also the reason the removal can
be done later at the same price. Leaf components do not accrete coupling while you
wait.

Two facts push it past "cheap either way" and into "leave it":

- It is **documented to customers as a product feature** (§Area 5). Removal is a
  contract break, not a display deletion.
- Its removal buys a smaller surface and one container. It does not make the product
  faster, more correct, more durable, or shippable sooner.

The rest of this document is the measurement, including the one place where a stack
*does* depend on Sentinel and my first pass got it wrong.

## Area 1 — Boot path

Neo4j's reversal turned on this: `MULTIHOP_ENABLED` gated *traversal*, while the
lifespan connected unconditionally and raised, so Cortex could not boot without it.

Sentinel has one such edge, and my first pass missed it the same way the Neo4j
estimate did — by inferring config semantics instead of reading them.

| Check | Result |
|---|---|
| `cortex-api` / `cortex-mcp` dependency | None. They hold `SENTINEL_URL` and call it over HTTP |
| Sentinel's own dependencies | `redis` only |
| Briefing failure containment | `environment_section()` catches per section; a dead Sentinel yields `status: unavailable` and `degraded: true`, and `GET /briefing` still returns **200** (verified: `test_environment_unavailable_on_health_failure`) |
| nginx upstream | **Safe.** `set $upstream_sentinel http://sentinel:8060;` + `proxy_pass $upstream_sentinel;` with `resolver 127.0.0.11 valid=10s` — the variable form defers DNS to request time, so the dashboard container still starts when the name does not resolve |
| `depends_on` | **Not safe.** `dashboard` declares `sentinel: {condition: service_healthy}` |

**The correction.** An earlier draft of this document claimed "stopping the container
is a supported state today." That is half true and the half that is false is the half
that matters:

- **Runtime stop is tolerated.** `depends_on` governs start ordering only, so
  `docker compose stop sentinel` on a running stack leaves the dashboard up and the
  briefing degrading gracefully. This part holds.
- **Cold start is not.** `docker compose up` blocks the dashboard until Sentinel
  reports healthy. Sentinel's healthcheck is TCP-only
  (`echo > /dev/tcp/localhost/8060`), so it passes as soon as the port is listening —
  but a Sentinel that cannot start at all takes the dashboard with it.

I produced the wrong answer first because I parsed `depends_on` with
`list(dep) if isinstance(dep, dict) else dep`, which prints the service names and
silently discards the `condition:` under each one. The names looked like a plain list,
so I read it as plain start ordering. This is the same error class as reading
`MULTIHOP_ENABLED` as proof Neo4j was optional: **a config whose semantics were
inferred from shape rather than read.** Recording it here because the lesson is the
document's whole purpose.

Removal would therefore also delete a real (if minor) startup coupling — a point in
favour of removal, not against it. It does not change the verdict, because the
coupling costs nothing while Sentinel works.

## Area 2 — Consumer surface

73 references across 23 files, but the distribution is what matters:

| Consumer | Sites | Nature |
|---|---|---|
| `sentinel/` itself | 1,324 LOC (**787 app / 537 test**) | Deleted wholesale |
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

## Area 5 — Sentinel is a documented product surface

This is the finding that turns the verdict from "cheap either way" into "leave it,"
and I had it filed under "nothing" until it was challenged.

`docs/INTEGRATIONS.md` publishes Sentinel to customers as one of four registerable
MCP endpoints:

| Service | URL |
|---|---|
| FirekeepSentinel | `http://<VPS_IP>:8060/mcp` |

…and names it explicitly in the product ladder: *"Full platform experience: add
Sentinel for environment awareness."* The client kit reinforces it — `firekeep install`
renders a `firekeep-sentinel` MCP server into every runtime's native config, and the
global agent instructions tell agents to call `sentinel_push_event` to record
observations.

So the inbound side is not empty. `sentinel_push_event` is an **instructed agent
behaviour**, not just a display feed, and any customer following the published
integration guide has Sentinel wired into their agents' configured toolset. Removing
it breaks a documented contract and silently drops a tool that instructions still tell
agents to call.

That is fixable — docs get edited, adapters get re-rendered — but it converts the
work from "delete a leaf" to "deprecate a published surface," which carries a notice
period rather than a commit.

## The two product decisions (for whenever this is revisited)

1. **Does a memory product ship infrastructure monitoring at all?** Every customer
   who would run this already runs something that does container and git observability
   better. Keeping Sentinel means maintaining a weak second copy of a solved problem.
2. **If it goes, does the alert path survive?** Relay broadcast on error is the one
   piece with no equivalent elsewhere. It could move to a ~40-line webhook receiver on
   Cortex, or be dropped.

## Recommendation: defer

The measurements point one way and the earlier draft of this section pointed the
other. It recommended removal and then spent a paragraph arguing removal buys nothing
— an equivocal verdict in a document whose entire purpose is to prevent a second
Neo4j-shaped misjudgment. Resolved in favour of the evidence:

- 1,324 LOC, green suite, zero write-path coupling → **cheap to keep**.
- No eval, pattern, or memory path reads it → **removal changes no output**.
- Leaf shape → **removable later at the same price**. Nothing is lost by waiting.
- Published to customers as a supported endpoint → **removal costs a deprecation**,
  which is strictly more than removing it costs today in benefit.

Deleting a working, isolated, already-tested, already-documented component is not on
the path to a sellable product. It shrinks a diagram.

**Scope note.** Sentinel's existence is not a defect found in the codebase — it is a
scope preference from a design document, and one whose companion proposal (Neo4j) has
already been reversed on survey. Treat removal as a product decision awaiting an
explicit call, not as outstanding work.

**Nothing here needs a further measurement.** Neo4j's open question was empirical
(does the graph leg beat pure vector on recall?) and is still unanswered. Sentinel's
is a product-scope question, and the facts are now all in hand.
