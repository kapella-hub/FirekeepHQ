# Workspaces, Entitlements, and Onboarding

**Status:** design, approved 2026-07-30
**Sequencing:** third. Depends on `2026-07-30-single-connection-config-design.md` (lands
first) and merges with `2026-07-30-client-enrollment-join-codes-design.md`, whose
`invite` becomes "invite a member" with a seat check on it.

## Purpose

Make Firekeep free for one developer's entire personal agent workflow, and paid at the
moment it starts connecting people. One codebase, one image, one wheel; the difference
is a signed entitlement the **server** holds, so the client install is byte-identical
either way.

---

## 1. The identity model

Three things that are routinely conflated, and must not be:

| Concept | What it is | Metered? |
|---|---|---|
| **Member** | A human. Owns a seat. | **Yes — this is the only meter.** |
| **Credential** | An API key or OAuth token belonging to a member. One per device, revocable individually. | No — unlimited |
| **Agent runtime** | A running Claude Code / Codex / kiro / OpenCode process, labelled by `agent_id` | No — unlimited |
| **Client** | The integration surface (adapter + MCP entry) | No — all clients, always free |

A free member may run any number of terminals, machines, `FIREKEEP_AGENT_ID`s and
coding clients simultaneously. **Team is unlocked when a second member is invited or
approved**, and nothing else.

### 1.1 Why agent identities must never be metered

Two things in this codebase make one person hold several agent identities at once:

- `night-shift` runs under its own identity — `nightshift.py:71` defaults to
  `"night-shift"`. It is a free, single-player feature; metering agents would have it
  consume the only slot.
- `CLAUDE.md` instructs users that "the supported partition for genuinely concurrent
  work is a distinct `FIREKEEP_AGENT_ID` per terminal."

### 1.2 Presence is telemetry and must never gate anything

`relay/app/presence.py:17` sets `ACTIVE_THRESHOLD = 600`. "Active" means *a heartbeat
within ten minutes*, and crash detection is explicitly "an active session with no
presence entry." Metering concurrency on this would lock a paying-nothing user out of
their own tool for up to ten minutes **because their editor crashed** — a paywall
triggered by a fault, using a signal the system itself documents as best-effort.

**Invariant, and it needs a guard test:** no presence value may be read by any
authorization or billing path. Presence is operational telemetry only.

---

## 2. Verified principal — the first milestone

Today the verified identity is used for **authorization but never for attribution**:
`auth/asgi.py:113` attaches `{agent_id, scopes, key_id}` to the request scope and
`auth/asgi.py:192` (`require_scope_asgi`) reads it for scope gates — but
`cortex/app/main.py:1129` and `:1205` persist the **self-asserted `X-Agent-Id` header**
as memory provenance. Any valid key can therefore write memories as anyone.

That is a live security gap, and fixing it is milestone 1. It must be a **vertical
slice**, not a set of unread fields: introducing `workspace_id`/`member_id` that nothing
consumes is dead-model work whose defects surface only when something finally depends
on them.

1. A credential (API key today, OAuth token later) resolves to
   `{workspace_id, member_id, credential_id, scopes}`.
2. The request context exposes that principal to application code — one accessor, not
   per-handler scope-dict spelunking.
3. Cortex memory **writes** persist workspace/member provenance from the principal, not
   from headers.
4. Cortex **reads** filter by the verified workspace.
5. `X-Agent-Id` survives as an **untrusted runtime label** for observability. It may
   appear in traces and dashboards; it may never select a workspace, member, or
   permission boundary.

Then the same context is carried into Bridge, Relay, replay and the dashboard APIs.
**Only after membership is authoritative** does the entitlement check go in, at one
decisive operation: creating or accepting a second membership.

### 2.1 Milestone 1 is single-workspace-per-deployment

Every deployment has exactly one workspace. The principal carries `workspace_id`, writes
persist it, reads filter on it — but there is only ever one value, so isolation defects
cannot bite. This buys the security fix and the entitlement hook with none of the
multi-tenant blast radius.

**True multi-tenancy is a hosted-product decision, deferred.** Self-hosted team
customers each run their own server and never share a datastore. The isolation work
below is required *only* if two tenants ever share one deployment, and each item is a
silent-failure risk rather than a loud one:

- **`recall_streaming` is a second read path** (`cortex/app/engine/rag.py:306`), already
  documented as diverging from `POST /memory/recall` (no lifecycle/OWM multipliers, no
  `memory_ids` stamping). Filtering one path and not the other turns a known divergence
  into a tenant-isolation hole. Both paths must share one filter implementation.
- **Neo4j multi-hop traversal bypasses payload filters.** A Qdrant filter constrains the
  *seed* set; 3-hop traversal then follows edges wherever they lead. Vector filtering and
  graph partitioning are separate problems.
- **Background workers hold no request context.** `run_memory_agent`'s dedup pass at
  `DEDUP_SIMILARITY_THRESHOLD=0.78` would merge two tenants' similar memories into one
  record — data leakage by merge. Same class in GC eviction, OWM scoring, skill
  staleness, and night-shift write-back.
- **Vault, replay and corpus** are likewise global keyspaces today.

### 2.2 Migration ordering is not optional

Existing memories carry `agent_id` and no `workspace_id` (~3.9K records already
backfilled once by `cortex/scripts/backfill_legacy_agent_id.py`). Enabling a read filter
before the backfill completes makes every existing memory vanish from recall.

**Order: backfill → verify counts match pre-backfill totals → then enable filtering.**
Add a Qdrant payload index on `workspace_id` in the same change, or recall degrades as
the collection grows. The backfill is idempotent and assigns all pre-existing data to
the deployment's single workspace.

---

## 3. Entitlements

A signed document the server holds. The client never evaluates it.

```json
{
  "workspace_id": "…", "customer": "…", "plan": "personal|team",
  "max_members": 1, "capabilities": ["…"],
  "issued_at": "…", "expires_at": "…"
}
```

- **Ed25519-signed, verified offline.** No phone-home. This matters for air-gapped
  buyers and matters more for a product that reads your codebase and runs a secrets
  vault — "we cannot see your usage" is a sales asset, not just a principle.
- **Absent entitlement = Personal plan.** A fresh install is free and works immediately
  with no account, no key, no network call to us.
- **Fails open, always.** An expired entitlement degrades to Personal; it never locks
  anyone out of memory they already wrote. There must be no code path capable of
  bricking accumulated context over a lapsed renewal.
- Expiry warns for 30 days before degrading, surfaced in the briefing and `doctor`.
- Applied via the dashboard Licence page, `firekeep-admin licence apply`, or
  `FIREKEEP_LICENCE` for a Kubernetes secret.

### 3.1 The free/paid boundary

| Personal (free, 1 member) | Team (paid, N members) |
|---|---|
| All four services; **all clients** — Claude Code, Codex, kiro, OpenCode | `max_members > 1` |
| Unlimited devices, credentials, agent runtimes, terminals | `/memory/contributors`, `/memory/handoff` |
| Memory, recall, skills, corpus, knowledge ingest | Cross-agent patterns (`/patterns/relevant?exclude_agent=`) |
| Sessions, replay, evals, patterns, night-shift | DMs, bulletin and broadcast **between members** |
| Decision board, symdex, own presence, leases, tasks | Briefing's `cross_agent` section |

Every paid capability is one that is **meaningless alone** — handing off to yourself,
learning from your own other agents, messaging yourself. The free tier carries no
artificial quota: no memory cap, no retention cliff, no client restriction.
**Agent-agnosticism is never gated** — it is the clearest differentiator against
IDE-locked tools and costs nothing to give away.

### 3.2 The single enforcement point

`POST /enroll` is the only place a new identity enters the system, so the seat check is
one condition on a path that already exists. Enrolling an additional **credential for an
existing member** is always free; enrolling a **new member** requires
`members < max_members`, and the refusal names the plan and the upgrade path rather
than returning a bare 403.

---

## 4. Onboarding — three commands, none of which asks "personal or team"

```bash
firekeep init                  # provision a new local / self-hosted server
firekeep join <invite-code>    # attach to an existing server, invite in hand
firekeep login <server-url>    # attach when there is no invite code
```

Neither attach path asks about plan or profile. **The server authenticates the user and
returns workspace, membership, endpoint and entitlement**; the client writes what it is
told. Plan is a property of the server, never a question for the installer — the
previous design's `[1] personal [2] office` prompt is precisely the failure this avoids
repeating.

`join` is the flow specified in the enrollment design. `login` is its no-invite sibling:
against a hosted or SSO-backed server it runs **OAuth 2.1 authorization-code with PKCE**,
discovering the authorization server via MCP resource metadata rather than having anyone
copy a long-lived key. `init` is the only command that provisions rather than attaches.

The installer then asks **one** question — which coding clients to configure — renders
each client's native configuration without overwriting user settings (the existing
marker-delimited upsert behaviour), and registers **one** MCP entry.

---

## 5. The MCP gateway

Today the adapters render **six** MCP servers per client (`adapters/base.py:18-19`):
`firekeep-cortex`, `-bridge`, `-sentinel`, `-relay`, `-symdex`, `-decision`. A user must
not have to understand or repair six entries.

Collapse to **one local `firekeep` stdio server** that aggregates all of them. This is an
evolution rather than a rewrite: `firekeep-shim` (`pyproject.toml:20`) already proxies the
four remote services and is parameterised by service name; the gateway hosts all four in
one process and additionally fronts the two local stdio servers.

- **stdio locally** — the broadest client denominator, and what every supported client
  handles today.
- **Streamable HTTP remotely** — for hosted/team, a client that supports remote MCP gets
  a single `https://…/mcp` endpoint instead of any local process.
- **Degrade per-backend.** Today a Cortex outage leaves the other five servers working;
  the gateway must preserve that. A backend that is down removes its tools and reports
  why — it must never fail the whole gateway.
- The gateway is where an entitlement refusal becomes **legible**: a Team-only tool
  returns an explanation in the model's context rather than a bare 403, and is hidden
  from the tool list entirely on the Personal plan.

---

## 6. Commercial licensing

Source-available under FSL or BUSL-style terms: readable and self-hostable for your own
use, not resaleable as a competing service, converting to an OSI licence after a fixed
term. Paired with signed offline entitlements as above.

Stated honestly in the spec so it is not re-litigated: **a fully OSI-open server cannot
prevent removal of a feature gate**, and neither can a readable Python wheel or an
inspectable Docker image. Entitlement checks are a speed bump plus a legal artifact.
Enterprises buy support, indemnity and compliance. The counter-argument for
source-availability is stronger than the enforcement argument against it: this product
ingests source code, stores session traces and runs a secrets vault, and "you may not
read it" is a real obstacle to a first sale.

---

## 7. Milestones

1. **Verified principal, vertical slice.** §2 steps 1–5, single workspace, entitlements
   unlimited. Ships a security fix (attribution stops being self-asserted) with no
   user-visible change.
2. **Carry the principal into Bridge, Relay, replay and dashboard APIs.**
3. **Signed entitlements**, Personal and Team, with the seat check at membership
   creation only.
4. **`init` / `join` / `login` plus the MCP gateway**, with adapter coverage tests across
   all four clients.

Multi-tenancy (§2.1) is explicitly **not** in this sequence and belongs to the hosted
product.

---

## 8. Testing

- `test_principal_resolution.py` — a credential resolves to workspace/member/credential;
  an unknown credential resolves to nothing; `X-Agent-Id` cannot influence any field of
  the principal.
- `test_memory_attribution_verified.py` — **the security guard**: a write carrying
  `X-Agent-Id: someone-else` persists the *principal's* member, not the header. This is
  the defect milestone 1 exists to close.
- `test_reads_filtered_by_workspace.py` — both `POST /memory/recall` and
  `recall_streaming` apply the same filter, asserted through one shared code path so the
  documented divergence cannot re-open as an isolation hole.
- `test_presence_never_gates.py` — **invariant guard**: no authorization or entitlement
  path imports or reads presence. A static check on the call graph, so a future
  "convenient" concurrency check fails CI rather than shipping.
- `test_entitlement_fails_open.py` — absent, malformed, unsigned, and expired
  entitlements all degrade to Personal and none denies a read of existing memory.
- `test_seat_check.py` — a second **credential** for an existing member is free; a second
  **member** is refused on Personal with a message naming the plan and the upgrade path.
- `test_workspace_backfill.py` — idempotent; recall totals before and after are equal;
  filtering disabled until the backfill is marked complete.
- `test_gateway_degrades.py` — one backend down removes only its tools; the gateway still
  serves the rest and reports the failure.
- `test_adapters_single_entry.py` — each of the four adapters renders exactly one MCP
  entry, and re-rendering removes the six legacy entries without touching foreign config.
