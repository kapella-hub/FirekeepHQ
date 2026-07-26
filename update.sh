#!/usr/bin/env bash
set -euo pipefail

echo "============================================"
echo "  Firekeep Update"
echo "============================================"
echo ""

if [ ! -f .env ]; then
    echo "ERROR: .env file not found. Run install.sh first."
    exit 1
fi

# --- Office-front safety guard ---
# Bare `docker compose` only loads docker-compose.yml unless COMPOSE_FILE
# is set (shell env or .env — docker compose v2 reads COMPOSE_FILE from
# .env automatically). On an office deploy, a bare `update.sh` would
# therefore silently drop the docker-compose.office.yml override: Caddy
# gets removed as an orphan and all 7 app services regenerate with the
# base file's 0.0.0.0 bindings, regressing the TLS front + 127.0.0.1
# isolation (auth still enforces, so this is not a bypass by itself — but
# X-API-Key then travels cleartext to publicly-bound ports).
#
# The guard keys off FIREKEEP_OFFICE_MODE=true (written to .env only by
# `install.sh --office`), NOT off docker-compose.office.yml's mere
# presence: that file is committed and always exists in this repo, so a
# default (non-office) install would otherwise trip this warning on every
# stranger's first `update.sh` (see install.sh's own --office fix). It
# also can't key off the COMPOSE_FILE line itself, since detecting that
# line going missing is the whole point of this guard.
#
# Known limitation: a deployment installed with an older install.sh (or
# any --office install predating this marker) has COMPOSE_FILE pinned but
# never received FIREKEEP_OFFICE_MODE — this guard is silent for it even if
# COMPOSE_FILE is later deleted. See docs/DEPLOYMENT-OFFICE.md section 1
# for how to add the marker to such a deployment by hand. Default
# (non-office) installs never write this marker, so the guard stays
# silent there by design — do not turn this into a hard failure.
if grep -q '^FIREKEEP_OFFICE_MODE=true' .env 2>/dev/null \
    && [ -z "${COMPOSE_FILE:-}" ] \
    && ! grep -q '^COMPOSE_FILE=' .env 2>/dev/null; then
    echo ""
    echo "############################################################"
    echo "# WARNING: this deployment is marked as an office install"
    echo "# (FIREKEEP_OFFICE_MODE=true in .env) but COMPOSE_FILE is not set"
    echo "# (checked shell env and .env). This update will use ONLY"
    echo "# docker-compose.yml — Caddy will be removed as an orphan and"
    echo "# all app ports will be rebound to 0.0.0.0."
    echo "#"
    echo "# Fix: add this line to .env, then re-run:"
    echo "#   COMPOSE_FILE=docker-compose.yml:docker-compose.office.yml"
    echo "#"
    echo "# See docs/DEPLOYMENT-OFFICE.md section 1."
    echo "############################################################"
    echo ""
fi

# --- Pull latest ---
echo "Pulling latest changes..."
git pull

# --- Security-default migration guard (runs AFTER the pull) ---------------
# Two defaults changed in docker-compose.yml: AUTH_ENABLED now defaults to
# true and the published ports now bind ${BIND_ADDR:-127.0.0.1} instead of
# 0.0.0.0. Both are read from .env, and an .env written by an older installer
# predates both — so this update silently changes the security posture of a
# running deployment in two opposite directions. Neither may pass unremarked.
#
# Deliberately placed after `git pull` (the new compose file has to be on disk
# for these defaults to mean anything) and before the rebuild/restart below,
# so anything written here is in place when compose next reads .env.
source "$(dirname "$0")/deploy/lib.sh"

echo ""
if auth_enforced .env; then
    if [ -z "$(env_value .env AUTH_ENABLED)" ]; then
        # No AUTH_ENABLED line at all: compose's ${AUTH_ENABLED:-true} turns
        # enforcement ON at this restart. That is the fix landing, and it is
        # the right outcome — but it is not what this deployment was doing a
        # minute ago, and unkeyed callers are about to start getting 401.
        echo "############################################################"
        echo "# AUTH IS NOW ENFORCED on this deployment."
        echo "#"
        echo "# Your .env has no AUTH_ENABLED line, so it picks up the new"
        echo "# default (true). Until now every API here accepted unkeyed"
        echo "# calls, including GET /vault/secrets and POST /auth/keys."
        echo "#"
        echo "# What keeps working with no action from you:"
        echo "#   - the dashboard (its nginx injects DASHBOARD_API_KEY)"
        echo "#   - all internal service-to-service calls (keys are minted"
        echo "#     into .env by deploy/bootstrap-keys.sh, run below)"
        echo "#"
        echo "# What breaks until you give it a key:"
        echo "#   - any script, agent or client calling these APIs directly."
        echo "#     Mint one per teammate:"
        echo "#       deploy/firekeep-admin keys create --agent <name>"
        echo "#"
        echo "# To stay unauthenticated (NOT recommended — this is the"
        echo "# configuration that leaked 12 secrets), add to .env:"
        echo "#   AUTH_ENABLED=false"
        echo "############################################################"
    else
        echo "[OK] Auth: ENFORCED (AUTH_ENABLED=$(env_value .env AUTH_ENABLED))"
    fi
else
    # Explicitly false in .env. compose's :- default cannot override an
    # explicit value, so this deployment stays wide open and the remedy has
    # NOT reached it. This is the loudest thing update.sh prints.
    echo "############################################################"
    echo "# WARNING: AUTH IS DISABLED on this deployment."
    echo "#"
    echo "# .env sets AUTH_ENABLED=false explicitly, which overrides the"
    echo "# new secure default. Every API on this host stays open to"
    echo "# anyone who can reach the port — with no key:"
    echo "#   GET  /vault/secrets   — reads your stored secrets"
    echo "#   POST /auth/keys       — mints new API keys"
    echo "#"
    echo "# Fix (one line, then re-run this script):"
    echo "#   AUTH_ENABLED=true"
    echo "############################################################"
fi

echo ""
if [ -z "$(env_value .env BIND_ADDR)" ]; then
    # Pre-BIND_ADDR .env. Leaving the line absent would let compose's
    # ${BIND_ADDR:-127.0.0.1} rebind all six published ports to loopback,
    # cutting off every remote dashboard user, agent and client mid-update —
    # a routine `git pull` script severing access, with "connection refused"
    # as the only symptom and nothing in the output explaining it.
    #
    # ...unless this is an office deployment, where loopback is the INTENDED
    # posture: docker-compose.office.yml rebinds every app port to 127.0.0.1
    # and clients come in through Caddy on :443. Writing 0.0.0.0 there would
    # preserve reachability nobody uses, and would do so by betting on how
    # the base and override `ports:` entries merge. Keying off the same
    # FIREKEEP_OFFICE_MODE marker the guard at the top of this script uses is
    # correct either way, so the bet is never placed.
    if grep -q '^FIREKEEP_OFFICE_MODE=true' .env 2>/dev/null; then
        echo "[OK] Office deploy: leaving BIND_ADDR unset (ports stay on"
        echo "     127.0.0.1 behind Caddy — see docs/DEPLOYMENT-OFFICE.md)"
    else
        # Preserve the reachability this deployment already had, explicitly,
        # and say exactly what was written and how to lock it down. The
        # judgement: for an UPGRADE path, silent breakage is worse than
        # preserving the status quo loudly — and the auth block above means
        # these ports are no longer the unauthenticated surface they were. A
        # FRESH install gets the secure default instead: .env.example ships
        # BIND_ADDR=127.0.0.1, so the line is present and this branch never
        # fires for it.
        echo "BIND_ADDR=0.0.0.0" >> .env
        echo "############################################################"
        echo "# NOTICE: BIND_ADDR=0.0.0.0 was added to your .env."
        echo "#"
        echo "# Published ports now honour \${BIND_ADDR:-127.0.0.1}. Your .env"
        echo "# predates that setting, so without this line all six ports"
        echo "# (8040-8100) would have dropped to loopback on this restart and"
        echo "# cut off every remote client. Your current reachability is"
        echo "# preserved unchanged."
        echo "#"
        echo "# Recommended once your clients are keyed: bind to loopback and"
        echo "# reach the host over an SSH tunnel. In .env set"
        echo "#   BIND_ADDR=127.0.0.1"
        echo "# then re-run this script."
        echo "############################################################"
    fi
else
    BIND_ADDR_EFFECTIVE="$(effective_bind_addr .env)"
    if bind_addr_is_public "$BIND_ADDR_EFFECTIVE"; then
        echo "[OK] Ports publish on ${BIND_ADDR_EFFECTIVE} (reachable off-box)"
    else
        echo "[OK] Ports bind ${BIND_ADDR_EFFECTIVE} (loopback only)"
    fi
fi

# --- Rebuild ---
echo ""
echo "Rebuilding changed services..."
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# --match excludes this repo's client-vX.Y.Z release tags (client/ has its own
# release cadence -- see CLAUDE.md) so a server build never reports a client
# version; falls back to the short SHA (--always) until server vX.Y.Z tags exist.
export APP_VERSION="$(git describe --tags --match 'v[0-9]*' --always --dirty 2>/dev/null || echo 0.6.0)"
echo "Build provenance: GIT_SHA=${GIT_SHA} BUILD_TIME=${BUILD_TIME} APP_VERSION=${APP_VERSION}"
docker compose build

# --- Bootstrap auth keys (idempotent) ---
# Must run BEFORE containers are recreated: an existing deployment flipping
# AUTH_ENABLED=true otherwise restarts into a 401 dead-end where even
# POST /auth/keys is unreachable (spec §6).
echo ""
echo "Bootstrapping auth keys..."
docker compose up -d redis
bash deploy/bootstrap-keys.sh

# --- Restart ---
echo ""
echo "Restarting services..."
docker compose up -d --remove-orphans

# --- Refresh the dashboard (single-file bind mounts don't track inode replacement) ---
# git pull rewrites dashboard/index.html (and the nginx template / .htpasswd) as NEW inodes,
# but Docker single-FILE bind mounts stay pinned to the inode that existed at container
# start — so `up -d` above does NOT pick up dashboard-only changes, and nginx keeps serving
# the pre-pull file. Force-recreate the container so the mounts re-resolve to the current
# files. (Directory mounts wouldn't have this problem, but .htpasswd + the nginx template
# must not land in the public web root, so the dashboard mounts individual files.)
echo ""
echo "Refreshing dashboard (bind-mounted static files)..."
docker compose up -d --force-recreate dashboard

# --- Prune old images ---
echo ""
echo "Pruning old images..."
docker image prune -f

# --- Health checks ---
echo ""
echo "Verifying health..."

# name:port:probe-path — kept identical in shape to install.sh's array so the
# two health checks cannot drift apart again. tests/test_install_health_probe.py
# parses both and fails if either stops agreeing with the compose port map.
services=(
    "Cortex API:8100:/health"
    "Cortex MCP:8080:/mcp"
    "FirekeepBridge:8070:/health"
    "FirekeepSentinel:8060:/health"
    "FirekeepRelay:8050:/health"
    # FirekeepSymdex deliberately absent: the server-side symdex container was
    # removed (client-side stdio only) — checking :8090 fails every update.
    "Dashboard:8040:/"
)

FAILED=0

for svc in "${services[@]}"; do
    # Split by position — see the note in install.sh: `${svc##*:}` returns the
    # probe path on a three-field entry, not the port.
    name="${svc%%:*}"
    rest="${svc#*:}"
    port="${rest%%:*}"
    probe="${rest#*:}"
    printf "  %-16s " "$name"
    for i in $(seq 1 30); do
        # This used to fall back to `bash -c "</dev/tcp/localhost/$port"`,
        # which any listening socket satisfies. A dashboard nginx that was up
        # but 500ing on every request — the exact failure a missing or
        # corrupted .htpasswd produces — reported [OK] and the update was
        # declared successful. Read the status code instead, and accept only
        # what install.sh accepts: 2xx, 401 (nginx enforcing basic auth) and
        # 405 (cortex-mcp's /mcp route exists but does not allow GET).
        code="$(curl -s -o /dev/null --max-time 2 -w '%{http_code}' "http://localhost:${port}${probe}" 2>/dev/null)" || code="000"
        case "$code" in
            2??|401|405)
                echo "[OK]"
                break
                ;;
        esac
        if [ "$i" -eq 30 ]; then
            echo "[TIMEOUT] (last HTTP ${code} from http://localhost:${port}${probe})"
            FAILED=1
        fi
        sleep 2
    done
done

if [ "$FAILED" -eq 1 ]; then
    echo ""
    echo "WARNING: Some services failed to start. Check: docker compose logs"
    exit 1
fi

echo ""
echo "Update complete."
echo ""
docker compose ps
