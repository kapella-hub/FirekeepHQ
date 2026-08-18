# Keep Backup — user-friendly backup/restore (design, 2026-08-18)

**Status: approved direction (decision board 03e78f63, four answers), spec
self-reviewed; build follows immediately.** Prerequisite for the next dex:
every dex raises what a lost disk costs.

Decision trail: four decision-board answers (off-box = workstation pull;
nightly 04:30 keep 7+4; `.env` included, archive treated as sensitive;
surface = `firekeep backup` command family + doctor row + dashboard card).
Naming note: the feared collision with `firekeep restore` (worktree
snapshots) does not exist — `backup` is a family (`firekeep backup
status|now|list|link|pull|restore`), and `restore` inside it is unambiguous.

## 1. What exists and what was missing

`deploy/backup.sh` + `deploy/restore.sh` are sound cold-snapshot engineering
(quiesce neo4j/qdrant/redis, verify artifacts not exit codes, cold/hot
marking, restart-on-any-exit). What was missing is everything around them,
measured on the live deployment 2026-08-18: exactly ONE backup existed
(taken by `update.sh` before v1.0.0), nothing scheduled, backups on the same
disk as the data (73% full), and `.env` — which holds `VAULT_KEY` — in no
archive, so bare-metal recovery would have lost every vault secret. A
customer must never discover their backup story during the disaster.

## 2. The nightly snapshot (server)

New `deploy/backup-cron.sh`, a wrapper around `backup.sh`:

1. Runs the existing cold snapshot with a new `--exclude-models` flag
   (skips `ollama_data`). Safe by construction: the compose stack's
   `ollama-pull` service re-populates the model store on `docker compose
   up -d`, so a weights-less restore self-heals; saves ~3.3 GB/archive.
2. Copies `.env` into the backup dir, mode 0600. The archive is now
   bare-metal restorable AND itself a secret — `manifest.json` says so.
3. Writes `manifest.json`: `{stamp, mode, commit, sensitive: true,
   files: [{name, sha256, bytes}], total_bytes}`. The manifest is what
   `pull` verifies against and what the status endpoint reads.
4. Applies retention over ONE flat `backups/` dir, pure policy: keep
   every backup ≤7 days old, plus the newest per ISO-week (`date +%G-%V`)
   for 4 weeks; delete the rest. No copy-doubling, no promotion dance.
   Dirs without a manifest (update.sh's ad-hoc backups, pre-feature dirs)
   are NEVER deleted by rotation and are listed as `unindexed`.

Installed as a root cron line at **04:30 server time** by `install.sh` and
`update.sh`, idempotently (the night-shift-cron pattern: grep-out + append).
04:30 is after the 03:30 night-shift cron by design; if night-shift ever
overruns into the 1–3 min quiesce window, its transient-defer semantics
already handle a briefly-down Redis. All four MCP services stay up through
the window (`restart: unless-stopped`, reconnect on Redis return); hooks
are fail-open, so agent sessions continue.

## 3. Status + download endpoints (cortex)

`docker-compose.yml` mounts `./backups:/backups:ro` into `cortex-api`.
Threat note, stated because the house standard (sentinel socket removal)
demands it: this widens nothing — cortex-api already holds `VAULT_KEY` in
env and every member's data in its stores; a read-only view of archives of
that same data adds no new class of exposure to a compromised cortex.

- `GET /ops/backups` — **member-readable** status: list of backups
  `{stamp, age_seconds, mode, total_bytes, indexed: bool}` + retention
  policy. Reveals existence and age only — no filenames, no content.
  Powers doctor and the dashboard card.
- `GET /ops/backups/{stamp}/{file}` — **admin-only, non-negotiable**,
  streamed. Raw volume tars contain every member's private corpus and
  `.env`/`VAULT_KEY`. This must never be member-reachable, and no
  `backup:read` scope may ever be added to `SCOPES`: the enrolled-member
  ceiling (`ENROLLABLE_SCOPES = SCOPES − {admin,*}`, applied at validation
  time since v1.0.0) would hand it to every member automatically —
  a new scope here IS admin-equivalence. Guarded by a test that asserts
  no backup scope exists in `SCOPES`.
  Path traversal: `{stamp}` and `{file}` validated against the listing,
  never joined raw.

## 4. The client family — `firekeep backup <action>`

- `status` — member key; prints age/count/policy + the honest off-box
  caveat: "off-box freshness = your last pull; last pull on this machine:
  <when|never>".
- `list` — member key; the status list, one line per backup.
- `link` — one-time: prompts for a deployment ADMIN key (minted via the
  dashboard or `deploy/firekeep-admin`), VERIFIES it live against the
  download endpoint's auth gate before storing (a key discovered broken at
  disaster-time is the failure mode), stores in `~/.firekeep/config`
  `[backup] admin_key`, 0600 like every credential there. `firekeep doctor`
  discloses that a backup admin key is stored on this machine.
- `pull [--dest DIR]` — downloads the newest indexed backup to
  `<dest>/firekeep-backup-<stamp>/` (default `~/FirekeepBackups`), streams
  each file, verifies EVERY sha256 against the manifest, keeps the last 3
  pulls locally (older pruned). No resume in round 1: a truncated download
  fails verification and re-pulls — disclosed, not discovered. Requires
  `link` first; the error names it.
- `restore` — guided, honest, no remote magic: restore runs ON the host
  with the stack down (that is physics). Prints the exact steps with real
  paths — scp the pulled dir to the host, `bash deploy/restore.sh <dir>`,
  `docker compose up -d` — and the fresh-VPS variant (install.sh first).
  `deploy/restore.sh` itself gains: restore `.env` when present in the
  archive (explicit confirmation, never silent overwrite of an existing
  .env) and a closing note that models re-pull on first `up`.

Client spine rules hold: stdlib only, transport via `resolver`/`transport`,
0600 config writes via the existing section-upsert helpers.

## 5. Visibility

- `firekeep doctor` row `backup`: ok "last backup <age> · N kept (7d+4w)"
  · warn when newest > 36h or only unindexed backups exist · fail "never —
  one disk holds everything". Plus, when `[backup] admin_key` is stored:
  an informational note in the row detail.
- Dashboard overview card "Backups": age, count, total size, policy line;
  reads the member-readable status endpoint through the existing proxy.
  No restore button, no download button in round 1.

## 6. Out of scope, stated

rclone/S3 push targets (the endpoint design leaves the door open);
scheduled automatic pulls on the workstation (documented one-liner for Task
Scheduler/launchd/cron instead); encrypted archives (the .env-inside
trade-off was chosen eyes-open and the manifest marks it); dashboard
restore/download buttons; Neo4j Enterprise online backup; resume on pull.

## 7. Tests

- Retention policy: table-driven dates → exactly which dirs survive
  (≤7d + newest-per-ISO-week×4; unindexed dirs untouchable), alongside
  `tests/test_deploy_lib.py`.
- backup-cron: manifest shape + sha256s match artifacts; `.env` present,
  0600; `--exclude-models` skips ollama_data; a failed volume still exits
  nonzero (inherited backup.sh contract).
- Endpoints: status member-readable; download 403 for member key, 200
  admin, path traversal refused; `test_no_backup_scope_exists` asserting
  `SCOPES` gains no backup scope (the member-ceiling argument, encoded).
- Client: link verifies-then-stores (broken key never stored); pull
  verifies sha256 and fails loudly on mismatch; local prune keeps 3;
  status/list render both indexed and unindexed; restore prints the
  real paths; all against a fake transport.
- Doctor row states; dashboard card renders from a fixture payload.
- Live (deploy verification): cron installed on the VPS; one real
  backup-cron run produces a manifest; `firekeep backup pull` from the
  workstation verifies end-to-end; restore.sh dry-check against the
  pulled dir.

## 8. Docs (kept current with the build — Change Consistency Checklist)

New `docs/guides/backup-and-restore.md` (the runbook: nightly design,
pull, the disaster path fresh-VPS→install→restore→up, retention, the
sensitive-archive caveat, off-box freshness caveat). Updates:
`docs/DEPLOYMENT.md` (backup section now points at the automated story),
`CLAUDE.md` (guides table + one line), `docs/guides/client-kit.md`
(command family), `docker-compose.yml` comments, dashboard, and after
release the site docs page + llms mirrors.
