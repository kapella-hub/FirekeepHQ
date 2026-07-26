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
docker run -d --name "$CONTAINER" -p 127.0.0.1:16379:6379 redis:7-alpine > /dev/null
trap 'docker rm -f "$CONTAINER" > /dev/null 2>&1' EXIT
until docker exec "$CONTAINER" redis-cli ping 2>/dev/null | grep -q PONG; do sleep 0.5; done

export ENV_FILE="$(mktemp)"
export BOOTSTRAP_REDIS_CMD="docker exec $CONTAINER redis-cli -n 7"

# --- Run 1: mints internal + dashboard + admin -------------------------------
OUT1="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT1" | grep -q '\[MINTED\] FIREKEEP_INTERNAL_KEY'  || { echo "FAIL: internal key not minted";  echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '\[MINTED\] DASHBOARD_API_KEY' || { echo "FAIL: dashboard key not minted"; echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q 'ADMIN API KEY'                  || { echo "FAIL: admin key not printed";    echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '3 key(s) minted'                || { echo "FAIL: expected 3 mints";         echo "$OUT1"; exit 1; }
grep -qE '^FIREKEEP_INTERNAL_KEY=nxs_[0-9a-f]{48}$'  "$ENV_FILE" || { echo "FAIL: .env internal key malformed";  exit 1; }
grep -qE '^DASHBOARD_API_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env dashboard key malformed"; exit 1; }
INTERNAL_KEY_1="$(grep '^FIREKEEP_INTERNAL_KEY=' "$ENV_FILE" | cut -d= -f2-)"
DBSIZE1="$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)"

# --- Run 2: mints NOTHING, rotates NOTHING -----------------------------------
OUT2="$(bash deploy/bootstrap-keys.sh)"
echo "$OUT2" | grep -q '0 key(s) minted' || { echo "FAIL: second run minted keys"; echo "$OUT2"; exit 1; }
echo "$OUT2" | grep -q 'ADMIN API KEY' && { echo "FAIL: admin key re-printed on second run"; exit 1; }
INTERNAL_KEY_2="$(grep '^FIREKEEP_INTERNAL_KEY=' "$ENV_FILE" | cut -d= -f2-)"
[ "$INTERNAL_KEY_1" = "$INTERNAL_KEY_2" ] || { echo "FAIL: internal key rotated"; exit 1; }
DBSIZE2="$(docker exec "$CONTAINER" redis-cli -n 7 DBSIZE)"
[ "$DBSIZE1" = "$DBSIZE2" ] || { echo "FAIL: DBSIZE changed $DBSIZE1 -> $DBSIZE2"; exit 1; }

# --- Layout check: the REAL validator accepts the bootstrapped key -----------
"$PYTHON_BIN" - "$INTERNAL_KEY_1" <<'PY'
import asyncio, sys
import redis.asyncio as aioredis
from auth import middleware

async def main():
    r = aioredis.from_url("redis://localhost:16379/7", decode_responses=True)
    await middleware.init_auth(redis_client=r, enabled=True)
    ident = await middleware.validate_key(sys.argv[1])
    assert ident is not None, "validate_key rejected the bootstrapped internal key"
    assert ident["agent_id"] == "firekeep-internal", ident
    assert set(ident["scopes"]) == {"memory:write", "session:read", "eval:read", "eval:write"}, ident
    assert ident["authenticated"] is True
    assert await middleware.validate_key("nxs_" + "0" * 48) is None, "bogus key accepted"
    print(f"validate_key OK: agent_id={ident['agent_id']} scopes={sorted(ident['scopes'])}")
    await r.aclose()

asyncio.run(main())
PY

rm -f "$ENV_FILE"
echo "PASS: bootstrap-keys idempotency + layout"
