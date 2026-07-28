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

from auth.middleware import SCOPES
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

# The local-mint path must be reachable WITHOUT an admin key. The admin key is
# printed once by bootstrap-keys.sh and never stored, so on any server that has
# been running a while it is gone - and with it, teammate onboarding. Assert the
# code path exists; the live mint is covered by install-smoke's environment.
grep -q "mint_local" deploy/firekeep-admin || { echo "FAIL: local mint path missing"; exit 1; }
grep -q "BOOTSTRAP_REDIS_CMD" deploy/firekeep-admin || { echo "FAIL: redis override missing"; exit 1; }

echo "PASS: firekeep-admin dry-run + scope sync + fail-fast + local-mint path"
