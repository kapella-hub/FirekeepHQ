#!/usr/bin/env bash
set -euo pipefail

# Firekeep — idempotent auth key bootstrap (SP1a §4.2).
#
# Mints (only if absent):
#   1. FIREKEEP_INTERNAL_KEY  — internal service key (bridge distiller, workers).
#      Scopes: memory:write, session:read, eval:read, eval:write (NOT admin — a leaked
#      internal key cannot mint keys or read vault). Plaintext -> .env.
#   2. DASHBOARD_API_KEY — dashboard nginx proxy key (spec §4.4b: the
#      dashboard IS the owner's admin surface). Scopes: ["*"]. Plaintext -> .env.
#   3. RELAY_INTERNAL_API_KEY — Relay's outbound key for the ONE call it makes
#      into Bridge: POST /sessions/{agent_id}/context, which persists NexusScope
#      decisions for origin:"mcp" sessions (relay/app/scope.py _persist_to_bridge).
#      Bridge gates that route with require_scope_asgi(request, "session:write")
#      (bridge/app/mcp_server.py:561), so that single scope is the whole
#      requirement — deliberately NOT the internal key's set and NOT ["*"].
#      docker-compose.yml already reads it (NR_FIREKEEP_API_KEY:
#      ${RELAY_INTERNAL_API_KEY:-}); nothing minted it, so the var resolved
#      empty and the write went out unkeyed. That was survivable only while
#      auth was off by default: _persist_to_bridge is best-effort and swallows
#      the failure, so with auth ON the decisions would have silently stopped
#      persisting with nothing but a warning in relay's log. Plaintext -> .env.
#   4. Admin key — the owner's key. Scopes: ["*"]. Plaintext printed ONCE,
#      never written to disk.
#
# Redis layout replicates auth/middleware.py create_key() EXACTLY:
#   auth:key:{sha256hex}  hash: agent_id, scopes (JSON array string),
#                               created_at (ISO-8601 UTC), key_id (hash[:16])
#   auth:key_index        zset: member = hash[:16], score = unix timestamp
# (no expires_at — bootstrap keys do not expire)
#
# Idempotency:
#   - env-backed keys: if .env carries the key and its hash is registered,
#     nothing happens. If .env has the key but Redis lost it (down -v), the
#     hash is re-registered — same plaintext, NO rotation.
#   - admin key: marker auth:bootstrap:admin_hash records the admin key hash;
#     if that hash is still registered, nothing is minted.
#
# Env overrides (used by tests):
#   ENV_FILE              target env file          (default: ./.env)
#   BOOTSTRAP_REDIS_CMD   redis-cli command line   (default: docker compose exec -T redis redis-cli -n 7)

ENV_FILE="${ENV_FILE:-.env}"
IFS=' ' read -r -a REDIS <<< "${BOOTSTRAP_REDIS_CMD:-docker compose exec -T redis redis-cli -n 7}"

# --- helpers ---------------------------------------------------------------

sha256() { printf '%s' "$1" | sha256sum | awk '{print $1}'; }

mint_key() { echo "nxs_$(openssl rand -hex 24)"; }

now_iso() { date -u +"%Y-%m-%dT%H:%M:%S+00:00"; }

# `|| true`: grep exits 1 on no-match, which set -e -o pipefail would fatal.
env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -n1 | cut -d= -f2-; }

env_set() {
    if grep -qE "^$1=" "$ENV_FILE" 2>/dev/null; then
        sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    fi
}

key_registered() { [ "$("${REDIS[@]}" EXISTS "auth:key:$1")" = "1" ]; }

register_hash() {  # $1=hash  $2=agent_id  $3=scopes-json
    local hash="$1"
    "${REDIS[@]}" HSET "auth:key:${hash}" \
        agent_id "$2" \
        scopes "$3" \
        created_at "$(now_iso)" \
        key_id "${hash:0:16}" > /dev/null
    "${REDIS[@]}" ZADD auth:key_index "$(date -u +%s)" "${hash:0:16}" > /dev/null
}

MINTED=0

ensure_env_key() {  # $1=env var  $2=agent_id  $3=scopes-json
    local var="$1" agent_id="$2" scopes="$3" key hash
    key="$(env_get "$var")"
    if [ -z "$key" ]; then
        key="$(mint_key)"
        env_set "$var" "$key"
        register_hash "$(sha256 "$key")" "$agent_id" "$scopes"
        MINTED=$((MINTED + 1))
        echo "[MINTED] $var  (agent_id=$agent_id scopes=$scopes)"
    else
        hash="$(sha256 "$key")"
        if key_registered "$hash"; then
            echo "[OK] $var already provisioned"
        else
            register_hash "$hash" "$agent_id" "$scopes"
            echo "[RE-REGISTERED] $var hash (Redis had lost it; plaintext unchanged)"
        fi
    fi
}

# --- preconditions (fail loudly — Reliability Principle) --------------------

if ! command -v openssl > /dev/null; then
    echo "ERROR: openssl is required to mint keys" >&2
    exit 1
fi

if ! "${REDIS[@]}" PING 2>/dev/null | grep -q PONG; then
    echo "ERROR: cannot reach Redis DB 7 via: ${REDIS[*]}" >&2
    echo "       (is the stack up? try: docker compose up -d redis)" >&2
    exit 1
fi

touch "$ENV_FILE"

# --- 1+2: env-backed service keys -------------------------------------------

ensure_env_key FIREKEEP_INTERNAL_KEY  firekeep-internal  '["memory:write","session:read","eval:read","eval:write"]'
ensure_env_key DASHBOARD_API_KEY firekeep-dashboard '["*"]'
ensure_env_key RELAY_INTERNAL_API_KEY firekeep-relay '["session:write"]'

# --- 4: owner admin key (printed once, never stored) -------------------------
#
# NOTE for anyone adding a key above: mint it through ensure_env_key, never a
# hand-rolled echo. ensure_env_key prints only the VAR NAME, agent_id and
# scopes — never the plaintext — which is what makes the admin key below the
# only `nxs_...` literal in this script's output. install.sh relies on exactly
# that to re-surface the admin key in its closing summary (it greps its
# captured bootstrap output for a single nxs_ token). A second plaintext in
# this stream would both leak that key into install.sh's captured stdout and
# make the summary print the wrong one.

ADMIN_MARKER="auth:bootstrap:admin_hash"
ADMIN_HASH="$("${REDIS[@]}" GET "$ADMIN_MARKER")"
if [ -n "$ADMIN_HASH" ] && key_registered "$ADMIN_HASH"; then
    echo "[OK] admin key already provisioned (key_id ${ADMIN_HASH:0:16})"
else
    ADMIN_KEY="$(mint_key)"
    ADMIN_HASH="$(sha256 "$ADMIN_KEY")"
    register_hash "$ADMIN_HASH" "admin" '["*"]'
    "${REDIS[@]}" SET "$ADMIN_MARKER" "$ADMIN_HASH" > /dev/null
    MINTED=$((MINTED + 1))
    echo ""
    echo "============================================================"
    echo "  ADMIN API KEY — shown ONCE, not written to disk."
    echo "  Store it in your password manager now:"
    echo ""
    echo "    $ADMIN_KEY"
    echo ""
    echo "  Use it with deploy/firekeep-admin to issue teammate keys."
    echo "============================================================"
fi

echo ""
echo "bootstrap-keys: done ($MINTED key(s) minted)"
