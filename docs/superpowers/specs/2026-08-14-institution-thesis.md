# The Institution Thesis — agents are hires; Firekeep is the employer

_Status: DIRECTION RECORD (2026-08-14), unpublished. Nothing here is promised
on firekeep.ai; the publishing gate for any pillar is the same as every rung
before it — the dashboard shows it before the site says it. Origin: analysis
of xAI's Grok Bot consumer launch (video review, 2026-08-14) and the question
"how do we reach more people — general-public client, local packaging?"_

## The observation

Every agent vendor is selling **staff**: increasingly capable workers rented
by the month — Grok Bot's "named teammates" ($200/mo on a dedicated VM),
Claude, Codex, whatever ships next quarter. Staff turn over. The model gets
deprecated, the subscription lapses, a better agent launches — and everything
that worker knew walks out the door, or stays locked in the vendor's VM.

Nobody is selling **the institution that employs them**: the thing a company
still is after you subtract the current staff — the archive, the rules, the
track records, the succession plan, the premises. For coding teams, Firekeep
already IS that thing. The thesis is that this is also the consumer product,
stated plainly:

> **Agents come and go. The Keep stays.**

The consumer "team" is the household. The expansion vehicle is rung 06's
profiles-not-clients decision — never a separate product, never a separate
memory store. What follows maps each pillar of the institution to machinery
that already ships, with honest effort estimates.

## The five pillars, mapped to what exists

### 1. The Archive — memory that outlives the staff (and the owner)

A household's permanent institutional memory, recallable by whatever agent is
employed this year or in ten. **Exists today**: semantic+graph memory,
outcome-weighted recall, provenance (workspace/member), corpus, vault,
JSONL export/import (`cortex` transfer API). **The extension that no vendor
can copy**: inheritance — an executor role whose access activates on the
owner's terms; the family Keep as a generational asset.
_Effort: the emotional moat is a provenance/access feature, not research —
executor role + scoped transfer + docs ≈ a week. Positioning value exceeds
engineering cost by an order of magnitude._

### 2. HR — trust you can audit, autonomy that is earned

The sleeper killer feature, ~70% shipped. The agent gateway already takes
declared predictions with stated confidence (`action_before`), reconciles
outcomes (`action_after`), and computes calibration (Brier) per session.
What's missing is the **per-agent employment record**: aggregation across
sessions into a track record — "214 declared actions, calibration honest to
±8%, 3 reversals" — and the **earned-autonomy ladder** that consumes it:
every agent starts sandboxed → drafts-only → autonomous, promoted by measured
calibration, demoted by failures, enforced by the policy engine (which
already evaluates 5 rules pre-edit; this adds a sixth keyed on the record).
Grok Bot's answer to "can I trust it with my email?" is one perimeter and
cute eyes. Ours is a probation period with receipts.
_Scoping below — this is the first domino._

### 2b. The Badge System — the capability broker (ADDED by same-day review)

**Correction, external review 2026-08-14 — the original draft carried a
central contradiction.** It promised autonomy *enforced* by policy while
disclaiming ownership of the action path ("integrations are the staff's
problem"). Both cannot hold: if the agent holds the Gmail credential and
calls the tool directly, Firekeep can only score what the agent voluntarily
declares (`action_before` is self-reported) — the employee can bypass the
employer. A trust ledger without an owned action path is **advisory
reputation, not earned autonomy**. The draft's "sixth policy rule" claim was
wrong twice over: no enforcement point, and no vocabulary — `PolicyContext`
is file-oriented (`file_path` is its required field,
`cortex/app/policy/engine.py:27`); it cannot express `email.send`.

The institution must own the doors and badges, not merely the personnel
files:

```
Agent → Firekeep gateway (capability broker) → typed capabilities → third-party MCP servers
        email.read · email.create_draft · email.send · calendar.create · finance.read_transactions
```

This is an extension of shipped architecture, not a new system: the local
gateway already mounts named backends (4 remote + 2 local stdio), namespaces
their tools, injects auth per call, and rewrites arguments in flight
(`_BridgeSessionTap`). The broker adds: third-party MCP servers mounted
BEHIND the gateway instead of registered directly in the client; a
tool→capability taxonomy; a `PolicyContext` v2 carrying capability, resource
and verb (draft vs send); and a policy consult on every brokered call.
**Credentials are untouched**: the third-party servers keep their tokens
locally (OS keychain / their own stores) exactly as they do today — Firekeep
gates the CALL, never holds the credential. The anti-goal sharpens rather
than falls: no cloud OAuth hub, and no credential custody at all.

**Enforcement boundary, stated honestly, strongest first:**
- *Consumer MCP-only runtimes* (Claude-Desktop-class — no shell): the
  gateway is the complete tool surface → real enforcement. This is the
  household profile's world, which is why the consumer story works.
- *Shell-bearing dev runtimes*: partial — the pre-edit hook and leases
  enforce the file path; brokered capabilities enforce what routes through
  them; an agent with Bash can go around, so the ledger is
  enforcement-plus-advisory there. Stated, not hidden.
- *The owner*: can always bypass (register a tool directly in the client).
  Correct and intended — badges bind the staff, not the building's owner.

_Effort: the broker is the real cost the original draft hid — gateway
mounting of arbitrary MCP servers + taxonomy + PolicyContext v2 + tests is
WEEKS, not "a policy rule + config." It is also the piece that makes every
other pillar true rather than aspirational._

### 3. The Constitution — house rules that measure themselves

Living Instructions pointed at life instead of code: "draft, never send,"
"nothing over $50 without asking," "medical facts go to the vault."
**Exists today**: the compliance table (frozen predicates, exposure receipts,
per-runtime slices — v0.4.4), the policy engine, the instruction layer.
Domain rules are new instruction contracts measured by the same machinery;
the 2026-08-12 measurement contract was built runtime-agnostic on purpose.
_Effort: per-domain instruction sets + predicates ride the existing
compliance build pattern (the round-1 table was a two-day build). Days per
domain, after profiles exist._

### 4. The Bake-off — agents as auditionable staff

Once memory, rules, and trust records live in the Keep, agents are
interchangeable: same task to a Claude agent, a Grok bot, the next thing;
per-session evals score outcomes; the record decides who keeps the seat.
**Exists today**: agent-agnostic gateway (any MCP client), per-session evals
(10 Tier-1 metrics), per-agent attribution on events. Grok Bot itself can be
a hire — its VM is a Linux box with a terminal; the kit installs there
(untested; 30-minute experiment when an account is available).
_Effort: the comparison VIEW is new (per-agent eval slices — mirrors the
compliance table's by_runtime build). The plumbing exists. Days._

### 5. The Premises — desktop-lite, then the appliance

The install wall is the real consumer barrier, not the client. Sequence:
**(a) Firekeep Desktop lite** — a trimmed single-user compose profile
(smaller embedding model is already a documented option; external-API
generation already supported via LLM endpoint selection; relay/sentinel
optional for a solo box) behind one script. Docker Desktop remains the
dependency initially — acceptable for the current audience, not for consumers.
_Effort: weeks, honest._ **(b) The Hearthstone** — a preloaded mini-PC:
plug into the router, the household has a brain, the Beacon on the lid.
Makes "you own your memory" physical; solves the install wall completely;
proven indie-hardware business shape. _Effort: a prototype is one day on any
mini-PC; the business (inventory, support) is a deliberate later decision._

## What this deliberately is not

Not a doing-agent: Firekeep still runs no models and executes no tasks — the
user's existing AI apps (Claude Desktop, ChatGPT, a Grok bot) are the staff.
Not an OAuth hub: integrations are the staff's problem or curated third-party
MCP servers, never our token store. Not a consumer launch: nothing ships to
"finances" until the trust ladder has hardened on lower-stakes domains — money
is the last domain, not the first. Not a new subsystem: every pillar above is
wiring over existing machinery, per the roadmap's standing rule.

## Sequencing

1. **Trust ledger round 1** (scoped below) — differentiating, mostly built,
   and it hardens the DEV product too: teams want agent probation as much as
   households do.
2. **Desktop-lite packaging** — prerequisite for any wider audience.
3. **First household profile** (email triage or personal research — not
   finances) via rung 06 profiles + third-party MCP servers + a measured
   domain constitution.
4. **Inheritance/executor** — cheap, ships whenever positioning needs it.
5. **Appliance** — when demand justifies inventory.

## Trust Ledger — round 1 scope (visibility only, the house pattern)

Same discipline as Autopilot round 1 and Living Instructions round 1: it
reports, it never gates. No autonomy enforcement until the measurement has
lived in production.

- **Per-agent aggregation**: one bounded scan in the compliance.py mold —
  walk stored evals + gateway reconcile events, aggregate per `agent_id`:
  declared-action count, reconciliation rate, Brier calibration (mean +
  trend), failure/reversal count, sessions observed, first/last seen.
  Unknowns stay unknown: an agent with no declarations has NO record, not a
  bad one.
- **Surface**: `GET /autopilot/trust` (admin, additive) + a dashboard card
  beside the compliance table — per-agent rows, the same honesty notes
  (behavior, not competence; calibration, not correctness).
- **Frozen at birth**: aggregation keys and formulas pre-registered in this
  file's successor spec before the first published number, so later rounds
  compare cleanly — the Living Instructions lesson, applied from day one.
- **Round 2 (corrected by review): the badge system.** Not a sixth rule —
  the capability broker of pillar 2b (gateway-mounted third-party servers,
  capability taxonomy, action-typed PolicyContext v2, per-call policy
  consult). The ledger measures; the broker enforces; **earned autonomy is
  the two together and exists only where both do.** Round 3: autonomy tiers
  — thresholds over the ledger, enforced at the broker, human-set.
- _Estimate: round 1 is a compliance-table-shaped build — API + scan + card
  + tests, a few days. Round 2 is weeks (the honest number the first draft
  hid behind "a policy rule")._

## Gates

Site silence until: the trust table renders on the Autopilot tab (pillar 2),
a lite box installs on a clean machine (pillar 5a), a profile demonstrably
shares one brain with the coding experience (pillar 3). The first public
sentence of this thesis, whenever earned: "Agents come and go. The Keep
stays."
