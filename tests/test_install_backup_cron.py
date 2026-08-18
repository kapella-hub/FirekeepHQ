"""Scheduling the nightly backup, and bringing `.env` home on restore (spec §2, §4).

Two halves of the same promise. A backup nobody scheduled is the state the live
deployment was actually in on 2026-08-18 (one archive, taken by update.sh before
v1.0.0, nothing recurring), and an archive whose `.env` cannot be reinstalled is
a restore that silently loses VAULT_KEY.

`crontab` is shadowed by an exported shell function rather than a PATH stub —
under Git Bash on Windows a PATH stub is silently unusable (see
test_deploy_lib._run_uninstall for the same reason) — so no real crontab is ever
touched and the test can read back exactly what would have been installed.
"""
import subprocess
from pathlib import Path

from test_deploy_lib import BASH, LIB, _p, skip_unless_chmod_enforced

REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "install.sh"
UPDATE_SH = REPO / "update.sh"
RESTORE_SH = REPO / "deploy" / "restore.sh"

CRON_STUB = (
    'crontab() {{ '
    'if [ "$1" = "-l" ]; then '
    '  if [ -s "$CRONTAB_FILE" ]; then cat "$CRONTAB_FILE"; else return 1; fi; '
    'elif [ "$1" = "-" ]; then cat > "$CRONTAB_FILE"; '
    'else return 2; fi; }}; export -f crontab; '
    'export CRONTAB_FILE="{table}"; '
)


def _install_cron(tmp_path: Path, *, existing: str = "", repo_root: str | None = None,
                  log_path: str = "/var/log/firekeep-backup.log", times: int = 1):
    """Run install_backup_cron `times` times against a fake crontab table."""
    table = tmp_path / "crontab.txt"
    table.write_text(existing, encoding="utf-8")
    root = repo_root or "/opt/Firekeep"
    calls = "; ".join(
        [f'install_backup_cron "{root}" "{log_path}"'] * times
    )
    script = (
        f'set -euo pipefail; source "{_p(LIB)}"; '
        + CRON_STUB.format(table=_p(table))
        + calls
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    return result, table.read_text(encoding="utf-8")


def test_install_backup_cron_helper_is_defined():
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; declare -F install_backup_cron >/dev/null '
                     f'&& echo DEFINED || echo MISSING'],
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "DEFINED"


def test_installs_the_nightly_line_at_0430(tmp_path):
    """04:30 by design: after the 03:30 night-shift cron, so the two never
    contend for the 1-3 minute quiesce window (spec §2)."""
    result, table = _install_cron(tmp_path)
    assert result.returncode == 0, result.stderr
    lines = [ln for ln in table.splitlines() if "backup-cron" in ln]
    assert len(lines) == 1, table
    line = lines[0]
    assert line.startswith("30 4 * * * "), line
    assert "cd /opt/Firekeep" in line
    assert "bash deploy/backup-cron.sh" in line
    assert ">> /var/log/firekeep-backup.log 2>&1" in line


def test_installing_twice_leaves_exactly_one_line(tmp_path):
    """Idempotence is the whole reason update.sh may run this on every update."""
    result, table = _install_cron(tmp_path, times=2)
    assert result.returncode == 0, result.stderr
    assert len([ln for ln in table.splitlines() if "backup-cron" in ln]) == 1, table


def test_an_existing_backup_cron_line_is_replaced_not_duplicated(tmp_path):
    """A deployment that installed the line under an older schedule or a
    different repo path must end up with ONE line — the current one."""
    stale = "0 2 * * * cd /srv/old-firekeep && bash deploy/backup-cron.sh >> /tmp/x.log 2>&1\n"
    result, table = _install_cron(tmp_path, existing=stale)
    assert result.returncode == 0, result.stderr
    backup_lines = [ln for ln in table.splitlines() if "backup-cron" in ln]
    assert len(backup_lines) == 1, table
    assert "/srv/old-firekeep" not in table


def test_unrelated_crontab_entries_survive(tmp_path):
    """The installer edits a table it does not own. Losing someone's certbot
    renewal because we appended a backup job is not an acceptable trade."""
    existing = (
        "0 3 * * * /usr/bin/certbot renew --quiet\n"
        "@reboot /opt/thing/start.sh\n"
    )
    result, table = _install_cron(tmp_path, existing=existing)
    assert result.returncode == 0, result.stderr
    assert "/usr/bin/certbot renew --quiet" in table
    assert "@reboot /opt/thing/start.sh" in table
    assert len([ln for ln in table.splitlines() if "backup-cron" in ln]) == 1


def test_an_empty_crontab_does_not_produce_a_leading_blank_line(tmp_path):
    """`crontab -l` exits 1 with no output when no table exists; piping that
    straight through writes a blank first line, which some crond builds reject
    outright."""
    result, table = _install_cron(tmp_path)
    assert result.returncode == 0, result.stderr
    assert table.splitlines()[0].strip() != ""


def test_a_failing_crontab_write_is_reported_not_swallowed(tmp_path):
    """If the table cannot be written the operator has no nightly backup, and
    the one thing worse than that is believing they do."""
    script = (
        f'set -euo pipefail; source "{_p(LIB)}"; '
        'crontab() { return 9; }; export -f crontab; '
        'if install_backup_cron /opt/Firekeep; then echo CRON_OK; else echo CRON_FAILED; fi'
    )
    result = subprocess.run([BASH, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr  # the caller must survive
    assert "CRON_FAILED" in result.stdout
    assert "cron" in (result.stdout + result.stderr).lower()


def test_default_log_path_is_the_documented_one():
    """Called with no explicit log path the helper picks /var/log when it can
    write there (the deployment case, installing as root) and falls back inside
    the checkout otherwise — never a path the cron line cannot append to."""
    source = LIB.read_text(encoding="utf-8")
    assert "/var/log/firekeep-backup.log" in source


class TestBothScriptsSchedule:
    """install.sh schedules it on a fresh deployment; update.sh schedules it on
    every existing one, which is how the ~all of them that predate this feature
    ever get a nightly backup at all."""

    def test_install_sh_calls_the_helper(self):
        assert "install_backup_cron" in INSTALL_SH.read_text(encoding="utf-8")

    def test_update_sh_calls_the_helper(self):
        assert "install_backup_cron" in UPDATE_SH.read_text(encoding="utf-8")

    def test_neither_script_can_be_aborted_by_a_cron_failure(self):
        """Both run under `set -euo pipefail`. A bare call would abort the whole
        install on any host without a crontab — a scheduling convenience taking
        down the deployment it was meant to protect."""
        for script in (INSTALL_SH, UPDATE_SH):
            calls = [ln.strip() for ln in script.read_text(encoding="utf-8").splitlines()
                     if "install_backup_cron" in ln and not ln.strip().startswith("#")]
            assert calls, f"{script.name} does not call install_backup_cron"
            for call in calls:
                assert call.startswith("if ") or "||" in call, (
                    f"{script.name} calls install_backup_cron unguarded: {call}"
                )


# --- restore.sh: .env comes home --------------------------------------------

def _run_restore(tmp_path: Path, *, archive_env: bool, existing_env: str | None,
                 args: list[str], stdin: str | None = None):
    """Drive restore.sh against a fake repo root, with docker shadowed.

    REPO_ROOT is derived from the script's own location, so the script is copied
    into a fake checkout (with its lib.sh) rather than run in place — otherwise
    a test could overwrite this developer's real .env.
    """
    fake_repo = tmp_path / "checkout"
    (fake_repo / "deploy").mkdir(parents=True)
    for name in ("restore.sh", "lib.sh"):
        (fake_repo / "deploy" / name).write_bytes(
            (REPO / "deploy" / name).read_bytes()
        )
    if existing_env is not None:
        (fake_repo / ".env").write_text(existing_env, encoding="utf-8")

    backup_dir = tmp_path / "firekeep-backup-20260818T043000Z"
    backup_dir.mkdir()
    (backup_dir / "neo4j_data.tar.gz").write_bytes(b"fake")
    if archive_env:
        (backup_dir / "env").write_text(
            "VAULT_KEY=DUMMY_VAULT_KEY_FOR_TESTS\nNEO4J_PASSWORD=from-archive\n",
            encoding="utf-8",
        )

    script = (
        'docker() { if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then return 0; fi; '
        'return 0; }; export -f docker; '
        f'bash "{_p(fake_repo / "deploy" / "restore.sh")}" "{_p(backup_dir)}" '
        + " ".join(args)
    )
    # BYTES, not text=True: Python's text-mode stdin translates "\n" to "\r\n"
    # on Windows, and `read -r reply` would then compare "restore\r" against
    # "restore" and abort every prompt-driven test for a reason that has nothing
    # to do with what is being tested.
    completed = subprocess.run(
        [BASH, "-c", script],
        input=stdin.encode("utf-8") if stdin is not None else None,
        capture_output=True, timeout=60,
    )
    result = subprocess.CompletedProcess(
        completed.args, completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )
    return result, fake_repo / ".env"


def test_restore_installs_env_from_the_archive_when_none_exists(tmp_path):
    result, envfile = _run_restore(
        tmp_path, archive_env=True, existing_env=None, args=["--yes"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert envfile.is_file(), "the archived .env was not restored"
    assert "VAULT_KEY=" in envfile.read_text(encoding="utf-8")


@skip_unless_chmod_enforced
def test_restored_env_is_mode_600(tmp_path):
    _result, envfile = _run_restore(
        tmp_path, archive_env=True, existing_env=None, args=["--yes"],
    )
    assert (envfile.stat().st_mode & 0o777) == 0o600


def test_restore_never_silently_overwrites_an_existing_env(tmp_path):
    """Declining the confirmation must leave the running deployment's .env
    byte-for-byte — overwriting it swaps VAULT_KEY under a live vault and every
    stored secret becomes undecryptable."""
    result, envfile = _run_restore(
        tmp_path, archive_env=True, existing_env="VAULT_KEY=live-key\n",
        args=[], stdin="restore\nno",
    )
    assert envfile.read_text(encoding="utf-8") == "VAULT_KEY=live-key\n"
    assert "from-archive" not in envfile.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


def test_restore_overwrites_an_existing_env_only_on_the_explicit_confirm_word(tmp_path):
    result, envfile = _run_restore(
        tmp_path, archive_env=True, existing_env="VAULT_KEY=live-key\n",
        args=[], stdin="restore\nrestore-env",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "from-archive" in envfile.read_text(encoding="utf-8")


def test_overwriting_an_env_keeps_a_copy_of_the_old_one(tmp_path):
    """The confirmation is explicit, but the value being replaced is a key that
    cannot be regenerated. A side copy costs nothing and is the difference
    between a mistake and a loss."""
    result, envfile = _run_restore(
        tmp_path, archive_env=True, existing_env="VAULT_KEY=live-key\n",
        args=["--yes"], stdin=None,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    saved = list(envfile.parent.glob(".env.pre-restore.*"))
    assert saved, "the replaced .env was not preserved"
    assert "live-key" in saved[0].read_text(encoding="utf-8")


def test_restore_without_an_archived_env_says_nothing_about_env(tmp_path):
    """Archives taken before this feature carry no `env` file. That is not an
    error and must not read like one."""
    result, envfile = _run_restore(
        tmp_path, archive_env=False, existing_env=None, args=["--yes"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not envfile.exists()
    assert "restore-env" not in result.stdout


def test_restore_closes_by_saying_models_re_pull(tmp_path):
    """--exclude-models means a restored stack starts with no weights. Without
    this line the operator's first `up -d` looks like a broken restore."""
    result, _envfile = _run_restore(
        tmp_path, archive_env=True, existing_env=None, args=["--yes"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "model" in result.stdout.lower()
