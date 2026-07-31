# Office Deployment Runbook — TLS front + full-surface auth (SP1a)

Since 2026-07-26 the first two items below are **baseline everywhere**, not office
specifics: `AUTH_ENABLED` defaults to `true` and app ports bind to `127.0.0.1` on
every install. What still makes this deployment different is the TLS front and the
multi-person key model.

1. `AUTH_ENABLED=true` — every MCP and REST request needs a valid `X-API-Key`. Now
   the default; the office difference is that keys are issued *per person* rather
   than one owner holding the admin key.
2. App ports are pinned to `127.0.0.1` by the `docker-compose.office.yml` override
   rather than by `BIND_ADDR`. The override uses `!override` on each `ports:` list,
   so it wins regardless of what `BIND_ADDR` is set to — an operator who sets
   `BIND_ADDR=0.0.0.0` here does **not** widen the office deployment. The only
   network-reachable surface stays Caddy on :443 (+ :80 redirect).
3. Caddy terminates TLS with an internal CA and routes by path:
   `https://<host>/mcp/<svc>` → the service's `/mcp` endpoint, and
   `https://<host>/api/<svc>/...` → the service's REST routes.
   `/mcp/symdex` is deliberately NOT routed — office symdex runs stdio-local.

## 1. Enable (fresh install or existing deployment)

Order matters. An existing deployment that flips `AUTH_ENABLED=true` without
keys locks itself out (even `POST /auth/keys` returns 401).

**Also pin `COMPOSE_FILE` in `.env`.** `install.sh`/`update.sh` both invoke a
BARE `docker compose` (no `-f` flags) for every build/up/restart. Docker
Compose v2 reads `COMPOSE_FILE` from `.env` automatically, so setting it once
makes every future bare `docker compose` call transparently load both files —
without it, a routine `update.sh` silently drops the Caddy TLS front (see §6 of
the ledger / final-review fix wave) — so the only TLS-terminated, path-routed
entry point to this deployment disappears, and the app ports fall back to
whatever `BIND_ADDR` says in the base compose file. That is loopback by default,
which fails safe; but if this host also sets `BIND_ADDR=0.0.0.0` for any reason,
losing the override republishes every app port in the clear, with no Caddy in
front. `install.sh --office` writes this line automatically on a fresh
`.env` (it also writes `FIREKEEP_OFFICE_MODE=true`, a separate marker that
`update.sh`'s office-front safety guard uses to know this deployment is
office at all — the guard can't use `docker-compose.office.yml`'s mere
presence for that, since the file is committed and always exists, nor the
`COMPOSE_FILE` line itself, since detecting that line going missing is the
guard's whole job). Add both lines by hand on an existing deployment that
predates this fix, so the safety guard covers it too:

```bash
cd /path/to/Firekeep
echo 'COMPOSE_FILE=docker-compose.yml:docker-compose.office.yml' >> .env
echo 'FIREKEEP_OFFICE_MODE=true' >> .env
bash deploy/bootstrap-keys.sh        # idempotent — safe to re-run any time
# -> writes FIREKEEP_INTERNAL_KEY= and DASHBOARD_API_KEY= into .env
# -> registers internal + dashboard + admin key hashes in Redis DB 7
# -> prints the ADMIN key plaintext ONCE. Save it in a password manager NOW.
chmod 600 .env                       # bootstrap-keys.sh writes live keys here
sed -i 's/^AUTH_ENABLED=.*/AUTH_ENABLED=true/' .env
docker compose -f docker-compose.yml -f docker-compose.office.yml up -d --build
```

> **Fully keyed as of SP1b Task 33:** every outbound call to an auth-enforced
> surface now carries an internal key under `AUTH_ENABLED=true`. Sentinel's
> alert broadcasting to Relay and its Cortex webhook firing are keyed (SP1b
> §11, Task 32 — threads `FIREKEEP_INTERNAL_KEY` via `NS_FIREKEEP_INTERNAL_KEY`).
> Cortex→Bridge (Skill Synthesis) is keyed. Symdex→Cortex calls are keyed
> (SP1b §11, Task 33 — threads bare `FIREKEEP_INTERNAL_KEY`, no Settings
> prefix, as `X-API-Key`) — this was the last dark integration. (Symdex
> itself has no auth middleware and is loopback-only as of Task 33, so
> inbound calls to it, e.g. Sentinel's git-reindex trigger, need no key.)
> No known gaps remain among the SP1b §11 background integrations (Sentinel,
> Symdex, Skill Synthesis). One separate known gap stays open: FirekeepScope's
> `origin:"mcp"` Relay→Bridge persistence needs a manually provisioned
> `NR_FIREKEEP_API_KEY` under `AUTH_ENABLED=true` (see CLAUDE.md's FirekeepScope
> section) — without it that one flow goes quiet.

`install.sh` and `update.sh` both invoke `deploy/bootstrap-keys.sh` automatically,
so routine updates keep the keys in place. Re-runs mint nothing if the key
hashes already exist in Redis. `update.sh` also fails loud (a warning banner,
not a hard stop) if this deployment is marked office (`FIREKEEP_OFFICE_MODE=true`
in `.env`) but `COMPOSE_FILE` is unset in both the shell environment and
`.env`, so a missing pin will not silently regress the TLS front on the next
routine update — as long as the marker above was set when this deployment was
enabled. A deployment predating the marker has no such coverage; add it by
hand (see §1) to close the gap.

### Issue teammate keys

```bash
deploy/firekeep-admin keys create --agent alice
# -> POST /auth/keys with the full NON-admin scope set; prints alice's nxs_ key once
```
Send the key out-of-band. The client kit's `firekeep-shim` injects `X-API-Key`
+ `X-Agent-Id` on every request from `[server]` and `[identity]` in
`~/.firekeep/config`; run `firekeep install` + `firekeep doctor` on the teammate's
machine to verify auth and CA trust.

## 2. CA export + trust installation (per teammate machine)

The `caddy_data` volume holds the internal CA. Export the root once:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./firekeep-root-ca.crt
```

Install (elevated) per OS:

| OS | Command |
|---|---|
| Windows | `certutil -addstore -f Root firekeep-root-ca.crt` |
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain firekeep-root-ca.crt` |
| Debian/Ubuntu | `sudo cp firekeep-root-ca.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| RHEL/Fedora | `sudo cp firekeep-root-ca.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust` |

Verify (no `-k` — it must pass real verification):
```bash
curl --fail -H "X-API-Key: <your key>" https://<host>/api/relay/presence
```

**Back up `caddy_data` with the same discipline as `VAULT_KEY`.** It contains
the CA private key. Losing it means Caddy mints a fresh root and EVERY
teammate machine must re-run trust installation.

## 3. kiro internal-CA empirical test (informational, 10 minutes)

Whether kiro-cli's MCP client trusts an OS-installed internal-CA root is
UNCONFIRMED (confirmed only for its AWS-backend connections). The shipped
default transport is the client kit's `firekeep-shim` (stdio↔Streamable-HTTP
bridge) regardless — this test only decides whether direct-CA trust works as
a drop-in alternative.

1. Install the root cert on a test machine (table above).
2. Add to the kiro agent config:
   `{"mcpServers": {"firekeep-cortex": {"url": "https://<host>/mcp/cortex", "headers": {"X-API-Key": "<key>", "X-Agent-Id": "<name>"}}}}`
3. Run `kiro-cli chat --agent firekeep` and check the MCP server connects
   (`kiro-cli mcp list` style output or a successful `memory_recall`).
4. If it fails: diagnostic only — retry with
   `SSL_CERT_FILE=<standard-roots + firekeep root concatenated bundle>`.
   Do NOT ship SSL_CERT_FILE as the mechanism (it REPLACES the OS store for
   rustls and breaks kiro's own AWS calls unless the bundle includes the
   standard roots).
5. Record the result (pass/fail + kiro version) in the SP1b spec.

## 4. Owner/teammate access (client kit)

Agent clients are turnkey via the portable client kit — no manual header
wiring required:

- Mint yourself a key: `deploy/firekeep-admin keys create --agent <you>`.
- On the agent's machine: unpack the `firekeep-client` tarball, run `./install`
  / `.\install.ps1` (or `firekeep install --runtime claude|codex|kiro|opencode|all`), then
  edit `~/.firekeep/config` — set `[server] base_url` to
  `https://<host>`, `api_key` to the minted `nxs_...` key, and `ca_path` to
  the exported root CA (section 2).
- `firekeep doctor` verifies connectivity, auth, and CA trust end-to-end. Every
  installed runtime uses the same `[server]` connection; re-run `firekeep install`
  after changing it to refresh any stale managed adapter entries.
- `firekeep-shim` (the stdio↔Streamable-HTTP bridge every runtime's MCP config
  points at) injects `X-API-Key` + `X-Agent-Id` on every request from the
  configured server — no per-tool header configuration needed.
- curl trust (for manual smoke checks): on Linux/macOS the OS trust install
  (section 2) covers curl. Git Bash on Windows ships its own CA bundle —
  either run against an SSH tunnel, or set
  `CURL_CA_BUNDLE=<standard bundle + firekeep root concatenated>`.
- On the office host itself, `127.0.0.1:<port>` still works plaintext
  (the office override rebinds, it does not remove, the host bindings).

## 5. Smoke checklist (run after every enable/update)

From a machine with the root CA trusted, `KEY` = any valid key,
`ADMIN` = the bootstrap admin key, `TEAM` = a non-admin teammate key:

```bash
# 1. keyless MCP -> 401
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/mcp/cortex \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'            # expect: 401
# 2. keyed MCP -> 200
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://<host>/mcp/cortex \
  -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'            # expect: 200
# 3. symdex not routed
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/mcp/symdex   # expect: 404
# 4. REST: keyless 401, keyed 200, /health skip-listed 200 without key
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/cortex/memory/stats                     # expect: 401
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" https://<host>/api/cortex/memory/stats # expect: 200
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/cortex/health                            # expect: 200
# 4b. dashboard: the public office front 404s /api/cortex/dashboard* at Caddy
#     (see deploy/Caddyfile) before auth even runs, so that path proves the
#     Caddy layer, not the auth layer. Probe the auth layer directly against
#     cortex-api (run from the box, e.g. over SSH or the office compose
#     override's 127.0.0.1:8100 rebind): the HTML shell stays keyless, its
#     /api/* data routes do not.
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/cortex/dashboard/api/memories             # expect: 404 (Caddy, not auth)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/dashboard/                              # expect: 200 (shell, keyless)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8100/dashboard/api/memories                  # expect: 401 (keyless, gated)
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $KEY" http://127.0.0.1:8100/dashboard/api/memories # expect: 200
# 5. confused-deputy closed: non-admin key calling vault_retrieve through cortex-mcp
#    -> in-band 403/permission error (NOT a decrypted secret)
curl -s -X POST https://<host>/mcp/cortex -H "X-API-Key: $TEAM" \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"vault_retrieve","arguments":{"key":"smoke-test"}}}'
# 6. internal key is not over-privileged
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Key: $FIREKEEP_INTERNAL_KEY" \
  -X POST https://<host>/api/cortex/auth/keys -H 'Content-Type: application/json' \
  -d '{"agent_id":"smoke","scopes":["memory:read"]}'             # expect: 403
# 7. relay/bridge/sentinel keyless -> 401
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/relay/presence    # expect: 401
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/bridge/sessions   # expect: 401
# 8. discovery is pre-auth
curl -s -o /dev/null -w '%{http_code}\n' https://<host>/.well-known/agent.json # expect: 200
```

## 6. Recovery

**Redis-wipe key recovery** (`docker compose down -v` or DB 7 loss): all key
hashes are gone, all clients 401. Fix: `bash deploy/bootstrap-keys.sh` again —
it re-registers the internal + dashboard keys from the plaintext still in
`.env` and mints a NEW admin key (printed once). Teammate keys must be
re-issued via `deploy/firekeep-admin keys create`.

**Locked out?** (lost admin key): two doors, on the office host itself.

1. **Re-run the bootstrap script** — the normal answer:
   ```bash
   bash deploy/bootstrap-keys.sh     # idempotent; prints a NEW admin key once
   ```
   It talks to Redis DB 7 with `redis-cli` and never calls the HTTP API, so
   being locked out of `/auth/keys` does not block it. No restart needed: keys
   are read per request.

   > **Turning auth off is NOT a recovery door, and stopped being one on
   > 2026-07-26.** This runbook used to say: set `AUTH_ENABLED=false`, mint via
   > `POST /auth/keys` because "anonymous passes through when disabled", then
   > flip back. That route is `require_scope("admin")`, and the anonymous
   > identity is now deliberately never granted `admin` — with auth off you get
   > a **403**, not a pass-through. Disabling auth to recover now costs you the
   > stack's protection and still does not mint a key.

2. **Direct hash write** (if the script itself is unavailable) — mirrors
   `auth/middleware.py` `create_key` exactly (`auth:key:{sha256}` hash +
   `auth:key_index` zset):
   ```bash
   KEY="nxs_$(openssl rand -hex 24)"
   HASH=$(printf '%s' "$KEY" | sha256sum | cut -d' ' -f1)
   docker compose exec redis redis-cli -n 7 HSET "auth:key:$HASH" \
     agent_id rescue-admin scopes '["admin"]' \
     created_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" key_id "${HASH:0:16}"
   docker compose exec redis redis-cli -n 7 ZADD auth:key_index "$(date +%s)" "${HASH:0:16}"
   echo "rescue admin key: $KEY"
   ```

**`DELETE /auth/keys/{key_id}` returns 409 (ambiguous key_id):** the short id
matches more than one verified record and nothing was revoked. The colliding
`auth:key:<hash>` records are named in both the 409 response detail and the
CRITICAL log line. Inspect each with `redis-cli -n 7 hgetall auth:key:<hash>`,
decide which is unwanted, and delete that one directly:
`redis-cli -n 7 del auth:key:<hash>`. The remaining credential's short id is
then unambiguous again and `DELETE /auth/keys/{key_id}` revokes it normally.

**"Healthy" containers serving 503s:** compose healthchecks are TCP-only, so a
container stays green while the auth middleware fails closed (Redis DB 7 down
while `AUTH_ENABLED=true` → every request 503s loudly). "Healthy" ≠ "serving".
The loudness surfaces in the 503 response body and the service logs
(`docker compose logs cortex-api relay bridge sentinel | grep -i "auth store"`).
Check Redis first: `docker compose exec redis redis-cli -n 7 ping`.
