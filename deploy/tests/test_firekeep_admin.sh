#!/usr/bin/env bash
# Syntax + dry-run POST-body test for deploy/firekeep-admin.
# Asserts NON_ADMIN_SCOPES == auth.middleware.SCOPES - {"admin"} (live sync
# check against the real scope table). Run from the repo root:
#   PYTHONPATH=. bash deploy/tests/test_firekeep_admin.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" > /dev/null || PYTHON_BIN=python

bash -n deploy/firekeep-admin || { echo "FAIL: syntax error"; exit 1; }

OUT="$(FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin keys create --agent alice)"
echo "$OUT" | "$PYTHON_BIN" -c '
import json, sys
lines = sys.stdin.read().splitlines()
assert lines[0] == "POST http://localhost:8100/auth/keys", lines[0]
body = json.loads(lines[1])
assert body["agent_id"] == "alice", body

from auth.keys import SCOPES
expected = SCOPES - {"admin"}
got = set(body["scopes"])
assert "admin" not in got, "teammate key must never carry admin"
assert "*" not in got, "teammate key must never be wildcard"
assert "twin:read" not in got, "twin:read is retired"
assert "eval:write" in got, "eval:write required for POST /agent/action/after"
assert got == expected, f"drifted from SCOPES-{{admin}}: extra={got-expected} missing={expected-got}"
print(f"DRY-RUN OK: {len(got)} non-admin scopes match auth.middleware.SCOPES")
'

# --expires-days is threaded into the body
OUT2="$(FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin keys create --agent bob --expires-days 90)"
echo "$OUT2" | grep -q '"expires_days": 90' || { echo "FAIL: expires_days missing"; echo "$OUT2"; exit 1; }

# missing --agent -> usage error
if FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin keys create 2>/dev/null; then
    echo "FAIL: missing --agent should exit nonzero"
    exit 1
fi

# No admin key, no TTY, no reachable Redis -> must FAIL FAST with a diagnosis.
# Regression guard: this used to call `read` unconditionally, so under BatchMode or
# CI it blocked on a prompt nobody could answer until the caller timed out, with no
# indication of why. Onboarding a teammate died here.
START="$(date +%s)"
OUT3="$(BOOTSTRAP_REDIS_CMD=false bash deploy/firekeep-admin keys create --agent carol </dev/null 2>&1 || true)"
ELAPSED=$(( $(date +%s) - START ))
echo "$OUT3" | grep -q "no FIREKEEP_ADMIN_KEY" || { echo "FAIL: no diagnosis on the unusable path"; echo "$OUT3"; exit 1; }
[ "$ELAPSED" -lt 10 ] || { echo "FAIL: took ${ELAPSED}s - it is prompting again"; exit 1; }
if BOOTSTRAP_REDIS_CMD=false bash deploy/firekeep-admin keys create --agent carol </dev/null >/dev/null 2>&1; then
    echo "FAIL: unusable path should exit nonzero"; exit 1
fi

# All three subcommands must reach their own dispatch branch.
REVOKE_OUT="$(FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin keys revoke 0123456789abcdef)"
echo "$REVOKE_OUT" | grep -q "REVOKE 0123456789abcdef" || {
    echo "FAIL: keys revoke dispatch missing"; exit 1;
}
TMP_ENV="$(mktemp)"
printf 'AUTH_ENABLED=true\nBIND_ADDR=127.0.0.1\nVPS_IP=203.0.113.9\n' > "$TMP_ENV"
INVITE_OUT="$(ENV_FILE="$TMP_ENV" FIREKEEP_ADMIN_DRY_RUN=1 USER=root bash deploy/firekeep-admin invite --agent alice --json)"
rm -f "$TMP_ENV"
echo "$INVITE_OUT" | grep -q "app.enroll.mint" || { echo "FAIL: invite does not use Python schema"; exit 1; }
echo "$INVITE_OUT" | grep -q -- "--transport tunnel" || { echo "FAIL: loopback invite not tunnel"; exit 1; }
echo "$INVITE_OUT" | grep -q -- "--ssh-target root@203.0.113.9" || { echo "FAIL: tunnel target wrong"; exit 1; }

# An explicit tunnel target must work without VPS_IP, and a host CA file must
# be carried into the cortex-api container rather than passed as an unmounted path.
TMP_ENV="$(mktemp)"; TMP_CA="$(mktemp)"
printf 'AUTH_ENABLED=true\nBIND_ADDR=127.0.0.1\n' > "$TMP_ENV"
printf '%s\n' '-----BEGIN CERTIFICATE-----' 'test' '-----END CERTIFICATE-----' > "$TMP_CA"
EXPLICIT_OUT="$(ENV_FILE="$TMP_ENV" FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin invite --ssh-target alice@example --json)"
echo "$EXPLICIT_OUT" | grep -q -- "--ssh-target alice@example" || { echo "FAIL: explicit tunnel target rejected"; exit 1; }
printf 'AUTH_ENABLED=true\nBIND_ADDR=127.0.0.1\nVPS_IP=203.0.113.9\n' > "$TMP_ENV"
CA_OUT="$(ENV_FILE="$TMP_ENV" FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin invite --host firekeep.example --ca-file "$TMP_CA" --json)"
rm -f "$TMP_ENV" "$TMP_CA"
echo "$CA_OUT" | grep -q -- "--ca-pem-b64" || { echo "FAIL: CA bytes not carried into container"; exit 1; }
echo "$CA_OUT" | grep -q -- "--transport tls" || { echo "FAIL: explicit CA did not select TLS"; exit 1; }
if echo "$CA_OUT" | grep -q -- "--ca-file"; then
    echo "FAIL: host CA path leaked into container command"; exit 1
fi

# The local create path must remain reachable without the printed-once admin key.
grep -q "nxs_" deploy/firekeep-admin || { echo "FAIL: local nxs_ mint path missing"; exit 1; }
grep -q "BOOTSTRAP_REDIS_CMD" deploy/firekeep-admin || { echo "FAIL: redis override missing"; exit 1; }

LIC_FILE="$(mktemp)"
printf 'fk_lic_v1.eyJwbGFuIjoidGVhbSJ9.c2lnbmF0dXJl\n' > "$LIC_FILE"
LIC_OUT="$(FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin licence apply "$LIC_FILE")"
rm -f "$LIC_FILE"
echo "$LIC_OUT" | grep -q "POST http://localhost:8100/licence" || {
    echo "FAIL: licence apply dispatch missing"; exit 1;
}
STATUS_OUT="$(FIREKEEP_ADMIN_DRY_RUN=1 bash deploy/firekeep-admin licence status)"
echo "$STATUS_OUT" | grep -q "GET http://localhost:8100/licence" || {
    echo "FAIL: licence status dispatch missing"; exit 1;
}

echo "PASS: firekeep-admin create/revoke/invite/licence dispatch + scope sync + fail-fast"
