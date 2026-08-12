# Roadmap promises — and the decisions behind them

Two forward-looking promises are published on firekeep.ai (the dashed "Roadmap"
rungs of the product ladder). This file records what each one means, what was
deliberately decided about how to build it, and what unlocks it — so the
promise on the page never drifts from the intent in the repo.

Decided 2026-08-09. Neither promise is scheduled; both are sequenced behind
revenue on the one shipped product.

## 1. Linked instances (rung 05 — "many teams")

Multiple Firekeep servers sharing knowledge across an organisation, so what one
team learns is recallable by another.

Nothing is designed yet, deliberately. The tenancy model this must compose
with (`workspace_id` as the unforgeable boundary derived from the verified
principal) and the provenance model (per-memory member/agent attribution)
already exist, and any federation design starts from those two facts rather
than from transport.

**Unlocks:** more than one real deployment with a reason to share (the first
customer with two teams, or customer↔customer knowledge exchange demand).

## 2. Domain profiles (rung 06 — "many kinds of work")

Separate experiences over one shared brain: a coding profile today; document
and research profiles ahead.

### The decision: profiles, not clients

The evaluated alternative was separate domain clients (Firekeep Code / Docs /
Research), each a purpose-built application. Rejected — recorded here so it is
not re-litigated from scratch:

- **Firekeep deliberately does not own the agent experience.** It rides
  Claude Code, Codex, Kiro, OpenCode. Standalone domain clients would flip the
  company from "memory layer behind your tools" to "agent application vendor"
  — three UIs to design, support and keep alive. The runtime-agnostic position
  is the structural advantage; profiles keep it.
- **Everything a "domain client" actually needs is a profile of the existing
  kit**: which gateway tools are exposed, which instruction block is rendered,
  which retrieval policies and memory conventions apply, which outcome signals
  are wired, which guardrails run. `firekeep install --profile docs` on the
  same kernel (bootstrap, venvs, hooks, gateway, adapters — it already exists).
- **No per-domain memory schemas.** Domain variation lives in payload
  conventions and namespaces on the ONE recall path. A schema fork is the
  namespace-partition class of bug at product scale: storage advice and
  retrieval have to agree, and the live store already proved what happens when
  they don't (146 memories unreachable). See root `CLAUDE.md`, "namespace is a
  CATEGORY".
- **Outcome signals gate ambition per domain.** Even code's outcome signal —
  the best available, with tests and CI — measured degenerate (the reason
  Knowledge Autopilot round 1 ships visibility, not automation; see
  `docs/guides/knowledge-autopilot.md`). Docs ("accepted edits, approvals")
  and research ("evidence quality, reproducibility") start weaker still. No
  profile gets lifecycle automation designed against a signal that does not
  yet exist for its domain.

### The enabling investment is linkage, not UI

The cross-domain behaviors that make the shared brain worth promising — a
runbook checked against how the code actually behaves, research informing an
implementation, an implementation keeping its documentation current — need
**stable entity references across stores** (corpus documents ↔ Symdex symbols
↔ replay traces ↔ memories), not new applications. Today that linkage is free
text and naming conventions. This is server/platform work that improves the
current product immediately and is the real prerequisite for rung 06's story.

### Sequencing

1. **Now: nothing.** Close sale blockers, get customers on the one product.
2. **Cheap and soon:** profile support in the kit (tool subsets + instruction
   packs + retrieval conventions per profile). Docs is the natural second
   profile — corpus ingest, docs→skills and the review inbox already exist,
   so a Docs profile is mostly curation plus dashboard.
3. **With revenue:** the entity/linkage layer, because it compounds everything.
4. **Research: deferred hardest.** Claims/citations/datasets schemas are a
   product category of their own, the outcome signal is the weakest, and the
   buyer differs from the current one. A paying customer drags it forward,
   not a roadmap.

## 3. Living Instructions (rung 05 — "the system itself"; round 1 + the round-2 measurement contract SHIPPED)

The instruction layer that measures itself — per-instruction compliance from
replay, fleet-authored rewrites under human verdict, briefing-delivered
variants, validated by the frozen experiment framework. Design and the
founding live measurement (2026-08-11 compliance baseline, six instructions,
32 sessions):
[`docs/superpowers/specs/2026-08-11-living-instructions-design.md`](superpowers/specs/2026-08-11-living-instructions-design.md).
It is wiring over existing machinery, not a new subsystem.

**The publishing gate held and is closed**: round 1 (`GET
/autopilot/compliance` + the dashboard table) shipped in server v0.4.1 and
was verified live before the rung went on firekeep.ai (2026-08-11). First
live read: every instruction trended UP between the older and newer halves
of the eval window (recall 44%→69%, write 31%→63%, recall-used 13%→44%,
ctx 56%→69%) — behavior only, no quality claim; rounds 2–3 exist to test
causation properly.

**The round-2 measurement contract shipped** in server v0.4.3 + client
0.1.41 (2026-08-12, deployed and verified live before this line was
written): instruction-content hashes stamped into the rendered block,
five attribution headers, per-runtime compliance slices, and
exposed/not-exposed/unknown states — with every pre-0.1.41 session
honestly `unknown`, forever. Shipping it surfaced two corrections now
recorded in the spec: the ctx row measured agent discipline all along
(the review finding that said otherwise was wrong), and the armed
experiment's "second channel" (Cortex MCP handshake) never reached kit
users — 0.1.41 restores it in the same release as the receipts, so the
exposure change is attributable, not a confound.

**Unlocks for rounds 2–3** (rewrites + A/B): declared-prediction traffic
from instructed clients (the 0.1.40/0.1.41 natural experiment, its two
exposure epochs now distinguishable by receipt), and enough session
volume for the frozen stats to resolve an effect.
