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

# Options and the output directory are parsed in one pass, so `backup.sh --hot`
# and `backup.sh <dir> --exclude-models` both mean what they look like. The
# previous `${1:-...}` took the first argument as the directory whatever it was,
# which quietly turned a leading flag into a directory named `--hot`.
HOT=0
EXCLUDE_MODELS=0
OUT_BASE=""
for arg in "$@"; do
    case "$arg" in
        --hot) HOT=1 ;;
        --exclude-models) EXCLUDE_MODELS=1 ;;
        -*)
            echo "ERROR: unknown option: $arg" >&2
            echo "Usage: bash deploy/backup.sh [output-dir] [--hot] [--exclude-models]" >&2
            exit 2
            ;;
        *) [ -n "$OUT_BASE" ] || OUT_BASE="$arg" ;;
    esac
done

OUT_DIR="${OUT_BASE:-$REPO_ROOT/backups}/firekeep-backup-$STAMP"
mkdir -p "$OUT_DIR"

VOLUMES="neo4j_data qdrant_data redis_data ollama_data"
# --exclude-models drops ~3.3GB of model weights from every archive. Safe by
# construction rather than by judgement: the compose stack's `ollama-pull`
# service re-populates the model store on `docker compose up -d`, so a
# weights-less restore self-heals on first start (spec §2.1). Kept OFF by
# default so a hand-run `backup.sh` still captures the whole stack; the nightly
# wrapper is what turns it on.
if [ "$EXCLUDE_MODELS" -eq 1 ]; then
    VOLUMES="neo4j_data qdrant_data redis_data"
fi

# Services whose volumes are being WRITTEN while they run. Tarring a live store
# is not a backup: Neo4j Community has no online-backup facility (that is an
# Enterprise feature), and a filesystem copy of a running store can capture a
# half-written page or an inconsistent transaction log. Qdrant segments and
# Redis's RDB have the same exposure. The result restores without error and is
# wrong — which is worse than no backup, because it is trusted.
#
# ollama_data is deliberately NOT stopped: it holds model weights, written once
# at pull time and read-only afterwards, so there is no torn write to capture
# and no reason to make the outage longer than it has to be.
QUIESCE_SERVICES="neo4j qdrant redis"

STOPPED=""
# Restart on ANY exit path, including a failure mid-backup or a Ctrl-C. A backup
# script that leaves the customer's stack down because tar returned non-zero has
# caused more damage than the missing backup would have.
restart_quiesced() {
    if [ -n "$STOPPED" ]; then
        echo "Restarting: $STOPPED"
        # shellcheck disable=SC2086
        docker compose start $STOPPED >/dev/null 2>&1 || {
            echo "ERROR: could not restart $STOPPED — run 'docker compose up -d' now." >&2
        }
        STOPPED=""
    fi
}
trap restart_quiesced EXIT INT TERM

if [ "$HOT" -eq 1 ]; then
    echo "WARNING: --hot — services stay up. Neo4j/Qdrant/Redis are being written" >&2
    echo "         while their volumes are read, so this archive may restore into" >&2
    echo "         an inconsistent store. Use it only if you accept that." >&2
else
    running=""
    for svc in $QUIESCE_SERVICES; do
        if [ -n "$(docker compose ps -q "$svc" 2>/dev/null)" ]; then
            running="$running $svc"
        fi
    done
    if [ -n "$running" ]; then
        echo "Stopping for a consistent snapshot:$running"
        # shellcheck disable=SC2086
        if docker compose stop $running >/dev/null 2>&1; then
            STOPPED="$running"
        else
            echo "ERROR: could not stop$running — refusing to take a backup that" >&2
            echo "       would silently be inconsistent. Re-run with --hot to" >&2
            echo "       override, understanding the risk." >&2
            exit 1
        fi
    fi
fi

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
    # Pinned tag+digest like every other image in the repo (root CLAUDE.md,
    # "Image pinning"): the 2026-08-19 nightly pulled whatever alpine:latest
    # resolved to that night, which put an unreviewed image in the path that
    # reads every datastore volume. The digest is the multi-platform index.
    if docker run --rm \
        -v "${full}:/from:ro" \
        -v "$(host_path "$OUT_DIR"):/to" \
        alpine:3.24.1@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b \
        tar czf "/to/${vol}.tar.gz" -C /from . ; then
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
# A restorer must be able to tell a quiesced archive from a --hot one. Without
# this the two are indistinguishable, and the inconsistent kind is the one you
# find out about while restoring it.
printf '%s\n' "$([ "$HOT" -eq 1 ] && echo hot || echo cold)" > "$OUT_DIR/MODE"
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
