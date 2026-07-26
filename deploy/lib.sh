#!/usr/bin/env bash
# Shared helpers for install.sh / update.sh. Sourced, never executed directly.
# Kept separate so tests/test_deploy_lib.py can drive them via bash.

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
        echo "  Vault:         Enabled (secrets encrypted at rest in Redis)"
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

    if [ -z "$vps_ip" ]; then
        echo "ERROR: VPS IP address must not be empty" >&2
        return 1
    fi
    if [ -z "$neo4j_password" ]; then
        echo "ERROR: Neo4j password must not be empty" >&2
        return 1
    fi
    case "$vps_ip" in
        *'|'*|*'&'*|*'\'*)
            echo "ERROR: VPS IP address must not contain |, & or \\" >&2
            return 1
            ;;
    esac
    case "$neo4j_password" in
        *'|'*|*'&'*|*'\'*)
            echo "ERROR: Neo4j password must not contain |, & or \\" >&2
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
    sed -i "s|YOUR_VPS_IP_HERE|${vps_ip}|g" "$tmp"
    sed -i "s|^NEO4J_PASSWORD=.*|NEO4J_PASSWORD=${neo4j_password}|" "$tmp"

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
