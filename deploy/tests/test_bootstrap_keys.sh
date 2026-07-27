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
echo "$OUT1" | grep -q 'ADMIN API KEY'                  || { echo "FAIL: admin key not printed";    echo "$OUT1"; exit 1; }
echo "$OUT1" | grep -q '4 key(s) minted'                || { echo "FAIL: expected 4 mints";         echo "$OUT1"; exit 1; }
grep -qE '^FIREKEEP_INTERNAL_KEY=nxs_[0-9a-f]{48}$'  "$ENV_FILE" || { echo "FAIL: .env internal key malformed";  exit 1; }
grep -qE '^DASHBOARD_API_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env dashboard key malformed"; exit 1; }
grep -qE '^RELAY_INTERNAL_API_KEY=nxs_[0-9a-f]{48}$' "$ENV_FILE" || { echo "FAIL: .env relay key malformed"; exit 1; }
INTERNAL_KEY_1="$(grep '^FIREKEEP_INTERNAL_KEY=' "$ENV_FILE" | cut -d= -f2-)"
RELAY_KEY_1="$(grep '^RELAY_INTERNAL_API_KEY=' "$ENV_FILE" | cut -d= -f2-)"

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
for v in FIREKEEP_INTERNAL_KEY DASHBOARD_API_KEY RELAY_INTERNAL_API_KEY; do
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

# --- Layout check: the REAL validator accepts the bootstrapped key -----------
"$PYTHON_BIN" - "$INTERNAL_KEY_1" "$RELAY_KEY_1" <<'PY'
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

    # Relay's outbound key. It exists for exactly one call — Bridge's
    # POST /sessions/{agent_id}/context, gated by
    # require_scope_asgi(request, "session:write") at bridge/app/mcp_server.py:561.
    # Assert the EXACT set, not a superset: this is the least-privilege
    # contract, and "*" here would hand Relay vault reads and key minting.
    relay = await middleware.validate_key(sys.argv[2])
    assert relay is not None, "validate_key rejected the bootstrapped relay key"
    assert relay["agent_id"] == "firekeep-relay", relay
    assert set(relay["scopes"]) == {"session:write"}, relay
    assert "admin" not in relay["scopes"] and "*" not in relay["scopes"], relay

    assert await middleware.validate_key("nxs_" + "0" * 48) is None, "bogus key accepted"
    print(f"validate_key OK: agent_id={ident['agent_id']} scopes={sorted(ident['scopes'])}")
    print(f"validate_key OK: agent_id={relay['agent_id']} scopes={sorted(relay['scopes'])}")
    await r.aclose()

asyncio.run(main())
PY

rm -f "$ENV_FILE"
echo "PASS: bootstrap-keys idempotency + layout"
