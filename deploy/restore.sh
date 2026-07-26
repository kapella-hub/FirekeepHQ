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
    docker run --rm \
        -v "${full}:/to" \
        -v "$(host_path "$BACKUP_DIR"):/from:ro" \
        alpine sh -c "rm -rf /to/* /to/..?* /to/.[!.]* 2>/dev/null; tar xzf /from/$(basename "$archive") -C /to"
    echo "    ok"
done

echo "[OK] Restore complete. Start the stack with: docker compose up -d"
