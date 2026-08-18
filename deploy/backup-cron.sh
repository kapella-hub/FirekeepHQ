#!/usr/bin/env bash
# The nightly Keep backup: a cold snapshot, plus the three things around it that
# make the snapshot worth having (spec §2).
#
#   1. `backup.sh --exclude-models` — the volumes, without the ~3.3GB of model
#      weights `docker compose up -d` re-pulls by itself.
#   2. `.env` copied in at mode 0600. Measured on the live deployment
#      2026-08-18: `.env` holds VAULT_KEY and was in NO archive, so bare-metal
#      recovery would have restored every store and lost every vault secret.
#      That is why manifest.json marks the archive `sensitive`.
#   3. manifest.json + retention. The manifest is what the status endpoint reads
#      and what `firekeep backup pull` verifies against; retention is what stops
#      the disk that holds the data from filling with archives of it.
#
# Usage: bash deploy/backup-cron.sh [backups-root]
# Installed as a root cron line at 04:30 by install.sh / update.sh, which
# redirect stdout+stderr to /var/log/firekeep-backup.log.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/lib.sh"
# Same reason as backup.sh: MSYS rewrites container-side paths in docker mounts.
export MSYS_NO_PATHCONV=1
cd "$REPO_ROOT"

BACKUPS_ROOT="${1:-$REPO_ROOT/backups}"
# Overridable ONLY so tests can point the .env capture at a fixture instead of
# copying a real deployment's secrets into a temp directory. Nothing in
# production sets it.
ENV_FILE="${FIREKEEP_BACKUP_ENV_FILE:-$REPO_ROOT/.env}"

mkdir -p "$BACKUPS_ROOT"

# --- 1. The snapshot ---------------------------------------------------------
# Output is captured rather than streamed so the created directory can be read
# back off backup.sh's closing line. That line is part of backup.sh's tested
# contract (tests/test_backup_restore.py asserts "[OK] Backup complete"), so
# parsing it is a pinned interface, not a guess — and it is still printed here
# in full, success or failure, because this script's stdout IS the log.
if ! backup_output="$(bash "$SCRIPT_DIR/backup.sh" "$BACKUPS_ROOT" --exclude-models 2>&1)"; then
    printf '%s\n' "$backup_output"
    echo "[backup-cron] FAILED: the snapshot did not complete — nothing was indexed" >&2
    echo "              and retention was skipped (an archive that failed is not a" >&2
    echo "              backup, and rotating against it could delete a good one)." >&2
    exit 1
fi
printf '%s\n' "$backup_output"

BACKUP_DIR="$(printf '%s\n' "$backup_output" | sed -n 's/^\[OK\] Backup complete: //p' | tail -n1)"
if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
    echo "[backup-cron] FAILED: backup.sh reported success but named no directory" >&2
    exit 1
fi
STAMP="$(basename "$BACKUP_DIR")"
STAMP="${STAMP#firekeep-backup-}"

# --- 2. .env, the half of a restore the volumes cannot carry -----------------
# A missing .env degrades the archive, it does not invalidate it: the volumes
# are still the data nobody can recreate. Warn loudly, keep going.
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "$BACKUP_DIR/env"
    chmod 600 "$BACKUP_DIR/env"
else
    echo "WARNING: no .env at $ENV_FILE — this archive cannot restore VAULT_KEY," >&2
    echo "         so a bare-metal recovery from it loses every vault secret." >&2
fi

# --- 3. manifest.json --------------------------------------------------------
# Schema is fixed and three-way: this writes it, GET /ops/backups reads it, and
# `firekeep backup pull` verifies downloads against it. Field names are the
# contract — see spec §2.3.
sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        echo "ERROR: no sha256sum or shasum on this host — a manifest without" >&2
        echo "       checksums cannot verify a pull, so refusing to write one." >&2
        return 1
    fi
}

MODE="cold"
[ -f "$BACKUP_DIR/MODE" ] && MODE="$(tr -d '\r\n' < "$BACKUP_DIR/MODE")"
COMMIT=""
[ -f "$BACKUP_DIR/COMMIT" ] && COMMIT="$(tr -d '\r\n' < "$BACKUP_DIR/COMMIT")"

total_bytes=0
file_count=0
files_json=""
for f in "$BACKUP_DIR"/*; do
    [ -f "$f" ] || continue
    name="$(basename "$f")"
    [ "$name" = "manifest.json" ] && continue
    sum="$(sha256_of "$f")"
    bytes="$(wc -c < "$f" | tr -d ' ')"
    total_bytes=$((total_bytes + bytes))
    [ -n "$files_json" ] && files_json="$files_json,"
    files_json="$files_json
    {\"name\": \"$name\", \"sha256\": \"$sum\", \"bytes\": $bytes}"
    file_count=$((file_count + 1))
done

# Written to a temp file and moved into place: a half-written manifest.json is
# worse than none at all, because retention treats its mere presence as "this
# directory is ours to rotate".
manifest_tmp="$(mktemp "$BACKUP_DIR/manifest.json.XXXXXX")"
cat > "$manifest_tmp" <<EOF
{
  "stamp": "$STAMP",
  "mode": "$MODE",
  "commit": "$COMMIT",
  "sensitive": true,
  "files": [$files_json
  ],
  "total_bytes": $total_bytes
}
EOF
mv "$manifest_tmp" "$BACKUP_DIR/manifest.json"

# --- 3b. Permissions the serving container can live with ---------------------
# cortex-api runs uid 1000, and on stock cloud images host uid/gid 1000 is a
# REAL user (`ubuntu` here) — found on the first live verify, when the 0600
# root-owned manifest made every backup read as unindexed and the admin
# download 404. So: a dedicated numeric gid (no host group needed), granted to
# the container via compose `group_add`. Files 0640 root:GID, dirs 0750 — the
# host's uid-1000 user reads nothing, the container reads everything, and the
# tars stop being world-readable on the host as a bonus. Normalized over EVERY
# backup dir each run (perms only — rotation's never-touch-unindexed rule is
# about deletion, and an unlistable dir would vanish from `firekeep backup
# list`, which is its own kind of data loss).
BACKUP_GID="${FIREKEEP_BACKUP_GID:-63719}"
if chgrp -R "$BACKUP_GID" "$BACKUPS_ROOT" 2>/dev/null; then
    chmod 0750 "$BACKUPS_ROOT"
    find "$BACKUPS_ROOT" -mindepth 1 -maxdepth 1 -type d -name 'firekeep-backup-*' -exec chmod 0750 {} \;
    find "$BACKUPS_ROOT" -mindepth 2 -maxdepth 2 -type f -exec chmod 0640 {} \;
else
    echo "WARNING: could not chgrp $BACKUP_GID (not root?) — the status endpoint" >&2
    echo "         and 'firekeep backup pull' may not be able to read this backup." >&2
fi

# --- 4. Retention ------------------------------------------------------------
# The plan is computed by a pure function (backup_retention_plan in lib.sh,
# table-tested); this half only executes it — and re-checks manifest.json on
# every directory it is about to remove. Belt and braces on the one irreversible
# operation in the whole feature.
entries=""
for d in "$BACKUPS_ROOT"/firekeep-backup-*; do
    [ -d "$d" ] || continue
    name="$(basename "$d")"
    if [ -f "$d/manifest.json" ]; then
        entries="$entries $name:1"
    else
        entries="$entries $name:0"
    fi
done

kept=0
deleted=0
# shellcheck disable=SC2086  # entries is a deliberately word-split list
plan="$(backup_retention_plan "$(date -u +%Y-%m-%d)" $entries)"
while read -r verdict name reason; do
    [ -n "${verdict:-}" ] || continue
    if [ "$verdict" != "delete" ]; then
        kept=$((kept + 1))
        continue
    fi
    if [ ! -f "$BACKUPS_ROOT/$name/manifest.json" ]; then
        echo "WARNING: refusing to rotate $name — it has no manifest.json." >&2
        kept=$((kept + 1))
        continue
    fi
    rm -rf "${BACKUPS_ROOT:?}/$name"
    echo "  rotated out: $name ($reason)"
    deleted=$((deleted + 1))
done <<EOF
$plan
EOF

printf '[backup-cron] %s stamp=%s mode=%s files=%d bytes=%d kept=%d deleted=%d\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STAMP" "$MODE" "$file_count" \
    "$total_bytes" "$kept" "$deleted"
