#!/usr/bin/env bash
# Idempotency + Redis-layout test for deploy/bootstrap-keys.sh.
# Uses a disposable Redis container; validates the written layout with the
# REAL validator (auth.middleware.validate_key). Run from the repo root:
#   PYTHONPATH=. bash deploy/tests/test_bootstrap_keys.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" > /dev/null || PYTHON_BIN=python

CONTAINER=firekeep-bootstrap-test
docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
# Pinned to the same reference docker-compose.test.yml uses. NOTE:
# tests/test_image_pins.py does NOT discover this file (it scans compose
# files and Dockerfiles), so this line has no automated guard — it was the
# last live floating tag left in the repo after the pinning pass.
docker run -d --name "$CONTAINER" -p 127.0.0.1:16379:6379 redis:7.4.10-alpine@sha256:e7723ff73d963f5cc6d9c4643ea3d989527a402a319239054e9472a7fb9219a2 > /dev/null
trap 'docker rm -f "$CONTAINER" > /dev/null 2>&1' EXIT
until docker exec "$CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.5; done

export ENV_FILE="$(mktemp)"
export BOOTSTRAP_REDIS_CMD="docker exec $CONTAINER redis-cli -n 7"

# --- Run 1: mints internal + dashboard + admin -------------------------------
OUT1="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT1" | grep -q '\[MINTED\] FIREKEEP_INTERNAL_KEY'  || { echo "FAIL: internal key not minted";  echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '\[MINTED\] DASHBOARD_API_KEY' || { echo "FAIL: dashboard key not minted"; echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '\[MINTED\] RELAY_INTERNAL_API_KEY' || { echo "FAIL: relay key not minted"; echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '\[MINTED\] FIREKEEP_BRIDGE_KEY'  || { echo "FAIL: bridge key not minted";  echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q 'ADMIN API KEY'                  || { echo "FAIL: admin key not printed";    echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '5 key(s) minted'                || { echo "FAIL: expected 5 mints";         echo "$OUT1"; exit 1; }
grep -qE '^FIREKEEP_INTERNAL_KEY=nxs_[0-9a-f]{48}$'  "$ENV_FILE" || { echo "FAIL: .env internal key malformed";  exit 1; }
grep -qE '^DASHBOARD_API_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env dashboard key malformed"; exit 1; }
grep -qE '^RELAY_INTERNAL_API_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env relay key malformed"; exit 1; }
grep -qE '^FIREKEEP_BRIDGE_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env bridge key malformed"; exit 1; }
grep -qE '^FIREKEEP_WORKSPACE_ID=workspace-[0-9a-f]{32}$' "$ENV_FILE" || { echo "FAIL: workspace id missing/malformed"; exit 1; }
grep -qE '^FIREKEEP_OWNER_MEMBER_ID=member-[0-9a-f]{32}$' "$ENV_FILE" || { echo "FAIL: owner member id missing/malformed"; exit 1; }
INTERNAL_KEY_1="$(grep '^FIREKEEP_INTERNAL_KEY=' "$ENV_FILE" | cut -d= -f2-)"
RELAY_KEY_1="$(grep '^RELAY_INTERNAL_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
BRIDGE_KEY_1="$(grep '^FIREKEEP_BRIDGE_KEY=' "$ENV_FILE" | cut -d= -f2-)"
WORKSPACE_ID="$(grep '^FIREKEEP_WORKSPACE_ID=' "$ENV_FILE" | cut -d= -f2-)"
OWNER_MEMBER_ID="$(grep '^FIREKEEP_OWNER_MEMBER_ID=' "$ENV_FILE" | cut -d= -f2-)"

# --- install.sh's admin-key capture, against the REAL output ----------------
# install.sh re-surfaces the admin key in its closing summary, because this
# script prints it once — before a container build and a ~3.3GB model pull
# that push it thousands of lines up the scrollback — and never writes it to
# disk. The two files are joined by nothing but a regex, in different
# languages, with no shared constant. Asserting it here against a REAL run is
# the only place that coupling is actually exercised; a hand-written fixture
# would only re-test the assumption.
#
# Kept byte-identical to install.sh's extraction on purpose. If you change one,
# this fails until you change the other — which is the entire point.
CAPTURED="$(printf '%s\n' "$OUT1" | grep -oE 'nxs_[0-9a-f]{48}' | head -n1 || true)"
[ -n "$CAPTURED" ] || {
    echo "FAIL: install.sh's admin-key extraction found nothing in a real fresh bootstrap run"
    echo "      (a fresh install would silently render the 'already provisioned,"
    echo "       not recoverable' branch and the operator would never see the key)"
    exit 1
}
[ "${#CAPTURED}" -eq 52 ] || {
    echo "FAIL: captured admin key is ${#CAPTURED} chars, expected 52 (nxs_ + 48 hex)"
    exit 1
}
echo "$OUT1" | grep -qF "$CAPTURED" || { echo "FAIL: captured value is not in the output"; exit 1; }

# The admin key must be the ONLY plaintext in this stream. A new ensure_env_key
# call that echoed its own plaintext would both leak that key into install.sh's
# captured stdout and make the summary reprint the WRONG key — `head -n1` would
# silently pick whichever came first.
NXS_IN_OUTPUT="$(echo "$OUT1" | grep -oE 'nxs_[0-9a-f]{48}' | sort -u | wc -l)"
[ "$NXS_IN_OUTPUT" -eq 1 ] || {
    echo "FAIL: expected exactly 1 plaintext key in bootstrap output (the admin key), found $NXS_IN_OUTPUT"
    exit 1
}
# ...and it must be the ADMIN key specifically, not one of the .env-backed
# ones leaking out.
for v in FIREKEEP_INTERNAL_KEY DASHBOARD_API_KEY RELAY_INTERNAL_API_KEY FIREKEEP_BRIDGE_KEY; do
    val="$(grep "^${v}=" "$ENV_FILE" | cut -d= -f2-)"
    [ "$CAPTURED" != "$val" ] || { echo "FAIL: capture returned $v, not the admin key"; exit 1; }
done
DBSIZE1="$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)"

# --- Run 2: mints NOTHING, rotates NOTHING -----------------------------------
OUT2="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT2" | grep -q '0 key(s) minted' || { echo "FAIL: second run minted keys"; echo "$OUT2"; exit 1; }
echo "$OUT2" | grep -q 'ADMIN API KEY' && { echo "FAIL: admin key re-printed on second run"; exit 1; }
INTERNAL_KEY_2="$(grep '^FIREKEEP_INTERNAL_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ "$INTERNAL_KEY_1" = "$INTERNAL_KEY_2" ] || { echo "FAIL: internal key rotated"; exit 1; }
RELAY_KEY_2="$(grep '^RELAY_INTERNAL_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ "$RELAY_KEY_1" = "$RELAY_KEY_2" ] || { echo "FAIL: relay key rotated"; exit 1; }
BRIDGE_KEY_2="$(grep '^FIREKEEP_BRIDGE_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ "$BRIDGE_KEY_1" = "$BRIDGE_KEY_2" ] || { echo "FAIL: bridge key rotated"; exit 1; }

# The other half of the capture contract: an idempotent re-run mints nothing,
# so there is NO plaintext to find. install.sh must get an empty string here
# and take the honest "not recoverable, here is how to re-mint" branch —
# rather than reprinting a stale key or, worse, dying under `set -euo
# pipefail` because grep matched nothing (hence the `|| true` in both places).
CAPTURED_2="$(printf '%s\n' "$OUT2" | grep -oE 'nxs_[0-9a-f]{48}' | head -n1 || true)"
[ -z "$CAPTURED_2" ] || {
    echo "FAIL: an idempotent re-run leaked a plaintext key into bootstrap output"
    exit 1
}
DBSIZE2="$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)"
[ "$DBSIZE1" = "$DBSIZE2" ] || { echo "FAIL: DBSIZE changed $DBSIZE1 -> $DBSIZE2"; exit 1; }
echo "$OUT2" | grep -q 'RECONCILED' && { echo "FAIL: second run re-scoped a key"; echo "$OUT2"; exit 1; }
echo "$OUT2" | grep -qE 'ATTRIBUTED|INDEXED' && { echo "FAIL: second run re-attributed a key"; echo "$OUT2"; exit 1; }
# The owner member row validate_key now requires exists WITHOUT cortex having
# booted: the FastMCP services never run ensure_workspace themselves.
[ "$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:member:${OWNER_MEMBER_ID}" status)" = "active" ]     || { echo "FAIL: bootstrap did not write the active owner member row"; exit 1; }
[ "$(docker exec "$CONTAINER" redis-cli -n 7 HGET auth:workspace:current workspace_id)" = "$WORKSPACE_ID" ]     || { echo "FAIL: bootstrap did not write the workspace record"; exit 1; }

# --- Run 3: an internal key minted before session:read:workspace existed ----
# A deployment that minted FIREKEEP_INTERNAL_KEY before 2026-10-01 would keep
# Cortex's workers confined to the owner's own Bridge sessions. ensure_env_key's
# scope reconciliation adds the scope in place: same plaintext, same hash,
# same credential_id.
INTERNAL_HASH="$(printf '%s' "$INTERNAL_KEY_1" | sha256sum | awk '{print $1}')"
INTERNAL_CRED_1="$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:key:${INTERNAL_HASH}" credential_id)"
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${INTERNAL_HASH}" scopes \
    '["memory:write","session:read","eval:read","eval:write"]' > /dev/null
OUT3="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT3" | grep -q '\[RECONCILED\] FIREKEEP_INTERNAL_KEY scopes += session:read:workspace' \
    || { echo "FAIL: pre-existing internal key not upgraded"; echo "$OUT3"; exit 1; }
echo "$OUT3" | grep -q '0 key(s) minted' || { echo "FAIL: upgrade run minted keys"; echo "$OUT3"; exit 1; }
UPGRADED_SCOPES="$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:key:${INTERNAL_HASH}" scopes)"
[ "$UPGRADED_SCOPES" = '["memory:write","session:read","eval:read","eval:write","session:read:workspace","relay:write:service"]' ] \
    || { echo "FAIL: upgraded scopes wrong: $UPGRADED_SCOPES"; exit 1; }
INTERNAL_CRED_3="$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:key:${INTERNAL_HASH}" credential_id)"
[ "$INTERNAL_CRED_1" = "$INTERNAL_CRED_3" ] || { echo "FAIL: upgrade changed credential_id"; exit 1; }
OUT4="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT4" | grep -q 'RECONCILED' && { echo "FAIL: upgrade is not idempotent"; echo "$OUT4"; exit 1; }

# --- Run 5: scope reconciliation on a key provisioned before a scope existed --
# A deployment bootstrapped before 2026-10-01 holds a bridge key WITHOUT
# memory:read, and POST /memory/recall now declares it. ensure_env_key must add
# the missing scope in place (union only, same plaintext, no mint), and must
# never remove a scope an operator added by hand.
BRIDGE_HASH="$(printf '%s' "$BRIDGE_KEY_1" | { sha256sum 2>/dev/null || shasum -a 256; } | awk '{print $1}')"
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${BRIDGE_HASH}" scopes     '["memory:write","session:read","eval:read","eval:write","eval:grade","relay:read"]' > /dev/null
OUT3="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT3" | grep -q '\[RECONCILED\] FIREKEEP_BRIDGE_KEY scopes += memory:read'     || { echo "FAIL: bridge key scopes not reconciled"; echo "$OUT3"; exit 1; }
echo "$OUT3" | grep -q '0 key(s) minted' || { echo "FAIL: reconciliation minted keys"; echo "$OUT3"; exit 1; }
echo "$OUT3" | grep -qE 'nxs_[0-9a-f]{48}' && { echo "FAIL: reconciliation leaked a plaintext"; exit 1; }
SCOPES3="$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:key:${BRIDGE_HASH}" scopes)"
for want in memory:read memory:write eval:grade memory:write:delegated relay:read; do
    echo "$SCOPES3" | grep -q "\"$want\"" || { echo "FAIL: reconciled scopes lost/missed $want: $SCOPES3"; exit 1; }
done
OUT4="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT4" | grep -q 'RECONCILED' && { echo "FAIL: reconciliation is not idempotent"; echo "$OUT4"; exit 1; }
# Restore the canonical set so the layout check below sees the declared scopes.
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${BRIDGE_HASH}" scopes     '["memory:read","memory:write","session:read","eval:read","eval:write","eval:grade","memory:write:delegated"]' > /dev/null

# --- Run 6: credentials with no recorded owner (2026-10-04) -------------------
# validate_key no longer hands an unattributed record to the owner; this script
# must stamp the owner explicitly, and say so. The first record is exactly
# what docs/DEPLOYMENT-OFFICE.md's rescue-key recipe wrote before 2026-10-04;
# the second was never indexed (list_keys could not show it).
RESCUE_KEY="nxs_$(openssl rand -hex 24)"
RESCUE_HASH="$(printf '%s' "$RESCUE_KEY" | sha256sum | awk '{print $1}')"
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${RESCUE_HASH}"     agent_id rescue-admin scopes '["admin"]' created_at "2026-10-04T00:00:00Z"     key_id "${RESCUE_HASH:0:16}" > /dev/null
docker exec "$CONTAINER" redis-cli -n 7 ZADD auth:key_index "$(date +%s)" "${RESCUE_HASH:0:16}" > /dev/null
ORPHAN_HASH="$(printf 'orphan' | sha256sum | awk '{print $1}')"
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${ORPHAN_HASH}"     member_id member-bob scopes '["memory:read"]' > /dev/null
GONE_HASH="$(printf 'gone' | sha256sum | awk '{print $1}')"
docker exec "$CONTAINER" redis-cli -n 7 HSET "auth:key:${GONE_HASH}" \
    workspace_id "$WORKSPACE_ID" member_id member-gone credential_id 00000000deadbeef \
    key_id 00000000deadbeef scopes '["memory:read"]' > /dev/null

# keys audit, BEFORE the attribution pass, through a Redis command that
# refuses every write verb: the classifier must be read-only to the letter.
GUARD="$(mktemp)"
cat > "$GUARD" <<GUARD_EOF
#!/usr/bin/env bash
case "\$(printf '%s' "\${1:-}" | tr '[:lower:]' '[:upper:]')" in
    HSET|HSETNX|HDEL|SET|DEL|ZADD|ZREM|EXPIRE|FLUSHDB) echo "WRITE BLOCKED: \$*" >&2; exit 99 ;;
esac
exec docker exec $CONTAINER redis-cli -n 7 "\$@"
GUARD_EOF
DBSIZE_PRE_AUDIT="$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)"
AUDIT1="$(BOOTSTRAP_REDIS_CMD="bash $GUARD" bash deploy/firekeep-admin keys audit 2>&1)" \
    || { echo "FAIL: keys audit failed"; echo "$AUDIT1"; exit 1; }
echo "$AUDIT1" | grep -q "WRITE BLOCKED" && { echo "FAIL: keys audit tried to write"; echo "$AUDIT1"; exit 1; }
[ "$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)" = "$DBSIZE_PRE_AUDIT" ] || { echo "FAIL: audit changed DBSIZE"; exit 1; }
echo "$AUDIT1" | grep -E "^${RESCUE_HASH:0:16} .*unattributed: will be assigned to owner ${OWNER_MEMBER_ID}" > /dev/null \
    || { echo "FAIL: audit did not flag the rescue key as unattributed"; echo "$AUDIT1"; exit 1; }
echo "$AUDIT1" | grep -E "^00000000deadbeef .*refused: member member-gone is not active" > /dev/null \
    || { echo "FAIL: audit did not flag a missing member"; echo "$AUDIT1"; exit 1; }
echo "$AUDIT1" | grep -E "^${ORPHAN_HASH:0:16} .*not in auth:key_index" > /dev/null \
    || { echo "FAIL: audit did not flag the unindexed record"; echo "$AUDIT1"; exit 1; }
echo "$AUDIT1" | grep -qE "credential\(s\): [0-9]+ ok, 2 unattributed, 1 refused, 0 expired" \
    || { echo "FAIL: audit summary wrong"; echo "$AUDIT1"; exit 1; }

OUT6="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT6" | grep -q "\[ATTRIBUTED\] credential ${RESCUE_HASH:0:16} (device rescue-admin) -> member ${OWNER_MEMBER_ID}"     || { echo "FAIL: rescue key not attributed"; echo "$OUT6"; exit 1; }
echo "$OUT6" | grep -q "\[INDEXED\] credential ${ORPHAN_HASH:0:16}"     || { echo "FAIL: unindexed record not indexed"; echo "$OUT6"; exit 1; }
[ "$(docker exec "$CONTAINER" redis-cli -n 7 HGET "auth:key:${ORPHAN_HASH}" member_id)" = "member-bob" ]     || { echo "FAIL: attribution overwrote a member that was set"; exit 1; }
echo "$OUT6" | grep -q '0 key(s) minted' || { echo "FAIL: attribution run minted keys"; echo "$OUT6"; exit 1; }
OUT7="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT7" | grep -qE 'ATTRIBUTED|INDEXED' && { echo "FAIL: attribution is not idempotent"; echo "$OUT7"; exit 1; }
AUDIT2="$(BOOTSTRAP_REDIS_CMD="bash $GUARD" bash deploy/firekeep-admin keys audit 2>&1)"
echo "$AUDIT2" | grep -E "^${RESCUE_HASH:0:16} .* ok" > /dev/null \
    || { echo "FAIL: attributed rescue key not ok in audit"; echo "$AUDIT2"; exit 1; }
# 2 refused: member-gone, and the orphan — attribution filled its workspace
# and credential id but never invents a member row for the member it names.
echo "$AUDIT2" | grep -E "^${ORPHAN_HASH:0:16} .*refused: member member-bob is not active" > /dev/null \
    || { echo "FAIL: audit did not flag the orphan's missing member"; echo "$AUDIT2"; exit 1; }
echo "$AUDIT2" | grep -qE "0 unattributed, 2 refused" \
    || { echo "FAIL: post-attribution audit summary wrong"; echo "$AUDIT2"; exit 1; }
rm -f "$GUARD"

# --- Layout check: the REAL validator accepts the bootstrapped key -----------
# Deliberately NO init_auth(): it runs ensure_workspace, which would write the
# owner member row itself and hide a bootstrap that forgot to. Bridge, Relay
# and Sentinel validate exactly like this — explicit client, no init.
"$PYTHON_BIN" - "$INTERNAL_KEY_1" "$RELAY_KEY_1" "$WORKSPACE_ID" "$OWNER_MEMBER_ID" "$BRIDGE_KEY_1" "$RESCUE_KEY" <<'PY'
import asyncio, os, sys
os.environ["FIREKEEP_WORKSPACE_ID"] = sys.argv[3]
os.environ["FIREKEEP_OWNER_MEMBER_ID"] = sys.argv[4]
import redis.asyncio as aioredis
from auth import middleware

async def main():
    r = aioredis.from_url("redis://localhost:16379/7", decode_responses=True)
    validate = middleware.validate_key
    async def _validate(key):
        return await validate(key, redis_client=r)
    middleware.validate_key = _validate
    ident = await middleware.validate_key(sys.argv[1])
    assert ident is not None, "validate_key rejected the bootstrapped internal key"
    assert ident["workspace_id"] == sys.argv[3], ident
    assert ident["member_id"] == sys.argv[4], ident
    assert "agent_id" not in ident, ident
    assert set(ident["scopes"]) == {
        "memory:write", "session:read", "eval:read", "eval:write",
        "session:read:workspace", "relay:write:service",
    }, ident
    assert ident["authenticated"] is True

    # Relay's outbound key. It exists for exactly one call — Bridge's
    # POST /sessions/{agent_id}/context, gated by
    # require_scope_asgi(request, "session:write") at bridge/app/mcp_server.py:561.
    # Assert the EXACT set, not a superset: this is the least-privilege
    # contract, and "*" here would hand Relay vault reads and key minting.
    relay = await middleware.validate_key(sys.argv[2])
    assert relay is not None, "validate_key rejected the bootstrapped relay key"
    assert relay["workspace_id"] == sys.argv[3], relay
    assert relay["member_id"] == sys.argv[4], relay
    assert "agent_id" not in relay, relay
    assert set(relay["scopes"]) == {"session:write"}, relay
    assert "admin" not in relay["scopes"] and "*" not in relay["scopes"], relay

    # Bridge's dedicated key (Task 5): the only credential in the fleet
    # carrying eval:grade, a SERVICE_ONLY_SCOPES member no admin-minted key
    # can ever hold.
    bridge = await middleware.validate_key(sys.argv[5])
    assert bridge is not None, "validate_key rejected the bootstrapped bridge key"
    assert bridge["workspace_id"] == sys.argv[3], bridge
    assert bridge["member_id"] == sys.argv[4], bridge
    assert "agent_id" not in bridge, bridge
    assert set(bridge["scopes"]) == {
        "memory:read", "memory:write", "session:read", "eval:read", "eval:write", "eval:grade",
        "memory:write:delegated",
    }, bridge
    assert "admin" not in bridge["scopes"] and "*" not in bridge["scopes"], bridge

    assert await middleware.validate_key("nxs_" + "0" * 48) is None, "bogus key accepted"

    rescue = await middleware.validate_key(sys.argv[6])
    assert rescue is not None, "the attributed rescue key does not authenticate"
    assert rescue["member_id"] == sys.argv[4], rescue
    assert rescue["workspace_id"] == sys.argv[3], rescue
    assert ident["credential_id"] != __import__("hashlib").sha256(sys.argv[1].encode()).hexdigest()[:16]
    print(f"validate_key OK: member_id={ident['member_id']} scopes={sorted(ident['scopes'])}")
    print(f"validate_key OK: member_id={relay['member_id']} scopes={sorted(relay['scopes'])}")
    print(f"validate_key OK: member_id={bridge['member_id']} scopes={sorted(bridge['scopes'])}")
    await r.aclose()

asyncio.run(main())
PY

rm -f "$ENV_FILE"
echo "PASS: bootstrap-keys idempotency + layout"
