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
bash deploy/bootstrap-keys.sh

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

echo ""
echo "============================================"
echo "  Firekeep is running!"
echo "============================================"
echo ""
echo "  Dashboard:     http://${VPS_IP}:8040"
echo "  Dashboard login: ${DASHBOARD_CREDS}"
echo ""
echo "  MCP Endpoints:"
echo "    Cortex:      http://${VPS_IP}:8080/mcp"
echo "    Bridge:      http://${VPS_IP}:8070/mcp"
echo "    Sentinel:    http://${VPS_IP}:8060/mcp"
echo "    Relay:       http://${VPS_IP}:8050/mcp"
echo ""
echo "  REST APIs:"
echo "    Cortex API:  http://${VPS_IP}:8100"
echo ""
vault_status_line .env
echo ""
echo "  Run 'bash update.sh' to update after git pull."
echo ""
echo "⚠️  SECURITY: Service ports 8040-8100 are exposed on 0.0.0.0"
echo "   Configure your firewall to restrict access:"
echo "   ufw allow from YOUR_IP to any port 8040:8100 proto tcp"
echo ""
