# Workspaces, Entitlements, and Onboarding

**Status:** design, approved 2026-07-30
**Sequencing:** third. Depends on `2026-07-30-single-connection-config-design.md` (lands
first) and merges with `2026-07-30-client-enrollment-join-codes-design.md`, which gains a
member-invite surface carrying the seat check. Its `invite` and its Devices tab stay
device-only.

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
| **Workspace** | The tenant boundary. Owns members, memories, sessions, credentials. One per deployment in milestone 1 (§2.1) | n/a — the container |
| **Member** | A human. Owns a seat. | **Yes — this is the only meter.** |
| **Device** | An enrolled machine, identified by a server-minted `device_id`. Holds one credential | No — unlimited |
| **Credential** | An API key or OAuth token, identified by a server-minted `credential_id`, belonging to a member and bound to a device. Revocable individually | No — unlimited |
| **Agent runtime** | A running Claude Code / Codex / kiro / OpenCode process, labelled by `agent_id` | No — unlimited |
| **Client** | The integration surface (adapter + MCP entry) | No — all clients, always free |

The chain is **workspace → member → credential → runtime**. Device sits beside
credential, not in the chain: `device_id` is enrollment's unit of revocation and
inventory (`2026-07-30-client-enrollment-join-codes-design.md` §1.12), while
`credential_id` is what the principal carries. Keeping them distinct is what stops a
later implementer folding the machine into the security principal — one member may hold
several devices, and a rebuilt machine gets a new credential under the same `device_id`.

`credential_id` is minted by the server independently of the credential hash
(`2026-07-30-client-enrollment-join-codes-design.md` §1.8) and is resolved through
`auth:cred:<credential_id>`. It is **not** today's `key_id`, which is `key_hash[:16]`
(`auth/keys.py:203`) and becomes client-influenceable the moment the client supplies the
hash. This spec consumes `credential_id` as enrollment defines it; backfilling the
mapping for pre-enrollment records is enrollment's job (`deploy/bootstrap-keys.sh`).

A free member may run any number of terminals, machines, `FIREKEEP_AGENT_ID`s and coding
clients simultaneously. **Team is unlocked when a second member is invited or approved**,
and nothing else.


### 1.1 Why agent identities must never be metered

Two things in this codebase make one person hold several agent identities at once:

- `night-shift` runs under its own identity — `nightshift.py:71` defaults to
  `"night-shift"`. It is a free, single-player feature; metering agents would have it
  consume the only slot.
- `CLAUDE.md` instructs users that "the supported partition for genuinely concurrent
  work is a distinct `FIREKEEP_AGENT_ID` per terminal."
- **Enrollment produces one `agent_id` per machine.** Join codes are strictly single-use
  and one is redeemed per machine
  (`2026-07-30-client-enrollment-join-codes-design.md` §1.10: "A teammate with a laptop
  and a desktop runs `invite` twice"), and the client derives that name from the invite
  label plus the local hostname (§1.12) into `[identity] agent_id`. One member with a
  laptop and a desktop therefore holds two agent identities by design, before running a
  single extra terminal. Metering agent identity would bill a member for owning two
  computers.

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
6. The credential record carries **no `agent_id`**. Enrollment binds credentials to
   devices, not names (`2026-07-30-client-enrollment-join-codes-design.md` §1.12), so
   `auth/asgi.py:113-117` attaches `{workspace_id, member_id, credential_id, scopes}`
   instead of `{agent_id, scopes, key_id}`. Verified repo-wide, the identity dict has nine reader sites. Four read
   `scopes` only and are unaffected: `auth/middleware.py:95`, `auth/middleware.py:148`,
   `auth/asgi.py:197`, `cortex/app/briefing/api.py:66`. One reads `agent_id` and **must
   be changed in this milestone**: `vault/api.py:74` sets
   `created_by=identity.get("agent_id", "admin")` on `POST /vault/secrets`. Because it
   supplies a literal default, dropping the field does not raise — it silently attributes
   every stored secret to `"admin"`. It is repointed at the principal's `member_id`. The
   remaining sites are non-consumers: `middleware.py:59` is a docstring, `asgi.py:114` is
   the attach site this step rewrites. `require_scope`'s docstring example
   (`auth/middleware.py:59`) must be updated so it stops documenting a field that no
   longer exists.
Then the same context is carried into Bridge, Relay, replay and the dashboard APIs.
**Only after membership is authoritative** does the entitlement check go in, at the two
operations §3.2 names: issuing and accepting a member invite.

This is the follow-up the enrollment design defers by name (§7 item 2, restated at its
§1.12: enrollment makes the credential device-owned and therefore *attributable*, and
"making cortex prefer the verified principal is milestone 1 of the workspace design"). Milestone 1
discharges it. It depends on the enrollment design, which lands first and mints the
`device_id`/`credential_id` this spec's principal carries (see §2.2 and the sequencing
header); it has no dependency on the client-side config collapse.

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

**The `AUTH_ENABLED=false` principal is defined here, not left to fall through.**
`auth/keys.py:95-99` returns `_ANONYMOUS_IDENTITY` with no workspace and no member, on
both disabled paths (`middleware.py:69`, `asgi.py:187`). Since step 4 filters reads by
the verified workspace, an undefined anonymous principal produces a filter matching
nothing — empty recall on every auth-off deployment, and those are live: an existing
`.env` carrying `AUTH_ENABLED=false` keeps winning after the 2026-07-26 default flip.

Anonymous therefore resolves to **the deployment's single workspace and its owner
member**, with the existing narrowed anonymous scope set unchanged. Auth-off remains
what it is documented to be — a convenience mode for a loopback-bound single user, open
and unattributed below `admin` — and gains no new capability from this design.

### 2.2 Migration ordering is not optional

Existing memories carry `agent_id` and no `workspace_id` (~3.9K records already
backfilled once by `cortex/scripts/backfill_legacy_agent_id.py`). Enabling a read filter
before the backfill completes makes every existing memory vanish from recall.

**Order: create workspace + owner member → backfill credentials → backfill memories →
verify counts match pre-backfill totals → then enable filtering.**

The credential store is not optional and is not covered by the memory backfill.
`auth/keys.py:199-206` writes `{agent_id, scopes, created_at, key_id}` with no member and
no workspace field, and `auth/keys.py:218` is the index a backfill walks. The dependency
is guaranteed rather than hypothetical: the enrollment design lands first and mints
device credentials with no member, so on the day milestone 1 ships **every** existing
credential resolves to no `member_id` and fails steps 3 and 4 of §2. §2.2 already
establishes this ordering discipline for the memory store; omitting the credential store
leaves the same failure reachable through a different door.

Add a Qdrant payload index on `workspace_id` in the same change, or recall degrades as
the collection grows. Both backfills are idempotent and assign all pre-existing data to
the deployment's single workspace and its owner member.

---

## 3. Entitlements

A signed document the server holds. The client never evaluates it.

```json
{
  "workspace_id": "…", "customer": "…", "plan": "solo|team",
  "max_members": 1,
  "issued_at": "…", "expires_at": "…"
}
```

- **Ed25519-signed, verified offline.** No phone-home. This matters for air-gapped
  buyers and matters more for a product that reads your codebase and runs a secrets
  vault — "we cannot see your usage" is a sales asset, not just a principle.
- **Absent entitlement = Solo plan.** A fresh install is free and works immediately
  with no account, no key, no network call to us.
- **Fails open, always.** An expired entitlement degrades to Solo; it never locks
  anyone out of memory they already wrote. There must be no code path capable of
  bricking accumulated context over a lapsed renewal.
- Expiry warns for 30 days before degrading, surfaced in the briefing and `doctor`.
- Applied via the dashboard Licence page, `firekeep-admin licence apply`, or
  `FIREKEEP_LICENCE` for a Kubernetes secret.

### 3.1 The free/paid boundary

| Solo (free, 1 member) | Team (paid, N members) |
|---|---|
| Everything, without exception | `max_members > 1` |
| All four services; **all clients** — Claude Code, Codex, kiro, OpenCode | |
| Unlimited devices, credentials, agent runtimes, terminals | |
| Memory, recall, skills, corpus, knowledge ingest | |
| Sessions, replay, evals, patterns, night-shift | |
| Decision board, symdex, presence, leases, tasks | |
| Cross-agent patterns, DMs, bulletin, broadcast, `/memory/handoff`, `/memory/contributors`, the briefing's `cross_agent` section | |

**There is no runtime capability check anywhere.** The Team column is not a feature list;
it is a description of what a second member makes non-empty. Every capability an earlier
draft placed there is keyed on `agent_id`, not on member —
`/patterns/relevant?exclude_agent=` (`cortex/app/patterns/api.py:97`),
`POST /dm/{agent_id}`, presence (`relay/app/presence.py:17`) — and §1.1 guarantees one
member holds several agent identities: `night-shift` runs as its own
(`client/firekeep_client/nightshift.py:71`) and `CLAUDE.md` prescribes a distinct
`FIREKEEP_AGENT_ID` per terminal. Gating them would charge a solo developer for handing
off between their own two agents and for messaging their own night-shift worker — both
documented single-user workflows, neither of which is meaningless alone.

The paid thing is the second person, and only the second person. The free tier carries no
artificial quota: no memory cap, no retention cliff, no client restriction.
**Agent-agnosticism is never gated** — it is the clearest differentiator against
IDE-locked tools and costs nothing to give away.

### 3.2 The two enforcement points

The seat check is **not** on `POST /enroll`. Enrollment mints a device credential and
knows nothing about members — the credential is bound to a device, not a person
(`2026-07-30-client-enrollment-join-codes-design.md` §1.12) — so a check there would
meter devices, which §3.1 gives away without limit, and would put plan language on a
surface that carries none today (a case-insensitive search of that design for
plan/tier/seat/billing/quota/entitlement returns nothing operative).

Membership is created in exactly two operations, and both are checked:

1. **Issuing a member invite** — `members + outstanding_member_invites < max_members`,
   evaluated at issue time so the refusal reaches the admin who can act on it rather than
   the teammate who cannot.
2. **Accepting a member invite** — re-checked at redemption, because seats can be
   consumed between issue and accept.

The refusal names the plan, the current and maximum member counts, and the upgrade path.
A bare 403 at accept time is indistinguishable from a broken invite.

Device enrollment (`POST /enroll`) and the Devices tab are untouched by this: they add
credentials to a member that already exists.

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

`join` is the flow specified in the enrollment design. `init` is the only command that
provisions rather than attaches.

`login` requires an authorization server. Against a hosted or org-SSO-backed deployment
it runs OAuth 2.1 authorization-code with PKCE, discovering the authorization server via
MCP resource metadata. Against the self-hosted default, which has no authorization server
at all, it is implemented as a **stub** that prints: *"this server issues join codes —
ask an admin for one, then run: `firekeep join <code>`."* The full OAuth path lands with
the hosted control plane (`2026-07-30-client-enrollment-join-codes-design.md` §7 item 3).
The command exists in both cases and asks nothing about tier either way.

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
- **The gateway never hides or filters tools by plan.** The tool list is identical on
  Solo and Team. A model that finds Firekeep tools missing cannot distinguish an unpaid
  plan from dormancy — `shim.run()` already serves an inert zero-tool MCP server under
  `is_bypassed()` (`client/firekeep_client/shim.py:501-506, 539-549`) — and one of those
  two states is expected to be silent. Hiding a tool is also a runtime capability gate,
  which §3.2 permits nowhere. The only thing the gateway makes legible is the **seat
  refusal**: an invite issue or accept that fails returns the plan, the counts and the
  upgrade path in the model's context rather than a bare 403.

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
3. **Signed entitlements**, Solo and Team, with the seat check at membership
   creation only.
4. **`init` / `join` / `login` plus the MCP gateway**, with adapter coverage tests across
   all four clients.

Multi-tenancy (§2.1) is explicitly **not** in this sequence and belongs to the hosted
product.

---

## 8. Testing

- `test_memory_attribution_verified.py` — **the security guard**: a write carrying
  `X-Agent-Id: someone-else` persists the *principal's* member, not the header. This is
  the defect milestone 1 exists to close.
- `test_reads_filtered_by_workspace.py` — both `POST /memory/recall` and
  `recall_streaming` apply the same filter, asserted through one shared code path so the
  documented divergence cannot re-open as an isolation hole.
- `test_entitlement_gates_members_only.py` (renamed from `test_presence_never_gates.py`)
  — **invariant guard**, a static check on the call graph so a future "convenient" check
  fails CI rather than shipping. No authorization or entitlement path may: read presence
  (`relay/app/presence.py`), import `nightshift`, read `agent_id` or the `X-Agent-Id`
  header, or execute on `POST /enroll`. These are the four things requirement 3.3 names,
  and §1.1's argument for agent identity is identical to §1.2's for presence — it just
  never got a guard.
- `test_entitlement_fails_open.py` — absent, malformed, unsigned, and expired
  entitlements all degrade to Solo and none denies a read of existing memory.
- `test_seat_check.py` — widened: a second **credential** for an existing member is free;
  a second **device** is free; a second **member invite** is refused on Solo with a
  message naming the plan, the counts and the upgrade path; the same refusal fires again
  at **accept** time when a seat was consumed between issue and accept; and `POST /enroll`
  performs no seat check on any path.
- `test_principal_resolution.py` — a credential resolves to
  `{workspace_id, member_id, credential_id, scopes}` and carries no `agent_id`; an
  unknown credential resolves to nothing; `X-Agent-Id` cannot influence any field;
  **with `AUTH_ENABLED=false` the anonymous caller resolves to the single workspace and
  its owner member**, and recall over pre-existing memories is non-empty (§2.1).
- `test_workspace_backfill.py` — idempotent; recall totals before and after are equal;
  **every credential in `auth:key_index` resolves to a member after the backfill**;
  filtering stays disabled until both backfills are marked complete.
- `test_gateway_degrades.py` — one backend down removes only its tools; the gateway still
  serves the rest and reports the failure.
- `test_adapters_single_entry.py` — each of the four adapters renders exactly one MCP
  entry, and re-rendering removes the six legacy entries without touching foreign config.
