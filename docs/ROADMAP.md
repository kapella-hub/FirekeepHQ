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

## 4. The Institution Thesis (candidate — UNPUBLISHED direction record)

"Agents come and go. The Keep stays." Agent vendors sell staff; Firekeep is
the institution that employs them — archive (inheritable household memory),
HR (per-agent trust ledger), **the badge system** (a local capability broker:
third-party MCP servers mounted behind the gateway, typed capabilities,
action-typed policy — added by same-day review, which caught that a ledger
without an owned action path is advisory reputation, not earned autonomy),
constitution (Living Instructions applied to domain rules), bake-offs
(per-agent eval slices), premises (desktop-lite, possibly an appliance).
Earned autonomy = ledger (measures) + broker (enforces), together only.
Full pillar-to-machinery mapping, enforcement boundaries stated honestly,
anti-goals (no doing-agent, no credential custody, finances LAST),
sequencing, and the trust-ledger round-1 scope:
[`docs/superpowers/specs/2026-08-14-institution-thesis.md`](superpowers/specs/2026-08-14-institution-thesis.md).
Nothing from this section appears on firekeep.ai until its gate holds — the
dashboard shows it before the site says it. First domino when work starts:
trust ledger round 1 (visibility only, the house pattern).

## 5. Tiers and Packs (decision record, 2026-08-15 — decided, not yet built)

Five decisions taken on the decision board, shaping the packaging pivot.
The frame: **the tier answers "who governs this Keep"; deployment answers
"where does it run"; they are orthogonal.** Tiers are never separate
binaries or unlockable tool lists — consistent with the 2026-08 licensing
decision (one product, BUSL, the licence is the entire boundary, nothing
enforced technically). Never metered, at any tier: devices, agent
identities, terminals, actions, memories, background workers. Pricing
anchors to human members.

1. **The middle tier is named Firekeep Teams** (not Pro — "Pro" reads as a
   more powerful individual product; what is sold is shared continuity
   between people). Personal = private continuity for one person, the
   whole product, free. Teams = shared operating memory, per-member.
   Enterprise = governed institutional continuity (SSO/SCIM, policy
   hierarchy, retention/audit/legal hold), sold as an annual design-partner
   engagement while the governance list is built — the page promises the
   category, not the full list.
2. **Personal Household is a reserved cheap paid SKU under Personal** —
   the current Additional Use Grant covers one natural person, so a second
   family member's identity is commercial use today. Grant language gets
   amended at the next lawyer touch (LICENSING.md's own rule: lawyer
   before first sale). Nothing is built; the licence stays the boundary.
3. **Client capabilities become Packs behind a manifest-driven gateway
   registry** (product name: Packs; `firekeep pack list/add/remove/
   update/doctor`). Symdex is the first pack and becomes **opt-in via
   `firekeep pack add symdex`** — this REVERSES the 0.1.4x always-installed
   decision, by explicit owner choice over the reviewer recommendation of
   default-on. **The rationale (stated 2026-08-15, and it is the premise
   for the whole pack model): Personal targets GENERAL use, so no domain
   pack is privileged — code intelligence is one peer among documents,
   email, calendar, all delivered on demand.** A general-use product whose
   code pack auto-installs has its identity decided by its first plugin.
   The today's-funnel concern (current users are all coding agents) is
   handled by SUGGESTION, not defaults: install detects a dev-shaped
   machine and the doctor/briefing surfaces the one-line add command —
   no new install questions, no privileged pack. Consequence: Documents
   is the strategically important second pack (first proof Personal is
   not a dev tool, buildable now over the existing corpus ingest); the
   still-missing half of general-use Personal is a HOST surface a
   non-developer actually opens (consumer MCP hosts near-term; the parked
   Desktop bet long-term). Migration rule when it lands: an update never removes a
   capability an install already has — existing installs keep symdex;
   opt-in applies to new installs. Pack permissions are DISCLOSURE
   client-side and scoped per-pack API keys server-side; no sandboxing is
   claimed that does not exist. Third-party packs wait for the capability
   broker (§4). The pack list is not announced — the system ships with
   Symdex as proof; the cheapest honest second pack is Documents (a thin
   local-folder-watch client over the existing corpus ingest).
4. **Pack milestone 1 starts when the install-story stream lands** its
   overhaul (it owns gateway.py/bootstrap/cli.py this week): manifest
   registry replacing the LOCAL_SERVERS tuple, symdex behind it (still
   bundled as the checksum-verified wheel — the signed supply chain
   stays), pack list/doctor, per-pack scoped identity, hooks and
   auto-index behind the pack boundary. The two-question install
   experience must not regress — no new install-time questions.
5. **Firekeep Desktop (embedded no-Docker core) is parked as explicit
   research** — replacing Neo4j/Qdrant/Redis/Ollama with embedded
   equivalents is a persistence-and-inference rewrite, not packaging. Off
   the pricing page, off roadmap promises. Personal today is honestly
   "Docker on any machine you own; laptops and desktops enrol into it."
   A Firekeep-operated cloud is likewise a different future product
   (multitenancy, ops, compliance, datastore licensing) and is not implied
   by the word "cloud" anywhere.

Nothing above appears on firekeep.ai until it exists to the §4 standard:
the dashboard shows it before the site says it. Docs and CLAUDE.md keep
describing CURRENT behaviour (symdex always-installed) until the pack
milestone actually lands — a doc that describes the decided future as the
present would simply be wrong.
