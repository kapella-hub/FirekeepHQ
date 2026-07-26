#!/usr/bin/env bash
# Capture everything a vendor needs to diagnose a customer's install, in one
# command, with no secret values.
#
# Usage: bash deploy/support-bundle.sh [output-dir]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib.sh"

OUT_DIR="${1:-$REPO_ROOT}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORK="$(mktemp -d)"
BUNDLE="$WORK/firekeep-support-$STAMP"
mkdir -p "$BUNDLE"
trap 'rm -rf "$WORK"' EXIT

cd "$REPO_ROOT"

echo "Collecting support bundle..."

# --- build identity, per service -------------------------------------------
{
    for port in 8100 8070 8060 8050; do
        echo "--- :$port/version"
        curl -fsS --max-time 5 "http://127.0.0.1:$port/version" 2>&1 || echo "(unreachable)"
        echo
    done
} > "$BUNDLE/versions.txt"

# --- health ----------------------------------------------------------------
{
    for port in 8100 8070 8060 8050; do
        echo "--- :$port/health"
        curl -fsS --max-time 10 "http://127.0.0.1:$port/health" 2>&1 || echo "(unreachable)"
        echo
    done
} > "$BUNDLE/health.txt"

# --- container state -------------------------------------------------------
# --no-interpolate matters: plain `docker compose config` performs variable
# substitution and would print VAULT_KEY, NEO4J_PASSWORD, FIREKEEP_INTERNAL_KEY
# and the wildcard dashboard key in cleartext -- the exact leak
# redact_env_file exists to prevent, arriving through a different door.
docker compose ps            > "$BUNDLE/compose-ps.txt"      2>&1 || true
docker compose config --no-interpolate > "$BUNDLE/compose-config.txt" 2>&1 || true
docker compose logs --tail 200 > "$BUNDLE/compose-logs.txt"  2>&1 || true
docker version               > "$BUNDLE/docker-version.txt"  2>&1 || true

# --- host ------------------------------------------------------------------
{
    echo "--- uname"; uname -a
    echo; echo "--- memory"; free -h 2>/dev/null || echo "(free unavailable)"
    echo; echo "--- disk"; df -h /
    echo; echo "--- cpus"; nproc 2>/dev/null || echo "(nproc unavailable)"
} > "$BUNDLE/host.txt" 2>&1

# --- configuration, REDACTED ----------------------------------------------
# Never copy .env directly: it holds VAULT_KEY, NEO4J_PASSWORD, the internal
# key and a wildcard-scoped dashboard key.
redact_env_file "$REPO_ROOT/.env" > "$BUNDLE/env.redacted.txt"

# --- git provenance --------------------------------------------------------
{
    git rev-parse HEAD 2>/dev/null || echo "(not a git checkout)"
    git status --short 2>/dev/null || true
} > "$BUNDLE/git.txt"

tar czf "$OUT_DIR/firekeep-support-$STAMP.tar.gz" -C "$WORK" "firekeep-support-$STAMP"

echo "[OK] Support bundle: $OUT_DIR/firekeep-support-$STAMP.tar.gz"
echo "     Contains no secret values — .env is included with values redacted."
echo "     Review it before sending if your deployment holds sensitive paths."
