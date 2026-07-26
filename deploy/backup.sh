#!/usr/bin/env bash
# Back up the four persistent volumes — the only data a customer cannot recreate.
# Usage: bash deploy/backup.sh [output-dir]
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

PREFIX="$(compose_project_prefix)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${1:-$REPO_ROOT/backups}/firekeep-backup-$STAMP"
mkdir -p "$OUT_DIR"

VOLUMES="neo4j_data qdrant_data redis_data ollama_data"

echo "Backing up volumes with prefix '${PREFIX}_' to $OUT_DIR"

failed=0
archived=0
for vol in $VOLUMES; do
    full="${PREFIX}_${vol}"
    if ! docker volume inspect "$full" >/dev/null 2>&1; then
        echo "  SKIP $full (does not exist)"
        continue
    fi
    echo "  ${full}..."
    if docker run --rm \
        -v "${full}:/from:ro" \
        -v "$(host_path "$OUT_DIR"):/to" \
        alpine tar czf "/to/${vol}.tar.gz" -C /from . ; then
        # Trust the ARTIFACT, not the exit code. A bind mount that resolves
        # somewhere unexpected lets tar exit 0 having written into the void --
        # observed on Docker Desktop, where the host side of the mount landed
        # inside the Linux VM. This is what makes "complete" mean something.
        if [ -s "${OUT_DIR}/${vol}.tar.gz" ]; then
            echo "    ok ($(du -h "${OUT_DIR}/${vol}.tar.gz" | cut -f1))"
            archived=$((archived + 1))
        else
            echo "    FAILED: tar exited 0 but ${vol}.tar.gz is missing or empty" >&2
            failed=1
        fi
    else
        echo "    FAILED" >&2
        failed=1
    fi
done

printf '%s\n' "$PREFIX" > "$OUT_DIR/PREFIX"
git rev-parse HEAD > "$OUT_DIR/COMMIT" 2>/dev/null || true

if [ "$failed" -ne 0 ]; then
    echo "ERROR: at least one volume failed to back up — do NOT treat this as a backup." >&2
    exit 1
fi

# A wrong prefix (drifted COMPOSE_PROJECT_NAME, renamed checkout) makes every
# `docker volume inspect` above miss and SKIP -- with $failed still 0. Without
# this check the script would print [OK] having archived nothing, exactly the
# silent-nothing-backed-up failure this tool exists to prevent.
if [ "$archived" -eq 0 ]; then
    echo "ERROR: no volumes matched prefix '${PREFIX}_' — nothing was backed up." >&2
    echo "       List actual names with: docker volume ls" >&2
    exit 1
fi

echo "[OK] Backup complete: $OUT_DIR"
echo "     Restore with: bash deploy/restore.sh $OUT_DIR"
