#!/usr/bin/env bash
set -euo pipefail

# Firekeep — idempotent auth key bootstrap (SP1a §4.2).
#
# Mints (only if absent):
#   1. FIREKEEP_INTERNAL_KEY  — internal service key (bridge distiller, workers).
#      Scopes: memory:write, session:read, eval:read, eval:write,
#      session:read:workspace (NOT admin — a leaked internal key cannot mint
#      keys or read vault). The last scope is SERVICE-ONLY (2026-10-01): Bridge
#      lists/reads only the caller's own sessions over REST unless the key holds
#      it, and Cortex's workers (OWM, skill scoring/synthesis, patterns) need
#      every session in the workspace. A key minted before it existed is
#      added in place by ensure_env_key's scope reconciliation. Also
#      relay:write:service (SERVICE-ONLY, 2026-10-04): the key's two Relay
#      writes — Sentinel's alert broadcast and Cortex's fleet POST /tasks —
#      which no longer pass on "any valid key" now that Relay's member-bound
#      operations require relay:read/relay:write (an owner-member service key
#      must not act as the owner inside Relay). Reconciled onto existing keys
#      the same way. Plaintext -> .env.
#   2. DASHBOARD_API_KEY — dashboard nginx proxy key (spec §4.4b: the
#      dashboard IS the owner's admin surface). Scopes: ["*"]. Plaintext -> .env.
#   3. (retired) RELAY_INTERNAL_API_KEY — no longer minted (2026-10-05,
#      THREAT-MODEL §5.18). It was Relay's outbound key for persisting
#      NexusScope decisions into Bridge (POST /sessions/{agent_id}/context).
#      Since 2026-10-04 Relay writes those with the key of the member who owns
#      the scope session (§5.14), and with auth off Bridge checks no key, so
#      nothing presented it. A minted copy was an owner-member credential with
#      session:write that no code used — a liability, not a fallback. On an
#      existing deployment retire_env_key REVOKES that record (only if it is
#      still the firekeep-relay credential this script minted) and removes the
#      line from .env, which every env_file service imports wholesale.
#   4. FIREKEEP_BRIDGE_KEY — Bridge's own dedicated credential (Task 5). Scopes:
#      memory:read, memory:write, session:read, eval:read, eval:write,
#      eval:grade. memory:read (2026-10-01) because Bridge's prior-art and
#      proactive-recall paths POST /memory/recall with this key, and that
#      route now declares memory:read — without it both would 403, and both
#      swallow the failure, so they would simply go quiet. TRANSITIONAL: needed
#      only while Bridge recalls with its own key. Once Bridge forwards the
#      caller's key on those paths (fix/bridge-session-ownership), drop
#      memory:read from the list below — and narrow already-provisioned keys
#      by hand (HSET auth:key:<hash> scopes ...), because reconciliation only
#      ever adds. The last
#      scope is SERVICE-ONLY (auth/keys.py SERVICE_ONLY_SCOPES): it authorizes
#      the task_result grade hint on POST /evals/sessions/{id}/compute
#      (cortex/app/evals/api.py _hint_authorized) and is minted onto this ONE
#      credential — never onto FIREKEEP_INTERNAL_KEY, never mintable through
#      POST /auth/keys (create_key rejects it outright). docker-compose.yml
#      wires it to ONLY the bridge service (NB_FIREKEEP_API_KEY:
#      ${FIREKEEP_BRIDGE_KEY:-}) and blanks it explicitly in every other
#      env_file: .env service. memory:write:delegated (2026-10-04) is
#      SERVICE-ONLY too: Bridge's background distiller writes each session's
#      distillate through POST /memory/learn/delegated, naming the session's
#      verified owner -- without it every teammate's distillate was attributed
#      to the deployment owner this key is minted as. Cortex matches it
#      literally ("*" keys do not pass). A key minted before it existed gains it
#      through ensure_env_key's scope reconciliation (update.sh); until then,
#      with auth enabled, distillation 403s and retries. Plaintext -> .env.
#   5. Admin key — the owner's key. Scopes: ["*"]. Plaintext printed ONCE,
#      never written to disk.
#
# Redis layout replicates auth/keys.py create_key() EXACTLY:
#   auth:key:{sha256hex}  hash: workspace_id, member_id, device_id,
#                               scopes (JSON array string), created_at,
#                               key_id, credential_id
#   auth:cred:{credential_id} -> sha256hex exact-resolution mapping
#   auth:key_index        zset: member = credential_id, score = unix timestamp
# (no expires_at — bootstrap keys do not expire)
#
# Idempotency:
#   - env-backed keys: if .env carries the key and its hash is registered,
#     no key is minted or rotated. If .env has the key but Redis lost it
#     (down -v), the hash is re-registered — same plaintext, NO rotation.
#   - scope reconciliation (2026-10-01): a registered env-backed key that is
#     MISSING a scope declared below gets it added in place ([RECONCILED]).
#     Union only — a scope is never removed, so an operator's deliberate
#     widening survives, and narrowing stays a manual act. Without this a
#     newly gated route 403s every deployment provisioned before the scope
#     was declared, because the key already exists and was never revisited
#     (the reason the Relay task routes were left ungated —
#     docs/guides/relay-coordination.md).
#   - admin key: marker auth:bootstrap:admin_hash records the admin key hash;
#     if that hash is still registered, nothing is minted.
#
# Env overrides (used by tests):
#   ENV_FILE              target env file          (default: ./.env)
#   BOOTSTRAP_REDIS_CMD   redis-cli command line   (default: docker compose exec -T redis redis-cli -n 7)

ENV_FILE="${ENV_FILE:-.env}"
IFS=' ' read -r -a REDIS <<< "${BOOTSTRAP_REDIS_CMD:-docker compose exec -T redis redis-cli -n 7}"

# Every Redis call goes through rcli, with stdin closed. In production REDIS is
# `docker compose exec -T redis redis-cli -n 7`, and docker compose exec reads
# stdin even with -T: inside `while read ...; done < <(scan)` the first call in
# the loop body swallowed the rest of the scan, so the loop stopped after one
# record (`keys audit` on a live box reported 1 credential of 15, 2026-10-04).
# Never call "${REDIS[@]}" directly.
rcli() { "${REDIS[@]}" "$@" < /dev/null; }

# --- helpers ---------------------------------------------------------------

# sha256sum is GNU coreutils; macOS ships `shasum -a 256` instead. Prefer the
# GNU tool where present (Linux, the CI-tested path), fall back on macOS/BSD.
sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        printf '%s' "$1" | sha256sum | awk '{print $1}'
    else
        printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
    fi
}

mint_key() { echo "nxs_$(openssl rand -hex 24)"; }

now_iso() { date -u +"%Y-%m-%dT%H:%M:%S+00:00"; }

# `|| true`: grep exits 1 on no-match, which set -e -o pipefail would fatal.
env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | head -n1 | cut -d= -f2-; }

# Portable in-place sed: BSD/macOS sed would consume the script as a -i backup
# suffix and corrupt the file. Temp-file behaves identically everywhere.
# bootstrap-keys.sh does not source deploy/lib.sh, so it carries its own copy.
sed_i() {
    local script="${1:?}" f="${2:?}" tmp
    tmp="$(mktemp "${f}.XXXXXX")" || return 1
    if sed "$script" "$f" >"$tmp"; then mv "$tmp" "$f"; else rm -f "$tmp"; return 1; fi
}

env_set() {
    if grep -qE "^$1=" "$ENV_FILE" 2>/dev/null; then
        sed_i "s|^$1=.*|$1=$2|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"
    fi
}

ensure_deployment_id() {  # $1=env var  $2=prefix
    local var="$1" prefix="$2" value
    value="$(env_get "$var")"
    if [ -z "$value" ]; then
        value="${prefix}-$(openssl rand -hex 16)"
        env_set "$var" "$value"
        echo "[GENERATED] $var" >&2
    elif [[ ! "$value" =~ ^[A-Za-z0-9._-]+$ ]] || [ "${#value}" -gt 128 ]; then
        echo "ERROR: $var must contain only letters, digits, dot, underscore or hyphen (max 128 chars)" >&2
        exit 1
    fi
    printf '%s' "$value"
}

key_registered() { [ "$(rcli EXISTS "auth:key:$1")" = "1" ]; }

env_del() { sed_i "/^$1=/d" "$ENV_FILE"; }

# Scope tokens of a JSON array of plain strings, one per line. Deliberately
# jq-free (this script needs only bash, redis-cli and openssl); scope names are
# [a-z:*] and never contain a quote, so the token grep is exact.
scope_tokens() { printf '%s' "$1" | grep -oE '"[^"]*"' | tr -d '"' || true; }

reconcile_scopes() {  # $1=hash  $2=declared scopes-json  -> prints added scopes
    local hash="$1" declared="$2" stored merged added="" s
    stored="$(rcli HGET "auth:key:${hash}" scopes)"
    merged="$stored"
    while IFS= read -r s; do
        [ -n "$s" ] || continue
        if ! scope_tokens "$stored" | grep -qxF -- "$s"; then
            added="${added:+$added,}$s"
            if [ "$(scope_tokens "$merged" | wc -l)" -eq 0 ]; then
                merged="[\"$s\"]"
            else
                merged="${merged%]},\"$s\"]"
            fi
        fi
    done < <(scope_tokens "$declared")
    if [ -n "$added" ]; then
        rcli HSET "auth:key:${hash}" scopes "$merged" > /dev/null
        printf '%s' "$added"
    fi
}

register_hash() {  # $1=hash  $2=device_id  $3=scopes-json
    local hash="$1" credential_id
    credential_id="$(openssl rand -hex 8)"
    rcli HSET "auth:key:${hash}" \
        workspace_id "$WORKSPACE_ID" \
        member_id "$OWNER_MEMBER_ID" \
        device_id "$2" \
        credential_id "$credential_id" \
        scopes "$3" \
        created_at "$(now_iso)" \
        key_id "$credential_id" > /dev/null
    rcli SET "auth:cred:${credential_id}" "$hash" > /dev/null
    rcli ZADD auth:key_index "$(date -u +%s)" "$credential_id" > /dev/null
}

backfill_credential_mappings() {
    local credential_id mapped candidate stored_id count match hash ttl
    while IFS= read -r credential_id; do
        [ -n "$credential_id" ] || continue
        mapped="$(rcli GET "auth:cred:${credential_id}")"
        [ -z "$mapped" ] || continue
        count=0; match=""
        while IFS= read -r candidate; do
            [ -n "$candidate" ] || continue
            stored_id="$(rcli HGET "$candidate" credential_id)"
            [ -n "$stored_id" ] || stored_id="$(rcli HGET "$candidate" key_id)"
            if [ "$stored_id" = "$credential_id" ]; then
                match="$candidate"
                count=$((count + 1))
            fi
        done < <(rcli --scan --pattern "auth:key:${credential_id}*")
        if [ "$count" -eq 1 ]; then
            hash="${match#auth:key:}"
            rcli SET "auth:cred:${credential_id}" "$hash" > /dev/null
            ttl="$(rcli TTL "$match")"
            [ "$ttl" -gt 0 ] && rcli EXPIRE "auth:cred:${credential_id}" "$ttl" > /dev/null
            rcli HSET "$match" credential_id "$credential_id" \
                device_id "$(rcli HGET "$match" agent_id)" > /dev/null
            echo "[BACKFILLED] auth:cred:${credential_id}"
        elif [ "$count" -gt 1 ]; then
            echo "[REFUSED] auth:cred:${credential_id}: ${count} legacy records are ambiguous" >&2
        fi
    done < <(rcli ZRANGE auth:key_index 0 -1)
}

# The workspace record and owner member row, mirroring auth/workspace.py
# ensure_workspace (HSETNX only: never rewrites what is there). validate_key
# refuses a credential whose member row is missing (2026-10-04), and the
# FastMCP services never run ensure_workspace themselves — without this a
# freshly minted key could not authenticate at Bridge/Relay/Sentinel until
# cortex-api had booted once.
ensure_owner_member() {
    local now; now="$(now_iso)"
    rcli HSETNX auth:workspace:current workspace_id "$WORKSPACE_ID" > /dev/null
    rcli HSETNX auth:workspace:current owner_member_id "$OWNER_MEMBER_ID" > /dev/null
    rcli HSETNX auth:workspace:current created_at "$now" > /dev/null
    rcli HSETNX "auth:member:${OWNER_MEMBER_ID}" member_id "$OWNER_MEMBER_ID" > /dev/null
    rcli HSETNX "auth:member:${OWNER_MEMBER_ID}" workspace_id "$WORKSPACE_ID" > /dev/null
    rcli HSETNX "auth:member:${OWNER_MEMBER_ID}" role owner > /dev/null
    rcli HSETNX "auth:member:${OWNER_MEMBER_ID}" status active > /dev/null
    rcli HSETNX "auth:member:${OWNER_MEMBER_ID}" created_at "$now" > /dev/null
    rcli ZADD auth:member_index NX "$(date -u +%s)" "$OWNER_MEMBER_ID" > /dev/null
}

# Upgrade path for credentials with no recorded owner (2026-10-04).
# validate_key used to give a record with no member_id/workspace_id to the
# deployment owner, silently. It now refuses such a record, so this pass makes
# the old meaning EXPLICIT and LOGGED before the new validators start
# (update.sh runs this script before `compose up`): the owner and this
# workspace are stamped onto any record missing them, a credential id is
# derived from the legacy key_id (or the hash prefix the old validator used),
# and a record the index never listed is indexed so it can be seen and
# revoked. Scans auth:key:* rather than the index for that reason. A field
# that is already set is never overwritten. Cortex runs the same pass on every
# boot (auth/workspace.py attribute_unowned_credentials) for deployments that
# never run this script. `deploy/firekeep-admin keys audit` is the read-only
# view of the same records.
attribute_legacy_credentials() {
    local key hash wid mid cid kid device changes
    while IFS= read -r key; do
        [[ "$key" =~ ^auth:key:[0-9a-f]{64}$ ]] || continue
        hash="${key#auth:key:}"
        wid="$(rcli HGET "$key" workspace_id)"
        mid="$(rcli HGET "$key" member_id)"
        cid="$(rcli HGET "$key" credential_id)"
        kid="$(rcli HGET "$key" key_id)"
        changes=()
        [ -n "$wid" ] || changes+=(workspace_id "$WORKSPACE_ID")
        [ -n "$mid" ] || changes+=(member_id "$OWNER_MEMBER_ID")
        if [ -z "$cid" ]; then
            cid="${kid:-${hash:0:16}}"
            changes+=(credential_id "$cid")
        fi
        [ -n "$kid" ] || changes+=(key_id "$cid")
        if [ "${#changes[@]}" -gt 0 ]; then
            rcli HSET "$key" "${changes[@]}" > /dev/null
            device="$(rcli HGET "$key" device_id)"
            [ -n "$device" ] || device="$(rcli HGET "$key" agent_id)"
            echo "[ATTRIBUTED] credential ${cid} (device ${device:-?}) -> member ${mid:-$OWNER_MEMBER_ID}, workspace ${wid:-$WORKSPACE_ID}"
        fi
        if [ -z "$(rcli ZSCORE auth:key_index "$cid")" ]; then
            rcli ZADD auth:key_index NX "$(date -u +%s)" "$cid" > /dev/null
            echo "[INDEXED] credential ${cid} was not in auth:key_index"
        fi
    done < <(rcli --scan --pattern 'auth:key:*')
}

MINTED=0

ensure_env_key() {  # $1=env var  $2=device_id  $3=scopes-json
    local var="$1" device_id="$2" scopes="$3" key hash
    key="$(env_get "$var")"
    if [ -z "$key" ]; then
        key="$(mint_key)"
        env_set "$var" "$key"
        register_hash "$(sha256 "$key")" "$device_id" "$scopes"
        MINTED=$((MINTED + 1))
        echo "[MINTED] $var  (device_id=$device_id scopes=$scopes)"
    else
        hash="$(sha256 "$key")"
        if key_registered "$hash"; then
            local added
            added="$(reconcile_scopes "$hash" "$scopes")"
            if [ -n "$added" ]; then
                echo "[RECONCILED] $var scopes += $added (plaintext unchanged)"
            else
                echo "[OK] $var already provisioned"
            fi
        else
            register_hash "$hash" "$device_id" "$scopes"
            echo "[RE-REGISTERED] $var hash (Redis had lost it; plaintext unchanged)"
        fi
    fi
}

# Retire an env-backed service key this script used to mint: revoke its record
# (the same three keys `firekeep-admin keys revoke` removes) and drop the .env
# line. Revokes ONLY a record whose device is $2 — the one this script minted.
# If an operator pointed the variable at some other credential, that
# credential is left alone and named, so it can be revoked deliberately. The
# .env line goes either way: nothing reads it, every env_file service would
# still import it, and an older bootstrap-keys.sh simply mints a fresh one on
# a rollback. A second run finds no line and does nothing.
retire_env_key() {  # $1=env var  $2=device_id this script minted it with
    local var="$1" device_id="$2" key hash record device cid
    key="$(env_get "$var")"
    if [ -n "$key" ]; then
        hash="$(sha256 "$key")"
        record="auth:key:${hash}"
        if key_registered "$hash"; then
            device="$(rcli HGET "$record" device_id)"
            [ -n "$device" ] || device="$(rcli HGET "$record" agent_id)"
            cid="$(rcli HGET "$record" credential_id)"
            [ -n "$cid" ] || cid="$(rcli HGET "$record" key_id)"
            if [ "$device" = "$device_id" ]; then
                rcli DEL "$record" > /dev/null
                if [ -n "$cid" ]; then
                    rcli DEL "auth:cred:${cid}" > /dev/null
                    rcli ZREM auth:key_index "$cid" > /dev/null
                fi
                echo "[REVOKED] $var (credential ${cid:-?}, device $device_id): retired, nothing presents it"
            else
                echo "[SKIPPED] $var names credential ${cid:-?} (device ${device:-?}), not the $device_id key this script minted; it stays live — revoke it deliberately if unused: deploy/firekeep-admin keys revoke ${cid:-CREDENTIAL_ID}" >&2
            fi
        fi
    fi
    if grep -qE "^${var}=" "$ENV_FILE" 2>/dev/null; then
        env_del "$var"
        echo "[RETIRED] $var removed from $ENV_FILE"
    fi
}

# --- preconditions (fail loudly — Reliability Principle) --------------------

if ! command -v openssl > /dev/null; then
    echo "ERROR: openssl is required to mint keys" >&2
    exit 1
fi

touch "$ENV_FILE"
WORKSPACE_ID="$(ensure_deployment_id FIREKEEP_WORKSPACE_ID workspace)"
OWNER_MEMBER_ID="$(ensure_deployment_id FIREKEEP_OWNER_MEMBER_ID member)"

if ! rcli PING 2>/dev/null | grep -q PONG; then
    echo "ERROR: cannot reach Redis DB 7 via: ${REDIS[*]}" >&2
    echo "       (is the stack up? try: docker compose up -d redis)" >&2
    exit 1
fi

ensure_owner_member

# --- 1, 2, 4: env-backed service keys -------------------------------------------

ensure_env_key FIREKEEP_INTERNAL_KEY  firekeep-internal  '["memory:write","session:read","eval:read","eval:write","session:read:workspace","relay:write:service"]'
ensure_env_key DASHBOARD_API_KEY firekeep-dashboard '["*"]'
ensure_env_key FIREKEEP_BRIDGE_KEY firekeep-bridge '["memory:read","memory:write","session:read","eval:read","eval:write","eval:grade","memory:write:delegated"]'

# --- 3: retired keys -----------------------------------------------------------
retire_env_key RELAY_INTERNAL_API_KEY firekeep-relay

# --- 5: owner admin key (printed once, never stored) -------------------------
#
# NOTE for anyone adding a key above: mint it through ensure_env_key, never a
# hand-rolled echo. ensure_env_key prints only the VAR NAME, device_id and
# scopes — never the plaintext — which is what makes the admin key below the
# only `nxs_...` literal in this script's output. install.sh relies on exactly
# that to re-surface the admin key in its closing summary (it greps its
# captured bootstrap output for a single nxs_ token). A second plaintext in
# this stream would both leak that key into install.sh's captured stdout and
# make the summary print the wrong one.

ADMIN_MARKER="auth:bootstrap:admin_hash"
ADMIN_HASH="$(rcli GET "$ADMIN_MARKER")"
if [ -n "$ADMIN_HASH" ] && key_registered "$ADMIN_HASH"; then
    ADMIN_CREDENTIAL_ID="$(rcli HGET "auth:key:${ADMIN_HASH}" credential_id)"
    echo "[OK] admin key already provisioned (credential_id ${ADMIN_CREDENTIAL_ID})"
else
    ADMIN_KEY="$(mint_key)"
    ADMIN_HASH="$(sha256 "$ADMIN_KEY")"
    register_hash "$ADMIN_HASH" "admin" '["*"]'
    rcli SET "$ADMIN_MARKER" "$ADMIN_HASH" > /dev/null
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

# Upgrade path for credentials created before independent ID mappings existed.
# Ambiguity is refused rather than guessed; every new record above already has
# the mapping, so idempotent runs add nothing. Attribution runs first so a
# record it newly indexes gets its mapping in the same run.
attribute_legacy_credentials
backfill_credential_mappings

echo ""
echo "bootstrap-keys: done ($MINTED key(s) minted)"
