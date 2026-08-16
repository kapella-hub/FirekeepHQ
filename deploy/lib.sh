#!/usr/bin/env bash
# Shared helpers for install.sh / update.sh. Sourced, never executed directly.
# Kept separate so tests/test_deploy_lib.py can drive them via bash.

# sed_i <sed-script> <file>
# Portable in-place sed. BSD/macOS sed REQUIRES a backup-suffix argument
# immediately after -i and otherwise consumes the sed script as that suffix --
# so `sed -i "s|a|b|" f` silently corrupts the file on macOS while working on
# Linux. Routing every in-place edit through a temp file behaves identically on
# GNU, BSD/macOS and busybox. mktemp yields mode 0600, which is exactly what
# .env (the only sensitive target) needs; install.sh re-asserts 600 regardless.
sed_i() {
    local script="${1:?sed script required}" f="${2:?file required}" tmp
    tmp="$(mktemp "${f}.XXXXXX")" || return 1
    if sed "$script" "$f" >"$tmp"; then
        mv "$tmp" "$f"
    else
        rm -f "$tmp"
        return 1
    fi
}

# vault_status_line <envfile>
# Echo the installer summary line for Vault, based on whether VAULT_KEY is
# actually set in the given .env. Never claims a security control is on
# unless it is.
vault_status_line() {
    local envfile="${1:?envfile required}"
    local value=""

    if [ -f "$envfile" ]; then
        # `|| true` matters: under `set -euo pipefail` (as install.sh runs),
        # grep finding no VAULT_KEY line exits 1, and pipefail propagates
        # that through the command substitution, aborting the caller at
        # this line instead of just reporting DISABLED.
        value="$(grep -E '^[[:space:]]*VAULT_KEY=' "$envfile" 2>/dev/null | tail -n1 | cut -d= -f2- || true)"
    fi

    if [ -n "$value" ]; then
        # A key is only half the story. Since audit blocker 7, cortex/app/main.py
        # refuses to MOUNT the vault router when auth is disabled and substitutes
        # a 503 stand-in on every /vault/* path — so a keyed vault on an
        # --insecure-no-auth install is completely unusable, and reporting
        # "Enabled" there is the same false-reassurance defect the audit already
        # caught once (Major 20: the installer claimed Vault: Enabled after
        # skipping key generation). Checking VAULT_KEY and nothing else is what
        # let it come back in a new form.
        if auth_enforced "$envfile"; then
            echo "  Vault:         Enabled (secrets encrypted at rest in Redis)"
        else
            echo "  Vault:         KEY SET, BUT NOT SERVED — /vault/* answers 503"
            echo "                 The vault router is not mounted while"
            echo "                 AUTH_ENABLED=false. Set AUTH_ENABLED=true and"
            echo "                 run: bash update.sh"
        fi
    else
        echo "  Vault:         DISABLED — VAULT_KEY is not set in .env"
        echo "                 Generate one:  python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
        echo "                 Then add it to .env and run: docker compose up -d"
    fi
}

# office_mode_requested "$@"
# True only when --office is passed explicitly. The mere presence of a
# committed docker-compose.office.yml must never change what an install does.
office_mode_requested() {
    local arg
    for arg in "$@"; do
        [ "$arg" = "--office" ] && return 0
    done
    return 1
}

# no_auth_requested "$@"
# True only when --insecure-no-auth is passed explicitly.
#
# Deliberately verbose. Auth is ON by default because the previous default —
# unauthenticated, with every anonymous caller handed scopes ["*"] — put
# GET /vault/secrets and POST /auth/keys on the open internet, and leaked 12
# real secrets off the author's VPS. Anyone turning that back on should have
# to type the word "insecure"; a terse --no-auth is too easy to copy out of a
# forum post without reading what it does.
no_auth_requested() {
    local arg
    for arg in "$@"; do
        [ "$arg" = "--insecure-no-auth" ] && return 0
    done
    return 1
}

# env_value <envfile> <key>
# Echo the LAST value assigned to <key> in <envfile>, or "" if the file or key
# is absent. Last-wins matches how docker compose parses a .env with a
# duplicated key, so a summary built on this can't disagree with what compose
# actually loaded.
#
# The `|| true` is load-bearing under `set -euo pipefail`: grep exits 1 on
# no-match and pipefail propagates that out of the command substitution,
# aborting the caller instead of reporting "unset" (the same trap
# vault_status_line documents above).
env_value() {
    local envfile="${1:?envfile required}" key="${2:?key required}"
    [ -f "$envfile" ] || { printf '%s\n' ""; return 0; }
    grep -E "^[[:space:]]*${key}=" "$envfile" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

# env_file_set <envfile> <KEY> <value>
# Idempotently pin KEY=value: replace the existing line or append one.
# env_value's writing half — install.sh and update.sh use the pair to keep
# .env in step with the mode they run in (deploy/bootstrap-keys.sh carries a
# private equivalent keyed to its $ENV_FILE global; it does not source this
# file, so that copy stays). `|` as the sed delimiter: values written here
# are image tags and mode flags, never text that could contain `|`.
env_file_set() {
    local envfile="${1:?envfile required}" key="${2:?key required}" value="${3:?value required}"
    if grep -qE "^${key}=" "$envfile" 2>/dev/null; then
        sed_i "s|^${key}=.*|${key}=${value}|" "$envfile"
    else
        printf '%s=%s\n' "$key" "$value" >> "$envfile"
    fi
}

# auth_enforced <envfile>
# True when this deployment will ENFORCE X-API-Key auth.
#
# Mirrors two layers that must agree, or the installer's summary lies:
#   1. docker-compose.yml passes AUTH_ENABLED: ${AUTH_ENABLED:-true}. The `:-`
#      form treats UNSET *and EMPTY* alike, so both an absent line and a bare
#      `AUTH_ENABLED=` resolve to the compose default, true.
#   2. The services parse it with pydantic (auth/config.py AuthSettings.ENABLED:
#      bool), which reads 0/off/f/false/n/no as false and 1/on/t/true/y/yes as
#      true. Anything else raises at startup — loud, not silent — so treating
#      only the documented false-y spellings as "off" here cannot understate
#      enforcement on a stack that actually boots.
auth_enforced() {
    local envfile="${1:?envfile required}" value
    value="$(env_value "$envfile" AUTH_ENABLED)"
    # Empty/absent -> compose default (true).
    [ -n "$value" ] || return 0
    case "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" in
        0|off|f|false|n|no) return 1 ;;
        *) return 0 ;;
    esac
}

# effective_bind_addr <envfile>
# Echo the address the published ports will actually bind to. Absent or empty
# resolves to 127.0.0.1, matching docker-compose.yml's ${BIND_ADDR:-127.0.0.1}
# (`:-` again: empty behaves as unset). The installer's closing summary prints
# reachable URLs and a firewall warning off this — both are false statements
# if it assumes the pre-BIND_ADDR 0.0.0.0 behaviour.
effective_bind_addr() {
    local envfile="${1:?envfile required}" value
    value="$(env_value "$envfile" BIND_ADDR)"
    if [ -z "$value" ]; then
        printf '%s\n' "127.0.0.1"
    else
        printf '%s\n' "$value"
    fi
}

# bind_addr_is_public <addr>
# True when <addr> accepts connections from off-box. Only the loopback
# literals are private; anything else (0.0.0.0, ::, a LAN IP, a public IP)
# reaches at least one non-loopback interface and earns the firewall warning.
bind_addr_is_public() {
    case "${1:-}" in
        127.0.0.1|localhost|::1|"[::1]") return 1 ;;
        *) return 0 ;;
    esac
}

# health_probe_hosts <envfile>
# Echo the space-separated host(s) a health check should try, most-likely
# first. Both install.sh and update.sh probed a hardcoded `localhost`, which
# reports EVERY service failed on any deployment whose ports bind elsewhere —
# a tailnet address, a LAN IP. Observed 2026-08-04 on a healthy stack: six
# [TIMEOUT] lines and exit 1 while all six services answered 200 on the bound
# address. A check that cries wolf on every deploy trains its operator to
# ignore it, which is strictly worse than having no check at all.
#
# Two hosts rather than one, because neither is correct everywhere and the
# script cannot tell which case it is in:
#   - the effective BIND_ADDR is where compose published, but
#     docker-compose.office.yml pins its ports to 127.0.0.1 with `!override`
#     literals, so BIND_ADDR is NOT authoritative on an office deploy;
#   - 127.0.0.1 covers that case, and also covers 0.0.0.0 / ::, which are
#     bind addresses and not necessarily connect addresses.
# A service answering on EITHER is up, so trying both is strictly more robust
# than modelling which one ought to apply — and it cannot regress the old
# behaviour, since 127.0.0.1 is always among the candidates.
health_probe_hosts() {
    local envfile="${1:?envfile required}" addr
    addr="$(effective_bind_addr "$envfile")"
    case "$addr" in
        # Already loopback — one probe is enough.
        127.0.0.1|localhost) printf '%s\n' "127.0.0.1" ;;
        # Wildcards: bind-only. Connect to loopback instead.
        0.0.0.0|::) printf '%s\n' "127.0.0.1" ;;
        # IPv6 literals need brackets in a URL; ::1 IS loopback.
        ::1|"[::1]") printf '%s\n' "[::1] 127.0.0.1" ;;
        *:*) printf '%s\n' "[$addr] 127.0.0.1" ;;
        *) printf '%s\n' "$addr 127.0.0.1" ;;
    esac
}

# flag_value <name> "$@"
# Echo the value of `--name <value>` or `--name=<value>` from the argument list,
# or "" when the flag is absent. Last occurrence wins, matching how a shell
# treats a repeated option. A flag given with no value is an error the CALLER
# reports, because only the caller knows what the value means -- returning ""
# here would make `--ip` (typo, no value) indistinguishable from no --ip at all.
flag_value() {
    local name="${1:?flag name required}"; shift
    local found="" arg next=0
    for arg in "$@"; do
        if [ "$next" = 1 ]; then
            found="$arg"; next=0; continue
        fi
        case "$arg" in
            "--${name}") next=1 ;;
            "--${name}="*) found="${arg#--${name}=}" ;;
        esac
    done
    printf '%s' "$found"
}

# detect_host_ip
# The address a REMOTE client would use to reach this host, best effort, always
# succeeding (falls back to 127.0.0.1).
#
# This replaced an interactive "VPS IP address:" prompt, so it is worth being
# precise about what the value actually does -- the prompt implied it was
# load-bearing and it is not. Two consumers:
#
#   1. the default `ssh_target` baked into tunnel-transport join codes
#      (cortex/app/enroll/api.py, cortex/app/members/api.py). Absent or wrong,
#      the invite API answers 400 "t=tunnel requires ssh_target" and the
#      operator passes --ssh-target explicitly. Recoverable, never silent.
#   2. the CORS origin for cortex-api :8100, which .env.example documents as
#      affecting nothing for the bundled dashboard (same-origin through nginx).
#
# So a detected-but-imperfect answer strictly beats blocking the install on a
# question whose stakes the person answering it cannot see.
#
# `ip route get` is a ROUTING TABLE QUERY, not a connection: no packet leaves
# the host, nothing is sent to 1.1.1.1, and it works with no network at all
# (it simply returns nothing, and we fall through). Behind NAT it yields the
# LAN address, which is the correct answer for "what would a client on my
# network SSH to" and the best any local method can do.
# EVERY probe below is `|| true`-guarded, and that is load-bearing rather than
# defensive habit: install.sh runs under `set -euo pipefail`, where an
# assignment from a failing command substitution ABORTS THE WHOLE INSTALLER.
# Both probes fail routinely on perfectly good hosts -- `ip` is absent on
# macOS/BSD and on minimal images, and `hostname -I` is a GNU coreutils
# extension that BSD/busybox/Git-Bash `hostname` rejects. Under pipefail an
# unguarded `hostname -I | tr | grep` therefore returns nonzero and kills the
# install at the first line of configuration, having printed nothing. Caught by
# tests/test_install_no_prompts.py, which drives this under the real options
# rather than an interactive shell's defaults -- the earlier hand-check passed
# precisely because it did not.
detect_host_ip() {
    local addr=""
    if command -v ip >/dev/null 2>&1; then
        addr="$( { ip -4 route get 1.1.1.1 2>/dev/null \
            | sed -n 's/.*[[:space:]]src[[:space:]]\{1,\}\([0-9.]\{7,\}\).*/\1/p' \
            | head -n1; } || true )"
    fi
    if [ -z "$addr" ] && command -v hostname >/dev/null 2>&1; then
        addr="$( { hostname -I 2>/dev/null | tr ' ' '\n' \
            | grep -E '^[0-9]+(\.[0-9]+){3}$' | grep -v '^127\.' | head -n1; } || true )"
    fi
    # Last resort before loopback: the addresses the resolver knows for this
    # host's own name. Present on macOS and BSD, where neither probe above works.
    if [ -z "$addr" ] && command -v getent >/dev/null 2>&1; then
        addr="$( { getent ahostsv4 "$(hostname 2>/dev/null || echo localhost)" 2>/dev/null \
            | awk '{print $1}' | grep -E '^[0-9]+(\.[0-9]+){3}$' \
            | grep -v '^127\.' | head -n1; } || true )"
    fi
    [ -n "$addr" ] || addr="127.0.0.1"
    printf '%s' "$addr"
}

# generate_secret [bytes]
# Print a hex secret of <bytes> entropy (default 32 -> 64 hex chars).
#
# Hex, not base64, and that is deliberate: configure_env rejects `|`, `&` and
# `\` because they corrupt its sed substitutions, and base64's alphabet
# includes `/` and `+` which have burned other substitution paths before.
# [0-9a-f] can never collide with any of it.
#
# Fails loudly rather than falling back to something weak. A predictable
# database password is worse than an install that stops and says why -- and
# every path here is a stock tool on any host that can already run Docker.
generate_secret() {
    local bytes="${1:-32}" value=""
    # Same pipefail discipline as detect_host_ip: each generator is captured
    # with `|| true` and then TESTED, so a tool that exists but fails (a broken
    # openssl, a python3 that is really a stub) falls through to the next one
    # instead of aborting the installer mid-configuration.
    if command -v openssl >/dev/null 2>&1; then
        value="$( { openssl rand -hex "$bytes"; } 2>/dev/null || true )"
    fi
    if [ -z "$value" ] && command -v python3 >/dev/null 2>&1; then
        value="$( { python3 -c \
            'import secrets,sys; sys.stdout.write(secrets.token_hex(int(sys.argv[1])))' \
            "$bytes"; } 2>/dev/null || true )"
    fi
    if [ -z "$value" ] && [ -r /dev/urandom ] && command -v od >/dev/null 2>&1; then
        value="$( { od -vAn -tx1 -N"$bytes" /dev/urandom | tr -d ' \n'; } 2>/dev/null || true )"
    fi
    if [ -n "$value" ]; then
        printf '%s' "$value"
        return 0
    fi
    echo "ERROR: no way to generate a random secret on this host (tried openssl," >&2
    echo "       python3 and /dev/urandom). Install openssl, or supply the value:" >&2
    echo "         bash install.sh --neo4j-password \"\$(your-generator)\"" >&2
    return 1
}

# models_ready
# True once the ollama-pull init container has finished both pulls.
#
# The pipefail-safe check: NOT `docker compose logs ollama-pull | grep -q ...` —
# under `set -o pipefail`, grep matching and closing the pipe early can SIGPIPE
# the writer (exit 141), and pipefail would then make the whole pipeline's
# status failure even though the match SUCCEEDED. Capture the logs into a
# variable first and pattern-match on that instead.
models_ready() {
    local logs
    logs="$(docker compose logs ollama-pull 2>&1 || true)"
    case "$logs" in
        *"Models ready"*) return 0 ;;
    esac
    return 1
}

# settle_model_pull <grace_seconds> <blocking:0|1> [poll_seconds]
#
# Wait a bounded time for the ~3.3GB embedding-model pull, then either report it
# ready, report a blocking timeout, or hand the wait to a detached watcher and
# say plainly what is still true.
#
# WHY THIS NO LONGER BLOCKS BY DEFAULT. install.sh used to wait up to 900
# seconds here, printing one line and then nothing. Two things were wrong with
# that. The install APPEARED hung at the exact point a first-time user is least
# able to tell a slow download from a broken product; and `firekeep init`
# wrapped the whole script in a 600-second timeout, so on a fresh box the
# documented happy path reported "firekeep init timed out" over a stack that was
# working perfectly.
#
# The models are not needed for the stack to be UP — nothing depends on
# ollama-pull completing (docker-compose.yml wires no
# service_completed_successfully edge). They are needed for writes to be
# RECALLABLE: until then a write returns HTTP 200 with status="partial" and is
# queued for backfill. That is a real degraded state, and the fix is to NAME it
# rather than hide it behind a progress-free wait. Cortex reports it as
# `services.embeddings` on /health and `firekeep doctor` renders it.
#
# Lives here rather than inline in install.sh so it can be driven directly by
# tests/test_install_no_prompts.py with a stub `docker` on PATH — a detached
# process spawn that no test ever executes is exactly the kind of thing that
# breaks silently in someone else's shell.
settle_model_pull() {
    local grace="${1:?grace seconds required}" blocking="${2:-0}" poll="${3:-10}"
    local waited=0 ready=0 log="model-pull.log" watcher

    echo ""
    echo "Checking the embedding models (~3.3GB on a first install)..."
    while [ "$waited" -lt "$grace" ]; do
        if models_ready; then
            ready=1
            break
        fi
        sleep "$poll"
        waited=$((waited + poll))
    done

    if [ "$ready" -eq 1 ]; then
        echo "[OK] models ready"
        return 0
    fi
    if [ "$blocking" -eq 1 ]; then
        echo "WARNING: timed out after ${grace}s waiting for the model pull."
        echo "  This usually just means a slow connection, not a broken install."
        echo "  Check progress with: docker compose logs ollama-pull"
        echo "  Until it finishes, memory writes report status=\"partial\" and are"
        echo "  queued for backfill rather than being recallable."
        return 0
    fi

    # Detached, so it survives this script. `firekeep init` runs install.sh as a
    # child and reaps it; a watcher left in the same process group would go with
    # it, and the log would simply stop mid-download with no explanation.
    watcher='
        for _ in $(seq 1 360); do
            logs="$(docker compose logs ollama-pull 2>&1 || true)"
            case "$logs" in
                *"Models ready"*)
                    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) models ready" >> '"$log"'
                    exit 0
                    ;;
            esac
            sleep 10
        done
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) still not ready after 1h" >> '"$log"'
    '
    if command -v setsid >/dev/null 2>&1; then
        setsid bash -c "$watcher" </dev/null >>"$log" 2>&1 &
    else
        nohup bash -c "$watcher" </dev/null >>"$log" 2>&1 &
    fi
    # `disown` is a bash builtin with no `sh` equivalent; guard it so this stays
    # runnable either way rather than dying on an unknown command at the very
    # end of an otherwise successful install.
    disown 2>/dev/null || true

    echo "[..] models are still downloading — continuing without waiting."
    echo "     The stack is UP and usable now. Until the pull finishes, memory"
    echo "     writes return status=\"partial\": they are stored and queued for"
    echo "     backfill, but not yet recallable by search."
    echo "     Progress:  docker compose logs -f ollama-pull"
    echo "     Or ask:    firekeep doctor   (reports 'embeddings' until ready)"
    echo "     Blocking install instead:  bash install.sh --wait-for-models"
    return 0
}

# configure_env <envfile> <example> <vps_ip> <neo4j_password>
#
# Validates the two installer prompts and atomically writes <envfile> from
# <example> with them substituted. Rejects empty answers (an empty Neo4j
# password writes NEO4J_PASSWORD= and the run dies later, opaquely, at
# compose's ${NEO4J_PASSWORD:?...}) and rejects `|`, `&`, `\` in either
# answer (`|` breaks the sed delimiter used below; `&`/`\` are sed
# replacement-text metacharacters -- a literal ampersand or backslash typed
# into either prompt would otherwise corrupt the substitution instead of
# being written verbatim).
#
# Writes to a mktemp'd file (mode 600, same directory as <envfile> so the
# final `mv` is an atomic same-filesystem rename) and only moves it over
# <envfile> once every check has passed. On ANY failure nothing is left
# behind -- <envfile> either does not exist or is fully configured -- so a
# fumbled prompt never leaves a half-written .env that silently short-circuits
# the next run's "if [ ! -f .env ]" config block.
configure_env() {
    local envfile="${1:?envfile required}" example="${2:?example required}" \
          vps_ip="${3-}" neo4j_password="${4-}"
    local tmp

    # install.sh no longer prompts for either value -- it derives both -- so
    # reaching these branches means a caller passed an empty --ip/--neo4j-password
    # or an env override that expanded to nothing. Each message therefore names
    # the FIX, not just the failed field: the old "must not be empty" was the
    # last line a first-time installer saw before giving up.
    if [ -z "$vps_ip" ]; then
        echo "ERROR: host address is empty." >&2
        echo "       Omit --ip to let the installer detect it, or give a value:" >&2
        echo "         bash install.sh --ip 203.0.113.10" >&2
        return 1
    fi
    if [ -z "$neo4j_password" ]; then
        echo "ERROR: Neo4j password is empty." >&2
        echo "       Omit --neo4j-password to have one generated (the normal case" >&2
        echo "       -- nothing but the containers ever reads it), or give a value:" >&2
        echo "         bash install.sh --neo4j-password \"\$(openssl rand -hex 24)\"" >&2
        return 1
    fi
    case "$vps_ip" in
        *'|'*|*'&'*|*'\'*)
            echo "ERROR: host address must not contain |, & or \\ (got: ${vps_ip})" >&2
            return 1
            ;;
    esac
    case "$neo4j_password" in
        *'|'*|*'&'*|*'\'*)
            echo "ERROR: Neo4j password must not contain |, & or \\" >&2
            echo "       Omit --neo4j-password and one will be generated for you." >&2
            return 1
            ;;
    esac

    tmp="$(mktemp "${envfile}.XXXXXX")" || return 1
    # Function-local RETURN trap: fires on every exit path (early `return 1`
    # below, or falling off the end after a successful `mv`). `rm -f` on an
    # already-moved path is a harmless no-op, so this is safe either way --
    # it exists to guarantee validation failures never leave the temp file
    # behind either.
    trap 'rm -f "$tmp"' RETURN
    chmod 600 "$tmp"

    cat "$example" > "$tmp"
    sed_i "s|YOUR_VPS_IP_HERE|${vps_ip}|g" "$tmp"
    sed_i "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=${neo4j_password}|" "$tmp"

    # A placeholder that survives here silently breaks the briefing fan-in,
    # so fail loudly rather than deploying a half-configured .env.
    if grep -nE "<VPS_IP>|YOUR_VPS_IP_HERE" "$tmp"; then
        echo "ERROR: unsubstituted placeholder(s) remain in ${envfile} (see above)" >&2
        return 1
    fi

    mv "$tmp" "$envfile"
}

# redact_env_file <path>
# Echo an env file with every VALUE replaced by <redacted>, keys and comments
# intact. Allow-listing "safe" keys is how the next secret leaks, so every
# value goes -- the vendor needs to see WHICH keys are set, never their values.
# A missing file is not an error: a bundle from a half-installed host is still
# worth having.
redact_env_file() {
    local envfile="${1:?envfile required}"
    [ -f "$envfile" ] || { echo "# (no such file: $envfile)"; return 0; }
    # The optional `(export[[:space:]]+)?` matches a shell-exported line
    # (`export KEY=value`), which is legal in a customer-edited .env and not
    # something the vendor controls. Without it, the value on such a line
    # survived redaction verbatim -- a real leak found by running the actual
    # support-bundle.sh against a full docker-compose.yml with an
    # export-prefixed secret in .env.
    sed -E 's/^([[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=).*/\1<redacted>/' "$envfile"
}

# compose_project_prefix
# Echo the prefix compose uses for this project's volumes. COMPOSE_PROJECT_NAME
# wins when set; otherwise compose derives it from the directory name AND
# LOWERCASES it — a checkout in `Firekeep/` yields `firekeep_neo4j_data`.
# Hardcoding the mixed-case name silently matches nothing.
compose_project_prefix() {
    if [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
        printf '%s\n' "$COMPOSE_PROJECT_NAME"
        return 0
    fi
    basename "$PWD" | tr '[:upper:]' '[:lower:]'
}

# host_path <path>
# Translate a path for the DOCKER DAEMON's view of the host filesystem.
# On Git Bash/MSYS with Docker Desktop the daemon runs in a Linux VM, so a bare
# /tmp/x in a bind mount is resolved INSIDE that VM: tar writes to ephemeral VM
# storage and the files never appear on the host (observed as both silent
# success and "No space left on device"). cygpath -w yields the Windows path
# the daemon actually understands. No-op on Linux, where customers run this.
host_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s\n' "$1"
    fi
}

# provenance_app_version <pull_mode:0|1> [image_tag]
# The APP_VERSION stamped into the build-provenance line (and, on a source
# build, the image build args).
#
# When PULLING published images the deployed version IS the release tag the
# images were built and published under (server-release.yml bakes
# APP_VERSION=<tag> into each image). git-describe run here would instead report
# the source-free bundle's absent-repo fallback (0.6.0) — a version nothing
# running actually is. From source, describe against this repo's server v-tags
# is correct, so the pull path is the only one that must override it.
#
# Kept out of install.sh's inline flow so tests/test_deploy_lib.py can assert the
# pull path reports the tag rather than the 0.6.0 fallback.
provenance_app_version() {
    local pull_mode="${1:?pull mode required}" image_tag="${2-}"
    if [ "$pull_mode" -eq 1 ]; then
        printf '%s\n' "$image_tag"
    else
        # --match excludes this repo's client-vX.Y.Z release tags (client/ has
        # its own release cadence -- see CLAUDE.md) so a server build never
        # reports a client version; falls back to the short SHA (--always) until
        # server vX.Y.Z tags exist, then 0.6.0 outside a git repo.
        git describe --tags --match 'v[0-9]*' --always --dirty 2>/dev/null || echo 0.6.0
    fi
}

# --- Framed summary output (box-drawing) -------------------------------------
# The closing installer summary prints the one-time admin key, which is stored
# nowhere on disk; framing it makes it impossible to skim past. Bars are built
# with printf/seq rather than hand-counted so the borders always match the
# declared width. Interior text passed to box_line MUST stay ASCII: a box char
# is 3 UTF-8 bytes but one column, and printf's %-*s pads by byte count, so a
# multibyte char inside the padded field would shift the right border left. The
# border characters themselves sit outside that field, so they are fine.
box_bar() { printf '═%.0s' $(seq 1 "${1:?width required}"); }         # N horizontal box chars
box_top() { printf '  ╔%s╗\n' "$(box_bar "${1:?width required}")"; }
box_mid() { printf '  ╠%s╣\n' "$(box_bar "${1:?width required}")"; }
box_bot() { printf '  ╚%s╝\n' "$(box_bar "${1:?width required}")"; }
# box_line <width> <text> — one bordered interior line, text left-justified and
# space-padded to <width> columns. Text longer than <width> overflows the right
# border rather than truncating (callers keep interior lines within width).
box_line() { printf '  ║%-*s║\n' "${1:?width required}" "${2-}"; }
