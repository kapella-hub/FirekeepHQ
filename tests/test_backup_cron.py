"""The nightly backup wrapper: retention policy, manifest, and .env capture.

Two halves, deliberately tested differently.

The RETENTION POLICY is a pure function in deploy/lib.sh
(``backup_retention_plan``) and is driven here as a table: dates in, one
``keep``/``delete`` verdict per directory out. It is pure because deleting the
wrong directory is unrecoverable and "it looked right when I ran it" is not
evidence — every rule in spec §2.4 gets a row, including the one that matters
most: a directory with no manifest.json is NEVER deleted.

The WRAPPER (deploy/backup-cron.sh) is driven end-to-end through the same
stubbed `docker` that tests/test_backup_restore.py uses, so the manifest is
asserted against artifacts that were actually produced rather than against the
script's source text.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_backup_restore import _run_with_docker_stub
from test_deploy_lib import BASH, LIB, _p, skip_unless_chmod_enforced

REPO = Path(__file__).resolve().parents[1]
CRON_SCRIPT = REPO / "deploy" / "backup-cron.sh"
BACKUP_SCRIPT = REPO / "deploy" / "backup.sh"

DATA_VOLUMES = ["neo4j_data", "qdrant_data", "redis_data"]


# --- Retention policy (pure) -------------------------------------------------

def _plan(today: str, entries: list[str]) -> dict[str, str]:
    """Run backup_retention_plan and return {stamp: verdict}.

    `entries` are `<dirname>:<indexed 0|1>` pairs, exactly the shape
    backup-cron.sh builds from a scan of backups/.
    """
    args = " ".join(f'"{e}"' for e in entries)
    result = subprocess.run(
        [BASH, "-c",
         f'set -euo pipefail; source "{_p(LIB)}"; backup_retention_plan "{today}" {args}'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    verdicts = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        verdict, stamp = line.split()[0], line.split()[1]
        verdicts[stamp] = verdict
    return verdicts


def _d(day: str, hhmmss: str = "043000") -> str:
    """A backup directory name for YYYYMMDD."""
    return f"firekeep-backup-{day}T{hhmmss}Z"


def test_retention_helper_is_defined():
    """Canary: a missing function would otherwise make every table row below
    fail with an opaque bash error rather than naming the cause."""
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; declare -F backup_retention_plan >/dev/null '
                     f'&& echo DEFINED || echo MISSING'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "DEFINED"


def test_backups_within_seven_days_are_all_kept():
    today = "2026-08-18"
    entries = [f"{_d(d)}:1" for d in
               ("20260818", "20260817", "20260815", "20260812", "20260811")]
    plan = _plan(today, entries)
    assert set(plan.values()) == {"keep"}, plan


def test_newest_of_an_iso_week_survives_beyond_seven_days():
    """Older than 7 days, inside 28: exactly one per ISO week lives."""
    today = "2026-08-18"  # ISO week 2026-34
    entries = [
        f"{_d('20260805')}:1",  # week 2026-32, Wednesday
        f"{_d('20260807')}:1",  # week 2026-32, Friday — the newest of that week
        f"{_d('20260803')}:1",  # week 2026-32, Monday
    ]
    plan = _plan(today, entries)
    assert plan[_d("20260807")] == "keep"
    assert plan[_d("20260805")] == "delete"
    assert plan[_d("20260803")] == "delete"


def test_weekly_survivors_stop_at_four_weeks():
    """Beyond 28 days even the newest-of-week is dropped — otherwise
    "keep 4 weekly" grows without bound and the disk that holds the data also
    fills with archives of it."""
    today = "2026-08-18"
    old = _d("20260701")  # 48 days back
    plan = _plan(today, [f"{old}:1"])
    assert plan[old] == "delete"


def test_a_directory_without_a_manifest_is_never_deleted():
    """The load-bearing rule (spec §2.4). update.sh's ad-hoc pre-update backups
    and everything taken before this feature existed carry no manifest; rotation
    does not own them and must not touch them, however old they are."""
    today = "2026-08-18"
    ancient = _d("20250101")
    plan = _plan(today, [f"{ancient}:0"])
    assert plan[ancient] == "keep"


def test_unindexed_and_indexed_are_judged_independently():
    today = "2026-08-18"
    entries = [f"{_d('20260101')}:0", f"{_d('20260102')}:1"]
    plan = _plan(today, entries)
    assert plan[_d("20260101")] == "keep"
    assert plan[_d("20260102")] == "delete"


def test_an_unparsable_directory_name_is_kept():
    """Anything rotation cannot date, it cannot reason about — so it keeps it.
    The failure mode being avoided is a rename convention changing and taking
    every archive with it."""
    plan = _plan("2026-08-18", ["firekeep-backup-not-a-date:1"])
    assert plan["firekeep-backup-not-a-date"] == "keep"


def test_a_realistic_month_of_nightlies_keeps_seven_plus_four():
    """End-to-end shape check: 40 consecutive nightly backups, one per day,
    leaves 7 dailies + one per ISO week within 28 days."""
    from datetime import date, timedelta

    today = date(2026, 8, 18)
    entries = [f"{_d((today - timedelta(days=n)).strftime('%Y%m%d'))}:1"
               for n in range(40)]
    plan = _plan(today.isoformat(), entries)
    kept = sorted(s for s, v in plan.items() if v == "keep")
    # 8 dailies — "<= 7 days old" spans 8 calendar days, 2026-08-11..18 — plus
    # the newest of each older ISO week still inside 28 days: 08-10 (week 33),
    # 08-09 (32), 08-02 (31), 07-26 (30). Week 29's newest is 30 days old and
    # goes, and everything older with it.
    assert len(kept) == 12, kept
    assert _d("20260818") in kept and _d("20260811") in kept
    assert _d("20260726") in kept
    assert _d("20260719") not in kept


# --- The wrapper: manifest, .env, --exclude-models ---------------------------

def _fake_env(tmp_path: Path) -> Path:
    env = tmp_path / "fake.env"
    env.write_text("NEO4J_PASSWORD=hunter2\nVAULT_KEY=DUMMY_VAULT_KEY_FOR_TESTS\n",
                   encoding="utf-8")
    return env


def _run_cron(tmp_path: Path, *, env_file: Path | None = None, **stub_env):
    """Drive backup-cron.sh with a stubbed docker, writing into tmp_path/backups.

    The .env it captures is redirected to a fake via FIREKEEP_BACKUP_ENV_FILE so
    the test never copies this developer's real secrets into a temp directory.
    """
    backups = tmp_path / "backups"
    if env_file is None:
        env_file = _fake_env(tmp_path)
    os.environ["FIREKEEP_BACKUP_ENV_FILE"] = _p(env_file)
    os.environ["COMPOSE_PROJECT_NAME"] = "happyprefix"
    try:
        result, log = _run_with_docker_stub(
            CRON_SCRIPT, [_p(backups)], tmp_path, **stub_env,
        )
    finally:
        os.environ.pop("FIREKEEP_BACKUP_ENV_FILE", None)
        os.environ.pop("COMPOSE_PROJECT_NAME", None)
    return result, log, backups


def _only_backup_dir(backups: Path) -> Path:
    dirs = sorted(p for p in backups.iterdir() if p.is_dir())
    assert len(dirs) == 1, f"expected one backup dir, found {dirs}"
    return dirs[0]


def test_cron_script_exists_and_parses():
    assert CRON_SCRIPT.is_file()
    subprocess.run([BASH, "-n", str(CRON_SCRIPT)], check=True)


def test_cron_run_writes_a_manifest_matching_the_plan_schema(tmp_path):
    result, _log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr

    manifest = json.loads((_only_backup_dir(backups) / "manifest.json").read_text())
    assert set(manifest) == {"stamp", "mode", "commit", "sensitive", "files", "total_bytes"}
    assert manifest["sensitive"] is True
    assert manifest["mode"] == "cold"
    assert isinstance(manifest["stamp"], str) and manifest["stamp"]
    assert isinstance(manifest["commit"], str)
    for entry in manifest["files"]:
        assert set(entry) == {"name", "sha256", "bytes"}


def test_manifest_checksums_match_the_bytes_on_disk(tmp_path):
    """The manifest is what `firekeep backup pull` verifies against. A sha256
    that does not describe the file it names turns every pull into a false
    corruption alarm — or worse, hides a real one."""
    import hashlib

    result, _log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    backup_dir = _only_backup_dir(backups)
    manifest = json.loads((backup_dir / "manifest.json").read_text())

    total = 0
    for entry in manifest["files"]:
        data = (backup_dir / entry["name"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], entry["name"]
        assert len(data) == entry["bytes"], entry["name"]
        total += len(data)
    assert manifest["total_bytes"] == total


def test_manifest_indexes_every_artifact_including_env(tmp_path):
    result, _log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    manifest = json.loads((_only_backup_dir(backups) / "manifest.json").read_text())
    names = {entry["name"] for entry in manifest["files"]}
    assert "env" in names, "the .env copy must be indexed — it is what makes the archive bare-metal restorable"
    for vol in DATA_VOLUMES:
        assert f"{vol}.tar.gz" in names
    assert "manifest.json" not in names, "the manifest must not index itself"


def test_env_is_copied_into_the_archive(tmp_path):
    """Without VAULT_KEY, a bare-metal restore silently loses every vault
    secret — the gap measured on the live deployment 2026-08-18."""
    result, _log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    env_copy = _only_backup_dir(backups) / "env"
    assert env_copy.is_file()
    assert "VAULT_KEY=" in env_copy.read_text(encoding="utf-8")


@skip_unless_chmod_enforced
def test_env_copy_is_mode_600(tmp_path):
    result, _log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    env_copy = _only_backup_dir(backups) / "env"
    assert (env_copy.stat().st_mode & 0o777) == 0o600


def test_exclude_models_skips_the_ollama_volume(tmp_path):
    """~3.3GB per archive of weights that `docker compose up -d` re-pulls by
    itself (spec §2.1). The wrapper must pass --exclude-models through."""
    result, log, backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ollama_data" not in log, f"ollama_data was archived anyway: {log!r}"
    for vol in DATA_VOLUMES:
        assert f"happyprefix_{vol}" in log
    assert not (_only_backup_dir(backups) / "ollama_data.tar.gz").exists()


def test_backup_flag_alone_still_archives_the_three_data_volumes(tmp_path):
    """backup.sh --exclude-models, driven directly: the flag must subtract
    ollama_data and nothing else."""
    out_dir = tmp_path / "out"
    os.environ["COMPOSE_PROJECT_NAME"] = "happyprefix"
    try:
        result, log = _run_with_docker_stub(
            BACKUP_SCRIPT, [_p(out_dir), "--exclude-models"], tmp_path,
            VOLUME_INSPECT_EXIT=0, RUN_EXIT=0,
        )
    finally:
        os.environ.pop("COMPOSE_PROJECT_NAME", None)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ollama_data" not in log
    for vol in DATA_VOLUMES:
        assert f"tar czf /to/{vol}.tar.gz -C /from ." in log


def test_a_failed_volume_fails_the_cron_run_and_writes_no_manifest(tmp_path):
    """Inherited from backup.sh's contract: an archive that failed is not a
    backup, and a manifest over it would make the status endpoint report a
    healthy nightly that does not exist."""
    result, _log, backups = _run_cron(
        tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0, RUN_PRODUCES_NOTHING=1,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    manifests = list(backups.rglob("manifest.json")) if backups.exists() else []
    assert manifests == [], f"a failed backup was indexed anyway: {manifests}"


def test_rotation_deletes_only_indexed_expired_directories(tmp_path):
    """The whole retention story, executed: an ancient unindexed directory and
    an ancient indexed one, side by side, in a real run."""
    backups = tmp_path / "backups"
    unindexed = backups / _d("20250101")
    unindexed.mkdir(parents=True)
    (unindexed / "neo4j_data.tar.gz").write_bytes(b"pre-feature archive")

    indexed = backups / _d("20250102")
    indexed.mkdir(parents=True)
    (indexed / "neo4j_data.tar.gz").write_bytes(b"expired archive")
    (indexed / "manifest.json").write_text(
        json.dumps({"stamp": "20250102T043000Z", "mode": "cold", "commit": "x",
                    "sensitive": True, "files": [], "total_bytes": 0}),
        encoding="utf-8",
    )

    result, _log, _backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert unindexed.is_dir(), "rotation deleted a directory it does not own"
    assert not indexed.exists(), "rotation kept an expired indexed backup"


def test_cron_run_prints_one_summary_line(tmp_path):
    """cron redirects stdout to /var/log/firekeep-backup.log; a run that says
    nothing leaves an operator with a log file that proves nothing."""
    result, _log, _backups = _run_cron(tmp_path, VOLUME_INSPECT_EXIT=0, RUN_EXIT=0)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = [ln for ln in result.stdout.splitlines() if ln.startswith("[backup-cron]")]
    assert len(summary) == 1, result.stdout
    assert "kept=" in summary[0] and "deleted=" in summary[0]


def test_missing_env_file_warns_but_still_produces_a_backup(tmp_path):
    """A .env that cannot be read is a degraded backup, not a failed one: the
    volumes are still the data nobody can recreate."""
    result, _log, backups = _run_cron(
        tmp_path, env_file=tmp_path / "does-not-exist.env",
        VOLUME_INSPECT_EXIT=0, RUN_EXIT=0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    backup_dir = _only_backup_dir(backups)
    assert (backup_dir / "manifest.json").is_file()
    assert not (backup_dir / "env").exists()
    assert "WARNING" in result.stdout + result.stderr


def test_backup_cron_normalizes_perms_for_the_serving_container():
    """cortex-api runs uid 1000; on stock cloud images host uid/gid 1000 is a
    REAL user, so archives are granted to a dedicated numeric gid via compose
    `group_add` instead (first live verify, 2026-08-18: 0600 root-owned
    manifests made every backup read as unindexed). The script and compose
    must agree on the variable, or the grant silently targets two gids."""
    script = Path(_p("deploy/backup-cron.sh")).read_text(encoding="utf-8")
    assert 'FIREKEEP_BACKUP_GID:-63719' in script
    assert "chgrp -R" in script and "chmod 0640" not in script.split("chgrp")[0]
    compose = Path(_p("docker-compose.yml")).read_text(encoding="utf-8")
    assert '"${FIREKEEP_BACKUP_GID:-63719}"' in compose
