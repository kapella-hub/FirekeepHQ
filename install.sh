#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/deploy/lib.sh"

echo "============================================"
echo "  Firekeep Installer"
echo "============================================"
echo ""

# --- Check Docker ---
if ! command -v docker &>/dev/null; then
    case "$(uname -s)" in
        Linux)
            echo "Docker not found. Installing..."
            curl -fsSL https://get.docker.com | sh
            sudo usermod -aG docker "$USER"
            echo "Docker installed. You may need to log out and back in for group changes."
            ;;
        *)
            # macOS, or Windows under Git Bash / WSL. get.docker.com is a
            # Linux-only convenience script and `usermod` does not exist here,
            # so auto-install would fail confusingly. Docker Desktop is the
            # supported path -- it runs the SAME Linux containers this stack is,
            # so everything below is unchanged once `docker` is on PATH.
            echo "ERROR: Docker not found." >&2
            echo "       On macOS or Windows, install Docker Desktop, start it," >&2
            echo "       then re-run this command:" >&2
            echo "         https://www.docker.com/products/docker-desktop/" >&2
            echo "       (Firekeep's server is a Docker Compose stack; Docker" >&2
            echo "       Desktop runs it the same as any Linux host would.)" >&2
            exit 1
            ;;
    esac
fi

if ! docker compose version &>/dev/null; then
    echo "ERROR: docker compose not available. Install Docker Compose v2."
    exit 1
fi

echo "[OK] Docker and Docker Compose available"

# --- Configure .env ----------------------------------------------------------
# THIS BLOCK ASKS THE OPERATOR NOTHING, and that is the point.
#
# It used to open with two blocking prompts:
#
#     read -rp  "VPS IP address: "  VPS_IP
#     read -rsp "Neo4j password: "  NEO4J_PASSWORD
#
# Both are questions this machine answers better than the person running it,
# and an empty answer to either one aborted the install with "must not be
# empty" — a message that names the field and not the fix. A first-time
# installer, arriving here straight from `firekeep init` on a box they had
# just provisioned, had no way to know that the expected VPS IP was their own
# loopback address, or that the Neo4j password was theirs to invent and would
# never be typed again.
#
# The tell was in our own CI. `.github/workflows/install-smoke.yml` — the job
# literally named "the stranger test" — piped the answers in:
#
#     printf '%s\n%s\n' "127.0.0.1" "smoke-test-password" | bash install.sh
#
# A test that supplies the answer key cannot detect a question that should
# never have been asked. Both values are now derived (see detect_host_ip and
# generate_secret in deploy/lib.sh for what each one actually controls), and
# the smoke test runs with stdin closed so this can never regress into a
# prompt again.
#
# Explicit overrides, highest precedence first. Each is a real use case:
#   --ip <addr>              a host whose routable address is not the one it
#                            routes from (NAT'd VPS, floating IP, DNS name)
#   --neo4j-password <pw>    restoring a backup, or a policy-managed secret
#   FIREKEEP_VPS_IP=…        the same two, for unattended provisioning where
#   FIREKEEP_NEO4J_PASSWORD= adding flags to a canned command line is awkward
if [ ! -f .env ]; then
    echo ""

    VPS_IP="$(flag_value ip "$@")"
    [ -n "$VPS_IP" ] || VPS_IP="${FIREKEEP_VPS_IP:-}"
    if [ -n "$VPS_IP" ]; then
        echo "[OK] Host address: ${VPS_IP} (given explicitly)"
    else
        VPS_IP="$(detect_host_ip)"
        if [ "$VPS_IP" = "127.0.0.1" ]; then
            echo "[OK] Host address: 127.0.0.1 (no routable address found — loopback)"
        else
            echo "[OK] Host address: ${VPS_IP} (detected)"
        fi
        echo "     Used for the SSH target in tunnel join codes and the CORS origin."
        echo "     Change it any time: set VPS_IP in .env, then run: bash update.sh"
    fi

    NEO4J_PASSWORD="$(flag_value neo4j-password "$@")"
    [ -n "$NEO4J_PASSWORD" ] || NEO4J_PASSWORD="${FIREKEEP_NEO4J_PASSWORD:-}"
    if [ -n "$NEO4J_PASSWORD" ]; then
        echo "[OK] Neo4j password: supplied"
    else
        # Not a secret any human needs: Cortex reads it from .env to reach the
        # neo4j container, and the port never leaves 127.0.0.1. Generated for
        # exactly the same reason VAULT_KEY is generated forty lines below —
        # this block simply predates that decision being applied consistently.
        NEO4J_PASSWORD="$(generate_secret 24)" || exit 1
        echo "[OK] Neo4j password generated (stored in .env, mode 0600)"
    fi

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

    # Generate vault encryption key.
    #
    # The cryptography import is the PREFERRED path, not the only one. It is
    # absent on a stock Ubuntu/Debian VPS -- exactly the machine this installer
    # targets -- and the fallback used to be "[SKIP] ... set VAULT_KEY manually
    # after deploy", which means a fresh install shipped with a dead vault and a
    # to-do the operator had no reason to believe was load-bearing. Observed on a
    # clean ubuntu:24.04 container in the install lab.
    #
    # A Fernet key is simply urlsafe-base64 of 32 random bytes, so openssl can
    # produce a real one; `tr '+/' '-_'` is the whole difference between standard
    # and URL-safe base64, and it matters -- Fernet rejects the standard alphabet.
    # `openssl rand -base64 32` emits exactly 44 chars including the trailing '='
    # and no newline issues at this length, but $(...) strips any trailing
    # newline regardless.
    VAULT_KEY=""
    if python3 -c "from cryptography.fernet import Fernet" 2>/dev/null; then
        VAULT_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    elif command -v openssl >/dev/null 2>&1; then
        VAULT_KEY=$(openssl rand -base64 32 | tr '+/' '-_')
    fi
    if [ -n "$VAULT_KEY" ]; then
        sed_i "s|^VAULT_KEY=.*|VAULT_KEY=${VAULT_KEY}|" .env
        echo "[OK] Vault encryption key generated"
    else
        # Reachable only on a host with neither python3-cryptography NOR
        # openssl, which is rare enough to be worth naming precisely instead of
        # blaming "cryptography". The vault is genuinely unusable until this is
        # set, so say that rather than filing it as a tidy-up.
        echo "[WARN] Could not generate VAULT_KEY: this host has neither the" >&2
        echo "       python3 'cryptography' module nor openssl. /vault/* will" >&2
        echo "       answer 503 until you set one. Generate and add it now:" >&2
        echo "         echo \"VAULT_KEY=\$(openssl rand -base64 32 | tr '+/' '-_')\" >> .env" >&2
        echo "       then: bash update.sh" >&2
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
            sed_i "s|^[[:space:]]*AUTH_ENABLED=.*|AUTH_ENABLED=false|" .env
        else
            echo "AUTH_ENABLED=false" >> .env
        fi
        echo ""
        echo "############################################################" >&2
        echo "# AUTH DISABLED BY REQUEST (--insecure-no-auth)" >&2
        echo "#" >&2
        echo "# Every API on this host is now open to anyone who can reach" >&2
        echo "# the port, with NO key required. That includes:" >&2
        echo "#   POST /memory/learn    — writes into your team memory" >&2
        echo "#   POST /memory/recall   — reads everything in it" >&2
        echo "#   POST /knowledge/ingest-url — fetches URLs from this host" >&2
        echo "#   the whole Bridge / Relay / Sentinel MCP surface" >&2
        echo "#" >&2
        echo "# NOT open: /vault/* and /auth/* are unmounted while auth is" >&2
        echo "# off and answer 503. Losing the vault is a CONSEQUENCE of this" >&2
        echo "# flag, not a protection it gives you." >&2
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

# --- Build from source, or pull published images? ---------------------------
# install.sh assumed a git checkout and built all seven services locally. That
# is right for a developer and wrong for a customer: the images are published to
# ghcr.io (see .github/workflows/server-release.yml) precisely so nobody has to
# be handed the source to run this.
#
# Chosen explicitly rather than guessed. Auto-detecting "is there source here"
# would make the mode depend on which files happen to exist, and a partial
# checkout would silently build something different from what was released.
#   --pull   fetch the published images (needs IMAGE_TAG; no registry login)
#   default  build from this checkout, as before
PULL_MODE=0
for arg in "$@"; do
    [ "$arg" = "--pull" ] && PULL_MODE=1
done

if [ "$PULL_MODE" -eq 1 ]; then
    IMAGE_TAG_VALUE="$(env_value .env IMAGE_TAG)"
    if [ -z "$IMAGE_TAG_VALUE" ] || [ "$IMAGE_TAG_VALUE" = "dev" ]; then
        echo "ERROR: --pull needs a release tag." >&2
        echo "       Set IMAGE_TAG in .env to the version you were given, e.g." >&2
        echo "         IMAGE_TAG=v0.1.0" >&2
        echo "       The default ('dev') names images that are never published;" >&2
        echo "       pulling it would fail with 'manifest unknown' after this" >&2
        echo "       script had already written .env and started Redis." >&2
        exit 1
    fi
    if ! docker manifest inspect "ghcr.io/kapella-hub/firekeep-cortex:${IMAGE_TAG_VALUE}" >/dev/null 2>&1; then
        echo "ERROR: cannot read ghcr.io/kapella-hub/firekeep-cortex:${IMAGE_TAG_VALUE}" >&2
        echo "       Release images are public and require no registry login." >&2
        echo "       Check the tag and network connection. If both are correct," >&2
        echo "       this release was not published with Public visibility." >&2
        echo "       Checked before starting anything so the install cannot" >&2
        echo "       fail half-way through." >&2
        exit 1
    fi
    echo ""
    echo "Pulling published images (IMAGE_TAG=${IMAGE_TAG_VALUE})..."
else
    echo ""
    echo "Building and starting services from source..."
fi
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# On --pull the deployed version is IMAGE_TAG_VALUE, not git-describe: the source-
# free bundle has no git repo, so describe would fall through to 0.6.0 and the
# provenance line would name a version nothing here runs. From source, describe
# is correct. GIT_SHA stays whatever git yields ('unknown' in the bundle) — the
# real SHA is baked into the pulled image and GET /version reads it from there.
# Both branches route through provenance_app_version (deploy/lib.sh), where the
# rule is unit-tested. IMAGE_TAG_VALUE is set only in the pull branch above; the
# ${...:-} keeps this safe under set -u on a source build.
export APP_VERSION="$(provenance_app_version "$PULL_MODE" "${IMAGE_TAG_VALUE:-}")"
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
# secret), and — after bootstrap-keys.sh below — FIREKEEP_INTERNAL_KEY,
# FIREKEEP_BRIDGE_KEY (bridge's own dedicated credential, the only one
# carrying eval:grade) and the admin-scoped DASHBOARD_API_KEY. Unconditional
# (not just in the fresh-.env branch above): a manually-created .env (the docs' "Manual Installation"
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

if [ "$PULL_MODE" -eq 1 ]; then
    docker compose pull
    docker compose up -d
else
    # Build mode must not build over a published release name pinned in a
    # pre-existing .env (this installer deliberately refuses to rewrite an
    # existing .env wholesale, so the pin survives re-installs). Same guard
    # and rationale as update.sh's image-tag hygiene block.
    if [ "$(env_value .env IMAGE_TAG)" != "dev" ]; then
        env_file_set .env IMAGE_TAG dev
        echo "NOTICE: IMAGE_TAG in .env was set to 'dev' — this is a source build;"
        echo "        published-image installs use --pull or 'firekeep init'."
    fi
    docker compose up -d --build
fi

# The dashboard bind-mounts index.html as a SINGLE FILE, and single-file bind
# mounts track the inode captured at container start — so on a re-run over an
# existing deployment (git pull, or a bundle re-extract via `firekeep init`),
# `up -d` does not recreate the unchanged-config dashboard container and nginx
# keeps serving the pre-update file. MEASURED on the v0.3.0 deploy: the
# checkout's index.html had no Licence tab while the served page still did.
# update.sh has carried this force-recreate for the same reason; the install
# path needs it for the re-run case. Idempotent and cheap on a fresh install.
docker compose up -d --force-recreate dashboard

# --- Model pull: brief wait, then hand it off ---------------------------------
# `up -d` above returns while ~3.3GB of models may still be downloading:
# cortex-api depends on `ollama: service_healthy`, NOT on ollama-pull completing
# (docker-compose.yml wires no service_completed_successfully edge to any app
# service). The health loop below only proves cortex-api answers HTTP, which it
# does — but a first memory_learn would take the embed-failure path (HTTP 200,
# status="partial", backfill-enqueued): a write that LOOKS successful and is not
# yet recallable.
#
# The wait, the detached watcher and the wording all live in settle_model_pull
# (deploy/lib.sh), which explains the reasoning and — the reason it lives there
# — can be driven by tests/test_install_no_prompts.py with a stub `docker` on
# PATH. An installer that spawns a detached process no test ever executes is the
# kind of thing that breaks silently in somebody else's shell.
MODEL_PULL_BLOCKING=0
for arg in "$@"; do
    [ "$arg" = "--wait-for-models" ] && MODEL_PULL_BLOCKING=1
done
if [ "$MODEL_PULL_BLOCKING" -eq 1 ]; then
    MODEL_PULL_GRACE_SECONDS=900
else
    MODEL_PULL_GRACE_SECONDS="${FIREKEEP_MODEL_PULL_GRACE:-120}"
fi
# settle_model_pull lives in deploy/lib.sh so tests can drive both of its
# branches with a stub `docker` on PATH; see tests/test_install_no_prompts.py.
settle_model_pull "$MODEL_PULL_GRACE_SECONDS" "$MODEL_PULL_BLOCKING"

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
# Where to probe. NOT localhost — see health_probe_hosts in deploy/lib.sh.
PROBE_HOSTS="$(health_probe_hosts .env)"
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
        # Accept the service as up if EITHER candidate host answers; keep the
        # most informative code for the failure message.
        code="000"
        for host in $PROBE_HOSTS; do
            hcode="$(curl -s -o /dev/null -w '%{http_code}' "http://${host}:${port}${probe}" 2>/dev/null)" || hcode="000"
            [ "$hcode" != "000" ] && code="$hcode"
            case "$hcode" in
                2??|401|405) break ;;
            esac
        done
        case "$code" in
            2??|401|405)
                echo "[OK]"
                break
                ;;
        esac
        if [ "$i" -eq 30 ]; then
            echo "[TIMEOUT] (last HTTP ${code}, tried ${PROBE_HOSTS// /, } on :${port}${probe})"
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

# --- Schedule the nightly backup ---
# Measured on the live deployment 2026-08-18: one backup existed, taken by
# update.sh before v1.0.0, and nothing was scheduled. A customer must never
# discover their backup story during the disaster, so the schedule lands with
# the install rather than waiting to be asked for.
echo ""
if BACKUP_LOG="$(install_backup_cron "$(pwd)")"; then
    echo "[OK] Nightly backup scheduled: 04:30 daily, keeping 7 nightly + 4 weekly"
    echo "     Log: ${BACKUP_LOG}   Archives: $(pwd)/backups"
else
    echo "[WARN] The nightly backup could not be scheduled (see above). Everything" >&2
    echo "       else is installed; run 'bash deploy/backup-cron.sh' by hand until" >&2
    echo "       it is." >&2
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
box_top 46
printf '  ║%*s%s%*s║\n' 13 '' 'Firekeep is running!' 13 ''
box_bot 46
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
    if [ -n "$ADMIN_KEY" ]; then
        # The single most important line of the whole install: shown once here,
        # stored nowhere on disk. Framed (box_line interior is ASCII on purpose —
        # see deploy/lib.sh) so it cannot be scrolled past. Interior lines are
        # kept within the 62-column width; the key itself is nxs_ + 48 hex = 52.
        box_top 62
        box_line 62 "  ADMIN API KEY - shown once at the START of this run, and"
        box_line 62 "  again here because it is NOT stored anywhere on disk."
        box_line 62 "  Put it in your password manager NOW:"
        box_mid 62
        box_line 62 ""
        box_line 62 "    ${ADMIN_KEY}"
        box_line 62 ""
        box_bot 62
        echo ""
        echo "  Your first authenticated call:"
        echo ""
        echo "    curl -H \"X-API-Key: ${ADMIN_KEY}\" \\"
        echo "      http://localhost:8100/auth/keys"
        echo ""
        echo "  Add each client device (never share the admin key):"
        echo ""
        echo "    Open Dashboard -> Devices -> Add device"
        echo "    # server-shell fallback:"
        echo "    deploy/firekeep-admin invite --agent <device-name> --json"
    else
        # Idempotent re-run: bootstrap-keys.sh found the admin key already
        # registered and minted nothing, so there is no plaintext to reprint.
        # It is unrecoverable by design (only its SHA-256 is stored) — do not
        # imply otherwise; give the re-mint path instead. Not boxed: the re-mint
        # commands below run past the box width, and there is no live key to frame.
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
    echo ""
    echo "  The dashboard needs no key from you — its nginx injects the"
    echo "  admin-scoped DASHBOARD_API_KEY from .env on every /api/ proxy."
else
    echo "  Auth:          ⚠️  DISABLED — every API on this host is OPEN."
    echo "                 Anyone who can reach a port can read and write"
    echo "                 your team memory (POST /memory/recall, /learn)"
    echo "                 and drive Bridge, Relay and Sentinel. No key."
    echo "                 /vault/* and /auth/* are unmounted (503) — that"
    echo "                 is a consequence of this flag, not a mitigation."
    echo "                 Turn it on: set AUTH_ENABLED=true in .env, then"
    echo "                 run: bash update.sh"
fi
echo ""
echo "  Run 'bash update.sh' to update after git pull."
echo "  Remove later: 'firekeep uninstall' takes off the client kit; add"
echo "                --server (or run this deployment's uninstall.sh) to also"
echo "                remove the stack and ALL its data."
echo ""

# --- Exposure warning: only when it is TRUE -------------------------------
# This used to claim "exposed on 0.0.0.0" unconditionally. Under the
# BIND_ADDR default that is simply false, and a security warning that is
# wrong half the time is one people learn to skip past.
if bind_addr_is_public "$BIND_ADDR_EFFECTIVE"; then
    echo "⚠️  SECURITY: service ports 8040-8100 are published on ${BIND_ADDR_EFFECTIVE}"
    echo "   — reachable from off this machine."
    echo ""
    echo "   ufw will NOT contain these ports. Docker programs its own"
    echo "   iptables DOCKER chain for published ports, and that chain is"
    echo "   traversed BEFORE ufw's rules — so 'ufw deny 8100' is never"
    echo "   consulted while 'ufw status' shows it active. Restrict them"
    echo "   one of these ways instead:"
    echo "     1. BIND_ADDR=127.0.0.1 in .env + 'bash update.sh', then reach"
    echo "        the host over an SSH tunnel:  ssh -L 8100:127.0.0.1:8100 ..."
    echo "     2. Publish only a TLS reverse proxy (docker-compose.office.yml"
    echo "        does this with Caddy) and keep the app ports on loopback."
    echo "     3. If you need a host firewall rule, it must go in DOCKER-USER,"
    echo "        which IS consulted (and does not survive reboot on its own):"
    echo "        iptables -I DOCKER-USER -p tcp --dport 8040:8100 \\"
    echo "                 '!' -s YOUR_IP -j DROP"
    if ! auth_enforced .env; then
        echo ""
        echo "   You have BOTH auth disabled AND non-loopback ports. That is"
        echo "   the exact configuration that leaks secrets to the internet."
        echo "   Fix one of the two before this host sees real data."
    fi
elif grep -q '^FIREKEEP_OFFICE_MODE=true' .env 2>/dev/null; then
    # Office mode pins docker-compose.office.yml, which keeps the six APP ports
    # on 127.0.0.1 and publishes Caddy on 0.0.0.0:443 and :80 as the single
    # deliberate front door. Deriving reachability from BIND_ADDR alone — the
    # only thing the branch above reads — would print "reachable only from this
    # machine" about a host that is serving the network on 443, which is a false
    # all-clear in the one place an operator looks for the real answer.
    echo "🔒 App ports 8040-8100 are bound to ${BIND_ADDR_EFFECTIVE} — not"
    echo "   reachable off this machine. Caddy fronts them on 0.0.0.0:443"
    echo "   (and :80, redirect only), which IS published to the network."
    echo "   That is this deployment's single reachable surface — firewall"
    echo "   443/80 rather than the app range, and see"
    echo "   docs/DEPLOYMENT-OFFICE.md."
else
    echo "🔒 Ports are bound to ${BIND_ADDR_EFFECTIVE} — reachable only from this"
    echo "   machine. To reach the dashboard from your laptop, tunnel:"
    echo "     ssh -L 8040:127.0.0.1:8040 <user>@${VPS_IP}"
    echo "   To publish them instead, set BIND_ADDR=0.0.0.0 in .env and run"
    echo "   'bash update.sh' — and note a host firewall will not contain"
    echo "   them: Docker's DOCKER chain is traversed before ufw's rules."
    echo "   Use DOCKER-USER, or prefer a TLS reverse proxy."
fi
echo ""
