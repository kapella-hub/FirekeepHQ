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

**OPEN:** `SHA256SUMS` itself is fetched over TLS but is **not signed**. Whoever
controls the release host controls what every client installs. Signing it, and
pinning the key in the bootstrap, is the fix. Until then the release host is a
single point of total compromise for every developer machine.

## 6. Threats, ranked

| # | Threat | State |
|---|---|---|
| 1 | Unauthenticated read of the vault over the network | **Fixed** — three independent layers (§5.1) |
| 2 | Release-host compromise → arbitrary code on every dev machine | **OPEN** — `SHA256SUMS` unsigned (§5.6) |
| 3 | A new route under a skip-list prefix is silently public | **Partly mitigated** — prefix/exact split; no test enumerates skip-list reachability |
| 4 | `.env` read → total compromise (VAULT_KEY, Neo4j, all keys) | **Accepted** — plaintext by design; `chmod 600` documented. Sentinel no longer mounts it. |
| 5 | Compromised agent with a valid non-admin key poisons memory | **OPEN, unmitigated** — writes are attributed but not validated, and poisoned memories are recalled like any other |
| 6 | SSRF via the crawler | **Mitigated**, DNS rebinding accepted (§5.3) |
| 7 | Lateral movement inside the Docker network | **Accepted** — single trust zone (§3) |
| 8 | Dependency CVE in a shipped wheel | **Now scanned** — `pip-audit` per dependency set in CI |
| 9 | Prompt injection reaching a tool call | **OPEN, out of our control** — the runtime's boundary, not ours; the gateway is advisory (§5.4) |

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
