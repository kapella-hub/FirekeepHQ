# Client Enrollment — Join Codes

**Status:** design, approved 2026-07-30
**Supersedes:** nothing. Extends `firekeep connect` (`client/firekeep_client/connect.py`).

## Problem

Installing the client against a real server requires the user to supply three
free-text answers — a host, an API key, and a choice between the `personal` and
`office` profile shapes — and the installer verifies none of them. Three
consecutive onboarding failures on 2026-07-29, all presenting identically as
"cannot connect", none of which was a network fault:

1. The public VPS IP was entered while every app port was bound to loopback. The
   only honest signal was a TCP probe; ICMP answered fine and implied the
   opposite.
2. A second machine had no API key at all. `AUTH_ENABLED=true` returned `401`,
   which the `session_start` briefing surfaced as a failed connection.
3. The key was pasted into `[office]` while the active profile was `[personal]`
   (`resolver.py:295` reads `api_key` from the active profile only), alongside a
   host typo — `100.91.3.515` — that the client accepted without complaint,
   because `resolver.py:304` interpolates the host into a URL without validating
   its shape.

`firekeep connect <user@host>` already automates mint-tunnel-verify and its module
docstring (`connect.py:1-25`) documents all three failure modes verbatim. It went
unused because it appears in `README.md` and nowhere else: not in `CLAUDE.md`'s
Local setup section, not in either bootstrap's output, and not in the installer's
own `NEXT STEPS` text (`cli.py:475-481`), which instead says "edit
`~/.firekeep/config`". **The good path exists and is invisible at the moment it is
needed.**

## Goal

One opaque join code replaces every free-text field. Three issuers, one
redemption path. A field the user cannot type is a field the user cannot typo.

Non-goals: a hosted control plane, OIDC/device-code enrollment, and a dashboard
issuance UI. Each is an additive front door onto the redemption path defined here;
none changes it.

---

## 1. Architecture

```
ISSUE (front doors)                        REDEEM (one path, always)
  firekeep-admin invite --agent bob          GET  /enroll/anchor?tid=…   (t=tls only)
  firekeep connect root@host (self-issue)    POST /enroll {ticket}
  [later] dashboard / hosted control plane   firekeep join fk_join_<code>
```

Ticket records live in Redis DB 7 beside the keys they mint:

```
auth:enroll:<sha256(ticket)>  hash {agent_label, scopes, ca_pem?, transport,
                                    host|base_url, ssh_target?, created_at,
                                    expires_at, expires_at_epoch,
                                    used_at?, issued_key_id?, issued_key_hash?,
                                    issuer}
                              EXPIRE 7d          (tombstone, NOT validity)
auth:enroll:index             zset tid -> created_at, no TTL
auth:enroll:rate:<YYYYMMDDHH> counter, EXPIRE 2h
```

### 1.1 The endpoint is `POST /enroll`, cortex-owned, mounted unconditionally

Not `/auth/enroll`. Two independent hard failures block that path on any
`AUTH_ENABLED=false` deployment — which is every deployment upgraded from a
pre-2026-07-26 `.env`, since compose interpolates the existing value
(`docker-compose.yml:248-251`):

- The entire `/auth` prefix is replaced by `_admin_surface_disabled_router("/auth", …)`
  (`cortex/app/main.py:240-246`), whose `@router.api_route("/{_unused:path}")`
  (`main.py:162-168`) returns 503 for every method with `_ENABLE_AUTH_HINT`
  (`main.py:143-147`) appended — remediation text that tells the caller to find an
  admin key "printed exactly once", the precise dead end `connect.py:12-15` exists
  to end.
- The lifespan calls `await init_auth(enabled=False)` with no client
  (`main.py:606-608`), leaving `auth/keys.py:62` `_redis = None`, so `create_key`
  raises `RuntimeError("Auth not initialized")` (`keys.py:187-188`). Mounting the
  route elsewhere is necessary but not sufficient.

Therefore:

- New package `cortex/app/enroll/` (`api.py`, `store.py`, `lua.py`, `mint.py`),
  registered in `_register_feature_routers` **unconditionally** — never inside
  `_register_admin_surface_routers`.
- The router builds its own `redis.asyncio` client from `AuthSettings.REDIS_URL`
  and never reads the `auth.keys` module global. This needs one auth-layer change:
  `create_key(..., redis_client=None)` gains an explicit-client seam **exactly
  mirroring** the one `validate_key` already has (`keys.py:271-284`), whose
  docstring documents the same motivation (callers that never run `init_auth()`).
  No other `auth/` restructuring — the enroll route lives in cortex so the shared
  `auth/` package never imports `app.config`, preserving the layering `keys.py:1-12`
  protects.
- Auth-off behaviour is **defined, not inherited**: `POST /enroll` returns **409**
  with the enrollment-specific truth (§4), and `invite` refuses to mint on an
  auth-off server with the same sentence.
- `AUTH_SKIP_EXACT_PATHS` becomes `("/dashboard", "/dashboard/", "/enroll", "/enroll/anchor")`.
  **Never** `AUTH_SKIP_PREFIXES`: a bare `/dashboard` prefix is what served full
  memory content unauthenticated until 2026-07-26 (`main.py:836-841`).
  `/enroll/anchor` must be listed individually — `skip_exact_paths` is a literal
  `in` test (`auth/asgi.py:77`).

### 1.2 The code authenticates the server to the client

The client today has no trust primitive beyond `resolver._verify_for`
(`resolver.py:237-265`) — no certificate pinning of any kind — and
`connect.py:181-182` writes `scheme=http, verify_tls=false`. A code carrying only
`{host, ticket}` lets anyone on the path harvest the ticket and silently repoint
the victim's entire stack. This is the invariant `kubeadm` enforces with
`--discovery-token-ca-cert-hash`; kubeadm names the alternative
`--discovery-token-unsafe-skip-ca-verification`, and an unpinned design is that
mode as the unnamed default.

For `t=tls`, redemption is two hops:

1. `GET /enroll/anchor?tid=<16 hex>` over an **unverified** context returns
   `{ca_pem}`. Public, non-secret, no credential sent. `tid = sha256(ticket)[:16]`,
   derived client-side — the id/secret split.
2. Client checks `b64url(sha256(ca_pem)[:16]) == f` from the code. Mismatch →
   **abort before any secret leaves the machine.**
3. Client builds a verified, hostname-checked context from `cadata=ca_pem` and only
   then sends `POST /enroll {ticket}`. An impostor may replay the public PEM but
   cannot present a leaf signed by that CA, so the handshake fails and the ticket is
   never transmitted.
4. The PEM is written to `~/.firekeep/<host-slug>-ca.crt` (0600) and becomes
   `ca_path` — the field `resolver.py:243-255` requires and which a naive payload
   would omit, making every `kind=paths` code unresolvable.

`f = "os"` is first-class: no anchor fetch, verify against the OS trust store via
`transport._build_ssl_context("os")` (`resolver.py:256-261`). That covers both the
MDM-managed corporate CA and any publicly-trusted certificate.

Cortex never reads the caddy_data volume. `invite` provisions `ca_pem` into the
ticket record at issue time from the export step `docs/DEPLOYMENT-OFFICE.md:94-109`
already documents.

### 1.3 Transport is derived server-side; loopback is supported, not refused

`.env.example:48` ships `BIND_ADDR=127.0.0.1` and all six app ports bind it
(`docker-compose.yml:234, 366, 561, 607, 672, 726`). Refusing to issue a loopback
code would leave the *default install* with no join path at all. `invite` runs in
the server shell and can read `VPS_IP` (`deploy/lib.sh:192-197`) and the effective
bind address exactly as `install.sh:454-469` already does.

| server `.env` | emitted `t` | how `join` reaches it |
|---|---|---|
| `BIND_ADDR` loopback (shipped default) | `tunnel`, `s=<user>@<VPS_IP>` | reuses `connect._tunnel_running()` / `_start_tunnel`, redeems over `http://127.0.0.1:8100` |
| network-reachable + TLS front, or `--ca-file` / `--ca os` | `tls` | direct, verified, pinned per §1.2 |
| network-reachable + plain http | **refused unless `--insecure-http`** → `t=http` | `join` prints the exposure verbatim, then redeems |

**Decision (2026-07-30):** plain HTTP across a network requires the operator to
pass `--insecure-http` explicitly. `BIND_ADDR=0.0.0.0` is a documented supported
configuration (`docker-compose.yml:222-224`) so it is not refused outright, but on
that path both the ticket and the minted key are observable, and `resolver.py:264-265`
will keep sending that key as `X-API-Key` on every subsequent request. Cleartext is
a deliberate, named act or it does not happen.

**Decision (2026-07-30):** v1 keeps `t=tunnel` for the shipped loopback default and
does **not** add a TLS front to the personal deployment. Consequence, stated
plainly: on the default install a join code cannot replace `firekeep connect` for a
teammate with no server account, because reachability requires SSH. Adding a Caddy
front to the personal stack — mirroring `docker-compose.office.yml` — is the
follow-up that makes `t=tls` the single default and `t=tunnel` a fallback (§7).

### 1.4 Mint, then burn

Burn-then-mint permanently spends a ticket on any `create_key` failure
(`keys.py:187-188`, `191-193`, three un-pipelined awaits at `211-218`), and there is
no plaintext to replay and no un-burn primitive (`keys.py:253-263`). Handler order:

1. global rate counter → 429 if over cap;
2. `PEEK` Lua (read-only) → 404 unknown / 409 used / 410 expired, **before** minting;
3. ticket scopes ⊆ `ENROLLABLE_SCOPES`, else 500 + `CRITICAL`;
4. normalize and de-collide `agent_id`;
5. `create_key(...)`;
6. `CLAIM` Lua — one round trip that `HSETNX`es `used_at` and writes
   `issued_key_id` / `issued_key_hash` together. Lost race → `revoke_key(new_key_id)`,
   then 409/410/404. Revoke failure → `CRITICAL` naming the `key_id`, never the
   plaintext.

A mint failure leaves the ticket unburned. Recovery from a genuinely burned ticket
is a second `invite`, which needs no admin credential
(`deploy/firekeep-admin:102-109`).

### 1.5 `HSETNX` must never create the hash

Redis `HSETNX` creates the key on a miss. On a deliberately unauthenticated
endpoint at the default `RATE_LIMIT=60/minute` (`cortex/app/config.py:12`) that is
~86k permanent, TTL-less `auth:enroll:*` keys per day, written into the same DB 7
that holds `auth:key:*` (`keys.py:64-65`) and `vault:secret:*` (`vault/store.py:17-18`)
per `docker-compose.yml:252, 256`. **Both Lua scripts begin with `EXISTS KEYS[1]`
and return `{'unknown'}` without writing.** Pinned by test (§5).

### 1.6 Rate limiting

`Limiter(key_func=get_remote_address)` (`main.py:135`) has no `storage_uri` — it is
in-memory and per-process — and keys on `request.client.host`. On a direct `:8100`
publish Docker's DNAT preserves the external source IP, so per-IP works; behind the
office Caddy (app ports pinned to loopback) or the dashboard nginx it collapses
every caller into one bucket. `@limiter.limit` is also per-route, so a new route
inherits nothing.

- `@limiter.limit(ENROLL_RATE_LIMIT)` (default `10/minute`) on the handler, which
  therefore takes `request: Request` first, as the four existing decorated routes do
  (`main.py:1094, 1163, 1338, 1378`);
- **plus** a Redis global ceiling `auth:enroll:rate:<YYYYMMDDHH>`,
  `ENROLL_MAX_ATTEMPTS_PER_HOUR=60`, `EXPIRE 2h` — the only control that survives the
  proxy collapse. Shared-fate by design: a flooded enrollment surface is a denial of
  onboarding, not a breach;
- ticket entropy is `secrets.token_bytes(32)` = 256 bits, above the house precedent's
  192 (`keys.py:170-175`). Brute force is not a threat at that width; the counters
  bound noise and cost.

### 1.7 `ENROLLABLE_SCOPES`

`create_key` accepts `admin` and `"*"` (`keys.py:191`) and `scopes_allow` honours
`"*"` (`keys.py:115`); nothing caps what a mint may request. Add to `auth/keys.py`:

```python
ENROLLABLE_SCOPES: frozenset[str] = frozenset(SCOPES - {"admin", "*"})
```

The enroll handler refuses (500 + `CRITICAL`) any ticket record asking outside it.
Not a live escalation path — `invite` has no `--scopes` flag and writing DB 7 already
implies a server shell — but it is the missing guard.

**`vault:read` stays in the set.** Removing it would reverse a decision documented
three times on 2026-07-29 (`keys.py:39-50`, `vault/api.py:42-55`,
`deploy/firekeep-admin:18-21`): without it an agent asked to deploy 403s on the
credential it needs. `ENROLLABLE_SCOPES` is exactly the `NON_ADMIN_SCOPES` literal at
`deploy/firekeep-admin:25`.

### 1.8 Provenance and key expiry

`create_key`'s metadata is only `{agent_id, scopes, created_at, key_id}` and
`expires_at` is written only `if expires_days:` (`keys.py:199-206`), so an enrolled
key would be permanent and untraceable.

- `create_key(..., extra: dict | None = None)` merges caller fields into the hash.
  The four canonical keys are reserved: an `extra` naming `agent_id`, `scopes`,
  `key_id` or `created_at` raises `ValueError`.
- Enrollment writes `enrolled_via=<tid>`, `enrolled_at`, and
  `issuer=<invite@shell | admin:<key_id> | dashboard:<user>>`. `list_keys`'
  projection (`keys.py:242-248`) gains `enrolled_via`.
- **Decision (2026-07-30):** `ENROLL_KEY_EXPIRES_DAYS=90` by default, overridable via
  `invite --expires-days N`; `0` means never and prints a warning. A lost machine
  expires itself, and the re-join path stays exercised rather than rotting.
- `auth:enroll:index` has no TTL, so the ticket→key link outlives the ticket record.

Existing auditing is adequate and unchanged: `ZRANGE auth:key_index 0 -1` enumerates
the inventory from the shell `invite` already runs in, and `revoke_key` takes any
short id from it (`keys.py:218, 253-263`). The API's `le=200` (`auth/api.py:63`) is a
request ceiling, not a storage limit.

### 1.9 Validity is a stored field, not the Redis TTL

Under `EXPIRE 24h` the record vanishes at the exact moment the server needs it to say
"expired", collapsing expired / never-existed / mangled-in-paste into one answer —
the same ambiguity class as a `401` reading as unreachable. Instead: the record stores
`expires_at` and `expires_at_epoch` (numeric, so the Lua comparison is arithmetic
rather than ISO string ordering) and carries a 7-day tombstone
(`ENROLL_TOMBSTONE_DAYS=7`). Validity is `ENROLL_TICKET_TTL_HOURS=24`; the tombstone
is what lets the server answer 410 with the real timestamp. Already-redeemed stays
distinguishable for the full window via `used_at`.

### 1.10 Single-use

**Decision (2026-07-30):** strictly single-use. No `--uses N`. A teammate with a
laptop and a desktop runs `invite` twice, which is the better outcome — each machine
gets its own `agent_id` and its own independently revocable key, and "was this code
shared?" stays answerable from the record.

### 1.11 `connect` is refactored to issue-then-redeem

`firekeep connect` already implements the whole redemption outcome without a ticket
(`connect.py:76-106` probe, `109-136` mint, `171-193` write, `234-243` verify) while
holding SSH, which strictly dominates a ticket. Keeping both would be two redemption
paths, contradicting this design's central claim.

- `connect` keeps `_probe_server` — the only code that reads the server's `BIND_ADDR`
  and `AUTH_ENABLED` (`connect.py:93-95`) — then runs
  `bash deploy/firekeep-admin invite --agent <id> --json` over SSH and hands the code
  to the **shared join core**.
- `_mint_key` is deleted. Its "server predates local minting" branch (`:127-133`)
  becomes "server predates enrollment — `git pull && bash update.sh` on the server".
- `_write_profile` becomes `config_write.upsert_profile()`, shared by `join`,
  `connect` and `install`, with three fixes: it does not flip `[active]` unless there
  is no `[active]` or `--activate` was passed; it prints one line per key it
  overwrote; and it refuses to change an existing section's `kind` without `--force`.

Two review claims are deliberately **not** carried into this design, having been
refuted on inspection: `%`-interpolation corruption does not occur (`configparser`
raises loudly on `%` and every Firekeep-minted secret is hex), and an `agent_id`
cannot inject an `api_key` option (`configparser.write()` escapes embedded newlines).

### 1.12 Per-machine `agent_id`

The client **proposes** `f"{agent_label}-{socket.gethostname().split('.')[0].lower()}"`
(label from the ticket record, so the admin's naming wins); the server **normalizes**
against `^[A-Za-z0-9_-]{1,64}$` (`auth/api.py:23` validates length only), rejects
out-of-charset values, de-collides against existing non-revoked keys by appending
`-2`, and returns the authoritative value, which the client writes verbatim.

Rationale: Bridge keeps one active session per `agent_id` and `START_SESSION_LUA`
pauses the prior active one (`bridge/app/session.py:167-177`), so two machines sharing
`bob` would pause each other's sessions; replay and eval joins also key on `agent_id`.
This is **not** justified by the session stash — `state.py:236-239` keys
`session_current_{agent}@{profile}` under the local platform cache dir, so two machines
never contend for it.

Honest limitation: `cortex/app/main.py:1129` and `:1205` read the self-asserted
`X-Agent-Id`, and no cortex handler reads the verified `scope["state"]["identity"]`
that `auth/asgi.py:112-117` attaches. Until recall/learn prefer the verified identity,
per-machine enrollment improves inventory and revocation granularity but does not
harden attribution. Named follow-up, out of scope here.

---

## 2. Join code format

```
fk_join_<BODY>.<CHK>
```

- `BODY` = `base64url(utf-8 JSON payload)`, unpadded.
- `CHK` = `base64url(sha256(BODY_ascii)[:3])`, unpadded → exactly 4 characters, 24 bits
  of error detection. `.` is outside the base64url alphabet, so the split is
  unambiguous.
- One token, no spaces — double-click selects it whole. Typical length 180–230 chars.

| key | type | required | meaning |
|---|---|---|---|
| `v` | int | always | format version, `1` |
| `t` | enum | always | transport: `tls` \| `tunnel` \| `http` |
| `k` | enum | always | profile kind: `ports` \| `paths` |
| `h` | str | iff `k=ports` | host |
| `u` | str | iff `k=paths` | `base_url` |
| `f` | str | iff `t=tls` | CA commitment: `base64url(sha256(ca_pem)[:16])` (22 chars) or literal `os` |
| `s` | str | iff `t=tunnel` | ssh target, `user@host` |
| `x` | str | always | ticket expiry, compact UTC `YYYYMMDDTHHMMSSZ`, **advisory only** |
| `q` | str | always | ticket secret, `base64url(32 random bytes)` (43 chars) |

`scheme` is deliberately **not** carried — it is derived (`t=tls` → `https`,
otherwise `http`). Two fields that can disagree about TLS is exactly the hazard
`resolver.py:313-318` refuses at resolve time.

`x` is advisory by contract. `join` never refuses on it; it is used only to word a
message *after* the server has answered 404/410, and if the local clock differs from
the response `Date` header by more than 5 minutes, `join` says so. A client with a
fast clock can never reject a code the server would have honoured.

### Decode / validate (client, stdlib only)

```
1. s = re.sub(r"\s+", "", pasted)              # kills mail-client line wrapping
   strip a leading "firekeep join " if the user pasted the whole command
2. require s.startswith("fk_join_")            -> E_NOT_A_CODE
3. rest = s[8:]; require rest.count(".") == 1  -> E_MALFORMED
   BODY, CHK = rest.split(".")
4. b64url(sha256(BODY)[:3]) == CHK             -> E_DAMAGED   (typo / truncation)
5. json.loads(b64url_decode(BODY + "=" * (-len(BODY) % 4)))
   require v == 1                              -> E_VERSION
6. shape invariants, each naming its own field -> E_MALFORMED:
     t in {tls,tunnel,http};  k in {ports,paths}
     exactly one of h/u, matching k
     f present iff t=="tls";  s present iff t=="tunnel"
     len(b64url_decode(q)) == 32
7. tid = sha256(q_bytes).hex()[:16]            # loggable; q is NEVER logged
```

Steps 1–6 are pure local computation: a damaged paste is named without a network
call, and `q` never leaves the process on any failure path.

---

## 3. Client commands

### `firekeep join <code> [--profile NAME] [--activate] [--force] [--no-print-key]`

Every step before 5 may fail freely; the ticket is spent only at 5.

| # | step | notes |
|---|---|---|
| 1 | parse + validate locally (§2) | cheapest failure first; no network, no secret sent |
| 2 | `resolver.is_bypassed()` → refuse | personal mode is a hard no-op everywhere else; be consistent |
| 3 | establish transport | `tunnel`: require `ssh`, reuse `connect._tunnel_running()` before `_start_tunnel(s)` so re-running never stacks forwarders → `http://127.0.0.1:8100`. `tls` + `f != "os"`: anchor fetch → fingerprint compare → abort on mismatch → verified context from `cadata`. `tls` + `f == "os"`: `transport._build_ssl_context("os")`. `http`: print exposure, proceed |
| 4 | **probe** `GET <rest_base>/health` | keyless (`AUTH_SKIP_PREFIXES`, `main.py:857`). Unreachable fails here — before the ticket burns and before any file is touched |
| 5 | `POST /enroll {ticket, agent_id_proposal, hostname}` | the single redemption path |
| 6 | **print the key to stdout** unless `--no-print-key` | plaintext exists for exactly one response (`keys.py:195-216` stores only the sha256). Printing before any filesystem write is what makes an unwritable `~/.firekeep` recoverable |
| 7 | write `ca_pem` → `~/.firekeep/<host-slug>-ca.crt`, 0600 | only when an anchor was fetched |
| 8 | `config_write.upsert_profile(...)` | writes `kind`/`scheme`/`host`\|`base_url`/`verify_tls`/`ca_path`/`agent_id`/`api_key`; 0600; `[active]` untouched unless absent or `--activate`; reports every overwritten key; refuses a `kind` change without `--force` |
| 9 | `run_doctor()` | exit 0 only if no row is `fail` |

### `firekeep connect <user@host>`

`_probe_server` → `bash deploy/firekeep-admin invite --agent <id> --json` over SSH →
the same join core. One redemption path; the SSH-holding operator keeps their
one-command experience.

### Retired prompts

Retired **only on the code path**, never globally:

- `firekeep install --join <code>` skips **host** (`wizard.py:122-124`), **API key**
  (`wizard.py:128-133`, `:217-218`), and **"configure which profile?"**
  (`wizard.py:107-118`). The code carries the shape; the response carries the key;
  the profile name comes from `--profile` or the kind's default (`ports`→`personal`,
  `paths`→`office`).
- The **identity** prompt survives: it is the one thing the code cannot know, and
  `invite --agent bob` only half-specifies it (the machine discriminator is local).
- `firekeep install` with no `--join` is unchanged — a dev checkout has no server to
  issue a code, and `_CONFIG_SKELETON` (`cli.py:201-222`) must keep working.
- **Both bootstraps must thread the code** or `join` runs after the prompts it exists
  to replace: `FIREKEEP_JOIN=<code>` / `--join` passed to `firekeep install` on
  **both** branches of each — the idempotent fast path
  (`client/bootstrap/install.sh:114`, `install.ps1:99`) and the main path
  (`install.sh:240`, `install.ps1:233`), which today hand off interactively via
  `< /dev/tty`.

### `deploy/firekeep-admin` restructuring

`[ "${1:-}" = "keys" ] && [ "${2:-}" = "create" ] || usage` (`:40`) runs before any
parsing, so `invite --agent bob` would print usage and exit 1.

- Replace that gate with a subcommand dispatch: `keys create` | `keys revoke` | `invite`.
- **`keys revoke <key_id>` is new and is required by this design, not optional.** Today
  revocation exists only as `auth/keys.py:253` `revoke_key` and the REST route
  `DELETE /auth/keys/{key_id}` (`auth/api.py:69-78`), which is admin-scoped — gated
  behind the credential that is "printed exactly once and never stored". So on a
  long-running server there is no operator path to revoke anything, while this design
  depends on revocation three times over: the 409 message instructs a user to get a
  stolen code's key revoked (§4), per-machine keys are justified by revocation
  granularity (§1.12), and 90-day expiry assumes revocation handles the interval
  (§1.8). `keys revoke` uses the same local-Redis path as the local mint
  (`deploy/firekeep-admin:102-109`) — no admin key on the server, for the same reason
  documented there — and deletes both the `auth:key:<hash>` record and its
  `auth:key_index` member.
- `chmod +x` → git mode `100755`. It is `100644` today, which is why every caller
  prefixes `bash` (`connect.py:114`, `deploy/tests/test_firekeep_admin.sh:14,34,38,48,52`)
  and why the documented invocation in `CLAUDE.md` fails on a fresh checkout.
- `invite` does **not** write Redis in bash. It shells
  `docker compose exec -T cortex-api python -m app.enroll.mint --agent … --json`, so
  the ticket schema has exactly one implementation. The DB-7 layout is already
  hand-rolled twice in bash (`firekeep-admin:90-97`, `bootstrap-keys.sh:70,75`), both
  diverging from `keys.py:199-218`; this adds no third copy.
- Rename the local mint's key prefix from `fk_` (`:87`) to `nxs_`, matching
  `generate_api_key` (`keys.py:175`). `fk_` now belongs to the join code, and two
  `fk_`-prefixed secrets in one product is a support trap.

---

## 4. Error handling

Every message names the actual cause. Ambiguous failure is the defect this design
exists to kill.

| exit | failure | message |
|---|---|---|
| 0 | success | `joined as <agent_id> [<profile>] — key expires <date>`, then doctor rows |
| 2 | not a join code | `that does not look like a Firekeep join code (expected it to start with 'fk_join_'). If you were given a host and an API key instead, use: firekeep install --host <h>` |
| 2 | checksum mismatch | `this join code is damaged (checksum mismatch) — it was probably truncated or line-wrapped in transit. Nothing was sent to the server. Ask <issuer> to resend it, ideally in a code block.` |
| 2 | shape invalid | `malformed join code: <field> is missing or invalid (t=tls requires a CA fingerprint 'f')` — never echoes `q` |
| 2 | future version | `this code was issued by a newer Firekeep server (format v<N>, this client understands v1). Run: firekeep update` |
| 3 | unknown ticket (404) | `the server does not recognise this join code (ticket <tid>). Either it was used more than 7 days ago, or it was mistyped. Ask <issuer> for a new one.` |
| 3 | already redeemed (409) | `this join code was already redeemed at <used_at>, issuing key <key_id>. Join codes are single-use. If that was not you, tell <issuer> — that key should be revoked: deploy/firekeep-admin keys revoke <key_id>` |
| 3 | expired (410) | `this join code expired at <expires_at> (<N>h ago). Join codes are valid for 24h. Ask <issuer> for a new one.` plus, if the local clock is >5 min from the response `Date`: `note: your machine's clock is <N>m off the server's.` |
| 4 | unreachable (step 4) | `<host:port> did not answer within 10s, so the code was NOT redeemed and is still valid. Retry when you can reach it. If the server binds to loopback, the code should have been issued with a tunnel — ask <issuer> to reissue.` |
| 5 | **CA fingerprint mismatch** | `SERVER IDENTITY MISMATCH — <host> presented a CA that does not match the fingerprint in your join code. Nothing was sent. This is either a man-in-the-middle or a CA rotation on the server. Do not retry; confirm the code out of band with <issuer>.` |
| 5 | TLS fails after pinning | `<host> could not prove it holds the CA your join code names (TLS verification failed: <reason>). The ticket was not sent.` |
| 6 | `AUTH_ENABLED=false` (409) | `this server enforces no authentication (AUTH_ENABLED=false), so there is no key to issue. Run: firekeep install --host <host>. To require keys, set AUTH_ENABLED=true on the server and reissue the code.` |
| 6 | route absent (404, non-JSON) | `this server predates client enrollment (no POST /enroll). On the server: git pull && bash update.sh — then ask for a new code. Meanwhile: firekeep connect <user@host> works over ssh.` |
| 6 | rate-limited (429) | `the server is refusing enrollment attempts right now (rate limit). Your code was NOT used. Retry in a minute; if this persists, someone may be probing the endpoint — check the server.` |
| 7 | no `ssh` for a tunnel code | `this join code needs an SSH tunnel (the server binds to loopback) but 'ssh' is not on PATH. Install OpenSSH, or ask <issuer> to expose the stack over TLS and reissue.` |
| 7 | tunnel start failed | `started an SSH tunnel to <s> but ports <list> never came up. Something else may be bound locally, or your ssh key is not authorised there. The code was not used.` |
| 0 | re-join, same kind | proceeds, printing `[personal] updating: host 10.0.0.4 -> 10.0.0.9, api_key <replaced>, agent_id bob -> bob-mbp`; when `[active]` is left alone, adds `note: [active] profile is still 'office' — run 'firekeep profile use personal' or re-run with --activate.` |
| 8 | re-join, different kind | `[office] is currently kind=paths and this code is kind=ports. Refusing to rewrite it — that would break every rendered MCP entry. Use --profile <newname>, or --force to overwrite.` |
| 1 | config write failed after redemption | `THE KEY WAS ISSUED but <path> could not be written (<errno>). Your key is above — save it now, then run: firekeep install --host <h> and paste it at the API key prompt.` (the key was printed at step 6) |
| 1 | personal mode active | `personal mode is ON, so Firekeep is dormant and join would be a no-op. Run: firekeep personal off` |
| 0 | `t=http` (warning) | `WARNING: this code redeems over plain http to <host>. The join code and the API key it returns cross the network unencrypted, and the key is then sent on every request. Continue only on a trusted network.` |

`transport.py:97-100` surfaces up to 500 bytes of a server's `detail` verbatim, so each
4xx above carries its full sentence server-side. The client does not translate status
codes into prose and must not invent prose the server did not send.

---

## 5. Testing

### `auth/tests/`
- `test_keys_explicit_client.py` — `create_key(..., redis_client=c)` mints with the
  module global still `None`; with neither, still raises `RuntimeError`.
- `test_enrollable_scopes.py` — **guard**: `ENROLLABLE_SCOPES == SCOPES - {"admin","*"}`;
  `"vault:read" in` it (a future PR must not tidy it out — `keys.py:39-50`); and it
  equals `deploy/firekeep-admin`'s `NON_ADMIN_SCOPES` literal (`:25`).
- `test_keys_provenance.py` — `extra` lands in the hash and in `list_keys`' projection;
  an `extra` naming a reserved key raises.

### `cortex/tests/`
- `test_enroll_api.py` — happy path; 400/404/409/410 bodies each name their own cause;
  response shape `{api_key, key_id, agent_id, scopes, kind, expires_at, server_version}`.
- `test_enroll_never_creates_keys.py` — **DB-7 pollution guard**: 1000 misses against
  `POST /enroll` and `GET /enroll/anchor` leave `dbsize()` unchanged and zero
  `auth:enroll:*` keys. Pins §1.5 against a refactor that reaches for `HSETNX` again.
- `test_enroll_atomicity.py` — a raising `create_key` leaves `used_at` absent (ticket
  still redeemable); a lost claim race revokes the orphan and returns 409; the
  plaintext never appears in a log record.
- `test_enroll_auth_off.py` — with `AUTH_ENABLED=false`, `POST /enroll` resolves to the
  enroll router rather than `_admin_surface_disabled_router`'s catch-all
  (`main.py:162-168`) and returns 409 with the enrollment sentence; `POST /auth/enroll`
  is not a route.
- `test_enroll_skip_list.py` — extend the existing literal pins
  (`test_auth_consolidation.py:236`, `test_dashboard_auth.py:28-34`):
  `AUTH_SKIP_EXACT_PATHS == ("/dashboard","/dashboard/","/enroll","/enroll/anchor")`,
  and assert no element of `AUTH_SKIP_PREFIXES` is a prefix of `/enroll` — the
  `/dashboard/api/*` lesson as a test.
- `test_enroll_rate_limit.py` — the global hourly counter refuses at the cap when every
  request carries the same `request.client.host`, proving the control does not depend on
  `get_remote_address`.
- `test_enroll_scope_ceiling.py` — a hand-written ticket with `scopes=["admin"]` or
  `["*"]` is refused and mints nothing.
- `test_enroll_agent_id.py` — charset rejection; collision → `-2`; the server's value
  wins over the client proposal.
- `test_enroll_anchor.py` — returns `ca_pem` for a known `tid`, 404 for unknown
  (creating nothing), and never returns the ticket, `used_at`, or `scopes`.

### `client/tests/`
- `test_joincode.py` — round-trip; whitespace and line-wrap tolerance; single-character
  flip → `E_DAMAGED`; truncation; wrong prefix; `v=2`; each shape invariant names its
  field; 32-byte enforcement; `tid` derivation; and `q` appears in no exception message,
  log line, or `repr`.
- `test_join.py` — **ordering guards**: an anchor mismatch makes zero requests carrying
  the ticket; a failed probe makes no `POST /enroll` and no config change; the key is
  printed before the first filesystem write; `t=http` emits the warning;
  `is_bypassed()` refuses before anything else.
- `test_join_config_write.py` — no `[active]` flip without `--activate`; an existing
  `office` section survives a `personal` join byte-for-byte outside its own keys; `kind`
  change refused without `--force`; 0600 with the Windows `OSError` tolerance
  `connect.py:189-192` already has.
- `test_connect.py` (extend) — `connect` issues then redeems through the join core;
  `_mint_key` no longer exists; a running tunnel is reused, not stacked.
- `test_import_boundary.py` (existing, `.github/workflows/ci.yml:50-55`) — join and
  joincode stay stdlib-only.
- `test_cli_install.py` (extend) — `--join` suppresses exactly the host, api_key and
  profile-choice prompts and keeps the identity prompt.
- `test_bootstrap_sh.py` / `test_bootstrap_ps1.py` (existing) — `--join` /
  `FIREKEEP_JOIN` threaded on **both** the fast path and the main path in both
  bootstraps.

### `deploy/tests/test_firekeep_admin.sh` (extend)
`invite` and `keys revoke` dispatch (the `:40` gate no longer eats them) and `keys create`
still works; `keys revoke <key_id>` removes both the `auth:key:<hash>` hash and the
`auth:key_index` member, is idempotent on an unknown id, and needs no admin key;
`invite` against a loopback `.env` emits `t=tunnel` with `s=<user>@$VPS_IP`; against a
network-reachable plain-http `.env` it refuses without `--insecure-http`; the file's git
mode is `100755`; and the emitted code passes the client validator through a shared
fixture, so the bash issuer and the Python redeemer cannot drift.

---

## 6. Config reference

| var | default | purpose |
|---|---|---|
| `ENROLL_TICKET_TTL_HOURS` | `24` | ticket validity |
| `ENROLL_TOMBSTONE_DAYS` | `7` | how long a spent/expired record survives so the server can still explain itself |
| `ENROLL_KEY_EXPIRES_DAYS` | `90` | lifetime of the minted key; `0` = never |
| `ENROLL_RATE_LIMIT` | `10/minute` | per-route slowapi limit |
| `ENROLL_MAX_ATTEMPTS_PER_HOUR` | `60` | global Redis ceiling, survives proxy IP collapse |

---

## 7. Follow-ups (explicitly out of scope)

1. **TLS front for the personal deployment.** Mirror `docker-compose.office.yml`'s
   Caddy + internal CA so `t=tls` becomes the single default and `t=tunnel` a fallback.
   Until then a join code cannot onboard a teammate who has no server account on the
   shipped default (§1.3).
2. **Verified identity on the cortex hot path.** `main.py:1129` and `:1205` read the
   self-asserted `X-Agent-Id` while `auth/asgi.py:112-117` already attaches a verified
   identity nothing reads (§1.12).
3. **Dashboard and hosted-control-plane issuance.** Additive front doors onto the same
   redemption path.
4. **Discoverability debt this design does not itself fix:** `CLAUDE.md`'s Local setup
   section, both bootstraps' output, and `cli.py:475-481`'s `NEXT STEPS` must all name
   the enrollment path. The original failure was never that the good path was missing.
