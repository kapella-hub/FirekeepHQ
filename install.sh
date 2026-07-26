#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/deploy/lib.sh"

echo "============================================"
echo "  Firekeep Installer"
echo "============================================"
echo ""

# --- Check Docker ---
if ! command -v docker &>/dev/null; then
    echo "Docker not found. Installing..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "Docker installed. You may need to log out and back in for group changes."
fi

if ! docker compose version &>/dev/null; then
    echo "ERROR: docker compose not available. Install Docker Compose v2."
    exit 1
fi

echo "[OK] Docker and Docker Compose available"

# --- Prompt for config ---
if [ ! -f .env ]; then
    echo ""

    read -rp "VPS IP address: " VPS_IP
    read -rsp "Neo4j password: " NEO4J_PASSWORD
    echo

    # configure_env (deploy/lib.sh) validates both answers and writes .env
    # atomically -- either it's fully configured or it doesn't exist at all,
    # so a fumbled prompt (empty answer, a `|`/`&`/`\` that would otherwise
    # corrupt the substitutions below) can always be fixed by just
    # re-running the installer, instead of leaving a half-written .env that
    # silently short-circuits every future run of this block.
    if ! configure_env .env .env.example "$VPS_IP" "$NEO4J_PASSWORD"; then
        exit 1
    fi
    # Belt-and-suspenders: configure_env already writes .env at mode 600 (it
    # moves a mktemp'd 600 file into place), but the sed/append operations
    # below rewrite it further before anyone else can read it -- reassert
    # 600 immediately so no implementation detail of those operations can
    # ever leave the freshly-written secrets world-readable in between.
    chmod 600 .env

    # Generate vault encryption key
    if python3 -c "from cryptography.fernet import Fernet" 2>/dev/null; then
        VAULT_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
        sed -i "s|^VAULT_KEY=.*|VAULT_KEY=${VAULT_KEY}|" .env
        echo "[OK] Vault encryption key generated"
    else
        echo "[SKIP] cryptography not installed — set VAULT_KEY manually after deploy"
    fi

    # Office deploy: pin COMPOSE_FILE on the fresh .env so a bare
    # `docker compose` (as run by this script and by update.sh) keeps
    # loading the Caddy TLS front + 127.0.0.1 port-rebind override
    # automatically — see docs/DEPLOYMENT-OFFICE.md section 1. Only runs
    # on a freshly-copied .env (this branch); an existing .env is never
    # touched here, matching the "skip config" branch below.
    #
    # FIREKEEP_OFFICE_MODE=true is a second, independent marker recording
    # that --office was explicitly requested. update.sh's office-front
    # safety guard keys off this marker rather than off
    # docker-compose.office.yml's mere presence (that file is committed
    # and always exists, which was the exact bug this task fixed for
    # install.sh) or off the COMPOSE_FILE line itself (the guard exists
    # to detect that line going missing, so it can't also be the signal
    # that this is an office deployment).
    if office_mode_requested "$@"; then
        if [ ! -f docker-compose.office.yml ]; then
            echo "ERROR: --office given but docker-compose.office.yml is missing" >&2
            exit 1
        fi
        echo "COMPOSE_FILE=docker-compose.yml:docker-compose.office.yml" >> .env
        echo "FIREKEEP_OFFICE_MODE=true" >> .env
        echo "[OK] COMPOSE_FILE pinned for office deploy (--office)"
    fi

    # --- Deliberate auth opt-out (--insecure-no-auth) ---
    # .env is copied from .env.example, which ships AUTH_ENABLED=true. This is
    # the ONLY way an install ends up unauthenticated, and it has to be asked
    # for by name. Rewriting the line (rather than appending a second one)
    # keeps a single source of truth in .env; the `grep || append` covers a
    # hand-edited .env.example that dropped the key entirely.
    if no_auth_requested "$@"; then
        if grep -qE '^[[:space:]]*AUTH_ENABLED=' .env; then
            sed -i "s|^[[:space:]]*AUTH_ENABLED=.*|AUTH_ENABLED=false|" .env
        else
            echo "AUTH_ENABLED=false" >> .env
        fi
        echo ""
        echo "############################################################" >&2
        echo "# AUTH DISABLED BY REQUEST (--insecure-no-auth)" >&2
        echo "#" >&2
        echo "# Every API on this host is now open to anyone who can reach" >&2
        echo "# the port, with NO key required. That includes:" >&2
        echo "#   GET  /vault/secrets   — reads your stored secrets" >&2
        echo "#   POST /auth/keys       — mints new API keys" >&2
        echo "#" >&2
        echo "# This is only defensible when the ports are unreachable from" >&2
        echo "# anywhere but this machine. Keep BIND_ADDR=127.0.0.1 in .env" >&2
        echo "# (the default) or firewall 8040-8100 completely." >&2
        echo "#" >&2
        echo "# To undo: set AUTH_ENABLED=true in .env and run: bash update.sh" >&2
        echo "############################################################" >&2
    fi

    echo ""
    echo "[OK] .env configured"
else
    echo "[OK] .env already exists, skipping configuration"
    if office_mode_requested "$@"; then
        echo ""
        echo "WARNING: --office was given but .env already exists, so the office" >&2
        echo "  config block above was skipped -- this run will NOT pin an office" >&2
        echo "  deployment. Add these two lines to .env by hand, then re-run:" >&2
        echo "    COMPOSE_FILE=docker-compose.yml:docker-compose.office.yml" >&2
        echo "    FIREKEEP_OFFICE_MODE=true" >&2
    fi
    # Same precedent as --office directly above: an existing .env is never
    # rewritten by a flag, because the flag cannot know what else the operator
    # has changed in it. Say plainly that the request had no effect rather
    # than letting them believe auth is off when it is on (or the reverse).
    if no_auth_requested "$@"; then
        echo ""
        echo "WARNING: --insecure-no-auth was given but .env already exists, so" >&2
        echo "  this run did NOT change the auth setting. Edit .env by hand:" >&2
        echo "    AUTH_ENABLED=false" >&2
        echo "  then re-run. (The summary at the end reports what is actually" >&2
        echo "  configured, not what was asked for.)" >&2
    fi
fi

# --- Build and start ---
echo ""
echo "Building and starting services..."
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# --match excludes this repo's client-vX.Y.Z release tags (client/ has its own
# release cadence -- see CLAUDE.md) so a server build never reports a client
# version; falls back to the short SHA (--always) until server vX.Y.Z tags exist.
export APP_VERSION="$(git describe --tags --match 'v[0-9]*' --always --dirty 2>/dev/null || echo 0.6.0)"
echo "Build provenance: GIT_SHA=${GIT_SHA} BUILD_TIME=${BUILD_TIME} APP_VERSION=${APP_VERSION}"

# --- Dashboard basic auth ---
# compose bind-mounts ./dashboard/.htpasswd; a missing source makes Docker
# create a DIRECTORY there and nginx then fails every request.
if [ ! -f dashboard/.htpasswd ]; then
    DASH_USER="admin"
    # NOTE: deliberately no trailing `| head -c 24` here (unlike the task
    # brief's literal text). base64 of 18 bytes is already exactly 24 chars
    # (18 is divisible by 3, no padding) and `tr -d` only ever removes
    # characters, so a truncating head can't do useful clamping here — it
    # can only race head's early pipe-close against tr's write() and, under
    # `set -euo pipefail`, turn an intermittent SIGPIPE (exit 141) in tr into
    # a silent install abort. Command substitution already strips the
    # trailing newline `base64` appends, so nothing else is lost by omitting
    # it.
    DASH_PASS="$(head -c 18 /dev/urandom | base64 | tr -d '/+=')"

    # Hash WITHOUT putting the password in argv — `ps` exposes argv to every
    # user on the host for the lifetime of the call.
    #
    # htpasswd's own DEFAULT algorithm is apr1-MD5 (weak), so the preferred
    # branch used to be exactly the weak one: any host with apache2-utils
    # installed (most Linux hosts) got apr1-MD5. Hence the explicit -5.
    #
    # Per the Apache httpd 2.4 htpasswd docs, -5 is "Use SHA-512 crypt()
    # based hashes for passwords. This is supported on most Unix platforms"
    # (and -2 is the SHA-256 equivalent). It is a documented mainline flag,
    # not a vendor extension — but it is still attempted rather than assumed,
    # because "most Unix platforms" is not all of them, and the fallback
    # costs nothing. A failure falls through to the openssl branch instead
    # of letting `set -e` abort the install.
    #
    # -B/bcrypt is deliberately NOT used: nginx's ngx_crypt delegates to the
    # system crypt(3), and glibc's crypt() does not support bcrypt ($2y$),
    # so a bcrypt hash would make nginx reject every login.
    if command -v htpasswd &>/dev/null \
        && printf '%s' "$DASH_PASS" | htpasswd -ic -5 dashboard/.htpasswd "$DASH_USER" 2>/dev/null; then
        # SHA-512 crypt ($6$) written directly by htpasswd. NOTE the flag numbering
        # differs between the two tools and it is easy to get backwards:
        #   htpasswd: -2 = SHA-256, -5 = SHA-512   (flag = bit-length shorthand)
        #   openssl:  -5 = $5$ SHA-256, -6 = $6$ SHA-512  (flag = the $N$ prefix)
        # So `htpasswd -5` and `openssl passwd -6` produce the SAME algorithm.
        :
    else
        rm -f dashboard/.htpasswd  # remove any partial file the failed attempt above may have left
        # -6 = SHA-512 crypt, -5 = SHA-256 crypt — both nginx-verifiable via
        # crypt(3). apr1 is a last resort, never silent: it only happens if
        # this openssl build has neither, and it says so loudly.
        DASH_HASH="$(printf '%s' "$DASH_PASS" | openssl passwd -6 -stdin 2>/dev/null)" \
            || DASH_HASH="$(printf '%s' "$DASH_PASS" | openssl passwd -5 -stdin 2>/dev/null)" \
            || { DASH_HASH="$(printf '%s' "$DASH_PASS" | openssl passwd -apr1 -stdin)"
                 echo "WARNING: neither htpasswd -5 nor openssl -6/-5 is available on this host —" >&2
                 echo "  writing a weak apr1-MD5 dashboard password hash. Regenerate it once a" >&2
                 echo "  newer openssl is available (see docs/DEPLOYMENT.md)." >&2
               }
        printf '%s:%s\n' "$DASH_USER" "$DASH_HASH" > dashboard/.htpasswd
    fi
    chmod 0644 dashboard/.htpasswd

    # The plaintext goes to a 0600 sidecar, NEVER to stdout: install.sh's
    # output is piped and captured verbatim by the install-smoke CI job, so
    # echoing the password would publish it into GitHub Actions logs on every
    # run. Written immediately (not only in the final summary): the
    # health-check block below can `exit 1` before the summary ever runs, and
    # a generated credential the operator is never shown is unrecoverable —
    # .htpasswd already exists on the next run, so the `else` branch below
    # won't regenerate or reprint it.
    ( umask 077; printf 'user=%s\npass=%s\n' "$DASH_USER" "$DASH_PASS" \
        > dashboard/.htpasswd.cred )
    echo "[OK] Dashboard credentials generated"
    DASHBOARD_CREDS="written to dashboard/.htpasswd.cred (mode 0600) — read once, then delete"
else
    echo "[OK] dashboard/.htpasswd already exists, leaving it alone"
    DASHBOARD_CREDS="(existing dashboard/.htpasswd — unchanged)"
fi

# .env holds NEO4J_PASSWORD, the Fernet VAULT_KEY (decrypts every vault
# secret), and — after bootstrap-keys.sh below — FIREKEEP_INTERNAL_KEY and the
# admin-scoped DASHBOARD_API_KEY. Unconditional (not just in the fresh-.env
# branch above): a manually-created .env (the docs' "Manual Installation"
# path, or one left 0644 by a pre-fix installer run) is covered here too,
# right before bootstrap-keys.sh writes live keys into it.
if [ -f .env ]; then
    chmod 600 .env
fi

# Bootstrap auth keys BEFORE app containers are created, so
# FIREKEEP_INTERNAL_KEY exists in .env when bridge starts (idempotent).
docker compose up -d redis

# The output is CAPTURED, not streamed, so the closing summary can re-surface
# the admin key. bootstrap-keys.sh prints that key exactly once and never
# writes it to disk — and it prints it HERE, before a container build and a
# model pull that can run 15 minutes and thousands of lines. On a fresh
# install it is off the top of the scrollback long before the operator reads
# the summary, and it is the only credential that can mint teammate keys or
# read the vault. Losing it means re-minting (see the summary).
#
# Captured in a shell variable, never a temp file: keeping "not written to
# disk" true is the point. Held only for this process's lifetime.
#
# `if cmd; then` rather than a bare call: command substitution under `set -e`
# would abort here and discard the output with it, so a bootstrap failure
# would print nothing about why. Capture, echo, then decide.
if BOOTSTRAP_OUT="$(bash deploy/bootstrap-keys.sh 2>&1)"; then
    printf '%s\n' "$BOOTSTRAP_OUT"
else
    printf '%s\n' "$BOOTSTRAP_OUT"
    echo "" >&2
    echo "ERROR: auth key bootstrap failed (see output above). Nothing was started." >&2
    exit 1
fi

# The admin key is the only plaintext key in that output — every other mint is
# reported by variable name only (deploy/bootstrap-keys.sh ensure_env_key), and
# deploy/tests/test_bootstrap_keys.sh asserts exactly one nxs_ token appears, so
# this grep cannot silently start matching the wrong key. Empty on an
# idempotent re-run, which the summary reports honestly rather than papering
# over.
ADMIN_KEY="$(printf '%s\n' "$BOOTSTRAP_OUT" | grep -oE 'nxs_[0-9a-f]{48}' | head -n1 || true)"

docker compose up -d --build

# --- Wait for the model pull ---
# cortex-api depends on `ollama: service_healthy`, NOT on ollama-pull
# completing (docker-compose.yml has no service_completed_successfully
# dependency wired to any app service) — so `up -d` above can return while
# ~3.3GB of models are still downloading. The health loop below only checks
# that cortex-api is answering HTTP, which it is; a stranger's first
# memory_learn would then take the embed-failure path (HTTP 200,
# status="partial", backfill-enqueued) — the write LOOKS successful and
# isn't actually recallable yet. ollama-pull's own command echoes "Models
# ready" as its last line on success; wait for that sentinel in its logs.
#
# The pipefail-safe way to check: NOT `docker compose logs ollama-pull |
# grep -q ...` — under `set -o pipefail`, grep matching and closing the
# pipe early can SIGPIPE the writer (exit 141), and pipefail would then
# make the whole pipeline's status failure even though the match succeeded.
# Capture the logs into a variable first and pattern-match on that instead.
echo ""
echo "Waiting for the model pull to finish — this can take several minutes"
echo "on first install (~3.3GB download)..."
MODEL_PULL_TIMEOUT_SECONDS=900
MODEL_PULL_WAITED=0
MODEL_PULL_READY=0
while [ "$MODEL_PULL_WAITED" -lt "$MODEL_PULL_TIMEOUT_SECONDS" ]; do
    pull_logs="$(docker compose logs ollama-pull 2>&1 || true)"
    case "$pull_logs" in
        *"Models ready"*)
            MODEL_PULL_READY=1
            break
            ;;
    esac
    sleep 10
    MODEL_PULL_WAITED=$((MODEL_PULL_WAITED + 10))
done

if [ "$MODEL_PULL_READY" -eq 1 ]; then
    echo "[OK] models pulled"
else
    # A slow link is not a broken install — warn clearly and continue rather
    # than failing the install outright.
    echo "WARNING: timed out after ${MODEL_PULL_TIMEOUT_SECONDS}s waiting for the model pull."
    echo "  This usually just means a slow connection, not a broken install."
    echo "  Check progress with: docker compose logs ollama-pull"
    echo "  Until it finishes, memory writes will report status=\"partial\" and"
    echo "  won't be recallable yet."
fi

# --- Health checks ---
echo ""
echo "Waiting for services to become healthy..."

# name:port:probe-path. cortex-mcp is a FastMCP server and serves NO /health --
# it only mounts /mcp. Probing /health there 404s forever, so this loop used to
# print "[TIMEOUT]" and "WARNING: Some services failed to start" on every
# successful install, which is how a warning gets trained out of people.
# A 405 from GET /mcp is proof of life: the route exists, the method doesn't.
services=(
    "Cortex API:8100:/health"
    "Cortex MCP:8080:/mcp"
    "FirekeepBridge:8070:/health"
    "FirekeepSentinel:8060:/health"
    "FirekeepRelay:8050:/health"
    "Dashboard:8040:/"
)

FAILED=0
for svc in "${services[@]}"; do
    # Three colon-separated fields: name:port:probe-path. Split them by
    # POSITION. `${svc##*:}` strips the LONGEST `*:` prefix, so on a
    # three-field entry it returns the PROBE PATH, not the port -- which built
    # the URL `http://localhost:/health/`, made curl fail with 000 for every
    # service, and exited this script 1 on every clean install. `%%:*` happens
    # to be correct for the name only because the name is field 1.
    name="${svc%%:*}"
    rest="${svc#*:}"            # "8100:/health"
    port="${rest%%:*}"          # "8100"
    probe="${rest#*:}"          # "/health"
    printf "  %-16s " "$name"
    for i in $(seq 1 30); do
        # Probe the path this service actually serves, and accept three codes:
        #   2xx  healthy.
        #   401  nginx (Dashboard) is up and enforcing basic auth on every
        #        path. Evidence of health, not failure -- a plain `curl -sf`
        #        treats it as an error and would time out here forever once
        #        dashboard/.htpasswd exists.
        #   405  the route exists but GET is not allowed. This is what
        #        cortex-mcp returns for GET /mcp: it mounts no /health at all,
        #        so a /health probe 404s forever. Method-not-allowed is proof
        #        the route is mounted and serving.
        # A dead service gives 000 (connection refused) and a broken one 5xx,
        # so none of the three accepted codes can mask a real failure.
        #
        # KNOWN DEGRADATION under AUTH_ENABLED=true (now the default): the four
        # /health probes are unaffected -- /health is on the auth skip list on
        # every service (auth/asgi.py DEFAULT_SKIP_PATHS, and each service's
        # own build_auth_middleware call). But cortex-mcp is probed at /mcp,
        # which is NOT skip-listed (cortex/app/mcp_server.py passes
        # skip_paths=("/health",)), so this unkeyed probe now gets 401 from
        # FirekeepKeyAuthMiddleware -- which runs BEFORE routing. That still
        # proves the process is up and serving (000/5xx both still fail the
        # probe), but it no longer proves the /mcp route is mounted, which is
        # what the 405 used to establish.
        #
        # Deliberately NOT "fixed" by sending FIREKEEP_INTERNAL_KEY here. What a
        # keyed GET /mcp returns from fastmcp streamable-http is not 405 by
        # assumption -- a bare GET with no `Accept: text/event-stream` can
        # answer 406 or 400. Any of those falls outside the accepted set, the
        # loop times out, and `bash install.sh` exits 1 on a healthy stack:
        # strictly worse than a weaker-but-correct liveness signal. Restoring
        # the stronger check needs the real response code observed against a
        # running stack first.
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${port}${probe}" 2>/dev/null)" || code="000"
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
    echo "WARNING: Some services failed to start"
    exit 1
fi

# --- Print status ---
VPS_IP=$(grep "^VPS_IP=" .env | cut -d= -f2)

# Every reachability claim below is DERIVED from the .env that was actually
# written, never assumed. The ports bind to ${BIND_ADDR:-127.0.0.1} now, so
# printing http://${VPS_IP}:8040 unconditionally would hand the operator a URL
# that refuses connections on a default install -- a claim a prospect
# disproves in one click.
BIND_ADDR_EFFECTIVE="$(effective_bind_addr .env)"
if bind_addr_is_public "$BIND_ADDR_EFFECTIVE"; then
    REACH_HOST="$VPS_IP"
    REACH_NOTE=""
else
    # Loopback: only reachable from this machine. Say so, and give the
    # SSH-tunnel one-liner instead of a URL that cannot work remotely.
    REACH_HOST="localhost"
    REACH_NOTE="  (loopback only — BIND_ADDR=${BIND_ADDR_EFFECTIVE})"
fi

echo ""
echo "============================================"
echo "  Firekeep is running!"
echo "============================================"
echo ""
echo "  Dashboard:     http://${REACH_HOST}:8040${REACH_NOTE}"
echo "  Dashboard login: ${DASHBOARD_CREDS}"
echo ""
echo "  MCP Endpoints:"
echo "    Cortex:      http://${REACH_HOST}:8080/mcp"
echo "    Bridge:      http://${REACH_HOST}:8070/mcp"
echo "    Sentinel:    http://${REACH_HOST}:8060/mcp"
echo "    Relay:       http://${REACH_HOST}:8050/mcp"
echo ""
echo "  REST APIs:"
echo "    Cortex API:  http://${REACH_HOST}:8100"
echo ""
vault_status_line .env
echo ""

# --- Auth posture: the single most important line in this summary ---------
# Reported from .env, not from what the flags asked for, so it stays true when
# --insecure-no-auth was ignored (existing .env) or when someone hand-edited
# the file between runs.
if auth_enforced .env; then
    echo "  Auth:          ENFORCED — every API call needs an X-API-Key header"
    echo ""
    echo "============================================================"
    if [ -n "$ADMIN_KEY" ]; then
        echo "  ADMIN API KEY — shown once at the START of this run, and"
        echo "  again here because it is NOT stored anywhere on disk."
        echo "  Put it in your password manager NOW:"
        echo ""
        echo "    ${ADMIN_KEY}"
        echo ""
        echo "  Your first authenticated call:"
        echo ""
        echo "    curl -H \"X-API-Key: ${ADMIN_KEY}\" \\"
        echo "      http://localhost:8100/auth/keys"
        echo ""
        echo "  Issue a key for each teammate (never share the admin key):"
        echo ""
        echo "    FIREKEEP_ADMIN_KEY='${ADMIN_KEY}' \\"
        echo "      deploy/firekeep-admin keys create --agent <name>"
    else
        # Idempotent re-run: bootstrap-keys.sh found the admin key already
        # registered and minted nothing, so there is no plaintext to reprint.
        # It is unrecoverable by design (only its SHA-256 is stored) — do not
        # imply otherwise; give the re-mint path instead.
        echo "  ADMIN API KEY — already provisioned by an earlier run."
        echo ""
        echo "  It was printed once, then, and is NOT recoverable: only its"
        echo "  SHA-256 is stored. If you no longer have it, revoke the old"
        echo "  one and mint a fresh key:"
        echo ""
        echo "    docker compose exec -T redis redis-cli -n 7 \\"
        echo "      DEL \"auth:key:\$(docker compose exec -T redis \\"
        echo "      redis-cli -n 7 GET auth:bootstrap:admin_hash)\""
        echo "    docker compose exec -T redis redis-cli -n 7 \\"
        echo "      DEL auth:bootstrap:admin_hash"
        echo "    bash deploy/bootstrap-keys.sh"
    fi
    echo "============================================================"
    echo ""
    echo "  The dashboard needs no key from you — its nginx injects the"
    echo "  admin-scoped DASHBOARD_API_KEY from .env on every /api/ proxy."
else
    echo "  Auth:          ⚠️  DISABLED — every API on this host is OPEN."
    echo "                 Anyone who can reach a port can read your vault"
    echo "                 secrets (GET /vault/secrets) and mint API keys"
    echo "                 (POST /auth/keys). No key is required."
    echo "                 Turn it on: set AUTH_ENABLED=true in .env, then"
    echo "                 run: bash update.sh"
fi
echo ""
echo "  Run 'bash update.sh' to update after git pull."
echo ""

# --- Exposure warning: only when it is TRUE -------------------------------
# This used to claim "exposed on 0.0.0.0" unconditionally. Under the
# BIND_ADDR default that is simply false, and a security warning that is
# wrong half the time is one people learn to skip past.
if bind_addr_is_public "$BIND_ADDR_EFFECTIVE"; then
    echo "⚠️  SECURITY: service ports 8040-8100 are published on ${BIND_ADDR_EFFECTIVE}"
    echo "   — reachable from off this machine. Restrict them:"
    echo "   ufw allow from YOUR_IP to any port 8040:8100 proto tcp"
    if ! auth_enforced .env; then
        echo ""
        echo "   You have BOTH auth disabled AND non-loopback ports. That is"
        echo "   the exact configuration that leaks secrets to the internet."
        echo "   Fix one of the two before this host sees real data."
    fi
else
    echo "🔒 Ports are bound to ${BIND_ADDR_EFFECTIVE} — reachable only from this"
    echo "   machine. To reach the dashboard from your laptop, tunnel:"
    echo "     ssh -L 8040:127.0.0.1:8040 <user>@${VPS_IP}"
    echo "   To publish them instead, set BIND_ADDR=0.0.0.0 in .env, run"
    echo "   'bash update.sh', and firewall the range."
fi
echo ""
