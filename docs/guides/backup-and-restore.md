# Backup and restore — the Keep's disaster story

The rule this feature exists to satisfy: **a customer must never discover
their backup story during the disaster.** Design decisions and their
evidence: [`docs/superpowers/specs/2026-08-18-keep-backup-design.md`](../superpowers/specs/2026-08-18-keep-backup-design.md).
What was true before it shipped, measured on the live deployment
2026-08-18: one backup (taken incidentally by an update), nothing
scheduled, archives on the same disk as the data, and `.env` — which
holds `VAULT_KEY` — in no archive, so bare-metal recovery would have
restored every store and silently lost every vault secret.

## What runs automatically

A root cron line (installed idempotently by `install.sh` and `update.sh`)
runs `deploy/backup-cron.sh` at **04:30 server time**:

1. **Cold snapshot** via the existing `deploy/backup.sh`, with
   `--exclude-models`: neo4j, qdrant and redis are stopped for the 1–3
   minutes of tarring (a filesystem copy of a live store restores without
   error and is wrong — worse than no backup, because it is trusted).
   Agents briefly see the Keep unreachable; every hook is fail-open, so
   sessions continue. Model weights are excluded because `docker compose
   up -d` re-pulls them by itself — a weights-less restore self-heals,
   and every archive is ~3.3 GB smaller for it.
2. **`.env` goes into the archive**, mode 0600. This is what makes the
   archive bare-metal restorable — and what makes it **sensitive**:
   anyone holding a backup holds `VAULT_KEY` and every store. The
   manifest says so (`"sensitive": true`); treat pulled backups like the
   credential they are.
3. **`manifest.json`** — stamp, mode, commit, per-file sha256 + sizes.
   The status endpoint reads it; `firekeep backup pull` verifies against
   it. A directory without a manifest (pre-feature backups, `update.sh`'s
   automatic pre-update snapshots) is listed as *unindexed* and is
   **never deleted by rotation**.
4. **Retention**, computed by a pure, table-tested policy
   (`backup_retention_plan` in `deploy/lib.sh`): keep every backup ≤7
   days old, plus the newest per ISO-week for 4 weeks. The executor
   re-checks `manifest.json` on every directory before `rm` — belt and
   braces on the one irreversible operation in the feature.

Log: `/var/log/firekeep-backup.log` on the server.

## The off-box copy — `firekeep backup pull`

A backup on the server's own disk dies with the server. The chosen
off-box model is **workstation pulls** (decision board 2026-08-18):

```bash
firekeep backup link          # once: paste a deployment ADMIN key
firekeep backup pull          # download newest backup, verify every sha256
firekeep backup status        # ages, counts, policy — and your last pull
firekeep backup list          # one line per backup, indexed and unindexed
```

- `link` **verifies the key against the live admin gate before storing
  it** (`~/.firekeep/config` `[backup] admin_key`, 0600) — a key you
  discover is broken at disaster-time is the failure mode this exists to
  prevent. Mint the key in the dashboard or with `deploy/firekeep-admin`.
  `firekeep doctor` discloses when a machine holds one.
- `pull` downloads to `~/FirekeepBackups/firekeep-backup-<stamp>/`
  (`--dest` overrides), verifies **every** file's sha256 against the
  manifest, and keeps the last 3 pulls locally. There is no resume: a
  truncated download fails verification loudly and you pull again.
- **Off-box freshness = your last pull.** `status` and the doctor row
  say when this machine last pulled. To make pulls automatic, schedule
  the one-liner your OS gives you:
  - Windows: `schtasks /Create /SC DAILY /ST 07:00 /TN FirekeepBackupPull /TR "firekeep backup pull"`
  - macOS/Linux: `0 7 * * * firekeep backup pull` in `crontab -e`

**Why download is admin-only, permanently:** raw volume archives contain
every member's private corpus plus `.env`/`VAULT_KEY`. No `backup:read`
scope can ever be added to `SCOPES` — enrolled member credentials resolve
to the full member ceiling at validation time, so any new scope reaches
every member automatically, and a member-reachable archive download would
be admin-equivalence by another name. A guard test
(`test_no_backup_scope_exists`) encodes this.

## Restore — the disaster runbook

Restore runs **on the server host with the stack down** — that is
physics, not a missing feature. `firekeep backup restore` prints these
steps with your real paths filled in:

```bash
# Same machine, or a fresh VPS after `bash install.sh`:
scp -r ~/FirekeepBackups/firekeep-backup-<stamp> root@<server>:/opt/Firekeep/backups/
ssh root@<server>
cd /opt/Firekeep && docker compose down
bash deploy/restore.sh backups/firekeep-backup-<stamp>
docker compose up -d        # ollama-pull re-fetches model weights (~5 min, once)
```

`restore.sh` restores `.env` from the archive when present — with an
explicit confirmation, and it refuses to silently overwrite an existing
`.env`. `firekeep doctor` from any enrolled machine confirms the Keep is
healthy afterwards.

## Visibility

- **`firekeep doctor`** — a `backup` row: ok with age and kept-counts,
  **warn** when the newest backup is older than 36 h, **fail** when no
  backup exists ("one disk holds everything").
- **Dashboard** — a Backups card on the overview: age, count, size,
  policy.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| cron schedule | `30 4 * * *` (root crontab) | edit the crontab line to move it |
| retention | 7 nightly + 4 weekly | `backup_retention_plan` in `deploy/lib.sh` |
| `--hot` (backup.sh) | off | snapshot without stopping stores — may be inconsistent, marked `hot` |
| `--exclude-models` (backup.sh) | on for cron runs | manual `backup.sh` runs still include weights unless passed |
| pull destination | `~/FirekeepBackups` | `firekeep backup pull --dest DIR` |
| local pulls kept | 3 | older pulled dirs pruned by `pull` |

## Out of scope (round 1, stated)

rclone/S3 push targets (the endpoint design leaves the door open);
automatic workstation pulls (the scheduler one-liners above instead);
encrypted archives — the `.env`-inside trade-off was chosen deliberately
and the manifest marks the archive sensitive; dashboard restore/download
buttons; Neo4j Enterprise online backup; resume on pull.
