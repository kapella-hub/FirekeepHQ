# Client Enrollment — Join Codes

**Status:** design, approved 2026-07-30 (revised same day: dashboard issuance, zero prompts)
**Depends on:** `2026-07-30-single-connection-config-design.md` and
`2026-07-30-key-id-resolution-hardening-design.md`, both of which must land first. The
hardening patch is a prerequisite rather than a nicety: this design lets the client choose
its credential hash, which turns that patch's 64-bit-collision precondition into a free
choice.
There is no `[personal]`/`[office]` profile; a join code writes the one `[server]`
section.
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

One opaque join code replaces every free-text field, and **the client install asks
nothing at all**. A field the user cannot type is a field the user cannot typo.

Two human actions, total:

1. An admin clicks **Add device** in the dashboard and copies one ready-made command.
2. The teammate pastes it.

Everything else is derived: the host and connection shape from the code, the key from
the redemption response, the identity from the invite plus the machine's hostname.

Non-goals: a hosted control plane and OIDC/device-code enrollment. Both are additive
front doors onto the redemption path defined here and neither changes it. Stated
plainly: **this design covers the solo and small-team cases, not SaaS.** SaaS needs
the control plane in §7, and until it exists a customer running their own server is
the only supported shape.

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
auth:enroll:<tid>             hash {agent_label, scopes, ca_pem?, transport,
                                    host|base_url, ssh_target?, created_at,
                                    expires_at, expires_at_epoch,
                                    used_at?, issued_credential_id?,
                                    issued_device_id?, issued_device_nonce?,
                                    issued_key_hash?, issuer}
                              tid = sha256(q_bytes).hex()[:16]  (§1.2)
                              EXPIRE 7d          (tombstone, NOT validity)
auth:cred:<credential_id>     string -> <key_hash>, same EXPIRE as the credential
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
- The router builds its own `redis.asyncio` client from `AuthSettings.REDIS_URL` and
  never reads the `auth.keys` module global. It calls **no** `auth/` write function:
  the only auth-layer addition is `build_credential_record(credential_id, device_id,
  scopes, now, expires_days) -> dict[str, str]`, a pure function extracted from
  `create_key`'s metadata block (`keys.py:199-206`) that returns the DB-7 field map and
  performs no I/O. `create_key` calls it too, so the layout has one definition and the
  Lua script carries no field-level schema knowledge. Two operations the field map
  cannot express stay explicit and documented in the script: the
  `ZADD auth:key_index` (`keys.py:218`) and the `EXPIRE`.

  The previously proposed `create_key(..., redis_client=None)` seam is **not added**.
  It existed only to route around the `RuntimeError` at `keys.py:187-188` on the enroll
  path; with enrollment no longer calling `create_key`, nothing needs it. `invite` mints
  tickets, `firekeep-admin keys create` mints locally via `redis-cli`
  (`deploy/firekeep-admin:85-100`), and `POST /auth/keys` runs in cortex-api, which does
  call `init_auth()`. `validate_key`'s own explicit-client seam (`keys.py:271-284`) is
  untouched and still required by `auth/asgi.py:91`.
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
   `{ca_pem}`. Public, non-secret, no credential sent. `tid = sha256(q_bytes).hex()[:16]`,
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

`t=tunnel` carries no `f` and needs none: **SSH's host key authenticates the server**,
and the ticket travels inside that tunnel to `127.0.0.1`. `t=http` authenticates
nothing, which is why it requires `--insecure-http` at issue time and prints the
exposure at redemption. Every transport therefore has a stated server-authentication
story, and exactly one of them is "none, and we say so out loud".

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

### 1.4 One atomic consume

The client generates its own credential secret before it sends anything, so the server
never mints on this path and there is no fallible generation step to sequence around a
burn. `_hash_key` is unkeyed, unsalted SHA-256 (`keys.py:165-167`) and `validate_key`
re-hashes whatever the caller presents (`keys.py:288-290`), so a stored record built
from a client-supplied hash validates identically to one built from a server-minted
key. Registration collapses to an `HSET` in the same DB 7 that holds the ticket
(`docker-compose.yml:252, 256`), which makes the whole redemption one Redis script.

**Client, before the first request.** Generate
`secret = "nxs_" + secrets.token_bytes(32).hex()` and a device nonce
`device_nonce = secrets.token_hex(8)`. Persist both to
`~/.firekeep/pending-join.json` at 0600 (the write `connect.py:189-192` already does,
Windows `OSError` tolerated), then send `credential_hash = sha256(secret)` — never
`secret`. Persisting before the request is what makes the retry in §1.10a idempotent:
a crash between send and response must find the same secret, or the retry burns the
ticket on a credential nobody holds. The nonce is sent on **every** attempt, first and
retry alike — it is the client's half of the idempotency key. `device_id` is minted by
the server (§1.12) and never appears in a request.

**Server, on `POST /enroll`.** Two steps, in this order:

1. Shape check in Python: `credential_hash` matches `^[0-9a-f]{64}$`, else `400`. This
   runs before the value ever becomes part of a Redis key name, and it is what keeps
   glob metacharacters out of the `scan_iter` patterns at `keys.py:239, 259`.
2. One `EVAL` of `ENROLL_CONSUME` against DB 7. Redis executes it serially, so
   validate / expire-check / rate-limit / register / claim are one indivisible
   operation.

`ENROLL_CONSUME` operates in this order, and the order is load-bearing:

| # | operation | on failure |
|---|---|---|
| 1 | `INCR auth:enroll:rate:<YYYYMMDDHH>`; `EXPIRE 7200` when the counter is 1 | over `ENROLL_MAX_ATTEMPTS_PER_HOUR` → `{'rate'}` |
| 2 | `EXISTS auth:enroll:<tid>` | false → `{'unknown'}`, **writing nothing else** |
| 3 | **replay match**: `used_at` present **and** `issued_key_hash == ARGV.hash` **and** `issued_device_nonce == ARGV.device_nonce` | → `{'replay', …}` with the original metadata |
| 3b | replay match but `EXISTS auth:key:<hash>` false | → `{'credential_gone', used_at, credential_id}` — §1.10a's third bullet depends on this branch |
| 4 | `used_at` present, either field differing | → `{'used', used_at, credential_id}` |
| 5 | `expires_at_epoch < now` | → `{'expired', expires_at}` |
| 6 | ticket `scopes` ⊆ `ENROLLABLE_SCOPES` (passed in `ARGV`, membership-tested in Lua) | → `{'scope_violation', scopes}` → 500 + `CRITICAL` |
| 7 | `EXISTS auth:key:<hash>` | true → `{'cred_exists'}` |
| 8 | `HSET auth:key:<hash>` ← the field map from `build_credential_record()`; `EXPIRE` it when the ticket carries a key lifetime; `SET auth:cred:<credential_id>` ← hash (same `EXPIRE`); `ZADD auth:key_index`; mint `device_id = secrets.token_hex(8)` unless the ticket already carries one (a regenerated code for an existing device, §1.10a); `HSET` the ticket's `used_at`, `issued_credential_id`, `issued_device_id`, `issued_device_nonce`, `issued_key_hash` | → `{'ok', …}` |

**Step 3 must precede step 7.** A retry after a lost response carries a hash that is
already registered; evaluated in the other order it would answer `cred_exists` instead
of `replay`, and the caller would conclude its own credential belongs to someone else.

**Step 1 must precede step 2.** A miss has to be counted, or the rate ceiling protects
nothing. This is the one write the script performs before it knows the ticket exists,
and §1.5 states the carve-out.

**The `EXPIRE` at step 8 is not optional.** `create_key` calls
`expire(redis_key, ttl)` whenever `expires_days` is set (`keys.py:209-213`); a Lua
script that omits it silently drops the credential's Redis TTL. Expiry is still
enforced through `validate_key`'s `expires_at` field read (`keys.py:295-303`), so the
omission would be invisible until an audit — which is exactly the class of drift the
single-implementation rule below exists to prevent.

**What each outcome leaves behind.** There is no compensating write anywhere:

| outcome | ticket | `auth:key:*` | `auth:cred:*` | rate key |
|---|---|---|---|---|
| `rate` | untouched | none | none | incremented, TTL |
| `unknown` / `expired` | untouched | none | none | incremented, TTL |
| `used` | unchanged (still spent) | unchanged | unchanged | incremented, TTL |
| `replay` | unchanged | unchanged | unchanged | incremented, TTL |
| `scope_violation` / `cred_exists` | untouched | none | none | incremented, TTL |
| `ok` | claimed | created | created | incremented, TTL |

A mid-script Redis failure cannot be observed as a partial state: Redis either runs the
script to completion or does not run it. That is a property of the design, not something
a test can assert with mocks.

**Duplicate hash (`cred_exists`).** The server returns a generic 409 that never
distinguishes "your hash" from "somebody's hash" — the ticket gate bounds this to one
probe per single-use ticket, and stating the invariant now keeps a future reusable-code
change (§7.4) from turning it into an oracle. Nothing was written, so the client
regenerates `secret` and `device_nonce`, rewrites `pending-join.json`, and
retries **once** before failing. In practice this fires only if a client reused a
secret; at 256 bits it is not a collision.

**Deleted outright** from the previous design: the `PEEK`/`CLAIM` split, the separate
`create_key` call, the lost-claim-race branch, the `revoke_key(new_key_id)`
compensation, and the `CRITICAL`-on-revoke-failure path. All of them existed to
sequence a mint that no longer happens.

Recovery from a genuinely burned ticket is a second `invite`, which needs no admin
credential (`deploy/firekeep-admin:102-109`).

### 1.5 The consume script writes nothing on a miss — with one named exception

Redis `HSETNX` creates the key on a miss. On a deliberately unauthenticated endpoint at
the default `RATE_LIMIT=60/minute` (`cortex/app/config.py:12`) that is ~86k permanent,
TTL-less `auth:enroll:*` keys per day, written into the same DB 7 that holds
`auth:key:*` (`keys.py:64-65`) and `vault:secret:*` (`vault/store.py:17-18`) per
`docker-compose.yml:252, 256`.

`ENROLL_CONSUME` performs exactly two writes before it knows the ticket exists:
`INCR auth:enroll:rate:<YYYYMMDDHH>` and, when that counter is 1, `EXPIRE` on it. If
`EXISTS auth:enroll:<tid>` is then false it returns `{'unknown'}` without touching
`auth:enroll:<tid>`, `auth:key:*`, `auth:cred:*` or `auth:key_index`.

The prohibition is on creating an `auth:enroll:<tid>` record — one unbounded, TTL-less
key per probe. The rate key is one bounded key per hour carrying a two-hour TTL, and
counting a miss is the entire point of a rate ceiling. Pinned by test (§5).

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
  proxy collapse. It is the **first two operations inside `ENROLL_CONSUME`**, not a
  separate round trip: counting and claiming in one script removes the window in which
  a crash between them either loses a count or spends a ticket uncounted. Shared-fate
  by design: a flooded enrollment surface is a denial of onboarding, not a breach;
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

- Provenance lives on the **ticket**, not in a generic `create_key` passthrough. The
  ticket record already carries `issuer`, and redemption stamps `used_at`,
  `issued_credential_id`, `issued_device_id` and `issued_key_hash` (§1.4). The
  credential record carries `credential_id`, `device_id`, `enrolled_via=<tid>` and
  `enrolled_at`, all written by `build_credential_record`. The previously proposed
  `create_key(..., extra=…)` merge with four reserved keys is **not added** — nothing
  outside enrollment needed it, and a generic field-merge into the credential hash is a
  wider seam than the one field-set this design writes. `list_keys`' projection
  (`keys.py:242-248`) gains `credential_id`, `device_id` and `enrolled_via`.

- **`credential_id` is minted by the server and is not derived from the credential
  hash.** `create_key` sets `key_id = key_hash[:16]` (`keys.py:203`), and both
  `list_keys` (`keys.py:239`) and `revoke_key` (`keys.py:259`) resolve a short id back
  to a record by `scan_iter(f"auth:key:{kid}*")`, taking the **first** match and
  returning `True` either way. Under server minting a 64-bit prefix collision is
  theoretical; once the client chooses the hash it also chooses the prefix, and a
  teammate legitimately enrolling a second device (§1.10) can submit a hash sharing
  device 1's prefix. `revoke_key` would then delete whichever record SCAN returned
  first and report success while the other credential kept authenticating. Two changes,
  both required:
  1. `credential_id = secrets.token_hex(8)`, minted inside `ENROLL_CONSUME`'s writing
     branch, unrelated to the hash. The client cannot influence it, which matters
     because it is the provenance handle the workspace design persists onto memory
     writes.
  2. `ENROLL_CONSUME` writes `auth:cred:<credential_id> -> <key_hash>`, and
     `revoke_key`/`list_keys` resolve through that mapping instead of the glob. For a
     record predating the mapping both fall back to `scan_iter` collecting **all**
     matches, and each behaves as `2026-07-30-key-id-resolution-hardening-design.md` §2
     already defines: `revoke_key` **refuses** on ambiguity rather than deleting the
     wrong credential; `list_keys` **emits every match** and marks the set ambiguous,
     because a listing that hides a live credential is the failure mode there. The two
     are fixed independently and must not be collapsed into one rule. `deploy/bootstrap-keys.sh` backfills the
     mapping for existing records from `auth:key_index`.

  This **supersedes** the earlier claim that existing auditing is adequate and
  unchanged. `ZRANGE auth:key_index 0 -1` still enumerates the inventory from the shell
  `invite` runs in, and the API's `le=200` (`auth/api.py:63`) is still a request ceiling
  rather than a storage limit — but the id→record lookup changes on both read and
  revoke paths.

- **Decision (2026-07-30):** `ENROLL_KEY_EXPIRES_DAYS=90` by default, overridable via
  `invite --expires-days N`; `0` means never and prints a warning. A lost machine
  expires itself, and the re-join path stays exercised rather than rotting.
- **Expiry must be visible before it bites**, or this feature recreates the original bug
  on a 90-day timer. `validate_key` returns `None` for an expired key
  (`keys.py:295-303`), the middleware 401s, and the client renders that as "cannot
  connect". Three parts, all required:
  1. `join` writes `credential_expires_at` into `[server]` from the enrollment response;
  2. `doctor` gains `_check_credential_expiry`, warning inside 14 days and failing
     after — mirroring `_check_ca_expiry` (`cli.py:696`);
  3. the auth middleware's 401 `detail` distinguishes *expired* from *unknown
     credential*. Safe to disclose: a caller seeing it already holds the credential.
- `auth:enroll:index` has no TTL, so the ticket→credential link outlives the ticket
  record.

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

### 1.10a Idempotent retry

Single-use bounds how many devices a code enrolls; it does not say what happens when
the response is lost. Under server minting that was unrecoverable: the ticket burned,
a credential existed, and the client had never seen the plaintext, so nobody could
present it and nothing could identify it. Client-generated secrets make redemption
naturally idempotent, and §1.4 step 3 defines it precisely:

- Retry carrying the **same** `credential_hash` **and** the same `device_nonce` → `200`
  with the original metadata. Both must match. A matching hash alone would let a stolen
  ticket-plus-hash re-enroll a second machine; a matching device alone would let a
  regenerated secret overwrite the first credential.
- Retry carrying a different hash or a different `device_nonce` against a claimed ticket →
  `409` naming `used_at` and the `credential_id`. **The code cannot enroll a second
  device.**
- Ticket claimed, hash and device match, but `auth:key:<hash>` is no longer present →
  `409`: *"this join code was redeemed and the credential it issued is no longer
  present on the server — it was revoked, or it expired and was reaped. Ask <issuer>
  for a new code."* The server cannot distinguish those two causes and does not
  pretend to; the client cannot distinguish them at all, because `validate_key` returns
  `None` for an absent record exactly as it does for an unknown one
  (`keys.py:288-293`), which is why this has to be an explicit server answer.

`used_at` is now the idempotency key as well as the audit field, which is the second
reason the tombstone window (`ENROLL_TOMBSTONE_DAYS=7`, §1.9) matters: a retry after the
tombstone expires is indistinguishable from an unknown ticket.

**Regenerating a device's join code** (§3, Devices tab) issues a fresh ticket for the
**prior `device_id`** with a fresh nonce and no `issued_key_hash`. It is a new
redemption, not a replay, and the comparison above treats it as such.

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
- `_write_profile` becomes `config_write.upsert_server()`, shared by `join`,
  `connect` and `install`, with two fixes: it prints one line per key it overwrote,
  and it refuses to change `[server]`'s `kind` without `--force`. (Its third original
  fix — never flip `[active]` — is moot: the single-connection collapse deletes
  `[active]` entirely.)

Two review claims are deliberately **not** carried into this design, having been
refuted on inspection: `%`-interpolation corruption does not occur (`configparser`
raises loudly on `%` and every Firekeep-minted secret is hex), and an `agent_id`
cannot inject an `api_key` option (`configparser.write()` escapes embedded newlines).

### 1.12 The credential is bound to the device, not to a name

Enrollment mints a **device credential**. Two identifiers come out of it, both
server-owned, and neither is a person:

- **`device_id`** — the enrolled machine. Minted server-side as `secrets.token_hex(8)`
  in `ENROLL_CONSUME`'s writing branch and returned to the client, which persists it.
  The client cannot propose it. It is the row key in the Devices tab (§3). The
  *idempotency* key for a retry is the client's `device_nonce` (§1.10a), which is a
  different value for a deliberate reason: the client must be able to retry before it
  has ever seen a `device_id`.
- **`credential_id`** — the credential itself, minted independently of the credential
  hash (§1.8).

**The credential record carries no agent identity.** `create_key` writes `agent_id`
today (`keys.py:200`) and `validate_key` projects it out (`keys.py:306`) into the
identity `auth/asgi.py:113-117` attaches. Enrollment writes `agent_id = <device_id>` as
a compatibility value, so `list_keys`' projection (`keys.py:244`) and the ASGI identity
dict keep a non-empty field — strictly better than today's `"unknown"` default. The
field is replaced outright by `{workspace_id, member_id, credential_id, scopes}` in
`2026-07-30-workspace-entitlements-and-onboarding-design.md` §2, which already defines
`X-Agent-Id` as an untrusted runtime label.

Consequently **the server does not normalize or de-collide an agent name.** An earlier
draft had it validate `^[A-Za-z0-9_-]{1,64}$` and append `-2`, `-3`, up to `-99`
against existing non-revoked keys. That ladder is a scan of the key store for a human
name, and a key store that holds no human name cannot run it. What survives is
advisory: the response carries
`suggested_agent_id = f"{agent_label}-{short_host}"`, where `short_host` is the request
body's `hostname` field lowercased and cut at the first dot — **not**
`socket.gethostname()`, which inside the cortex container names the server and would
suggest one identity to every enrollee. Label from the ticket record, so the admin's
naming wins; `short_host` alone when `invite` ran with no `--agent`; the field is omitted
entirely when `hostname` is absent or fails `^[A-Za-z0-9_.-]{1,64}$`. The client writes it
verbatim into `[identity] agent_id`
unless `--agent-id` overrides. It is a label the runtime asserts, checked nowhere.

**There is no identity prompt.** The ticket carries the person and the OS carries the
machine, so the name is derivable, and a prompt for a derivable value is exactly the
habit that produced the three-prompt wizard.

One name per machine is still the right default: Bridge keeps one active session per
`agent_id` and `START_SESSION_LUA` pauses the prior active one
(`bridge/app/session.py:167-177`), so two machines sharing `bob` would pause each
other's sessions. This is **not** justified by the session stash — `state.py:236-239`
keys under the local platform cache dir, so two machines never contend for it.

**Stated boundary, not a defect.** `cortex/app/main.py:1128-1129` and `:1204-1205` read
the self-asserted `X-Agent-Id` for replay attribution and memory provenance, and no
cortex handler reads the verified identity. This design makes the credential
device-owned and therefore *attributable*; making cortex prefer the verified principal
is milestone 1 of the workspace design, and it is purely additive on top of what lands
here.

### 1.13 Request and response

Request body:

    {ticket, credential_hash, device_nonce, hostname}

- `credential_hash` = `sha256(secret)`, 64 lowercase hex. The **client** generates
  `secret`; see §1.4 and the invariants below.
- `device_nonce` is client-generated (§1.4), identical on the first attempt and every
  retry, and is the idempotency key (§1.10a). `device_id` is server-minted and appears
  only in the response.
- `agent_id_proposal` is **removed**. The name is advisory and flows the other way.

Response `200`:

    {device_id, credential_id, suggested_agent_id, scopes, kind,
     host|base_url, ca_pem?, credential_expires_at, server_version}

**There is no `api_key` field.** The claim this supports is bounded, and the bound is
part of the claim: **the credential never crosses the wire during enrollment** — not in
the request, not in the response. Only `sha256(secret)` does, on the request, and an
intercepted hash is not a bearer token (`validate_key` re-hashes what the caller
presents, `keys.py:288-290`). What that buys is narrow and real: the credential is absent
from the enrollment request body, the response body, proxy access logs, response buffers
and tracebacks on the one exchange that would otherwise have carried a brand-new
long-lived secret in plaintext.

It buys nothing after that. **Every subsequent authenticated call sends the secret itself
as the `X-API-Key` bearer credential** (`resolver.py:294-298`), because that is the only
way the server can authenticate it — `validate_key` hashes what is presented and looks the
result up, so it must receive the plaintext. Those calls therefore depend entirely on the
transport: TLS on `t=tls`, the SSH tunnel on `t=tunnel`, and **nothing at all on `t=http`**,
which is why that transport requires `--insecure-http` at issue time and prints its
exposure at redemption (§1.3, §4).

This also does **not** remove the need to authenticate the server during enrollment: the
ticket `q` is still a bearer secret in the request body, so §1.2's CA pinning stays
load-bearing.

The client writes to `[server]`: `kind`, `scheme`, `host`|`base_url`, `verify_tls`,
`ca_path`, `api_key` (its own generated secret — the config key name is unchanged
because `resolver.py:294-295` reads the literal option `api_key`), `credential_id`,
`device_id`, `credential_expires_at`; and to `[identity]`: `agent_id`. `credential_id`
and `device_id` are non-secret support handles — they are what a user reads out to an
admin who needs to revoke something.

### Invariants

- **I-1 · Client entropy.** `secret = "nxs_" + secrets.token_bytes(32).hex()` — 256
  bits from the OS CSPRNG, above the 192-bit house precedent (`keys.py:170-175`) and
  matching the ticket's width. Forbidden: `random.*`, `uuid`, any seeded or reused PRNG
  object, any derivation from hostname, time, pid or agent name, and any reuse of an
  existing local secret. On Linux `getrandom()` blocks until the pool is initialised, so
  a first-boot container needs no warm-up step. **Named limitation:** the server can
  enforce the hash's shape but can never verify the secret's entropy —
  `sha256(4 random bytes)` and `sha256(32 random bytes)` are indistinguishable at the
  endpoint. This is acceptable only because Firekeep ships the client, and the blast
  radius is confined to the enrollee's own non-admin credential (`ENROLLABLE_SCOPES`,
  §1.7).
- **I-2 · The `nxs_` prefix is mandatory** even though the server cannot check it: a
  human pasting a credential can identify it, and the CI `secrets` gitleaks gate can
  pattern-match it. `fk_` now belongs exclusively to the join code.
- **I-3 · No server mint on the enrollment path, and no request-shape fallback.** A
  server that minted when `credential_hash` was omitted would be one stripped field away
  from a downgrade on the `t=http` path. `create_key`'s plaintext-returning behaviour is
  retained unchanged for `deploy/bootstrap-keys.sh` and `firekeep-admin keys create`,
  which are not enrollment. If a mint mode is ever needed it is server-policy-gated and
  default-off, never inferred from the request.

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
| `k` | enum | always | connection kind: `ports` \| `paths` |
| `h` | str | iff `k=ports` | host |
| `u` | str | iff `k=paths` | `base_url` |
| `f` | str | iff `t=tls` | CA commitment: `base64url(sha256(ca_pem)[:16])` (22 chars) or literal `os` |
| `s` | str | iff `t=tunnel` | ssh target, `user@host` |
| `x` | str | always | ticket expiry, compact UTC `YYYYMMDDTHHMMSSZ`, **advisory only** |
| `q` | str | always | ticket secret, `base64url(32 random bytes)` (43 chars) |

`scheme` is deliberately **not** carried — it is derived (`t=tls` → `https`,
otherwise `http`). Two fields that can disagree about TLS is exactly the hazard
`resolver.py:313-318` refuses at resolve time.

`k` names a deployment topology, not a profile. There is no profile to name — the
single-connection design collapses `[personal]`/`[office]` into one `[server]` whose
`kind` is derived and never prompted.

**Client-side credential generation changes nothing in this section.** The code still
carries only the ticket: the field table, the checksum, the decode steps and the `tid`
derivation are unchanged. Putting a credential into a pasteable, mail-forwardable string
would be strictly worse than the design it replaces.

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

### `firekeep join <code> [--agent-id NAME] [--force] [--print-key] [--resume]`

Every step before 6 may fail freely; the ticket is spent only at 6.

| # | step | notes |
|---|---|---|
| 0 | prepare local state | generate `secret` (I-1) and `device_nonce`; write `~/.firekeep/pending-join.json` at 0600. **An unwritable config dir fails here, before the ticket is spent** — the previous design could not detect this at all until after redemption. `--resume` reuses an existing pending file instead of generating |
| 1 | parse + validate locally (§2) | cheapest failure first; no network, no secret sent |
| 2 | `resolver.is_bypassed()` → refuse | personal mode is a hard no-op everywhere else; be consistent |
| 3 | establish transport | `tunnel`: require `ssh`, reuse `connect._tunnel_running()` before `_start_tunnel(s)` so re-running never stacks forwarders → `http://127.0.0.1:8100`. `tls` + `f != "os"`: anchor fetch → fingerprint compare → abort on mismatch → verified context from `cadata`. `tls` + `f == "os"`: `transport._build_ssl_context("os")`. `http`: print exposure, proceed |
| 4 | **probe** `GET <rest_base>/health` | keyless (`AUTH_SKIP_PREFIXES`, `main.py:857`). Unreachable fails here — before the ticket burns |
| 5 | write `ca_pem` → `~/.firekeep/<host-slug>-ca.crt`, 0600 | only when an anchor was fetched |
| 6 | `POST /enroll {ticket, credential_hash, device_nonce, hostname}` | the single redemption path; idempotent per §1.10a |
| 7 | `config_write.upsert_server(...)` | writes `[server]` (`kind`/`scheme`/`host`\|`base_url`/`verify_tls`/`ca_path`/`api_key`/`credential_id`/`device_id`/`credential_expires_at`) and `[identity] agent_id`; 0600; `[dist]` untouched; reports every overwritten key; refuses a `kind` change without `--force` |
| 8 | delete `pending-join.json`; `run_doctor()` | exit 0 only if no row is `fail` |

**The credential is not printed by default.** Under server minting it had to be —
plaintext existed for exactly one response — and `--no-print-key` was the opt-out. The
client now holds the secret for as long as it likes, so the default inverts:
`--print-key` opts *in*, for a user who wants a copy. Scrollback, tmux buffers and CI
logs stop receiving a long-lived credential by default, and the on-disk cost is one
extra 0600 file that is reaped at step 8.

`pending-join.json` is reaped on success and swept on any `join`/`doctor` run older
than `ENROLL_TICKET_TTL_HOURS`. Reaping is mandatory, not advisory: a 0600 file holding
a live credential must not outlive the join it belongs to.

### `firekeep connect <user@host>`

`_probe_server` → `bash deploy/firekeep-admin invite --agent <id> --json` over SSH →
the same join core. One redemption path; the SSH-holding operator keeps their
one-command experience.

### Retired prompts — all of them

`firekeep install --join <code>` is **fully non-interactive**. Every prompt is either
carried by the code or derived:

| Prompt | Today | Replaced by |
|---|---|---|
| `Configure which profile? [1][2][3]` | `wizard.py:107-118` | deleted outright by the single-connection collapse |
| `service host (IP or hostname…)` | `wizard.py:122-124` | the code's `h`/`u` |
| `API key (blank if AUTH_ENABLED=false)` | `wizard.py:128-133`, `:217-218` | the enrollment response |
| `Agent identity` | `wizard.py:242-244` | ticket label + hostname (§1.12) |

- `firekeep install` with no `--join` is unchanged — a dev checkout has no server to
  issue a code, and `_CONFIG_SKELETON` (`cli.py:201-222`) must keep working.
- `firekeep install` with no `--join` is unchanged — a dev checkout has no server to
  issue a code, and `_CONFIG_SKELETON` (`cli.py:201-222`) must keep working.
- **Both bootstraps must thread the code** or `join` runs after the prompts it exists
  to replace: `FIREKEEP_JOIN=<code>` / `--join` passed to `firekeep install` on
  **both** branches of each — the idempotent fast path
  (`client/bootstrap/install.sh:114`, `install.ps1:99`) and the main path
  (`install.sh:240`, `install.ps1:233`), which today hand off interactively via
  `< /dev/tty`.

### Dashboard — Devices tab (the primary issuing surface)

`deploy/firekeep-admin` is a shell script run over SSH. Requiring a server shell to
onboard a teammate is the remaining unintuitive step, so **the dashboard is the
primary front door and the script becomes the fallback.**

This adds **no new trust boundary.** The dashboard already renders a **Reveal** button
on every vault secret (`dashboard/index.html:4155`) and nginx injects an
admin-scoped `DASHBOARD_API_KEY` on every `/api/*` proxy (`nginx.conf.template:35`).
Anyone past its basic auth can already read every stored secret; minting a single-use
ticket is strictly less powerful. An earlier draft objected that this would put
credential issuance behind a second auth system needing its own hardening — that
objection was wrong, and it is recorded here so it is not re-raised.

New tab, alongside the existing fourteen:

- **Device list** — one row per device, keyed on `device_id`: device name, enrolled
  date, last seen (Relay presence, telemetry only), `credential_id`, credential expiry
  with a warning inside 14 days. Actions per row: **Rename**, **Revoke**, **Regenerate
  join code**. The row is **not** keyed on `agent_id` — the credential is bound to a
  device, not a name (§1.12), and after the compatibility shim `agent_id` on a
  credential record *is* the `device_id`, so the column would be both wrong and
  misleading.
- **Rename** edits the device label only. It never touches the credential, and there is
  no reissue.
- **Regenerate join code** issues a fresh ticket for the existing `device_id` — new
  nonce, no `issued_key_hash` — so the replay comparison in §1.10a correctly treats the
  redemption as new rather than as a retry. Used when a machine is rebuilt.
- **`[+ Add device]`** — optional name, optional expiry override; on submit shows a
  **complete, ready-to-paste install command**, not a bare code:

  ```
  curl -fsSL <dist-base>/latest/install.sh | FIREKEEP_JOIN=fk_join_… sh
  ```

  with a copy button, the platform toggle (sh / PowerShell), a TTL countdown, and the
  bare code shown separately for a machine that already has the client
  (`firekeep join …`). **Emitting a command rather than a code removes a decision, not
  just a step.**
- **Outstanding invites** — unredeemed tickets with time remaining and a **Cancel**
  button (deletes the ticket record; the `tid` stays in `auth:enroll:index`).

This tab manages **device credentials only**. It carries no plan, tier, seat, quota or
billing language, and it never gates on one. Member management and seat accounting are
a different surface entirely, defined in
`2026-07-30-workspace-entitlements-and-onboarding-design.md` §3.2 — which places the
seat check on member invite issue and accept, never on `POST /enroll`.

Backing routes on Cortex, all `require_scope("admin")` and reached through the existing
dashboard proxy: `POST /enroll/invite` (mint a ticket, return the code and the rendered
commands), `GET /enroll/invites` (outstanding), `DELETE /enroll/invites/{tid}`, and
`GET /auth/keys` + `DELETE /auth/keys/{key_id}` for the device list, which already
exist (`auth/api.py:63, 69-78`). These are admin-scoped and therefore **not** on the
auth skip list — only `POST /enroll` and `GET /enroll/anchor`, which are redeemed by a
machine that has no key yet, are.

### `deploy/firekeep-admin` restructuring

`[ "${1:-}" = "keys" ] && [ "${2:-}" = "create" ] || usage` (`:40`) runs before any
parsing, so `invite --agent bob` would print usage and exit 1.

- Replace that gate with a subcommand dispatch: `keys create` | `keys revoke` | `invite`.
- **`keys revoke <credential_id>` is new and is required by this design, not optional.** Today
  revocation exists only as `auth/keys.py:253` `revoke_key` and the REST route
  `DELETE /auth/keys/{key_id}` (`auth/api.py:69-78`), which is admin-scoped — gated
  behind the credential that is "printed exactly once and never stored". So on a
  long-running server there is no operator path to revoke anything, while this design
  depends on revocation three times over: the 409 message instructs a user to get a
  stolen code's key revoked (§4), per-machine keys are justified by revocation
  granularity (§1.12), and 90-day expiry assumes revocation handles the interval
  (§1.8). `keys revoke` uses the same local-Redis path as the local mint
  (`deploy/firekeep-admin:102-109`) — no admin key on the server, for the same reason
  documented there — and deletes the `auth:key:<hash>` record, its
  `auth:key_index` member, **and the `auth:cred:<credential_id>` mapping** (§1.8) —
  leaving the mapping behind points a live id at a deleted record. It resolves the id
  through `auth:cred:` and falls back to the ambiguity-refusing scan of
  `2026-07-30-key-id-resolution-hardening-design.md` §2 for pre-mapping records.
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
| 0 | success | `joined as <agent_id> on device <device_id> — credential <credential_id> expires <date>`, then doctor rows |
| 2 | not a join code | `that does not look like a Firekeep join code (expected it to start with 'fk_join_'). If you were given a host and an API key instead, use: firekeep install --host <h>` |
| 2 | checksum mismatch | `this join code is damaged (checksum mismatch) — it was probably truncated or line-wrapped in transit. Nothing was sent to the server. Ask <issuer> to resend it, ideally in a code block.` |
| 2 | shape invalid | `malformed join code: <field> is missing or invalid (t=tls requires a CA fingerprint 'f')` — never echoes `q` |
| 2 | future version | `this code was issued by a newer Firekeep server (format v<N>, this client understands v1). Run: firekeep update` |
| 3 | unknown ticket (404) | `the server does not recognise this join code (ticket <tid>). Either it was used more than 7 days ago, or it was mistyped. Ask <issuer> for a new one.` |
| 3 | already redeemed (409) | `this join code was already redeemed at <used_at> by a different device, issuing credential <credential_id>. Join codes are single-use. If that was not you, tell <issuer> — that credential should be revoked: deploy/firekeep-admin keys revoke <credential_id>` |
| 0 | idempotent retry (200, replay) | `this join code was already redeemed by this device — reusing the existing credential <credential_id>. Nothing changed on the server.` |
| 3 | invalid `credential_hash` (400) | `this client sent a malformed credential fingerprint. Nothing was redeemed. Run: firekeep update — and if it persists, report it.` |
| 3 | duplicate credential (409, `cred_exists`) | `that credential is already registered on this server. Retrying once with a fresh secret; if this repeats, the client is reusing a secret — report it. Your join code was not spent.` |
| 5 | scope violation (500) | `this join code asks for privileges the server refuses to enroll. Nothing was issued. Tell <issuer>: the ticket was hand-edited or minted by a mismatched tool version.` |
| 3 | credential gone (409) | `this join code was redeemed and the credential it issued is no longer present on the server (revoked, or expired and reaped). Ask <issuer> for a new code.` |
| 3 | expired (410) | `this join code expired at <expires_at> (<N>h ago). Join codes are valid for 24h. Ask <issuer> for a new one.` plus, if the local clock is >5 min from the response `Date`: `note: your machine's clock is <N>m off the server's.` |
| 4 | unreachable (step 4) | `<host:port> did not answer within 10s, so the code was NOT redeemed and is still valid. Retry when you can reach it. If the server binds to loopback, the code should have been issued with a tunnel — ask <issuer> to reissue.` |
| 5 | **CA fingerprint mismatch** | `SERVER IDENTITY MISMATCH — <host> presented a CA that does not match the fingerprint in your join code. Nothing was sent. This is either a man-in-the-middle or a CA rotation on the server. Do not retry; confirm the code out of band with <issuer>.` |
| 5 | TLS fails after pinning | `<host> could not prove it holds the CA your join code names (TLS verification failed: <reason>). The ticket was not sent.` |
| 6 | `AUTH_ENABLED=false` (409) | `this server enforces no authentication (AUTH_ENABLED=false), so there is no key to issue. Run: firekeep install --host <host>. To require keys, set AUTH_ENABLED=true on the server and reissue the code.` |
| 6 | route absent (404, non-JSON) | `this server predates client enrollment (no POST /enroll). On the server: git pull && bash update.sh — then ask for a new code. Meanwhile: firekeep connect <user@host> works over ssh.` |
| 6 | rate-limited (429) | `the server is refusing enrollment attempts right now (rate limit). Your code was NOT used. Retry in a minute; if this persists, someone may be probing the endpoint — check the server.` |
| 7 | no `ssh` for a tunnel code | `this join code needs an SSH tunnel (the server binds to loopback) but 'ssh' is not on PATH. Install OpenSSH, or ask <issuer> to expose the stack over TLS and reissue.` |
| 7 | tunnel start failed | `started an SSH tunnel to <s> but ports <list> never came up. Something else may be bound locally, or your ssh key is not authorised there. The code was not used.` |
| 0 | re-join, same kind | proceeds, printing `[server] updating: host fk.internal -> fk.corp, api_key <replaced>, agent_id bob-mbp unchanged` |
| 8 | re-join, different kind | `[server] is currently kind=paths (https://fk.corp) and this code is kind=ports. Refusing to repoint this machine at a different server shape — re-run with --force if that is what you want, or use FIREKEEP_CONFIG=<path> to keep both.` |
| 1 | personal mode active | `personal mode is ON, so Firekeep is dormant and join would be a no-op. Run: firekeep personal off` |
| 0 | `t=http` (warning) | `WARNING: this code redeems over plain http to <host>. Your credential is generated locally and is not sent during enrollment, but it IS sent as X-API-Key on every request afterwards, in cleartext on this transport. The join code itself also crosses the network now, so anyone on this path can redeem it before you. Continue only on a trusted network.` |

`transport.py:97-100` surfaces up to 500 bytes of a server's `detail` verbatim, so each
4xx above carries its full sentence server-side. The client does not translate status
codes into prose and must not invent prose the server did not send.

---

## 5. Testing

### `auth/tests/`
- `test_build_credential_record.py` — `build_credential_record` is pure (no I/O), and
  `create_key`'s stored field map is byte-identical to it, so the DB-7 layout has one
  definition.
- `test_credential_id_integrity.py` — **the revocation-bypass guard**: `credential_id` is
  not derivable from `credential_hash`; `revoke_key` and `list_keys` resolve through
  `auth:cred:<credential_id>` rather than `scan_iter(f"auth:key:{kid}*")`; and a legacy
  record with no mapping and two prefix matches makes `revoke_key` **refuse** rather than
  delete the first match and return `True` (`keys.py:239, 259`).
- `test_enrollable_scopes.py` — **guard**: `ENROLLABLE_SCOPES == SCOPES - {"admin","*"}`;
  `"vault:read" in` it (a future PR must not tidy it out — `keys.py:39-50`); and it
  equals `deploy/firekeep-admin`'s `NON_ADMIN_SCOPES` literal (`:25`).
- `test_build_credential_record.py` (extend) — `credential_id`, `device_id`,
  `enrolled_via` and `enrolled_at` land in the credential hash and in `list_keys`'
  projection (`keys.py:242-248`); no generic `extra` passthrough exists on `create_key`.

### `cortex/tests/`
- `test_enroll_api.py` — happy path; 400/404/409/410 bodies each name their own cause;
  response shape `{device_id, credential_id, suggested_agent_id, scopes, kind,
  host|base_url, ca_pem?, credential_expires_at, server_version}`; and **no `api_key`
  field on any path** — the enrollment exchange never carries the credential (§1.13); a
  `credential_hash` failing `^[0-9a-f]{64}$` is 400 before any Redis call.
- `test_enroll_never_creates_keys.py` — **DB-7 pollution guard**: 1000 misses against
  `POST /enroll` and `GET /enroll/anchor` create **zero `auth:enroll:<tid>` records**,
  zero `auth:key:*`, zero `auth:cred:*` and zero `auth:key_index` members. The only key
  they may create is the single hourly `auth:enroll:rate:<YYYYMMDDHH>` counter, which
  must exist and must carry a TTL — counting a miss is the point of the ceiling (§1.5).
  Pins §1.5 against a refactor that reaches for `HSETNX` again.
- `test_enroll_idempotent_retry.py` — **new**: same ticket + same hash + same `device_id`
  → 200 replay with identical metadata; a different hash → 409; a different `device_id` →
  409; a claimed ticket whose `auth:key:<hash>` was deleted → 409 naming the credential as
  no longer present. Pins §1.10a and requirement 2.3.
- `test_enroll_consume_ordering.py` — **new**: the replay comparison is evaluated before
  the `cred_exists` guard.
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
- `test_enroll_anchor.py` — returns `ca_pem` for a known `tid`, 404 for unknown
  (creating nothing), and never returns the ticket, `used_at`, or `scopes`.

### `client/tests/`
- `test_joincode.py` — round-trip; whitespace and line-wrap tolerance; single-character
  flip → `E_DAMAGED`; truncation; wrong prefix; `v=2`; each shape invariant names its
  field; 32-byte enforcement; `tid` derivation; and `q` appears in no exception message,
  log line, or `repr`.
- `test_join.py` — **ordering guards**: an anchor mismatch makes zero requests carrying
  the ticket; a failed probe makes no `POST /enroll` and no config change;
  `pending-join.json` exists at 0600 before the first network call and is deleted at
  step 8; nothing is printed to stdout without `--print-key`; `t=http` emits the
  warning; `is_bypassed()` refuses before anything else; `--resume` reuses the persisted
  secret byte-for-byte rather than generating a new one.
- `test_join_config_write.py` — `[dist]` survives a join byte-for-byte; `kind` change
  refused without `--force` and accepted with it; `credential_id`, `device_id` and `credential_expires_at` are written from the
  response; 0600 with the Windows `OSError` tolerance `connect.py:189-192` already has.
- `test_doctor_credential_expiry.py` — warns inside 14 days, fails after, and is silent when
  `key_expires_at` is absent (a key minted before this feature). Guards the §1.8
  requirement that expiry is visible before it bites.
- `test_connect.py` (extend) — `connect` issues then redeems through the join core;
  `_mint_key` no longer exists; a running tunnel is reused, not stacked.
- `test_import_boundary.py` (existing, `.github/workflows/ci.yml:50-55`) — join and
  joincode stay stdlib-only.
- `test_cli_install.py` (extend) — `--join` is **fully non-interactive**: assert zero
  calls to the `ask` callable, with no tty attached. `firekeep install` without
  `--join` still prompts.
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
2. **Verified principal on the cortex hot path.** `main.py:1129` and `:1205` read the
   self-asserted `X-Agent-Id` while `auth/asgi.py:112-117` already attaches a verified
   identity nothing reads (§1.12).
3. **Hosted control plane (SaaS).** The third issuer. Accounts, billing, device
   management, `firekeep login`. Its only contract with this design is
   `issue_ticket(agent, ttl) -> code`; redemption is unchanged, which is what makes it
   an extension rather than a rewrite. Dashboard issuance is **no longer a follow-up**
   — it is in scope (§3).
4. **Reusable codes.** The remaining per-device admin click exists *because* codes are
   single-use (§1.10). `invite --uses N` would remove it — one code in a team wiki,
   nobody clicks anything — at the cost of making "was this code shared?" unanswerable
   and turning one leak into N enrolled machines. Deferred, not rejected; revisit if
   per-device issuance proves to be the friction people complain about.
4. **Discoverability debt this design does not itself fix:** `CLAUDE.md`'s Local setup
   section, both bootstraps' output, and `cli.py:475-481`'s `NEXT STEPS` must all name
   the enrollment path. The original failure was never that the good path was missing.
