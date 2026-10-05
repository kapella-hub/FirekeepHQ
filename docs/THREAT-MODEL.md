# Firekeep Threat Model

**Date:** 2026-07-26
**Scope:** all four services (Cortex, Bridge, Sentinel, Relay), the dashboard, the
client kit, the URL crawler, and — where a human has opted into it — the Hands
desktop operator (§5.8).
**Supersedes:** `cortex/docs/SECURITY_REVIEW.md` (2026-03-02), which covered Cortex
v0.1.0 only and predates auth, the vault, the agent gateway and the crawler. That
file is kept as a record of what was reviewed then, not as current state.

This document says what we believe is true, including where it is uncomfortable.
Findings marked **OPEN** are not mitigated today.

---

## 1. Deployment shape

Firekeep is **single-tenant and self-hosted**. The customer runs the whole stack on
their own infrastructure; there is no vendor-operated service and no shared plane.
The tenant boundary is the customer's machine.

This removes a large class of threats (cross-tenant leakage, noisy-neighbour,
vendor-side breach of customer data) and concentrates the rest into two questions:

1. **Who can reach the ports?**
2. **What can a caller do once they can?**

Everything below is one of those two.

## 2. Assets, in the order an attacker would want them

| Asset | Where | Why it matters |
|---|---|---|
| Vault secrets | Redis DB 7, Fernet-encrypted at rest | Decrypted on read. Holds the customer's *other* systems' credentials — this is the highest-value target and the one that was actually leaked. |
| `VAULT_KEY` | `.env`, plaintext | Decrypts the above. Compromise of `.env` is compromise of the vault. |
| API keys | Redis DB 7, SHA-256 hashed | An `admin` key is total authority. |
| Memories | Qdrant (vectors + payloads) and Neo4j | The product's substance. Plaintext at rest; often contains code, hostnames, and whatever an agent was told. |
| Session context | Redis DB 3 | Working state, file paths, decisions. |
| `NEO4J_PASSWORD` | `.env`, plaintext | Direct graph access. |
| Replay traces | Redis DB 6 | Records what every agent did. |

Note the asymmetry: the vault is encrypted at rest and the memories are not. An
attacker with filesystem access to the Qdrant volume does not need any key.

Three more exist **only on a workstation whose human has enabled Hands** (§5.8),
and on such a machine they outrank everything above, because they are not data
about the customer's systems — they are the systems:

| Asset | Where | Why it matters |
|---|---|---|
| The logged-in desktop session | the workstation | Every application the human is signed into is reachable by a click, without a credential. |
| The Hands browser profile | `~/.firekeep/hands/chrome-profile` | Whatever the human signed into *through* Hands stays signed in, inside the agent's reach. |
| The broker's bearer token | `~/.firekeep/hands/broker.json`, `0600`, minted per broker run | Lets a same-user process ask for and spend permits. It cannot grant one; see the residuals in §5.8. |

## 3. Trust boundaries

```
   ┌─ untrusted ─────────────────────────────────────────────────┐
   │  the network the host is on (only if BIND_ADDR=0.0.0.0)     │
   └──────────────────────┬──────────────────────────────────────┘
                          │  published ports 8040-8100
   ┌─ boundary 1: auth ───▼──────────────────────────────────────┐
   │  FirekeepKeyAuthMiddleware on all 5 surfaces                │
   │  skip list: /health /version /.well-known/agent.json        │
   │             + /docs /redoc /openapi.json (Cortex)           │
   │  exact (Cortex): /enroll /enroll/anchor                     │
   │    /members/invites/accept /members/invites/anchor          │
   └──────────────────────┬──────────────────────────────────────┘
   ┌─ boundary 2: scope ──▼──────────────────────────────────────┐
   │  require_scope (FastAPI) / require_scope_asgi (Starlette)   │
   │  admin gates: vault, /auth/keys, DLQ, policy, quarantine    │
   └──────────────────────┬──────────────────────────────────────┘
   ┌─ trusted ────────────▼──────────────────────────────────────┐
   │  the Docker network: Neo4j, Qdrant, Redis, Ollama.          │
   │  NO auth between services. Datastore ports are pinned to    │
   │  127.0.0.1 and BIND_ADDR cannot widen them.                 │
   └─────────────────────────────────────────────────────────────┘
```

**The internal network is a single trust zone.** Redis has no password, Neo4j's
credentials are in `.env`, and any container on that network reaches all of them.
A compromise of any one service is a compromise of all stored data. This is a
deliberate simplification for single-tenant deployment, and it is why the two
boundaries above carry the whole load.

## 4. Actors

- **Operator** — has `.env`, so has everything. Not a threat boundary.
- **Teammate agent** — holds a scoped key (`firekeep-admin keys create` mints the
  full non-admin set). Trusted to read and write memory; *not* trusted with admin.
- **Anonymous caller who can reach a port** — the actor that matters.
- **A compromised agent** — an LLM agent with a valid key, driven by hostile input
  (a poisoned repo, a prompt-injecting web page). Has whatever its key has.
- **Someone with host filesystem access** — has `.env` and the volumes. Out of
  scope; that is the operator's boundary to defend.

## 5. Entry points and their current state

### 5.1 The five HTTP surfaces

Cortex REST `:8100`, Cortex MCP `:8080`, Bridge `:8070`, Sentinel `:8060`,
Relay `:8050`. All five carry `FirekeepKeyAuthMiddleware` when `AUTH_ENABLED=true`,
which is the default as of 2026-07-26.

**Fixed 2026-07-26 (audit blocker 7):** the default was `false`, and with auth off
every caller was handed `scopes: ["*"]`. `GET /vault/secrets` and `POST /auth/keys`
were open to anyone who could reach the port, and all six ports bound `0.0.0.0`.
That combination put 12 real secrets from this project's own deployment on the
public internet. Three independent changes now stand between a default install and
that state: auth on by default; the anonymous identity carries every scope *except*
`admin`, and the scope check runs on the disabled path; and `BIND_ADDR` defaults to
`127.0.0.1`.

**Fail-closed:** with auth enabled and Redis DB 7 unreachable, the middleware
returns 503 rather than passing traffic. Compose healthchecks are TCP-only, so
containers stay green during such an outage — an operator sees 503s, not a red stack.

**OPEN — the skip list is a standing risk.** `/health`, `/version` and
`/.well-known/agent.json` are unauthenticated by design so probes work when
backends are down. A route added under one of those prefixes is silently public.
This has happened once: `/dashboard` was a *prefix* skip, which exempted
`/dashboard/api/memories` and served 4,066 memories to unauthenticated callers.
Fixed by splitting prefix and exact matching, but the class remains — the skip
list is a place where a one-word change has no local signal that it is dangerous.
Since 2026-10-01 no `/dashboard` path is on either list at all: the exact entries
existed only for the legacy cortex-served shell, removed with it (§5.2), and
`cortex/tests/test_legacy_dashboard_removed.py` fails if one is re-added.

### 5.2 The dashboard `:8040`

nginx with basic auth, injecting an admin-scoped `DASHBOARD_API_KEY` on every
`/api/*` proxy. Two consequences worth naming:

- The basic-auth file is the only thing between a reachable dashboard and admin
  authority. It is generated with SHA-512 crypt; an earlier version used apr1-MD5.
- **Fixed 2026-10-01:** there was a second, older dashboard served by cortex-api
  itself at `/dashboard/`. It had no key mechanism at all, so under
  `AUTH_ENABLED=true` its data tabs simply failed; it was superseded but still
  shipped, keyless via two exact skip-list entries. Removed: the `GET /dashboard/`
  HTML route in `cortex/app/dashboard.py`, the `cortex/app/static/dashboard.html`
  asset, and the `/dashboard` and `/dashboard/` entries in `app/main.py`'s
  `AUTH_SKIP_EXACT_PATHS`. Both paths now 404 (401 first without a key, since
  they are no longer exempt). The auth-gated JSON routes under `/dashboard/api/*`
  stay: the `:8040` SPA calls them through nginx with `DASHBOARD_API_KEY`. Guarded
  by `cortex/tests/test_legacy_dashboard_removed.py`.

### 5.3 The URL crawler

`POST /knowledge/ingest-url` fetches attacker-influencable URLs from inside the
trust boundary — the classic SSRF shape. `crawler.is_safe_url` resolves every A/AAAA
record and rejects loopback, private, link-local, reserved, multicast and
unspecified addresses, including `169.254.169.254`. Checked before the start URL,
before every same-site link, and before every redirect hop (max 4).

**OPEN, accepted:** DNS rebinding. The resolve-then-fetch gap means a name that
passes the check can resolve differently on the actual request. Mitigating it
properly means pinning the resolved IP into the connection. Accepted because the
endpoint is admin-gated and single-owner; it would not be acceptable if this were
ever exposed to untrusted callers.

### 5.4 The agent gateway and pre-edit hook

`POST /agent/action/before` returns `allow | rethink | block`. On Claude Code the
block is enforced by the hook's exit code; **on kiro 2.12.1 the block is advisory
only** — the hook fires and the agent proceeds. Documented in
`docs/KIRO-VALIDATION.md`. Anyone treating this as a security control on kiro is
mistaken. It is a safety rail against agent error, not against an adversary: an
agent that wants to edit a denied path can call the filesystem directly.

### 5.5 Sentinel's collectors

**Fixed 2026-07-26:** Sentinel mounted `/var/run/docker.sock` read-write and the
entire repository (`./:/watch:ro`, which included `.env`). Docker socket access is
root on the host — `:ro` restricts the socket file, not the API. Neither mount did
anything by default. Both are removed; the docker collector is opt-in.

### 5.6 The client kit's update path

`firekeep update` fetches and executes vendor code on developer machines, by
default once a day. Mitigations: the wheel and the mirrored `uv` are both
checksum-verified against a per-version `SHA256SUMS` fetched first; the wheel is
downloaded to a local path and installed by path, never by URL (`uv pip install
<url>` does no hash checking) and never by name (`nexus-client` on PyPI is a third
party's package).

**Mitigated (2026-08-05), with stated residuals:** `SHA256SUMS` is now signed —
an Ed25519 detached signature in minisign format (`SHA256SUMS.minisig`, produced
by `client/scripts/make_release.py` when the `FIREKEEP_SIGNING_KEY` CI secret is
set; verifiable with the standard `minisign` tool). The client pins the public
key as `PINNED_PUBLIC_KEY` in `client/firekeep_client/signing.py` (a pure-stdlib
verifier — the import boundary rules out `cryptography`, and RFC 8032 test
vectors pin the arithmetic). On `firekeep update`, the client verifies the target
release's `SHA256SUMS` signature against that key (the *target's*, so `--to`
rollbacks are signed too, plus the latest release's when they differ — that is
what anchors the `latest/` bootstrap being executed), cross-checks the unsigned
`latest.json` bootstrap hash against the signed sums entry (the bootstraps are
listed in `SHA256SUMS` and published under `<version>/`), and refuses a valid
signature minted for a different version (the trusted comment binds
`version:<X.Y.Z>`). The verified sums bytes are then handed to the bootstrap by
path (`FIREKEEP_SUMS_FILE`, 0600), and under that hand-off the bootstrap makes
**no** sums/`.minisig` network fetch of its own — closing the two-fetch split
where a host could serve honest bytes to the client's verification fetch and
attacker bytes to the bootstrap's re-fetch (the two requests are trivially
distinguishable by user agent). Key custody, rotation, and the compromise
procedure: `docs/RELEASE-SIGNING.md`.

What the signature actually buys, and from whom: a compromised **release host**
can no longer introduce code the signing key never signed into the update path.
It says nothing about a compromised **signing key** or CI, and the residuals are
real:

- **First install is TOFU and stays TOFU.** `curl | sh` fetches the bootstrap
  from the very host it would need to distrust; a key delivered by that host
  cannot authenticate it. Signing protects *updates*, where the pinned key
  predates the fetch. A cautious first installer can pin out of band via
  `FIREKEEP_SIGNING_PUB` (the published `latest/signing.pub` is a transparency
  copy, not a trust anchor).
- **Enforcement is ON by default (flipped 2026-10-01); absence now refuses.**
  `[dist] require_signed` defaults to `true`: a release whose signature cannot
  be verified — no `.minisig`, sums unfetchable, verification unavailable on
  that Python, or no pinned key — fails the update before anything is
  downloaded or executed, and the error names the override. The flip waited on
  production evidence: keys minted 2026-08-12 (ID `7D6D83D1240D4A61`, private
  half in the `FIREKEEP_SIGNING_KEY` Actions secret and offline with the
  operator), the public key pinned from client 0.1.42, every release since
  served with a byte-verified `.minisig`, and at the flip every version on the
  release host (1.5.0 through 1.6.1 — releases predating signing are no longer
  served at all) verified against the pinned key with `require_signed=true`.
  An *invalid* signature was always fatal and still is, with no override:
  invalid is tampering evidence, absence is history. Residuals of the flip,
  named plainly: (a) an operator can opt out with `[dist] require_signed =
  false` in `~/.firekeep/config` — there is deliberately no environment-variable
  override, so a process environment cannot quietly disable it — and under
  that opt-out absence is attacker-choosable again, warned on stderr and by an
  unsigned-install marker the next session-start briefing prints once; (b)
  enforcement is a property of the INSTALLED client, so it protects updates
  *from* the first release carrying the flip onward; (c) a release published
  unsigned (CI secret missing — `make_release.py` still builds unsigned rather
  than failing) is refused by every client and stalls the fleet's updates until
  re-published signed. The background auto-update's stderr is DEVNULL, so a
  refusal persists a one-shot marker the next briefing prints rather than
  failing silently every day.
- **Downgrade/freeze window.** `latest.json` is unsigned, so a compromised host
  can still replay an older *signed* release or pin the fleet to one. It cannot
  introduce new code.
- **The shell bootstrap's own check is best-effort.** It verifies with the
  standard `minisign` binary only if one is installed (baked key or
  `FIREKEEP_SIGNING_PUB` — which `firekeep update` exports from the client's own
  pinned key); a bare machine falls back to TLS + checksums. On the update
  re-exec path the in-script check does not run at all: the client verified the
  sums itself and hands the verified bytes through `FIREKEEP_SUMS_FILE`, which
  is strictly stronger than re-checking a re-fetch.

### 5.7 The field-failure reporting channel

Two new surfaces, both covered in full in
`docs/superpowers/specs/2026-08-22-field-failure-reporting-design.md` (see its Review
record for the load-bearing design changes and the "Implementation pass (2026-08-23)"
note for where the build deviated from the first draft).

**The public collector, `failure-report.php` on firekeep.ai, is deliberately
unauthenticated** — installing software cannot hold a credential before it has
successfully installed. Mitigations: every field is validated against a fixed enum
table (`client` against a released-version allowlist, everything else against
closed vocabularies) and an unrecognised value rejects that event rather than
logging it; `client` values outside the allowlist are rejected the same way,
closing the one open string the schema would otherwise carry; a per-signature mail
budget (5 immediate mails per rolling hour, overflow deferred to a digest) bounds
what an attacker can do with the outbound mail side effect; state is a single
`flock()`'d critical section with atomic temp-file+rename writes; and disk growth
is bounded at every layer — the active log seals on size OR age (4MB or 6h,
whichever comes first), the sealed-segment total is byte-capped (oldest segments
dropped past 256MB), and the dedup ring is count-capped (trimmed back to its cap
once it grows past 2x) — so an unauthenticated unlimited write endpoint cannot
fill the disk that also holds the support mailboxes. **Residual, accepted:** the
data is low-integrity by construction — an attacker can fabricate failure
patterns or bury a real one in noise — so every event that reaches Sentinel is
labelled `integrity: "unverified"`
in `details`, and any dashboard or agent-facing summary treats it as a signal to
corroborate, not to act on directly.

**Outbound mail composition is its own attack surface**, one the earlier
`doctor-report.php` review never had to consider because that endpoint sends no
mail. Recipients and subject are fixed, never derived from a request; every
report-derived value that reaches the mail body is stripped of CR/LF before
composition, closing the embedded-newline header-injection class the same file's
comments document elsewhere; and the novelty/digest logic that decides *whether*
to mail is itself budgeted and lock-guarded (above), so the mail path cannot be
used to force unbounded outbound mail even before body composition is reached.

**The VPS→Sentinel hop inside the ingest pipeline (`deploy/failure-ingest/`) is
honestly at-least-once, not exactly-once.** The loop that POSTs each aggregated
signature to Sentinel and then moves the source segment to `done/` is unguarded
against a crash mid-batch: a process killed after some POSTs succeed but before
the segment moves leaves it in `inbox/` for a full retry on the next cron tick,
re-sending every signature in it. Nothing on the VPS side deduplicates that
replay — `details.batch` (`"<segment-name>|<signature-hash>"`) is the
deterministic key any downstream consumer (the dashboard view, a future aggregate
reader) must use to collapse it, because Sentinel's own `XADD` does not dedup.

### 5.8 Hands — the desktop operator

`firekeep-hands` is an **opt-in** capability: not bundled with a release, never
seeded into the registry, installed and registered only by a human running
`firekeep hands enable`. A machine that has not run it has none of this surface.
A machine that has is a different security proposition from every other part of
Firekeep, because the thing being protected is no longer a database — it is the
person's own computer and every application they are signed into. Behaviour,
limits and the CLI are [`docs/guides/hands.md`](guides/hands.md).

**Assets.** The machine itself, at the privilege of the user's own login. Every
application session that login already holds — mail, chat, bank, source control —
reachable through a click, not through a credential. `~/.firekeep/hands/`, which
holds `config.json`, the allowlist `policy.json`, `broker.json` (the broker's
loopback port and per-run bearer token, `0600`), the evidence ledgers, and
`chrome-profile/`, the dedicated browser profile that holds whatever the human
has signed into *through Hands*. Screenshots, which exist transiently on the
evidence path and leave the machine entirely whenever the runtime asks for one.

**Attacker models.**

1. **A compromised or merely over-eager runtime.** The model drives the MCP
   server directly and can attempt any action, at any time, for any reason —
   including reasons it invented. This is the primary model, and it is the one
   the whole design is shaped around.
2. **Prompt-injected UI text.** A web page or an application's own content is
   read into the model's context by `hands_observe`. Anything the model reads may
   be an instruction. This is not hypothetical and is not solved.
3. **Local malware running as the same user.** It can read `broker.json`, talk to
   the broker's loopback API, and synthesise keystrokes. It cannot elevate; it
   does not need to.
4. **A holder of a workspace API key who is not the machine's human** — the
   driving agent (which shares the machine's kit key), a second agent, a
   teammate, anything with a key. Relevant only when phone approvals have been
   turned on.

**Mitigations.**

- **The broker is a separate process.** The permit store lives outside the MCP
  server the model drives, so a compromised runtime is not one function call away
  from approving itself. The loopback API can create, read and consume a permit;
  **no route grants one**. Approval enters only through the OS input listener or
  the phone bridge.
- **Injected input is rejected.** Windows: a `WH_KEYBOARD_LL` hook requires both
  `LLKHF_INJECTED` (`0x10`) and `LLKHF_LOWER_IL_INJECTED` (`0x02`) clear, so every
  key `SendInput` delivers — Hands' own typing included — is ignored. macOS: a
  `CGEventTap` rejects events carrying Hands' `kCGEventSourceUserData` marker and
  events whose source state is not `kCGEventSourceStateHIDSystemState`.
- **Permits are bound to the step.** The challenge id is derived from machine,
  agent session, task, step index and a hash of the action dict; the server
  recomputes it from the action about to run and refuses a mismatch. One use, 60 s
  TTL, memory only, expiry applied to approved permits as well as pending ones,
  and `denied`/`expired`/`consumed` terminal.
- **Fail closed.** No broker, or a broker that stops answering between the health
  check and the request, refuses the protected step. A restarted broker holds no
  permits.
- **Effects, not verbs, decide protection.** Six classes (`send`, `money`,
  `destroy`, `credential`, `install`, `boundary`) are computed by Hands from the
  routed action and its observed target, never from a label the model supplies.
  `boundary` is the catch-all, and it is keyed on where a step LANDS rather than
  on the two "switch app" verbs: any click, keystroke, typed string, value set or
  scroll whose target window or control belongs to an app that was not declared
  or allowlisted, plus any domain not allowlisted. A task started with `apps=[]`
  therefore cannot operate the window that happens to be in front, and a blank
  app name — which the classifier once exempted, and which Windows resolves to
  any window at all — is refused outright. Consuming a `boundary` permit
  declares that app (or that exact host) for the remainder of that task only —
  in memory, never in `policy.json`, and for no other class. What it declares
  is **exactly what the permit sentence named**: apps and hosts are kept in
  separate task-scoped lists, and a control in a different app from the
  foreground window is named in the prompt rather than widened silently
  alongside it.
- **No model-supplied coordinates.** Actions carrying `x`/`y`/`point` are
  rejected; every pointer action resolves a `ref` from the observation the runtime
  was shown, and Hands computes the point from that control's own rect.
- **The permit text is built by Hands, not by the model** — from the routed
  control's own name and the window's app, whitespace-collapsed, stripped of
  unprintable characters and capped at 60 characters. And it is *checked*, not
  merely built: a permit already standing at that challenge under a different
  title or different classes refuses the step instead of spending it, so getting
  in first buys nothing.
- **Typing is bounded.** 500 characters per `type` action, with the Windows
  elevation guard re-checked every 100 characters so it cannot decay mid-injection
  while focus moves.
- **Evidence is tamper-evident.** Per-task `steps.jsonl` with a sha256 chain over
  each line; a dropped, reordered or edited line breaks every chain after it.
- **One operator per machine**, enforced by a relay lease on
  `hands:<machine_id>`; a refused lease aborts the task rather than proceeding.

**Residuals, all OPEN.**

- **A process running as the same user can read `broker.json` and consume permits
  it did not earn.** The token is `0600` and the socket is loopback-only, which
  means a caller needs read access to the user's own `~/.firekeep` — a real limit
  and not a strong one, since a same-user process has it. This is why possession
  of the token buys the ability to *ask*, not the ability to answer: the permit
  still requires a real human chord (or a phone tap) to exist at all. What such a
  process gains is the ability to spend an approval the human granted for
  something else, in the window before it is consumed or expires, and to
  **pre-create permits for steps that have not happened yet** — every input to a
  challenge id is reachable by it (`machine_id` is a file, `session_id` is in
  `evidence/<task>/task.json`, `task_id` and `step_index` come back in tool
  results), so it can fill the human's toasts and `pending.json` with prompts
  Hands never asked for. What it can no longer do is change *what the human
  approves*: `_gate` recomputes the honest title and classes on its own side and
  refuses the step outright — before consuming anything — when the permit
  standing at that challenge describes something else, on the first request and
  on the retry after an expiry alike. Note also that the spec's claim that
  nothing secret is stored on disk is not true as built — the bearer token is in
  `broker.json`, minted per broker run.
- **Kernel-level input injection defeats the real-input filter.** A kernel-mode
  driver can originate events with no injection bit set, and the broker trusts the
  OS's flag. This filter stops user-mode malware and honest mistakes, not a
  rootkit. Out of scope, as it is for the hardware products this design compares
  itself to.
- **Screenshots reach the runtime's model provider.** `hands_observe(detail=
  "screenshot")` and region zooms send the screen to whatever cloud model is
  driving. Accessibility trees are the default precisely because they are not
  this, but there is no per-task screenshot switch in this release; the control is
  the runtime not asking.
- **The macOS source-state filter is unverified on real hardware.** It is
  implemented as specified and logs `(keycode, flags, userData, sourceStateID)` at
  DEBUG so it can be measured; until it is, the Hands marker is the half of that
  filter known to hold. The Windows injected-flag rejection *is* verified
  (2026-09-05, this PC).
- **The broker's own notice is informational.** A pending permit raises an OS
  notification (Windows balloon, macOS notification) with the step title, its
  classes and the chord, and `firekeep hands status` lists the pending set from
  `pending.json` — text Hands built from the routed control, which a model cannot
  forge. But a toast can be missed or arrive while the human is already reaching
  for the chord, the chord approves the **oldest pending** permit regardless, and
  on the chord path the runtime that asked for the step is the thing being gated.
  The phone path remains the only one where the human reads the broker's text on
  a surface the model does not draw. **OPEN** residual: a permit arriving between
  reading the toast and pressing the chord.
- **Phone approvals, when opted in, trust every workspace credential except
  the requester's — narrowed, not closed (2026-10-01).** Until 2026-10-01 relay's
  `update_task` recorded status, result and assignee and **not the completing
  principal**, so the driving agent — which holds the machine's kit key and
  reaches `relay_task_list`/`relay_task_update` through the gateway — could
  complete its own `hands_permit:` task and be granted the permit within one
  poll. Relay now stamps every task write with the principal the auth layer
  verified (`created_by`, `updated_by`, `completed_by`, and `by` on each history
  entry; never `X-Agent-Id`/assigner/assignee). `completed_by` is the last writer
  that left the task terminal, so rewriting only the `result` of a human-resolved
  task restamps it, and it is cleared — never inherited — on a reopen or an
  unverified write. The broker honours an approve only when both stamps exist,
  both are authenticated, the workspaces match, the completing credential is
  **not** the posting one, and — when `phone_approvers` is set — it is on that
  list; it reads its own task by id so a same-title decoy cannot stand in.
  Everything else denies the permit and logs why: a relay that predates the
  stamps and a Keep with `AUTH_ENABLED=false` (every caller the same anonymous
  owner) refuse every phone approval. **Residual, OPEN:** without
  `phone_approvers`, any *other* authenticated credential — a second machine's
  kit key (and so an agent running there), a teammate's — can approve (an
  agent cannot mint one itself: minting needs `admin` and enrolment an
  admin-issued join code), because the auth layer has no notion of a human member (the owner's
  dashboard key and agent keys share a member id). With `phone_approvers` pinned
  to the dashboard credential, the dashboard's basic-auth password becomes the
  boundary: an agent that can read it (it has been stored in the vault, which
  `vault:read` reaches) and reach the dashboard approves as the dashboard. A
  local one-time code would not close it — Hands can screenshot the screen the
  code is on. **`phone_approvals` therefore still defaults to `False`.** Closing
  it needs a credential class the auth layer marks as human-held — a
  dashboard-session principal agents cannot mint — which is not built.
- **Prompt injection through observed UI text.** Unchanged from threat 9 below,
  with a wider blast radius: the injected instruction now reaches a tool that can
  click. Permits and the allowlist bound the damage; they do not remove the risk.
- **`action_before` gates only on an explicit `block`.** Hands declares the task
  to Cortex; a `block` decision refuses `hands_task_start` (lease released, ledger
  marked abandoned). `allow`, `rethink`, no answer and an unreachable Keep all let
  the task start — the Keep is a veto, not a gate, and the gate that does the
  everyday work is local. **OPEN.**
- **Two live Hands servers sharing one agent id.** A lease held by our own agent
  id is reclaimed on the assumption that the holder is a dead session of ours,
  because relay carries no liveness signal; two live servers on one machine under
  the same `NEXUS_AGENT_ID` are indistinguishable from one live and one dead, so
  the second takes the lease. A per-process holder id would close it but changes
  the agent-id contract shared with `action_before` and relay tasks. **OPEN**,
  PR2.

### 5.9 Intra-workspace member isolation (2026-10-01)

Members of one workspace are separate principals: a teammate's key carries its
own `member_id`, and member-private content (docdex/maildex corpus chunks,
`visibility=member`) is filtered by it. The 2026-10-01 authz audit found that
boundary bypassed wherever a service acted for a caller with a credential minted
for the **deployment owner** — every service key `deploy/bootstrap-keys.sh` mints
carries `member_id=$OWNER_MEMBER_ID`.

**Fixed 2026-10-01 — Bridge recalls with the caller's key (F1, Bridge half).**
Bridge's proactive recall (`ctx_update`), prior-art recall (`ctx_start_session`)
and skill-evaluate trigger now present the live caller's `X-API-Key`; with auth
on, a missing caller key skips the call instead of falling back to the service
key. Before, Bob's `ctx_update` returned the owner's member-private chunks into
Bob's shadow. The eval trigger keeps the service key on purpose (`eval:grade`;
it returns nothing to the caller) — see `docs/guides/bridge-context-and-briefing.md`.

**Fixed 2026-10-01 — Bridge sessions are owned by workspace + member (F3).**
Any teammate key could list every member's sessions, read their full shadow by
id, write into them through the shared label pointer or an `X-Session-Id`
header, and pause another member's live session by starting one under her
label. Every Bridge session tool and REST route now gates on the verified
principal (`bridge/app/session.py` `session_owned_by`); `X-Agent-Id` is a label,
never a gate. Legacy sessions with no recorded owner belong to the deployment
owner alone. Workspace-wide REST reads need the new service-only scope
`session:read:workspace`, minted onto `FIREKEEP_INTERNAL_KEY` for Cortex's
workers. Details: `docs/guides/bridge-context-and-briefing.md` "Session
ownership".

**OPEN:**
- ~~Relay takes a scope session's target `agent_id` from the request body and
  writes into Bridge with its owner-member service key, so teammates'
  `origin:"mcp"` scope decisions stop persisting.~~ Fixed 2026-10-04 (§5.14):
  scope sessions are owned by the verified member and Relay writes decisions
  with the owning member's key.
- Bridge's label pointer (`nb:active:{agent_id}`) is still shared across
  members: a label held by one member's live session is refused to another
  (availability, not confidentiality).
- Prior art's "in flight" line shows teammates' active-session goals inside one
  workspace, by design.
- ~~Bridge's distillation worker writes with the service key, so every
  distillate is attributed to the owner member.~~ Fixed 2026-10-04 (§5.12).

### 5.10 Authenticated is not authorized: Cortex route scopes

**Mitigated 2026-10-01.** The global key middleware (§5.1) proves a caller holds
*some* valid key. Until this date that was the only check on a set of Cortex routes
that destroy data or reveal another member's, so every valid key — the narrowest
service key, a teammate's laptop key, a leaked relay key — could:

- **Delete any Qdrant point.** `DELETE /skills/{id}` deleted whatever id it was
  handed, with no `memory_type` check, no workspace check and no scope, and went
  ahead even when the lookup failed. Point ids come back in every recall result, so
  a teammate's member-private document chunk was one call away.
- **Approve its own skill.** `PATCH /skills/{id} {"skill_status": "active"}` is the
  human approval act (it stamps `approved_by: "human"`), and nothing distinguished
  the agent that drafted a poisoned skill from the human reviewing it.
  `GET /skills/{id}` likewise returned any point's content by id.
- **Re-embed the whole store** under a model of its choosing
  (`POST /admin/embeddings/reembed?model=`).
- **Read every member's recall queries** from `/audit/memory`, which returned each
  `memory_read` event's query text to any caller.
- **Use the core memory routes with any scope at all** — `/memory/recall`, `/learn`,
  `/stream`, `/feedback`, `/contributors`, `/handoff` declared none, so a
  `session:write`-only key could read and write the owner's memory.

What changed. The memory routes declare `memory:read` / `memory:write` (each "or
`admin`", because only `*` is a scope superset and a literal `["admin"]` key must
not lose a route it always had); the streaming recall twin is gated the same way.
`/skills/{id}` resolves the id to a point that is `memory_type == "skill"` in the
caller's workspace and answers 404 for anything else, and 503 — never a blind
delete — when the lookup fails. Review decisions (deleting a skill, any
`skill_status` change, the `needs_rereview` / `stale` / `clear_duplicate_of` flags,
and rewriting the text of a non-draft skill) require `admin`, which the dashboard's
key holds and no member or service key does; an agent can still refine a draft and
compile `step_specs`. Re-embedding is `admin`. `/audit/*` requires `replay:read`,
never crosses a workspace, and shows a non-admin only the events stamped with its
own `member_id` — `memory_read`/`memory_write` events now carry the verified
`workspace_id`/`member_id` as stream fields. Guarded by
`cortex/tests/test_peripheral_route_authorization.py`,
`test_skill_route_authorization.py` and `test_audit_authorization.py`, which drive
real minted keys through the real dependencies.

Before any route was gated, every legitimate caller and the scopes its key carries
was enumerated (the table is in the commit message). One needed a minting change:
Bridge's prior-art and proactive recall call `/memory/recall` with
`FIREKEEP_BRIDGE_KEY`, which had `memory:write` but not `memory:read`, and both
swallow failures — gating without the scope would have silenced them on every
deployment with no error anywhere. `deploy/bootstrap-keys.sh` now declares
`memory:read` on that key and **reconciles scopes on already-provisioned keys**
(union only, never narrowing), so `update.sh` fixes existing deployments in place.
That grant is **transitional**: Bridge is moving those recalls to the caller's own
key, after which `memory:read` should leave the bridge key's declared list and be
narrowed by hand on deployed keys — reconciliation only ever adds.

**Residuals.**

- **Auth-disabled boxes allow review decisions.** With `AUTH_ENABLED=false` no
  middleware runs and every caller is the anonymous deployment owner, so there is
  no identity left to separate a human from an agent; refusing would only break the
  dashboard review queue. Same posture as the rest of an auth-off box.
- **Memory poisoning by a valid key is unchanged (threat 5).** These gates stop an
  agent from *approving* or *rewriting an approved* skill, not from writing ordinary
  memories, which every member key may do and recall will surface.
- **Unattributed audit events are hidden from non-admins.** Events from before this
  change, and server-written receipts (the briefing's skill-exposure receipt), carry
  no member; a member cannot see their own pre-upgrade history. Fail-closed by
  choice.
- **Three skills routes declared no scope** (`POST /skills`, `GET /skills`,
  `POST /skill/evaluate`). **Closed 2026-10-04 (§5.10.1).**
- **Two cross-workspace reads** (`GET /skills`, `GET /memory/contributors`
  filtered by project, not workspace). **Closed 2026-10-04 (§5.10.1).**
- **The owner's service keys are owner-member principals.** `FIREKEEP_BRIDGE_KEY`
  carries the deployment owner's `member_id`. Bridge's synchronous recall paths now
  forward the live caller's key (§5.9), so the bridge key's transitional
  `memory:read` can be dropped from `deploy/bootstrap-keys.sh` and narrowed by hand
  on deployed keys.

#### 5.10.1 The Cortex residuals, closed (2026-10-04)

The routes §5.10 left open, plus one it did not name:

- **Skills routes gated.** `GET /skills` needs `memory:read`, `POST /skills`
  `memory:write`, `POST /skill/evaluate` `eval:write` — each "or `admin`".
  Evaluate takes the eval scope because it rides the same `ctx_complete_session`
  path as `POST /evals/sessions/{id}/compute`, and Bridge sends the caller's own
  key to both. Every caller was enumerated first: the cortex MCP skill tools and
  night shift (through the gateway) forward an enrolled member key, which holds
  all three; the dashboard holds `*`; no worker, CI job or client-kit path calls
  these routes over REST. The auth-disabled anonymous principal holds all three
  scopes, so a personal box is unchanged. No key needed a new scope.
- **Workspace-filtered listings.** `GET /skills`, `GET /memory/contributors` and
  the briefing's skills section (including its trial fallback) now confine their
  Qdrant scroll to the caller's workspace, with unattributed legacy points
  belonging to the deployment workspace only — one helper,
  `app/db/visibility.py::workspace_condition`, the scroll-side twin of
  `_load_owned_skill`. `/memory/contributors` additionally applies member
  visibility and groups by verified member (§5.12); this change only gave its
  workspace match the legacy arm.
- **`POST /skills reauthor_of` resolved through `_load_owned_skill`.** The bare
  retrieve accepted an original that was not a skill at all (a memory, a corpus
  chunk), and an unattributed original from any workspace. Both are now 404
  (the documented `reauthor_of skill not found`), a failed lookup 503.
- **Feedback scoped and de-duplicated.** `POST /memory/feedback` voted on any
  point id: another workspace's memory, or a teammate's member-private memory the
  caller cannot even recall. A target outside the caller's workspace, or another
  member's `visibility="member"` point (unless the caller is an operator — admin,
  or auth disabled — as the dashboard already is for visibility), is now treated
  exactly like a missing id. And one key could vote the same memory forty times
  and saturate the ±`FEEDBACK_WEIGHT` clamp, defeating the Beta prior that makes
  "one reader's thumb nudges, never yanks" true. With auth enabled each verified
  credential now has ONE ballot per memory (`feedback_votes`), last vote wins;
  pre-existing counts carry no voter and stay in the totals. The ballot is the
  key's, not the member's, because dashboard- and `firekeep-admin`-minted keys
  all carry the owner's `member_id`; a member ballot would merge different
  people. Residual: a person holding several keys holds several ballots.
  Auth-disabled boxes keep accumulating, because every caller there is the same anonymous owner and
  deduping would collapse a person's thumbs into one.
- **`GET /admin/untagged-calls`** had no gate. It now needs `admin` when auth is
  enforced (the dashboard), and stays open on auth-disabled boxes, where no caller
  can hold `admin` and the dashboard's discipline card would otherwise 403.

Guarded by `cortex/tests/test_cortex_authz_residuals.py` (real minted keys, a fake
Qdrant that evaluates the filter it is handed) and
`test_briefing_skills_workspace.py` (the briefing's route and builder).

**Residuals.**

- **`POST /skill/evaluate` accepts any `session_id`.** The synthesis worker reads
  the session with the internal service key and stamps the deployment workspace,
  so a member can queue synthesis of a teammate's session; the result is a
  `draft`, which needs `admin` to become visible to agents but is listed to every
  `memory:read` holder in the review queue. Closing it needs an ownership check
  against Bridge before queuing. **Closed 2026-10-05 (§5.16).**
- **The briefing's discipline section reports the same deployment-wide untagged
  counter** to every `session:read` caller that `/admin/untagged-calls` now
  restricts. It is a count, not content. Accepted.
- **Ballots are read-modify-write**, like the counters they replace: two racing
  votes can lose one. Same benign-undercount contract as before.

### 5.11 The briefing as a confused deputy

**Cortex half mitigated 2026-10-01; closed only together with the Bridge fix.**
`GET /briefing` requires `session:read` of its caller, then fanned out to Bridge
(`GET /sessions?agent_id=…`) and Relay (`/presence/{agent_id}`, `/tasks`,
`/bulletin`) with `FIREKEEP_INTERNAL_KEY` — a workspace service credential — for
whatever `agent_id` the caller put in the query string. So Bob's
`GET /briefing?agent_id=alice` returned Alice's paused and active session goals and
her presence, with Cortex vouching for the read. The three user-scoped sections now
present the live caller's own `X-API-Key` (the key `require_scope` just verified;
nothing when auth is off), so Bridge and Relay authorize the read against the
person actually asking. `caller_api_key` is a required keyword on each builder: a
section that forgets it fails with a `TypeError` rather than falling back to a
service key. The environment section keeps the internal key — it reads
deployment-wide Sentinel state, not anyone's data. Guarded by
`cortex/tests/test_briefing_sections_outbound.py` and `test_briefing_api.py`.

**Closed for sessions 2026-10-01, with the Bridge half (§5.9):** forwarding Bob's
key is only as strong as the check behind it, and Bridge's `GET /sessions` now
returns only the verified caller's own sessions (workspace-wide reads need the
service-only `session:read:workspace`). Relay's tasks and bulletins are
workspace-visible by design, so for those two sections the change removes the
deputy without changing what Bob can see.

### 5.12 Who wrote a memory: verified write provenance (2026-10-04)

**Fixed 2026-10-04 — attribution is the verified principal, not the label.**
`/memory/learn` verified the caller's member but stored no credential and no
runtime, so the only "who" a reader could see on a memory was `agent_id` — the
self-asserted `X-Agent-Id` — and `GET /memory/contributors` grouped by it. A
memory's author was whatever its client claimed. Every write now records the
verified `workspace_id`, `member_id` and `credential_id`, plus a `runtime_id`
derived from the credential and the label (`auth/principal.py`
`request_attribution`); `agent_id` stays, as a display field.
`/memory/contributors` groups by verified member and is now confined to the
caller's workspace and recall visibility (it was neither, so a teammate's
member-private source names and counts were visible there — the row 13 residual).

**New service-only contract — `POST /memory/learn/delegated`.** A key holding
`memory:write:delegated` *literally* (not via `*`) names the member it writes
for; Cortex verifies that member is active in the key's own workspace and that
a named credential belongs to them, and answers every failure with one 403.
The service is still trusted to name the right member — the scope is minted onto
one service key only — so this narrows who can mis-attribute, it does not make
mis-attribution impossible for that key's holder.

**Fixed 2026-10-04 — distillates belong to the member who did the work.** Bridge's
distiller wrote every session distillate with `FIREKEEP_BRIDGE_KEY`, minted as the
deployment owner, so every teammate's distilled session history was the owner's
(§5.9). It now writes through `/memory/learn/delegated`, naming the session's
`owner_member` (and `owner_credential`), bound at `ctx_start_session` from the
verified principal — the binding is recorded synchronously, while the member's
own request is live, before any background work. Legacy unbound sessions belong to
the deployment owner (the §5.9 rule); a session whose owner cannot be established
is refused and parked in the DLQ, never written as the owner. Auth-disabled
deployments are unchanged. `FIREKEEP_BRIDGE_KEY` gains `memory:write:delegated`
(deploy migration: `update.sh` reconciles it in place).

**Residuals:** re-learning identical text in one workspace updates one point, and
it then names the latest writer's member under the first writer's label
(`_merge_lifecycle`); graph nodes carry `member_id` but not the credential; a
holder of `FIREKEEP_BRIDGE_KEY` can attribute a write to any active member of its
workspace — the scope narrows mis-attribution to that one key, it does not
prevent it. Memory writes still carry no `visibility`, so a distillate is
workspace-visible whoever it is attributed to (unchanged, by design).

### 5.13 Replay and evals: reads scoped per event (2026-10-04)

**Mitigated 2026-10-04.** `replay:read`, `eval:read` and `eval:write` are all
enrollable, so every member key holds them, and every `/replay/*` route accepted
the verified identity and ignored it. Bob could read Alice's session timeline (recall
query text, memory content snippets, file paths), inspect any event by id, rebuild
her context snapshots — whole shadows — through `context-at`, and narrow over her
session; `GET /evals/sessions/{sid}` returned anyone's eval, `/evals/summary` listed
every member's session ids, and `POST /evals/sessions/{sid}/compute` computed a
foreign eval and returned it. The cortex MCP tools (`replay_*`, `eval_*`,
`audit_memory`) proxy these routes with the caller's own key, so they inherited it.

Now one predicate (`replay/authz.py::event_visible`) decides every read, against the
`workspace_id` / `member_id` the emitter stamped from the verified request principal:
a non-admin key sees its own member's events, an `admin`/`*` key its workspace, and
an unattributed event belongs to the deployment owner member only (the rule Bridge's
`session_owned_by` uses; `/audit/*` now shares the predicate, so the owner's
non-admin runtime keys see its pre-attribution history — the "hidden from members"
residual in row 13 now applies to every member except the owner). A foreign event is
answered exactly like a missing one, and counts, labels, snapshot walks and narrowing
links never cross the boundary. Ownership is per event rather than per session
because the session id is client-generated: a first-writer session index could be
pre-claimed. Bridge stamps its lifecycle and context events with the session's
recorded owner; an eval records the owner stamped on its session-start event, which
only Bridge emits — never a vote over events anyone can write under a chosen session
id. The two service keys (`eval:grade`, `session:read:workspace`) still compute every
member's eval. Auth-disabled mode is unchanged. Guarded by
`replay/tests/test_workspace_authorization.py`,
`cortex/tests/test_eval_authorization.py`,
`cortex/tests/test_replay_provenance_stamping.py`,
`bridge/tests/test_replay_owner_stamp.py`.

**Residuals.** Integrity, not confidentiality: any key could still *write* events into
another member's session id (they stay attributed to the writer and invisible to the
victim, but in-process consumers — the eval scorers, OWM, the pattern engine — read
the session unfiltered, so injected events can skew a victim's metrics) — fixed
2026-10-05, §5.16. Background
emitters (collectors, Sentinel) stay unattributed (Relay's own events are stamped
since 2026-10-05, §5.18), so
their events are visible only to the owner and admins. `/evals/trends` and the
briefing's quality/discipline sections remain workspace-wide aggregates (no session
id, grade or event). Pre-change events and evals are unattributed until they age out
(30 days).

### 5.14 Relay: labels are not principals (2026-10-04)

**Mitigated 2026-10-04.** Every Relay tool and route took identity from an
argument — `agent_id`, `from_id`, `sender`, `author`, the path's `agent_id` — so
any valid key could read another member's DMs (`relay_get_dm(agent_id="alice")`,
`GET /dm/alice`), mark them read, send DMs and post bulletins or broadcasts as
her, release or heartbeat her lease or claim (the fencing token is returned by
`relay_lease_status` to anyone), overwrite or deregister her presence, and post
into, poll or complete her FirekeepScope sessions.

What changed (`relay/app/principal.py`). Each record now carries the verified
`owner_workspace` / `owner_member` of the key that wrote it, and every read or
mutation checks the verified caller: leases and claims need the acquiring
member (inside the Lua script, atomically) as well as the holder label and
token; presence rows are re-registered, refreshed and removed only by their
member; a presence row binds its label, so nobody else may send DMs, post or
broadcast under it; a DM records the member its recipient label was bound to
**when it was sent**, and only that member reads it; scope sessions belong to
their creator. DMs, bulletins and channel messages record the verified sender
as `by`. The dashboard's `["*"]` key reads every inbox, removes any presence
row and lists and answers every scope session in its workspace — it is the
owner's admin surface. Legacy records (no owner) belong to the deployment owner
alone; with auth disabled nothing is enforced and the box behaves as before.
Guards: `relay/tests/test_principal_binding_mcp.py`,
`test_principal_binding_rest.py`, `test_principal_binding_scope.py`.

**Relay → Bridge confused deputy, closed.** Relay wrote a scope decision into
Bridge with `RELAY_INTERNAL_API_KEY` — an owner-member service key — for
whatever `agent_id` the scope session named. Since §5.9 Bridge refuses that for
any session the owner does not own, so teammates' decisions silently stopped
persisting. Relay now presents the key of a principal that **owns** the scope
session: the answerer's own key when the answerer is the owner; otherwise the
answer is marked deferred and written with the owner's own key when the
owner's agent next collects it (`scope_ask` / `scope_check`). No deputy scope
was added, and the relay key is never presented with auth on. This closes the
first OPEN bullet of §5.9.

**Residuals.**

- **Owner-member service keys — closed by scope.** Every key
  `deploy/bootstrap-keys.sh` mints carries the owner's `member_id`, so binding
  records to members alone would have made a leaked `FIREKEEP_INTERNAL_KEY` the
  owner inside Relay. Member-bound tools and routes now require `relay:read` /
  `relay:write`, which no service key carries; the internal key's two Relay
  writes (Sentinel's alert broadcast, Cortex's fleet `POST /tasks`) are
  authorized by the service-only `relay:write:service`, reconciled onto existing
  keys by `update.sh`. The same gate keeps service keys out of `relay_task_update`,
  i.e. out of Hands phone approvals (row 12). Guard:
  `relay/tests/test_service_scope_auth.py`.
- **Presence labels are first-come.** Unbound (pre-upgrade) presence rows are
  adopted by their next verified writer, because strict owner-only would
  freeze every teammate's presence — rows never expire. A teammate can adopt
  another member's label in that window; no stored data is exposed (older DMs
  stay owner-only) and the real owner's next register is refused loudly.
- **A service's label can be squatted.** Sentinel broadcasts as `sentinel`
  with the internal key, which is not an admin key; a teammate who registers a
  presence row under `sentinel` makes every alert broadcast refused until an
  admin removes the row. Availability only, and it needs an authenticated
  teammate.
- **A DM to an unbound label is owner-only.** A message sent while its
  recipient has no presence row reaches only the deployment owner (and the
  dashboard), never the intended teammate.
- **Single-workspace reads.** Presence, bulletins and channel backlogs are not
  filtered by workspace; they matter the day a second workspace shares a Relay.
- **`RELAY_INTERNAL_API_KEY` — retired 2026-10-05 (§5.18).**

### 5.15 Credential attribution: who a key belongs to (2026-10-04)

**Join-code regeneration — fixed.** The member a credential belongs to is what
every member-isolation check in §5.9–5.11 keys on, so a credential minted for the
wrong member defeats all of them at once. Until 2026-10-04 an enrollment ticket
recorded no member: `mint_invite` never passed one, and the credential built at
redemption fell back to the deployment owner. Dashboard **Regenerate** on a
teammate's device (`POST /enroll/invite` with that `device_id`) therefore handed the
teammate a credential that authenticated as the owner — the owner's member-private
docdex/maildex content and vault entries, and every later write attributed to the
owner. Now the ticket carries the member (the verified issuer's for a new device;
the device's existing member, resolved from its credential records and refused on
disagreement, for a regeneration), the member must be active in the workspace when
the code is issued, and the redeeming Lua script re-checks the member row and the
ticket's pinned member/device in the same atomic operation that registers the
credential. Tickets from an older server carry no member and are refused unspent
(`unattributed`). Guarded by `cortex/tests/test_enroll_api.py` and, against real
Redis, `test_enroll_redis_integration.py`.

**Validation integrity — fixed, with a migration.** The same default lived in
`validate_key_by_hash`: a stored record with no `member_id`/`workspace_id`
authenticated as the deployment owner, the member's status was never read, an
unparseable or naive `expires_at` meant "never expires", and the stored scope
value was trusted — a dict or a string made `"*" in scopes` a key lookup or a
substring test, so a stored `"x*"` was a wildcard. Now a record must name its
workspace (this one), member and credential id; the member row must exist,
match, and be `active`; the expiry must parse with a timezone; and the scope
document must be a list of strings (unknown strings are dropped, not fatal,
because legacy teammate keys carry the retired `twin:read`). Records that were
relying on the owner fallback are stamped to the owner **explicitly and logged**
by `deploy/bootstrap-keys.sh` before the services restart and by cortex-api on
every boot; `deploy/firekeep-admin keys audit` reports, read-only, which
credentials will authenticate. Residuals: ~~there is still no member-removal
path~~ (closed 2026-10-05: §5.17 sets `status: removed`);
`auth:cred` reverse-mapping and index membership are not required; keys minted
with `POST /auth/keys` belong to the owner by design. Guarded by
`auth/tests/test_credential_validation_integrity.py`,
`cortex/tests/test_workspace_backfill.py` and
`deploy/tests/test_bootstrap_keys.sh`.

### 5.16 Naming a session you do not own (2026-10-05)

**Fixed 2026-10-05.** Two Cortex writes took a client-named session id on trust.

*`POST /skill/evaluate`.* Any `eval:write` key (every member key) could queue skill
synthesis for any session id. The synthesis worker then read that session with
`FIREKEEP_INTERNAL_KEY` (`session:read:workspace`) and filed a draft built from its
goal, outcome and shadow in the review queue every `memory:read` holder lists — a
member could publish a teammate's session as a skill draft. The route now proves the
caller can read the session *before* queuing: it presents the caller's own key to
Bridge's `GET /sessions/{id}`, so the answer is Bridge's `session_owned_by` verbatim
(the owner passes; `session:read:workspace` holders, i.e. the internal key and `*`
keys, pass within their workspace; a legacy session passes only for the deployment
owner). Anything Bridge refuses is reported as 404, so a guessed id discloses
nothing. An `admin` key passes without the round trip (it administers the queue).
Bridge unreachable or erroring is **503, nothing queued** — fail closed. Bridge's own
call (ctx_complete_session forwards the completing caller's key, #47) is the owner's
and keeps working; `SKILL_SYNTHESIS_ENABLED=false` still answers `disabled` first.

*Replay event writes.* §5.13 stamps every event with its verified writer and hides
foreign events from readers, but eval compute, OWM, the pattern engine and the
autopilot compute over every event filed under a session, so events written under
another member's `X-Session-Id` skewed her metrics. Every Cortex request-path emit
goes through `app.main._replay_emit`, which now files an event whose verified writer
does not own the named session under `"unknown"` (the id Cortex already uses when no
header is sent) — the event survives, with its stamp, in the writer's own timeline.
The agent gateway applies the same rule to `action_before`'s `session_id` at the
REST boundary, which also keeps the prediction record (that the reconcile and the
Celery overdue sweep emit under) and the per-session rethink counter out of a
teammate's session. The owner comes from `cortex/app/session_owner.py`: an
in-process cache (owners never change), then the session's `session_start` event in
replay, which only Bridge emits and stamps with the recorded owner, then — only for a
start event with no stamp — Bridge's `GET /sessions/{id}` with the internal key, which
now returns `owner_member`/`owner_workspace`. That fallback exists because Bridge
began stamping start events on 2026-10-04, in the same deploy: without it every
session in flight at that deploy would fall to the legacy rule and its own member's
events would be re-filed, changing what eval compute grades. The Bridge read never
runs inside a request: it is scheduled in the background (at most 4 in flight, 0.5 s
hard timeout, a failed read not retried for that session for 30 s) and the emit that
scheduled it keeps its claimed id; later emits use the cached answer. So recall
latency does not depend on Bridge: a cached session is a dict lookup, a cold one
three replay Redis round trips once per process. Eval compute and `_trigger_eval` are untouched. One rule decides
ownership in both services: `auth.principal.owns_session`, which Bridge's
`session_owned_by` now delegates to. Bridge's own emits were already owner-stamped
after its ownership checks — not a gap. Auth-disabled mode checks nothing. Guarded by
`cortex/tests/test_session_ownership_writes.py`, `auth/tests/test_owns_session.py`
and `bridge/tests/test_rest_session_ownership.py`.

**Residuals.** Replay attribution fails **open** while the owner is not known: the
first emit of each unstamped session in each process (its Bridge read is still in
flight), every emit while that read keeps failing (Bridge unreachable,
`FIREKEEP_INTERNAL_KEY` missing or without `session:read:workspace`), and emits
dropped past the in-flight bound. Failing closed would re-file legitimate events of
in-flight sessions; blocking would put Bridge on the recall path. The window covers
only sessions whose start event carries no stamp; it is logged and counted
(`session_owner.get_stats()["unresolved"]`, `["bridge_saturated"]`), and readers
still hide foreign events.
A Bridge 404 — a session Bridge does not know, an expired one, or one in another
workspace than the internal key's — falls to the legacy rule (deployment owner only);
for an unstamped session in a second workspace that re-files its own member's events.
`POST /evals/sessions/{id}/compute` still recomputes any session for an `eval:write`
key: it reads events but writes none, and changing it is out of scope (Outcome
Truth).

### 5.17 Removing a member (2026-10-05)

**The gap — closed.** There was no way to remove a person from a workspace. Since
§5.15 `validate_key` refuses every credential whose member row is not `active`,
but nothing ever wrote another status, so the only lever was revoking a leaver's
devices one credential at a time — and any join code still outstanding for them
(a device invite, the code their accepted member invite hands back on replay)
could mint a fresh one.

**The operation.** `DELETE /members/{member_id}` (admin), dashboard **Members →
Remove** (confirm dialog), or `deploy/firekeep-admin members remove <id>` — all one
implementation, `auth/members.py` `remove_member`. In this order, which is the
concurrency argument: (1) the member row is set to `status: removed` (kept, never
deleted — memories, sessions, replay and relay records stay attributed to it);
from this write on every credential naming the member is refused by every
service, and the redeeming Lua script refuses their join codes unspent
(`member_inactive`), so no credential can be registered for them afterwards;
(2) every `auth:key:*` record naming the member is deleted with its `auth:cred:`
mapping and `auth:key_index` entry — found by a keyspace scan, so an unindexed
record is caught too, and anything registered before (1) is found here;
(3) their unredeemed join codes are deleted and their member invites marked
`member_removed`; replaying an accepted invite is refused before it returns a
code. Idempotent: a repeat reports `already_removed: true` and re-sweeps, which
is also how a removal interrupted between (1) and (2) is finished (`keys audit`
lists any leftover as "member not active").

**Refused.** The deployment owner (`FIREKEEP_OWNER_MEMBER_ID`, or any row with
role `owner`) can never be removed — 409. An admin cannot remove the member it
is acting as. A removal that would leave no active member holding the owner
role or an `admin`/`*` credential is refused; today every admin-capable
credential (bootstrap, dashboard, `POST /auth/keys`) belongs to the owner and
`ensure_workspace` keeps the owner active, so this check cannot fire — it is
defense in depth, **not** a live gap. Unknown, malformed and other-workspace ids
are one 404.

**Caches — none.** Cortex REST, cortex-mcp, Bridge, Relay and Sentinel all
authenticate per request through `FirekeepKeyAuthMiddleware` → `validate_key`,
which reads Redis DB 7 every time; no service caches an identity, and all four
MCP servers run `stateless_http=True`, so no MCP session outlives a request.
The window is a request already past the middleware (one streaming recall at
most). Bridge's distiller writes a pending distillate for the session's owner
through `/memory/learn/delegated`, which re-checks the member is active: a
removed member's queued distillations fail, retry within `MAX_ATTEMPTS`, and land
in the DLQ — never re-attributed to anyone.

**Their data — kept, re-stamped to nobody.** Removal transfers nothing:
member-private memories (`visibility: member`), Bridge sessions, Relay presence,
DMs and leases, and member-owned vault secrets stay attributed to the removed
`member_id`. No credential can authenticate as that member, so no
member-principal read can return their private data; operator surfaces see
exactly what they saw before — `/memory/export` and admin vault reads were never
member-filtered (§5.9, `docs/guides/dexes.md` "The threat boundary"), and
removal neither widens nor narrows them. The one thing that changes risk: a
removed member's `maildex.<id>` app password is a live credential to a
third-party mailbox that nobody can now act for. The operator should delete it
(`DELETE /vault/secrets/maildex.<id>`, admin) and the member should revoke it at
their provider. Relay presence has no expiry; an admin removes it from the
dashboard (§5.14 lets admin act on any member's Relay rows). **Not built:** a
purge of a removed member's memories across Qdrant and Neo4j — a destructive
multi-store operation that deserves its own reviewed change; until then the data
is retained, unread by members, and returns to its owner on restore.

**Restore.** `POST /members/{id}/restore` (admin; body = the device-invite
connection fields) or dashboard **Restore** reactivates the **same** member and
mints one join code whose credential belongs to them — their history and private
memories are theirs again. Only a removed member qualifies (409 otherwise), so
this is not a way to mint codes for active members. Shell:
`firekeep-admin members restore <id>` then `firekeep-admin invite --member <id>`.
Old credentials stay deleted.

Guarded by `auth/tests/test_member_removal.py`,
`cortex/tests/test_member_removal.py` (401 on Cortex, owner refused, join codes
cancelled and — against the real redeem script when `lupa` is installed —
refused), `cortex/tests/test_member_removal_cli.py`,
`bridge/tests/test_removed_member_key.py`,
`relay/tests/test_removed_member_key.py`, `tests/test_dashboard_members.py` and
`deploy/tests/test_firekeep_admin.sh`.

### 5.18 Relay leftovers: service key, replay attribution, retention (2026-10-05)

**`RELAY_INTERNAL_API_KEY` — retired.** After §5.14 Relay presented its service
key only with auth off, where Bridge installs no auth middleware and checks no
key — so with auth on nothing used it, and with auth off nothing needed it. It
was still minted on every install: an owner-member credential with
`session:write`, in `.env`, imported by every `env_file: .env` container. A
leaked copy could write into any owner or legacy Bridge session. Now Relay has no
`FIREKEEP_API_KEY` setting (a lingering `NR_FIREKEEP_API_KEY` is ignored) and
sends no key with auth off; compose and `.env.example` no longer wire it;
`deploy/bootstrap-keys.sh` does not mint it and, on an existing deployment,
revokes the record (record, `auth:cred` mapping, `auth:key_index` entry) **only
if its device is `firekeep-relay`** — a variable an operator re-pointed at some
other credential is reported and left live — and removes the `.env` line either
way. `update.sh` runs that before `compose up`, so a relay still running the old
image loses its best-effort scope-decision writes for the restart window only.
Guards: `relay/tests/test_principal_binding_scope.py`,
`deploy/tests/test_bootstrap_keys.sh` (Run R), `deploy/tests/test_auth_posture.sh`.

**Relay's replay events — attributed.** Relay emitted its coordination, claim and
release events (task created/updated/deleted, DMs, bulletins, broadcasts,
presence, leases, claims) with no `workspace_id` / `member_id`, so under §5.13 a
teammate's own Relay activity was visible only to the deployment owner and
admins. Each emit now carries the verified caller's stamp — `Caller.stamp()` or
the task's `created_by` / `updated_by` principal, never the `agent_id` /
`from_id` / `assigner` label — and only when that caller was authenticated; with
auth off the stream is unchanged. The REST `POST /tasks` (Cortex's fleet enqueue,
on the owner-member internal key) is stamped as the owner member, which is what
an unstamped event already meant. Events emitted before this change stay
owner-only until they age out. Guard: `relay/tests/test_replay_stamping.py`.

**Replay retention — enforced.** "Age out" above was only half true:
`trim_old_events` was never scheduled, so `rp:events` was bounded by
`RP_STREAM_MAXLEN` alone while each event's `rp:eid:` lookup key expired at
`RP_RETENTION_DAYS`. Events past retention — recall text, file paths, snapshot
references — stayed readable to the unscoped in-process readers. cortex-beat now
runs the trim daily (`replay-trim`, `RP_TRIM_INTERVAL_SECONDS`), draining every
expired entry, its session-index entry and its lookup key. The retention value
is unchanged (30 days). Guards: `replay/tests/test_trim_retention.py`,
`cortex/tests/test_replay_trim_task.py`.

## 6. Threats, ranked

| # | Threat | State |
|---|---|---|
| 1 | Unauthenticated read of the vault over the network | **Fixed** — three independent layers (§5.1) |
| 2 | Release-host compromise → arbitrary code on every dev machine | **Mitigated, active since 2026-08-12** — signed `SHA256SUMS` verified against the Ed25519 key pinned in client 0.1.42+; enforcement on by default since 2026-10-01 (`[dist] require_signed`, opt-out documented); residuals: TOFU first install, the operator opt-out, unsigned-downgrade window (§5.6) |
| 3 | A new route under a skip-list prefix is silently public | **Partly mitigated** — prefix/exact split; no test enumerates skip-list reachability |
| 4 | `.env` read → total compromise (VAULT_KEY, Neo4j, all keys) | **Accepted** — plaintext by design; `chmod 600` documented. Sentinel no longer mounts it. |
| 5 | Compromised agent with a valid non-admin key poisons memory | **OPEN, unmitigated** — writes are attributed but not validated, and poisoned memories are recalled like any other |
| 6 | SSRF via the crawler | **Mitigated**, DNS rebinding accepted (§5.3) |
| 7 | Lateral movement inside the Docker network | **Accepted** — single trust zone (§3) |
| 8 | Dependency CVE in a shipped wheel | **Now scanned** — `pip-audit` per dependency set in CI |
| 9 | Prompt injection reaching a tool call | **OPEN, out of our control** — the runtime's boundary, not ours; the gateway is advisory (§5.4) |
| 10 | Unauthenticated field-failure collector fabricates/floods failure data | **Mitigated, residual accepted** — enum-value validation, released-version allowlist, mail budget, locked state, sealed caps (§5.7); data stays low-integrity by construction and is labelled `integrity: "unverified"` downstream |
| 11 | A compromised runtime with Hands enabled operates the human's desktop | **Mitigated, residuals OPEN** — the broker is a separate process with no grant route, injected input is rejected, permits are one-use and bound to the exact step, classification is on effects not model labels, fail closed (§5.8). Residuals: same-user permit theft, kernel-level injection, screenshots to the model provider, the unverified macOS source-state filter, and the broker's notification being informational (the chord approves the oldest pending permit whether or not the toast was read) |
| 12 | Phone approvals approved by a key holder who is not the human | **Partly mitigated (2026-10-01), residual OPEN** — relay stamps the verified principal on every task write and the broker refuses an approve from the requesting credential (the kit key the driving agent shares), from an unauthenticated Keep, or from a relay too old to stamp. Residual: any *other* workspace credential can still approve unless `phone_approvers` pins the approvers, and a pinned dashboard credential is only as strong as its basic-auth password; the auth layer has no human-member notion. `phone_approvals` stays `False` by default (§5.8) |
| 13 | A valid key of any scope deletes another member's data, approves its own skill, re-embeds the store, or reads teammates' recall queries | **Mitigated 2026-10-01** — scope + `memory_type` + workspace checks on `/skills/{id}`, `admin` for review decisions and re-embedding, member-scoped `/audit`, scopes declared on every core memory route (§5.10). Residuals: review decisions allowed on auth-disabled boxes, unattributed audit history hidden from members (the `GET /skills` / `/memory/contributors` workspace gap closed 2026-10-04, §5.10.1) |
| 13a | A valid key lists other workspaces' skills, files skills or queues synthesis with any scope, or votes repeatedly — or on memory it cannot recall — to move recall ranking | **Mitigated 2026-10-04** — scopes on every `/skills` route, workspace-filtered skill and contributor listings, feedback confined to recallable points with one ballot per key, `reauthor_of` resolved as a skill in the caller's workspace, `admin` on `/admin/untagged-calls` (§5.10.1; contributors also honour member visibility, §5.12). `/skill/evaluate` session ownership: fixed 2026-10-05 (§5.16, row 20) |
| 14 | `GET /briefing?agent_id=<teammate>` reads a teammate's sessions and presence with the internal service key | **Mitigated 2026-10-01** — user-scoped sections present the caller's own key (§5.11) and Bridge filters `GET /sessions` by the verified owner member (§5.9) |
| 15 | A teammate key reads or writes another member's sessions or member-private memory inside one workspace | **Partly mitigated (2026-10-01, 2026-10-04)** — Bridge recalls with the caller's key and gates every session path on workspace+member (§5.9); distillates are written for the session's verified owner (§5.12, row 16); `/memory/feedback` refuses a teammate's member-private memory and `/memory/contributors` honours visibility (§5.10.1, §5.12). Relay's scope-session `agent_id` is now bound to the verified member and Relay writes decisions with the owner's key (§5.14, row 18) |
| 16 | A memory's author is a self-asserted label; distillates of every member's sessions are attributed to the owner | **Mitigated 2026-10-04** — writes record the verified member and credential, contributors group by member inside the caller's workspace and visibility, and Bridge distils through the literal-scope `/memory/learn/delegated` naming the session's verified owner (§5.12; this closes row 15's "owner-attributed distillates"). Residuals: the one service key holding `memory:write:delegated` can name any active member; identical-text relearns name the latest writer |
| 17 | A teammate key reads another member's replay timeline, events, context snapshots or evals | **Mitigated 2026-10-04** — every replay/eval read is filtered per event by the verified writer's stamp; unattributed history belongs to the deployment owner; service keys still compute all evals (§5.13). The write-side residual (events written into another member's session id) was fixed 2026-10-05 (§5.16, row 20) |
| 18 | A teammate key reads another member's Relay DMs, posts as her, or releases her leases; Relay writes scope decisions into Bridge as the owner | **Mitigated 2026-10-04** — every Relay record is owned by the verified member that wrote it and checked on read and mutation; scope decisions reach Bridge only with the owning member's key (§5.14). Member-bound operations require `relay:read`/`relay:write`, and the internal key's two Relay writes use the service-only `relay:write:service`. Residuals: first-come presence labels at upgrade, owner-only DMs to unbound labels, single-workspace reads |
| 19 | A credential authenticates as a member it was not issued to (regenerated teammate device minted as the owner; any unattributed record treated as the owner; a malformed scope document read as a wildcard) | **Fixed 2026-10-04** — join codes carry their member, regeneration keeps the device's member, redemption re-checks it atomically; validation reads attribution and member status and checks the scope document; legacy records are stamped to the owner explicitly and logged (§5.15). Member removal: row 21 |
| 20 | A member key queues skill synthesis of a teammate's session (the worker reads it with the internal key and files a draft every `memory:read` holder sees), or writes replay events under a teammate's session id to skew her evals, OWM and patterns | **Fixed 2026-10-05** — `/skill/evaluate` presents the caller's key to Bridge and queues nothing unless Bridge lets it read the session (404 otherwise, 503 if Bridge is down); a request-path replay event or gateway action naming a session its verified writer does not own is filed under `"unknown"` (§5.16). Residual: attribution fails open while the owner of an unstamped session is not yet known (its Bridge read runs off the request path) or cannot be resolved |
| 21 | A person who has left keeps working credentials or an outstanding join code, and there is no way to remove them | **Fixed 2026-10-05** — admin-only removal (REST, dashboard, `firekeep-admin`) flips the member to `removed` (every service refuses their keys on the next request; no auth cache exists), deletes every credential naming them, and cancels their join codes; the owner can never be removed; restore brings back the same member with a new code (§5.17). Residuals: member-private data is retained (operator-visible as before, readable by no member) with no purge tool; a removed member's `maildex.<id>` app password stays in the vault until an operator deletes it |
| 22 | Relay leftovers: an unused owner-member service key (`RELAY_INTERNAL_API_KEY`, `session:write`) in every `.env`; Relay's replay events unattributed (a member's own activity hidden from her); replay events kept past `RP_RETENTION_DAYS` | **Fixed 2026-10-05** — the key is no longer minted, wired or read, and `update.sh` revokes an existing `firekeep-relay` credential and removes its `.env` line; Relay stamps every event with the verified caller; cortex-beat trims the stream daily (§5.18). Residual: pre-change Relay events stay owner-only until trimmed |

Threat 5 deserves emphasis because it is the one the product's own design creates:
Firekeep exists to make agents act on stored memory. Anything that can write a
memory can influence a future agent's behaviour, and there is no provenance
weighting that would let a reader distinguish a poisoned memory from a good one.
Until 2026-10-04 this paragraph claimed "`agent_id` attribution records who wrote
it" — that was false: `agent_id` is the client-chosen `X-Agent-Id` label. Every
write now records the verified `member_id` and `credential_id` (and, for a
service writing on a member's behalf, the service's credential) — §5.12. That
supports forensics after the fact; it still prevents nothing.

## 7. Not claimed

- No formal audit by a third party.
- No penetration test.
- No cryptographic review of the Fernet usage beyond "it is the library's intended API".
- Multi-tenancy is not a goal and is not defended against.
- `AUTH_ENABLED=false` and `BIND_ADDR=0.0.0.0` are supported configurations whose
  consequences are documented; they are not defects.
