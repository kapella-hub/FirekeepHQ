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
   │             + /docs /redoc /openapi.json /dashboard (Cortex)│
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

### 5.2 The dashboard `:8040`

nginx with basic auth, injecting an admin-scoped `DASHBOARD_API_KEY` on every
`/api/*` proxy. Two consequences worth naming:

- The basic-auth file is the only thing between a reachable dashboard and admin
  authority. It is generated with SHA-512 crypt; an earlier version used apr1-MD5.
- **OPEN:** there is a second, older dashboard served by cortex-api itself at
  `/dashboard/`. It has no key mechanism at all, so under `AUTH_ENABLED=true` its
  data tabs simply fail. It is superseded but still shipped.

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
- **Enforcement is off by default, and while it is, absence is
  attacker-choosable.** Releases predating signing have no `.minisig`, so
  absence is a one-line warning, not a failure, until
  `[dist] require_signed = true` — the flip waits until every supported version
  is signed. Named plainly: an attacker with host write access can just publish
  *unsigned* and the default installs it with a warning — tolerating absence is
  the explicit cost of the migration default, removed only by flipping
  `require_signed`. The warning is therefore made impossible to lose: the
  background auto-update runs detached with stderr on DEVNULL, so the client
  persists an unsigned-install marker and the next session-start briefing
  prints it once. An *invalid* signature is fatal regardless of the flag:
  invalid is tampering evidence, absence is history. **Active in the field
  since 2026-08-12:** keys minted (ID `7D6D83D1240D4A61`, private half in the
  `FIREKEEP_SIGNING_KEY` Actions secret and offline with the operator), the
  public key pinned from client 0.1.42, and release 0.1.42 published signed —
  the workflow's serve-verification byte-compared the live `.minisig` against
  the built one. `require_signed` stays default-false for one release cycle
  (a flipped default with a misconfigured secret would stall every client's
  updates; 0.1.42 is the production evidence the flip waits for).
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
4. **A holder of the workspace API key who is not the machine's human** — a
   second agent, a teammate, anything with the key. Relevant only when phone
   approvals have been turned on.

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
  `boundary` is the catch-all: any app not declared or allowlisted, any domain not
  allowlisted.
- **No model-supplied coordinates.** Actions carrying `x`/`y`/`point` are
  rejected; every pointer action resolves a `ref` from the observation the runtime
  was shown, and Hands computes the point from that control's own rect.
- **The permit text is built by Hands, not by the model** — from the routed
  control's own name and the window's app, whitespace-collapsed, stripped of
  unprintable characters and capped at 60 characters.
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
  something else, in the window before it is consumed or expires. Note also that
  the spec's claim that nothing secret is stored on disk is not true as built —
  the bearer token is in `broker.json`, minted per broker run.
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
- **The broker renders no prompt of its own.** It has no window and no
  notification. On the chord path the human sees the step description only if the
  runtime relays `needs_permit.title` to them — and the runtime is the thing being
  gated. A model cannot forge that text (Hands builds it from the routed control),
  but it can decline to show it, or bury it. The phone path is the only one where
  the human reads the broker's own text on a surface the model does not draw.
- **Phone approvals, when opted in, trust every holder of the workspace API
  key.** Relay's `update_task` records status, result and assignee and **not the
  completing principal**, so an approved `hands_permit:` task proves only that
  somebody with the key completed it. The driving agent holds that key and reaches
  `relay_task_list`/`relay_task_update` through the MCP surface it already has, so
  it can complete its own permit task and be granted the permit within one poll. A
  local one-time code would not close it either — Hands can screenshot the screen
  the code is on. **This is why `phone_approvals` defaults to `False`**; PR2 closes
  it by stamping the verified principal on relay task updates, at which point the
  broker can require a human member.
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

## 6. Threats, ranked

| # | Threat | State |
|---|---|---|
| 1 | Unauthenticated read of the vault over the network | **Fixed** — three independent layers (§5.1) |
| 2 | Release-host compromise → arbitrary code on every dev machine | **Mitigated, active since 2026-08-12** — signed `SHA256SUMS` verified against the Ed25519 key pinned in client 0.1.42+; residuals: TOFU first install, enforce-off default (flip pending one cycle of production evidence), unsigned-downgrade window (§5.6) |
| 3 | A new route under a skip-list prefix is silently public | **Partly mitigated** — prefix/exact split; no test enumerates skip-list reachability |
| 4 | `.env` read → total compromise (VAULT_KEY, Neo4j, all keys) | **Accepted** — plaintext by design; `chmod 600` documented. Sentinel no longer mounts it. |
| 5 | Compromised agent with a valid non-admin key poisons memory | **OPEN, unmitigated** — writes are attributed but not validated, and poisoned memories are recalled like any other |
| 6 | SSRF via the crawler | **Mitigated**, DNS rebinding accepted (§5.3) |
| 7 | Lateral movement inside the Docker network | **Accepted** — single trust zone (§3) |
| 8 | Dependency CVE in a shipped wheel | **Now scanned** — `pip-audit` per dependency set in CI |
| 9 | Prompt injection reaching a tool call | **OPEN, out of our control** — the runtime's boundary, not ours; the gateway is advisory (§5.4) |
| 10 | Unauthenticated field-failure collector fabricates/floods failure data | **Mitigated, residual accepted** — enum-value validation, released-version allowlist, mail budget, locked state, sealed caps (§5.7); data stays low-integrity by construction and is labelled `integrity: "unverified"` downstream |
| 11 | A compromised runtime with Hands enabled operates the human's desktop | **Mitigated, residuals OPEN** — the broker is a separate process with no grant route, injected input is rejected, permits are one-use and bound to the exact step, classification is on effects not model labels, fail closed (§5.8). Residuals: same-user permit theft, kernel-level injection, screenshots to the model provider, the unverified macOS source-state filter, and the broker rendering no prompt of its own |
| 12 | Phone approvals approved by a key holder who is not the human | **OPEN, mitigated only by the default** — relay records no completing principal, so any workspace-key holder (the driving agent included) can complete a `hands_permit:` task. `phone_approvals` is `False` by default and the guide discloses the trade; PR2 stamps the principal (§5.8) |

Threat 5 deserves emphasis because it is the one the product's own design creates:
Firekeep exists to make agents act on stored memory. Anything that can write a
memory can influence a future agent's behaviour, and there is no provenance
weighting that would let a reader distinguish a poisoned memory from a good one.
`agent_id` attribution records who wrote it, which supports forensics after the
fact but prevents nothing.

## 7. Not claimed

- No formal audit by a third party.
- No penetration test.
- No cryptographic review of the Fernet usage beyond "it is the library's intended API".
- Multi-tenancy is not a goal and is not defended against.
- `AUTH_ENABLED=false` and `BIND_ADDR=0.0.0.0` are supported configurations whose
  consequences are documented; they are not defects.
