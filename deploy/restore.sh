#!/usr/bin/env bash
# Restore volumes from a backup produced by deploy/backup.sh.
# Usage: bash deploy/restore.sh <backup-dir> [--yes]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib.sh"
# Git Bash / MSYS rewrites arguments that look like absolute POSIX paths into
# Windows paths before the process sees them, so `-v "$DIR:/to"` and the
# in-container `/to/file` both get mangled (observed: tar trying to open
# "C:/Program Files/Git/to/neo4j_data.tar.gz"). Every docker mount here is a
# container-side path, so disable the conversion wholesale. No-op on Linux,
# which is where customers actually run this.
export MSYS_NO_PATHCONV=1
cd "$REPO_ROOT"

BACKUP_DIR="${1:?usage: restore.sh <backup-dir> [--yes]}"
ASSUME_YES="${2:-}"

[ -d "$BACKUP_DIR" ] || { echo "ERROR: no such backup dir: $BACKUP_DIR" >&2; exit 1; }

PREFIX="$(compose_project_prefix)"

# Restoring into running containers corrupts the target.
if [ -n "$(docker compose ps -q 2>/dev/null || true)" ]; then
    echo "ERROR: containers are running. Stop them first:" >&2
    echo "  docker compose down" >&2
    exit 1
fi

echo "This OVERWRITES volumes with prefix '${PREFIX}_' from:"
echo "  $BACKUP_DIR"
if [ -f "$BACKUP_DIR/PREFIX" ]; then
    echo "  (backup was taken from prefix '$(cat "$BACKUP_DIR/PREFIX")')"
fi

if [ "$ASSUME_YES" != "--yes" ]; then
    printf "Type 'restore' to confirm: "
    read -r reply
    [ "$reply" = "restore" ] || { echo "Aborted."; exit 1; }
fi

for archive in "$BACKUP_DIR"/*.tar.gz; do
    [ -e "$archive" ] || { echo "ERROR: no archives in $BACKUP_DIR" >&2; exit 1; }
    vol="$(basename "$archive" .tar.gz)"
    full="${PREFIX}_${vol}"
    echo "  ${full}..."
    docker volume create "$full" >/dev/null
    # Pinned tag+digest — same pin as deploy/backup.sh; this container writes
    # every datastore volume during a disaster restore, the worst possible
    # moment to run whatever a floating tag resolves to that day.
    docker run --rm \
        -v "${full}:/to" \
        -v "$(host_path "$BACKUP_DIR"):/from:ro" \
        alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b \
        sh -c "rm -rf /to/* /to/..?* /to/.[!.]* 2>/dev/null; tar xzf /from/$(basename "$archive") -C /to"
    echo "    ok"
done

# --- .env: the half of a restore the volumes cannot carry --------------------
# Archives taken by deploy/backup-cron.sh contain the deployment's .env as
# `env`. Restoring it is what makes bare-metal recovery actually work: VAULT_KEY
# lives there, and without it every secret in the restored Redis is
# undecryptable ciphertext. Archives predating that feature have no `env` file,
# which is normal and says nothing.
#
# Overwriting an EXISTING .env is the dangerous direction, not the missing one:
# it swaps VAULT_KEY under a deployment whose vault is already populated. So it
# takes a second, differently-worded confirmation, and the file being replaced
# is kept beside it either way.
if [ -f "$BACKUP_DIR/env" ]; then
    echo ""
    if [ -f "$REPO_ROOT/.env" ]; then
        echo "This backup contains a .env, and $REPO_ROOT/.env already exists."
        echo "Replacing it swaps VAULT_KEY — every secret encrypted under the"
        echo "current key becomes unreadable. The current file will be kept as"
        echo "  .env.pre-restore.<timestamp>"
        RESTORE_ENV=0
        if [ "$ASSUME_YES" = "--yes" ]; then
            RESTORE_ENV=1
        else
            printf "Type 'restore-env' to replace it (anything else keeps yours): "
            # `|| true`: at EOF (closed stdin, a piped answer with no trailing
            # newline) read exits nonzero having still assigned the partial
            # line, and under `set -e` that would abort the script AFTER the
            # volumes were already restored — the worst possible place to stop.
            env_reply=""
            read -r env_reply || true
            [ "$env_reply" = "restore-env" ] && RESTORE_ENV=1
        fi
        if [ "$RESTORE_ENV" -eq 1 ]; then
            cp "$REPO_ROOT/.env" "$REPO_ROOT/.env.pre-restore.$(date -u +%Y%m%dT%H%M%SZ)"
            cp "$BACKUP_DIR/env" "$REPO_ROOT/.env"
            chmod 600 "$REPO_ROOT/.env"
            echo "  .env restored from the archive (previous copy kept alongside)"
        else
            echo "  Keeping the existing .env. The archived one is at:"
            echo "    $BACKUP_DIR/env"
        fi
    else
        cp "$BACKUP_DIR/env" "$REPO_ROOT/.env"
        chmod 600 "$REPO_ROOT/.env"
        echo "[OK] .env restored from the archive (mode 0600) — VAULT_KEY and the"
        echo "     Neo4j password came back with it."
    fi
fi

echo "[OK] Restore complete. Start the stack with: docker compose up -d"
# Nightly archives are taken with --exclude-models, so the model store is empty
# after a restore. Without this line the first `up -d` looks like a broken
# restore rather than a 3.3GB download.
echo "     Model weights are not in nightly archives: the ollama-pull service"
echo "     re-downloads them (~3.3GB) on the first 'up'. Until it finishes,"
echo "     memory writes return status=\"partial\"."
