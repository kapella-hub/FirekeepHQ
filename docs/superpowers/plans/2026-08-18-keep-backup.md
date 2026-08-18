# Keep Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatic nightly Keep backups with retention, workstation off-box pulls, guided restore, and visibility everywhere the owner already looks.

**Architecture:** Server: a cron wrapper over the existing `deploy/backup.sh` (manifest + retention + `.env`), and two cortex endpoints over a read-only `./backups` mount. Client: a `firekeep backup` action family (status/list/link/pull/restore) + doctor row. Dashboard: one status card.

**Authoritative spec:** `docs/superpowers/specs/2026-08-18-keep-backup-design.md` — where this plan compresses, the spec wins.

## Global Constraints

- Download endpoint is ADMIN-only; no backup scope is ever added to `SCOPES` (spec §3 — the member-ceiling argument; a guard test encodes it).
- Client spine stays stdlib-only; config writes via existing 0600 helpers.
- Cron installs idempotently (grep-out + append — the night-shift pattern).
- Retention never deletes a dir lacking `manifest.json` (unindexed = protected).
- Versions: server rides next `v1.x` tag; client rides `client-v1.0.1` (tree already bumped to 1.0.1 — do NOT re-bump).

---

### Task S1: backup.sh `--exclude-models` + backup-cron.sh (manifest + retention + .env)

**Files:** Modify `deploy/backup.sh` (VOLUMES filter on flag). Create `deploy/backup-cron.sh`. Test `tests/test_backup_cron.py` (pure-function style like `tests/test_deploy_lib.py` — retention policy table-driven via a `select_survivors(dirs, today)` shell function exercised through bash -c, or extract policy into `deploy/lib.sh`).

- `backup.sh`: `--exclude-models` removes `ollama_data` from `VOLUMES`. Flag parsing joins the existing `--hot` loop.
- `backup-cron.sh`: run `backup.sh --exclude-models` into `backups/`; copy `.env` → `<dir>/env` chmod 0600; write `manifest.json` `{stamp, mode, commit, sensitive: true, files:[{name,sha256,bytes}], total_bytes}` (sha256sum each artifact incl. `env`); retention: delete manifest-bearing dirs that are neither ≤7 days old nor the newest of their ISO-week (`date +%G-%V`) within 28 days; NEVER touch dirs without manifest.json. Log one summary line to `/var/log/firekeep-backup.log` (the cron redirects).
- [ ] Tests first (retention table incl. unindexed protection; manifest shape), then implement, `pytest tests/test_backup_cron.py -q` green. Commit: `feat(backup): nightly wrapper — manifest, retention, .env in the archive`.

### Task S2: cron install + restore.sh .env

**Files:** Modify `install.sh`, `update.sh` (idempotent root-cron line `30 4 * * * cd /opt/Firekeep && bash deploy/backup-cron.sh >> /var/log/firekeep-backup.log 2>&1` — grep-out `backup-cron` + append, mirroring their existing structure; guard `tests/test_install_backup_cron.py` asserting both scripts carry the line and the idempotence pattern). Modify `deploy/restore.sh`: if `<dir>/env` exists, prompt (`--yes` skips) then install to `$REPO_ROOT/.env` chmod 0600 — refuse silent overwrite (existing .env → require explicit `restore-env` confirm word); closing echo notes models re-pull on first `up`.

- [ ] Tests → implement → green. Commit: `feat(backup): scheduled nightly + restore brings .env home`.

### Task S3: cortex endpoints + mount

**Files:** Modify `docker-compose.yml` (cortex-api: `- ./backups:/backups:ro`, with the spec §3 threat note as comment). Create `cortex/app/ops_backups.py` router; wire in `cortex/app/main.py`. Tests `cortex/tests/test_ops_backups.py`.

- `GET /ops/backups` (member: `require_scope("memory:read")`): scan `/backups` (env `FIREKEEP_BACKUPS_DIR` default `/backups`, absent dir → `{backups: [], enabled: false}`); per dir: manifest present → `{stamp, age_seconds, mode, total_bytes, indexed: true}`; else `{stamp?, indexed: false}` (mtime-derived age). Sorted newest first + `{policy: "nightly 04:30 · keep 7 nightly + 4 weekly"}`.
- `GET /ops/backups/{stamp}/{file}` (`require_scope("admin")`): resolve ONLY via the scanned listing + manifest file names (never raw path join); stream with `FileResponse`.
- `test_no_backup_scope_exists`: asserts no `backup` substring in `auth.keys.SCOPES` — the member-ceiling guard, with the spec §3 comment.
- [ ] Tests (member 200 status / member 403 download / admin 200 / traversal `..%2f` refused / no-scope guard) → implement → cortex suite green. Commit: `feat(cortex): backup status + admin download over a read-only mount`.

### Task C1: `firekeep backup` family

**Files:** Modify `client/firekeep_client/cli.py` (parser: positional-choices pattern like `personal`; `backup` + `action` in `{status,list,link,pull,restore}` + `--dest`). New `client/firekeep_client/backups.py` (the logic; cli.py stays dispatch). Modify `client/firekeep_client/resolver.py` ONLY IF the existing section-upsert helpers (`set_generic_agents_md` precedent) can't be reused generically — prefer a `backups.py`-local use of the same pattern. Tests `client/tests/test_cli_backup.py` (fake transport).

Per spec §4 exactly: `status`/`list` via member key (`resolver.resolve("cortex")` + `transport.get_json`); `link` prompts (or `--key` arg for non-tty), VERIFIES via a HEAD-style GET of the newest backup's manifest file through the admin gate (403→"key lacks admin"; network fail→named), stores `[backup] admin_key` 0600; `pull` streams files (`transport` addition: a `download(url, dest, headers, verify)` that streams to a temp name then os.replace — keep it in `backups.py` using urllib directly is FORBIDDEN, extend `transport.py` with a streaming `get_file` instead, same TLS/error contract), verifies each sha256 vs manifest, prunes local pulls to 3; `restore` prints the guided steps with real paths (pulled dir if present, ssh/scp template with `[server]` host from config). Last-pull stamp: `~/.firekeep/backups-pull.json` per spec'd status caveat.

- [ ] Tests (each action; link never stores a failing key; sha256 mismatch fails loudly; prune-to-3; status shows never-pulled) → implement → full client suite green. Commit: `feat(client): firekeep backup — status, list, link, pull, guided restore`.

### Task C2: doctor row + dashboard card

**Files:** cli.py `_check_backup` (in `run_doctor`, member status call, 5s budget: ok/warn>36h/fail never + admin-key-stored note; server unreachable → the row says so, never blocks doctor); tests in `client/tests/test_cli_doctor.py`. `dashboard/index.html`: overview card "Backups" (age, count, size, policy) via `CONFIG.CORTEX_API + '/ops/backups'`, fail-quiet like the memory-graph card; renders `enabled: false` as "no backups yet — nightly runs at 04:30".

- [ ] Tests → implement → suites green. Commit: `feat(backup): the stale-backup nag — doctor row + dashboard card`.

### Task D1: docs + deploy + verify live

- [ ] `docs/guides/backup-and-restore.md` (spec §8 contents: nightly design, pull, disaster runbook fresh-VPS→install→restore→up, retention, sensitive-archive + off-box-freshness caveats, scheduled-pull one-liners per OS). Update `docs/DEPLOYMENT.md` backup section, `CLAUDE.md` guides table, `docs/guides/client-kit.md`. Commit: `docs: backup-and-restore guide + sweep`.
- [ ] Deploy: push; VPS `git pull` + `bash update.sh` (compose change → containers recreate; update.sh itself installs the new cron). Verify: cron line present; run `bash deploy/backup-cron.sh` once manually → manifest correct, `.env` inside 0600, rotation left the unindexed 20260817 backup alone; `GET /ops/backups` member-readable; download 403s the member key.
- [ ] Workstation: `firekeep backup link` (admin key via dashboard) → `pull` → sha256s verify → `status` + doctor row green. THEN releases: tag `client-v1.0.1` (tree already at 1.0.1 — confirm test_package pin) + server `v1.0.1`; after green, site docs page + llms mirrors gain the backup section (accuracy rule: released only).

## Self-review notes

Spec coverage: §2→S1/S2, §3→S3, §4→C1, §5→C2, §7 tests distributed per task, §8→D1. Type consistency: manifest schema identical in S1 (writer), S3 (reader), C1 (verifier) — the three-way agreement IS the feature; each task's tests pin the same field names. Pinned-test risk: none of the four hot files (`install.sh`, `update.sh`, `cli.py`, `docker-compose.yml`) change existing behavior, only add; bootstrap suites untouched.
