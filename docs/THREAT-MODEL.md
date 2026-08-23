# Firekeep Threat Model

**Date:** 2026-07-26
**Scope:** all four services (Cortex, Bridge, Sentinel, Relay), the dashboard, the
client kit, and the URL crawler.
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
`flock()`'d critical section with atomic temp-file+rename writes; and both the
active log and the sealed-segment total are size- and count-capped, so an
unauthenticated unlimited write endpoint cannot fill the disk that also holds the
support mailboxes. **Residual, accepted:** the data is low-integrity by
construction — an attacker can fabricate failure patterns or bury a real one in
noise — so every event that reaches Sentinel is labelled `integrity: "unverified"`
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
