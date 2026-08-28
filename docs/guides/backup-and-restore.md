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

## Migration — the identity-v2 freeze runbook

A separate, rarer procedure from the nightly backup/restore above: the one-time
freeze migration that re-keys every memory to the scoped identity described in
[`docs/guides/memory-and-recall.md`](memory-and-recall.md#memory-identity-v2--scoped-point-identity-the-v1-bridge-and-quarantine-2026-08-28).
Design record: [`docs/superpowers/specs/2026-08-27-memory-identity-v2-design.md`](../superpowers/specs/2026-08-27-memory-identity-v2-design.md)
D6/D10. **Deploying the code is not consent to migrate** — the tool
(`cortex/app/workers/memory_identity_migration.py`) ships inert: no Celery
task, no beat entry, no lifespan hook. It runs only when a human invokes it,
inside a maintenance freeze, on a separate explicit go — after the dry-run
report below has been reviewed.

### Before you freeze anything

The tool runs **inside the `cortex-api` container** — it imports `app.*`
directly, so it needs that container's Python environment — as:

```bash
docker compose exec cortex-api python -m app.workers.memory_identity_migration <subcommand>
```

Its durable id map defaults to `/backups/mem-idmap-v2.jsonl`, deliberately
beside the freeze-start cold backup below. But `docker-compose.yml` mounts
`./backups` into `cortex-api` **read-only** (`- ./backups:/backups:ro`,
`cortex-api:` service block) — nothing writable is mounted by default. The
`execute` step checks writability up front and refuses cleanly if it can't
write, but discovering that for the first time *mid-freeze* strands you with
a partial shadow collection and no map. Before you start the freeze: either
remount `./backups` read-write for the migration window and revert it after,
or plan to pass `--idmap-path` to a volume that already is writable. Decide
this now, not during the run.

You can also run `dry-run` any time before the freeze, purely for planning —
it touches nothing (see step 2).

### The freeze migration, in order

1. **Freeze.** Two separate actions, both required:
   - Stop `cortex-worker` and `cortex-beat` (`docker compose stop cortex-worker cortex-beat`)
     — gc, memory_agent, owm, dreams, sleep_cycle, skills, collectors and the
     backfill drain all write through raw clients that bypass the API gate below.
   - Set `MIGRATION_FREEZE=true` and recreate `cortex-api` — `cortex-mcp` is a
     pure HTTP proxy to it (`FIREKEEP_API_URL: http://cortex-api:8000`,
     forwarding the caller's own key) with no `MIGRATION_FREEZE` of its own to
     flip, so it needs no recreate for the gate to take effect. While set,
     `/memory/learn`, `/memory/stream`,
     `/memory/feedback`, the lifecycle mutators, `/memory/import`,
     `/knowledge/ingest(-url)` and `/corpus/ingest` return 503; recall/export
     stay up. See `MIGRATION_FREEZE` in
     [`docs/guides/cortex-configuration.md`](cortex-configuration.md) for the
     exact route list — including the two paths it does NOT cover.
     `POST /admin/embeddings/reembed` is one of those: it isn't gated, and it
     only enqueues Celery work, which `cortex-worker` being stopped for this
     freeze means it sits queued and fires the moment the worker restarts at
     unfreeze (step 7) — do not trigger it during the window.

   **Take the cold backup now, at freeze start** — `deploy/backup.sh --exclude-models`,
   the same script the nightly cron uses (see "What runs automatically" above).
   This is the run's restore point, and the freeze is what makes it a *true*
   one: state the RPO to yourself plainly before continuing — restoring this
   backup later discards **everything** written after this moment, migration
   included.

2. **Dry run (read-only; also safe to run before the freeze, for planning).**

   ```bash
   docker compose exec cortex-api python -m app.workers.memory_identity_migration dry-run
   ```

   Classifies every point by provenance, writes the plan artifact
   (`<idmap-path>.plan.json` — the mapping itself, six figures of entries, goes
   only to the JSONL, not this summary) and prints a JSON report to review
   before doing anything else:
   - **Per-bucket counts** — corpus (including legacy pre-65606df chunks whose
     ids happen to already look like `uuid5(text)`), dream/profile/skill,
     already-v2, v1-migratable, the **repaired-text** bucket (memory points
     whose id matches neither scheme because the text was hand-repaired after
     minting — ~19 on the reference store), quarantine, and unclassified
     (copied verbatim, never dropped).
   - **The occupancy list** (`occupied_targets`) — predicted v2 ids already
     present in the source, split into `target_groups` (legitimate D5 twins:
     merged deterministically, order-independent) and `conflicts` (a
     predicted id held by something that is not a twin — corpus/skill/etc. —
     which `execute` refuses on outright, because merging would destroy it).
   - The **dangling-reference baseline** — `superseded_by`/`contested_with`
     references already broken before this run, so `verify` can tell a
     *pre-existing* break from one the migration caused.

   **Read this report before doing anything else.** A nonzero `conflicts`
   list means `execute` will refuse; review it now, not after the freeze has
   already cost you the outage window.

3. **Execute — the shadow copy.**

   ```bash
   docker compose exec cortex-api python -m app.workers.memory_identity_migration execute
   ```

   Refuses without `MIGRATION_FREEZE=true`, without a reviewed plan on disk
   that still matches the live store (re-classifies and compares before
   touching anything), and on any occupancy conflict. Creates
   `firekeep_memory_v2` from the source collection's own
   `config.params` (never from env), with the three payload indexes and
   `indexing_threshold: 0` during the bulk load (restored after). Writes the
   id map to disk **before** writing any point, so a crash leaves a complete
   map beside a partial shadow, never the reverse. If it's interrupted, resume
   with:

   ```bash
   docker compose exec cortex-api python -m app.workers.memory_identity_migration resume
   ```

4. **Flip.** This tool cannot perform the flip itself — it is your own env
   change plus a container recreate, **still inside the freeze**:
   - Set `QDRANT_COLLECTION=firekeep_memory_v2` in the deployment env.
   - Recreate `cortex-api` so the new setting is live (again, `cortex-mcp`
     carries no `QDRANT_COLLECTION` of its own — it only proxies HTTP to
     `cortex-api` — so it needs no recreate here either). Workers stay
     stopped.
   - Record it:

     ```bash
     docker compose exec cortex-api python -m app.workers.memory_identity_migration mark-flipped
     ```

     This refuses unless the *running process's own* `QDRANT_COLLECTION`
     already equals the shadow — it checks that the flip actually happened
     rather than trusting you to have done it.

   **This is the rollback boundary.** Before this step succeeds, nothing is
   committed: delete the shadow collection and unset `MIGRATION_FREEZE` to
   fully undo the attempt. After it succeeds, there is no more "undo" —
   see Rollback below.

5. **Graph remap and hash folds — after the flip, in this order.**

   ```bash
   docker compose exec cortex-api python -m app.workers.memory_identity_migration graph-remap
   docker compose exec cortex-api python -m app.workers.memory_identity_migration fold-hashes
   ```

   `graph-remap` rewrites `MemoryRef.vector_id` and chain-node `memory_ids`
   through the id map, and stamps `legacy_unscoped` on the legacy chain nodes
   (see `memory-and-recall.md`'s identity-v2 section, D4). It enumerates the three relationship types a `MemoryRef` can
   hold — `SUPERSEDES`, `BACKLINK`, `RELATES_TO` — because plain Cypher can't
   parameterise a relationship type and this deployment carries no APOC; **a
   future fourth `MemoryRef` type needs `_MEMORY_REF_REL_TYPES` in the
   migration module updated, or a remap after that change would silently miss
   it.** It's idempotent but not cursored — a crash mid-remap is fixed by
   re-running the step, no partial-progress marker needed at this graph's
   scale — and any relationship pairs that form a rewrite cycle are left
   alone rather than risk a wrong delete; their count is recorded in the
   state hash for the record, not silently dropped. `fold-hashes` translates
   `memory:access_counts`/`memory:last_recalled` through the same map (skill-id
   fields pass through unmapped, by design) and refuses if a `:flushing` key
   is non-empty (a `memory_agent` drain still mid-flight would otherwise write
   counts straight back under the old ids). Both require the freeze, same as
   every step from here on.

6. **Verify — exact and fatal, only meaningful because of the freeze.**

   ```bash
   docker compose exec cortex-api python -m app.workers.memory_identity_migration verify
   ```

   This is the step that writes the completion marker
   (`mem:migration:v2:complete`) and the idmap's recorded entry count
   (`mem:idmap:v2:count`) — the two things `owm.py`'s nightly pass trusts to
   tell "pre-migration deploy" from "the idmap cache degraded, don't sweep"
   (see "After a successful migration" below). It checks, with no tolerance: exact per-bucket
   counts against the reviewed plan; a fidelity sample (vector + payload,
   field by field, against a non-zero floor — a plan whose sample came out
   empty is a failure, not a silent pass); collection `config.params`
   equality; the three payload indexes present; **search parity** — recorded
   queries run against BOTH collections with `SearchParams(exact=True)` **on
   both sides** (the source is HNSW-approximate after months of live writes,
   the freshly bulk-loaded shadow is brute-force; comparing approximate
   against exact would report a false-fatal mismatch that is really just
   HNSW's own approximation), with its own non-zero floor; no new dangling
   reference beyond the step-2 baseline; and zero points still keyed by the
   v1 formula outside quarantine. **If the report shows many `truncated` or
   skipped search-parity probes, read it before trusting the run** — that's
   the signal the check's own floor exists to surface, not just a number to
   scroll past.

   `verify` itself refuses without `MIGRATION_FREEZE=true` — **it cannot be
   re-run later as a post-hoc audit once you've unfrozen.** If you need to
   re-satisfy yourself after the fact, that means re-freezing.

   **The plan artifact from step 2 is the fixed baseline every fatal check in
   `verify` compares against — never regenerate it once the flip has
   happened.** After the flip, `QDRANT_COLLECTION` names the shadow, so a
   bare `dry-run` with no `--source` would classify the shadow, not the
   original source, and a plan built from the shadow makes every comparison
   trivially pass by construction — the tool refuses this specifically (a
   post-flip `dry-run` must pass `--source` explicitly, and separately
   refuses to overwrite an existing plan that names a different source
   collection), but the reason matters more than the refusal: **don't run
   `dry-run` again after the flip without `--source` pointing at the true
   original**, even out of habit.

7. **Unfreeze.** Restart `cortex-worker`/`cortex-beat`, unset
   `MIGRATION_FREEZE` and recreate `cortex-api` again (`cortex-mcp` still
   needs no recreate, per the note above). The old
   collection is **retained** — nothing deletes it automatically, and once
   `QDRANT_COLLECTION` no longer names it, retaining it conflicts with
   nothing. Deleting it is a later, separate, explicit act once you're
   satisfied.

### Rollback

- **Before `mark-flipped` succeeds (step 4):** delete the shadow collection
  (`firekeep_memory_v2`) and unset `MIGRATION_FREEZE`. Nothing was ever
  committed — the source collection was never touched.
- **After it succeeds:** there are exactly two options — roll **forward**
  (finish graph-remap/fold-hashes/verify/unfreeze) or **restore the
  freeze-start cold backup** from step 1, accepting its RPO (everything since
  is lost). **Never both** — restoring the backup after a partial forward
  roll, or vice versa, leaves Qdrant/Neo4j/Redis disagreeing about which
  collection and which ids are current.

### After a successful migration

`owm.py`'s nightly pass reads the same `mem:idmap:v2:count` and
`mem:migration:v2:complete` keys `verify` just wrote: historical replay
events still naming old ids are translated through the map before scoring,
and the stale-reset sweep is skipped (loudly, not silently) rather than
wiping every migrated memory's efficacy if the idmap cache is ever missing or
partially degraded later — see `memory-and-recall.md`'s OWM bullet. Once
you're confident no more relearns of v1-only text will occur (i.e., no v1
points remain reachable anywhere they'd matter), `MEMORY_ID_V1_BRIDGE` can be
turned off — it's a separate, later, non-urgent cleanup, not part of this
runbook.

## Out of scope (round 1, stated)

rclone/S3 push targets (the endpoint design leaves the door open);
automatic workstation pulls (the scheduler one-liners above instead);
encrypted archives — the `.env`-inside trade-off was chosen deliberately
and the manifest marks the archive sensitive; dashboard restore/download
buttons; Neo4j Enterprise online backup; resume on pull.
