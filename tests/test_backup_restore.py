"""Backup/restore for the four data volumes.

The prefix derivation is the subtle part: compose lowercases the project name
it derives from the directory, so a checkout in `Firekeep/` produces
`firekeep_neo4j_data`. Getting that wrong silently backs up nothing.
"""
import os
import stat
import subprocess
from pathlib import Path

from test_deploy_lib import BASH, LIB, _p

REPO = Path(__file__).resolve().parents[1]
VOLUMES = ["neo4j_data", "qdrant_data", "redis_data", "ollama_data"]
BACKUP_SCRIPT = REPO / "deploy" / "backup.sh"
RESTORE_SCRIPT = REPO / "deploy" / "restore.sh"

# A fake `docker` that logs every invocation (one line per call, full argv)
# to $DOCKER_STUB_LOG and returns controlled exit codes/output, so
# backup.sh/restore.sh's docker-dependent branches can be exercised
# behaviorally without a real daemon (the Docker daemon is unavailable in
# this environment). Grep-only tests can't tell a real guard from a stale
# comment; this can, because it asserts on what the script actually DID.
_DOCKER_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_STUB_LOG"
if [ "$1" = "compose" ] && [ "$2" = "ps" ]; then
    if [ -n "${DOCKER_STUB_PS_OUTPUT:-}" ]; then
        printf '%s\\n' "$DOCKER_STUB_PS_OUTPUT"
    fi
    exit "${DOCKER_STUB_PS_EXIT:-0}"
fi
if [ "$1" = "volume" ] && [ "$2" = "inspect" ]; then
    exit "${DOCKER_STUB_VOLUME_INSPECT_EXIT:-0}"
fi
if [ "$1" = "volume" ] && [ "$2" = "create" ]; then
    exit 0
fi
if [ "$1" = "run" ]; then
    # A faithful `docker run` also PRODUCES the artifact. backup.sh no longer
    # trusts tar's exit code -- it checks the tarball exists and is non-empty --
    # so a stub that only exits 0 simulates the exact bug that check catches
    # (Docker Desktop resolving the host mount inside its own VM).
    # DOCKER_STUB_RUN_PRODUCES_NOTHING=1 deliberately simulates that bug.
    if [ "${DOCKER_STUB_RUN_EXIT:-0}" = "0" ] && [ -z "${DOCKER_STUB_RUN_PRODUCES_NOTHING:-}" ]; then
        _to_host=""; _archive=""
        _prev=""
        for _a in "$@"; do
            case "$_a" in
                *:/to) _to_host="${_a%:/to}" ;;
                /to/*.tar.gz) _archive="${_a##*/}" ;;
            esac
            _prev="$_a"
        done
        if [ -n "$_to_host" ] && [ -n "$_archive" ]; then
            # host_path() hands docker a Windows path on Git Bash; convert back
            # so the stub can write where the real daemon would have.
            if command -v cygpath >/dev/null 2>&1; then
                _to_host="$(cygpath -u "$_to_host")"
            fi
            mkdir -p "$_to_host"
            printf 'stub-archive-content\n' > "${_to_host}/${_archive}"
        fi
    fi
    exit "${DOCKER_STUB_RUN_EXIT:-0}"
fi
exit 0
"""


def _install_docker_stub(bin_dir: Path) -> Path:
    """Write the stub `docker` into bin_dir and return the log file path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "docker"
    # newline="\n" matters on Windows: Path.write_text's default newline
    # translation would put a \r before the shebang's newline, and MSYS
    # bash reads that as part of the interpreter name ("bad interpreter").
    stub.write_text(_DOCKER_STUB, encoding="utf-8", newline="\n")
    mode = stub.stat().st_mode
    stub.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir / "docker.log"


def _run_with_docker_stub(script: Path, args: list[str], tmp_path: Path, *,
                           stdin: str | None = None, **stub_env) -> tuple:
    """Run a deploy script with the fake `docker` shadowing the real one.

    Returns (CompletedProcess, log_text). The stub dir is PREPENDED to PATH
    (not substituted for it) so ordinary utilities the script also needs
    (mkdir, git, tar's caller, date, ...) keep resolving normally.
    """
    bin_dir = tmp_path / "stubbin"
    log_path = _install_docker_stub(bin_dir)
    env = dict(os.environ)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["DOCKER_STUB_LOG"] = _p(log_path)
    for key, value in stub_env.items():
        env[f"DOCKER_STUB_{key}"] = str(value)
    result = subprocess.run(
        [BASH, str(script), *args],
        input=stdin, capture_output=True, text=True, env=env, cwd=str(tmp_path),
    )
    log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    return result, log_text


def _prefix(env: dict | None = None, cwd: str | None = None) -> str:
    result = subprocess.run(
        [BASH, "-c", f'source "{_p(LIB)}"; compose_project_prefix'],
        capture_output=True, text=True, check=True, env=env, cwd=cwd,
    )
    return result.stdout.strip()


def test_prefix_prefers_compose_project_name(monkeypatch, tmp_path):
    import os
    env = dict(os.environ, COMPOSE_PROJECT_NAME="myproject")
    assert _prefix(env=env, cwd=str(tmp_path)) == "myproject"


def test_prefix_falls_back_to_lowercased_directory_name(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}
    mixed = tmp_path / "Firekeep"
    mixed.mkdir()
    assert _prefix(env=env, cwd=str(mixed)) == "firekeep"


def test_prefix_lowercases_because_compose_does(tmp_path):
    """The regression this exists to prevent: `Firekeep_neo4j_data` does not
    exist; compose created `firekeep_neo4j_data`."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "COMPOSE_PROJECT_NAME"}
    d = tmp_path / "MiXeDCase"
    d.mkdir()
    assert _prefix(env=env, cwd=str(d)) == "mixedcase"


def test_both_scripts_exist_and_parse():
    for name in ("backup.sh", "restore.sh"):
        script = REPO / "deploy" / name
        assert script.is_file(), f"deploy/{name} missing"
        subprocess.run([BASH, "-n", str(script)], check=True)


def test_backup_covers_all_four_volumes():
    source = (REPO / "deploy" / "backup.sh").read_text(encoding="utf-8")
    for vol in VOLUMES:
        assert vol in source, f"backup.sh does not cover {vol}"


def test_restore_refuses_to_run_against_a_live_stack():
    """Restoring into running containers corrupts the target. The script must
    require the stack to be down rather than hoping the operator noticed."""
    source = (REPO / "deploy" / "restore.sh").read_text(encoding="utf-8")
    assert "docker compose ps" in source or "compose ps -q" in source, \
        "restore.sh does not check whether the stack is running"


def test_restore_requires_an_explicit_confirmation():
    source = (REPO / "deploy" / "restore.sh").read_text(encoding="utf-8")
    assert "--yes" in source or "read -r" in source, \
        "restore.sh overwrites volumes with no confirmation"


# --- Behavioral tests: the grep tests above only prove the guard *text*
# exists somewhere in the file, including inside a comment. These exercise
# the actual control flow through a stubbed `docker`, so a guard that gets
# deleted (but whose comment survives) fails these even though it would
# still pass the grep tests. ---

def test_restore_declining_confirmation_never_touches_a_volume(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "neo4j_data.tar.gz").write_bytes(b"fake")

    result, log = _run_with_docker_stub(
        RESTORE_SCRIPT, [_p(backup_dir)], tmp_path, stdin="no\n",
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Aborted" in result.stdout
    assert "volume create" not in log, \
        f"declining confirmation must not touch a volume; docker calls were: {log!r}"


def test_restore_refuses_when_containers_are_running(tmp_path):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    (backup_dir / "neo4j_data.tar.gz").write_bytes(b"fake")

    result, log = _run_with_docker_stub(
        RESTORE_SCRIPT, [_p(backup_dir)], tmp_path,
        PS_OUTPUT="deadbeef1234",  # a fake running-container id
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "containers are running" in result.stderr
    assert "volume create" not in log, \
        f"a live-stack refusal must abort before touching any volume; docker calls were: {log!r}"


def test_backup_fails_loudly_when_no_volume_matches_the_prefix(tmp_path):
    """The failure mode the brief's own header names: a wrong prefix makes
    every `docker volume inspect` miss, so nothing gets archived. If the
    script doesn't notice, it reports [OK] having backed up nothing."""
    out_dir = tmp_path / "out"

    result, log = _run_with_docker_stub(
        BACKUP_SCRIPT, [_p(out_dir)], tmp_path,
        VOLUME_INSPECT_EXIT=1,  # every volume looks missing
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "[OK]" not in result.stdout, \
        "backup.sh reported success while archiving zero volumes"
    assert "nothing was backed up" in result.stderr


# --- Happy-path behavioral tests: the negative-path tests above only prove
# that *absence* of the guard string in the stub log is correct when the
# script refuses. None of them prove the success path actually composes the
# right volume name -- exactly the "prefix helper silently backs up/restores
# the wrong thing" bug class this task exists to prevent. These assert on
# the stub's logged argv for a run that is expected to succeed. ---

def test_backup_composes_the_correct_volume_name_and_mounts_on_success(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "happyprefix")
    out_dir = tmp_path / "out"

    result, log = _run_with_docker_stub(
        BACKUP_SCRIPT, [_p(out_dir)], tmp_path,
        VOLUME_INSPECT_EXIT=0,  # every volume "exists"
        RUN_EXIT=0,             # tar "succeeds"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK] Backup complete" in result.stdout
    for vol in VOLUMES:
        full = f"happyprefix_{vol}"
        assert f"volume inspect {full}" in log, \
            f"backup.sh did not inspect {full}; log was: {log!r}"
        assert f"-v {full}:/from:ro" in log, \
            f"backup.sh did not bind-mount {full} read-only for {vol}; log was: {log!r}"
        assert f"tar czf /to/{vol}.tar.gz -C /from ." in log, \
            f"backup.sh did not tar {vol} to the expected archive name; log was: {log!r}"


def test_backup_fails_when_tar_exits_zero_but_writes_nothing(tmp_path, monkeypatch):
    """The artifact check's own regression test.

    This is the real bug, observed on Docker Desktop: the host side of the bind
    mount resolved inside the daemon's Linux VM, so tar exited 0 having written
    3.1GB into ephemeral VM storage. Nothing about the exit code revealed it.
    backup.sh must refuse to call that a backup.
    """
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "happyprefix")
    out_dir = tmp_path / "out"

    result, _log = _run_with_docker_stub(
        BACKUP_SCRIPT, [_p(out_dir)], tmp_path,
        VOLUME_INSPECT_EXIT=0,          # volumes "exist"
        RUN_EXIT=0,                     # tar "succeeds"
        RUN_PRODUCES_NOTHING=1,         # ...but no archive appears
    )

    assert result.returncode != 0, (
        "backup.sh reported success while producing no archive — "
        "this is the silent-empty-backup bug\n" + result.stdout + result.stderr
    )
    assert "NOT a backup" in (result.stdout + result.stderr) \
        or "missing or empty" in (result.stdout + result.stderr), \
        "failure message must say plainly that this is not a backup"


def test_restore_composes_the_correct_volume_name_and_mounts_on_success(tmp_path, monkeypatch):
    # Uses --yes rather than typed-confirmation stdin: subprocess text-mode
    # stdin on Windows translates "\n" to "\r\n" on write, which would leave
    # a trailing \r on `read -r reply` and never equal the literal string
    # "restore" -- a platform quirk unrelated to what this test is checking
    # (the volume-name/mount composition), so --yes sidesteps it cleanly.
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "happyprefix")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    for vol in VOLUMES:
        (backup_dir / f"{vol}.tar.gz").write_bytes(b"fake")

    result, log = _run_with_docker_stub(
        RESTORE_SCRIPT, [_p(backup_dir), "--yes"], tmp_path,
        RUN_EXIT=0,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK] Restore complete" in result.stdout
    for vol in VOLUMES:
        full = f"happyprefix_{vol}"
        assert f"volume create {full}" in log, \
            f"restore.sh did not create {full}; log was: {log!r}"
        assert f"-v {full}:/to" in log, \
            f"restore.sh did not bind-mount {full} for restore of {vol}; log was: {log!r}"
        assert f"tar xzf /from/{vol}.tar.gz -C /to" in log, \
            f"restore.sh did not untar {vol} from the expected archive name; log was: {log!r}"
