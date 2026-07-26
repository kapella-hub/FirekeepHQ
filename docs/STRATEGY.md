# Firekeep — Strategy

_What makes it one-of-a-kind, and how to take it to the next level. Steering doc; revisit before adding any new subsystem._

## Positioning (one sentence)

**The self-hosted, GPU-free shared brain a small engineering team's coding agents write to and read from — and that measures whether what it recalled actually made the next session go better.**

## The moat (be honest about what it is)

Firekeep-**the-product** has almost no moat — every feature (MCP tools, the client kit, leases, the Decision Board, the Gateway) is copyable in a week, and Anthropic will commoditize half of them in the harness. Firekeep-**the-instance**, run inside a team for a year, has a large one: **a private, human-approved corpus of skills and validated strategy patterns welded to your codebase, your Confluence, your contributor identities, and your org's failure modes.** A competitor can steal every line of source and still not have *your team's* trace. A departing engineer can't carry it out; a rival tool can't bootstrap it.

The unique intersection nobody else holds: **the trace + the fleet** — a complete behavioral trace of a *team's* coding agents (replay) AND a swarm of strong client models allowed to reason over it (the connected agents). That is the one-of-a-kind ground.

**What is NOT a moat** (stop treating these as the product): memory tech (Mem0/Zep out-engineer recall — don't enter that fight), coordination primitives (harness + MCP absorb them), the multi-runtime install kit (differentiated plumbing, but plumbing), CPU-only (a clever constraint, not a moat), self-hosted (a *qualifier* that picks your buyer, not a moat).

## The one defining bet

Organize everything around the **closed learning loop that proves memory's worth**:

> capture every session (replay) → mine into skills + observations → deliver via the briefing → **measure whether the delivery improved the next session** → redeliver only what helped.

"Memory for agents" is commodity. "We measure whether the memory made the next agent measurably better, and reinforce only what does" is a claim almost nobody can make — because it requires having instrumented the whole surface *before* there was anything to measure. You already paid for that.

**The catch:** today the loop validates at ≥25 sessions and A/B-splits volume you don't have — it's a promise, not a product. **Fix: make it pay off at N=1** (see `docs/superpowers/specs/2026-07-12-n1-learning-loop.md`).

## The two signature moves (both are wiring, not new subsystems)

1. **Fleet-as-GPU — self-maintaining memory on borrowed compute.** A beat tick enqueues *reasoning jobs* onto Relay's task queue ("re-author these 5 stale skills against current code," "merge these 3 near-dup memories," "distill this abandoned session into a handoff"). The next agent to start a session claims one, generates with **its own** model, and writes back via `skill_create`/`memory_learn`. The server only routes + stores. Every primitive already exists (task queue, `skill_create`, client-generation, Celery beat, replay) — this is why you **freeze, not delete, the task queue.** The moat maintains itself, forever, with no GPU. This is the moonshot and it is uniquely yours.

2. **Memory as a guardrail, not a notepad.** When a validated skill or risk-hotspot covers a touched file, the pre-edit gate hands the diff + that skill (as a rubric) to the *client* model and returns rethink/block. The lesson the team learned once, from a real traced incident, now physically stops the next agent from repeating it. The `pre_tool` hook + Agent Gateway + pattern risk-cards all exist — this fuses them. Memory becomes something that gets *harder to breach with every incident.*

## Double down / freeze / kill

**Double down:** replay-as-substrate · human-approved skills + draft queue + Confluence collectors · the briefing as the delivery surface (carrying N=1 observations, not only N=25 patterns) · the Decision Board (it *retrieves from* team memory, so it feeds the flywheel) · CPU-only / client-side generation (it's the architecture, honor it) · Bridge shadow state.

**Freeze / kill / narrow (name the sacred cows):**
- **Freeze the Pattern Engine's _statistics_** (promotion thresholds ≥10/15/25, A/B treatment/control, chi-square/Cohen's h experiments) behind a flag. Keep the *detectors* surfacing descriptively. Kill the stats that need scale, not the mechanism.
- **Kill every server-side generation path** (CPU knowledge-classify, server skill-synth). If the server ever needs to *think*, you've lost.
- **Neo4j multi-hop → probation.** You ripped the Corpus entity graph after auditing zero entities; apply the same knife — prove graph+vector beats pure vector on recall *with your own evals*, or cut the container.
- **Narrow Relay** to leases + presence + DM (freeze, don't delete, the task queue — it's the moonshot substrate). **Narrow the Gateway** to the deny-list + the guardrail move.
- **Symdex** = connective tissue only. Zero marketing, never framed as a code-intelligence product.

## Roadmap — three horizons

**NOW (weeks — sharpen the core):**
1. Make the loop pay off at **N=1** — split *observed* from *validated*; surface a per-session quality read + one grounded tip into the very next briefing. Drop the ≥25 gate for *surfacing*.
2. Freeze the statistical machinery behind a flag.
3. Kill CPU-side server generation.
4. Put Neo4j multi-hop on probation (measured decision in weeks).
5. Narrow Relay + Gateway (freeze the task queue).
6. **Dogfood** — use memory + skills + collectors + briefing on your own Firekeep work every day; track recall hit-rate, not feature count.

**NEXT (months — the differentiated moves):**
1. **Three teammates writing + recalling daily for 60 days** (the un-fun, load-bearing work). Measure recall hit-rate + skill-approval throughput.
2. **Memory as a guardrail** — fuse validated skills/risk-patterns into the Gateway as enforcement.
3. **Close the causal loop** — cross predict→reconcile with tip-shown/withheld for real failure-*prevention* evidence.
4. Get the office Confluence collector live.

**BET (the moonshot):** **Fleet-as-GPU** (above). Secondary: a **doc↔behavior drift detector** — cross collectors (documented procedure) with replay (observed successful behavior) to flag stale docs / risky drift. Only Firekeep holds both signals.

## The gating risk — and the antidote

**You fail by building subsystem #11 instead of landing user #3.** The moat only compounds if a disciplined team writes and recalls *daily*; the two forces that starve it are **scope-sprawl** and **voluntary-logging**, and scope-sprawl wins.

**Antidote (both required):**
1. **Declare the loop the product; everything else is a plugin in service of it. Freeze new subsystems.** Redirect the reclaimed attention to adoption: dogfood at N=1, then three teammates for 60 days, measuring recall hit-rate.
2. **Attack discipline-dependence structurally** — see below. Don't rely on "MANDATORY" in CLAUDE.md; compliance you have to scream for is the failure mode, not the fix.

## Discipline without decree (fixing the "MANDATORY" problem)

The substrate exists only if agents actually log. "MANDATORY" is an imperative aimed at a probabilistic system — it fails under load and causes alert fatigue. Replace it with three layers:

1. **Deterministic capture (hooks, not the model).** Anything code can capture, code captures. The `stop` hook itself records the session's durable facts (branch, commits, diff, tests) and **enqueues a distill job for the fleet** — logging *happens* rather than being *requested*. Model cooperation becomes the ceiling (rich skills/decisions), never the floor.
2. **Immediate visible payoff (operant, not imperative).** The N=1 briefing shows "last time you hit X, here's the fix you logged" — the logger is rewarded the same session and logs again. Reinforcement beats decree.
3. **Instrument the gap, gently.** Surface recall hit-rate / logging trend (from the existing `/admin/untagged-calls` + discipline section) as a signal — not 15 lines of MANDATORY.

**Meta-fix for CLAUDE.md:** cut the wall of MANDATORY to a short list of load-bearing behaviors, each with a concrete trigger + its why-in-self-interest ("after a hard-won fix → `skill_create`, because your next session recalls it"). Structure carries the load; instructions explain the reward.
